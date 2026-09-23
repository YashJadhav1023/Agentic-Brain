"""Job Manager and Unified Execution Layer for Mission Control.

Manages the lifecycle of Jobs across Agent and API providers, with:
- Deterministic routing (--provider, --account, --model or --worker)
- Account pool checkout with health/cooldown checking
- Safe credential retrieval with zero token leakage
- Explicit configured failovers (never silent fallbacks)
- Retry management and audit logging with secret redaction
- Usage tracking
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.base.adapter import TaskExecutionResult
from brain.orchestrator.job import Job
from providers.base import AIProvider, ProviderType
from providers.registry.account_registry import Account, AccountPool, AccountRegistry, AccountStatus
from providers.registry.credential_manager import CredentialManager, SecretRedactor
from providers.registry.model_registry import ModelRegistry
from providers.registry.provider_registry import ProviderRegistry

logger = logging.getLogger("MissionControl.JobManager")


class UsageTracker:
    """Tracks token, request, and cost usage per provider and account."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def record(
        self,
        job_id: str,
        provider_id: str,
        account_id: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._records.append({
            "job_id": job_id,
            "provider_id": provider_id,
            "account_id": account_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
            "cost_usd": cost_usd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def get_summary(self, provider_id: str | None = None, account_id: str | None = None) -> dict[str, Any]:
        filtered = [
            r for r in self._records
            if (provider_id is None or r["provider_id"] == provider_id)
            and (account_id is None or r["account_id"] == account_id)
        ]
        total_requests = len(filtered)
        total_tokens = sum(r["total_tokens"] for r in filtered)
        total_cost = sum(r["cost_usd"] for r in filtered)
        return {
            "requests": total_requests,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(total_cost, 6),
        }


class JobManager:
    """Universal job scheduler, dispatcher, and audit logger for Mission Control."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        storage_dir: Path | None = None,
        credential_manager: CredentialManager | None = None,
    ) -> None:
        self.registry = provider_registry
        self.accounts: AccountRegistry = provider_registry.accounts
        self.models: ModelRegistry = provider_registry.models
        self.credential_manager = credential_manager or CredentialManager()
        self.usage_tracker = UsageTracker()
        self.redactor = SecretRedactor()

        self._jobs: dict[str, Job] = {}
        self._audit_log: list[dict[str, Any]] = []
        self._storage_dir = storage_dir
        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            self._load_persisted_jobs()

    def _load_persisted_jobs(self) -> None:
        if not self._storage_dir or not self._storage_dir.is_dir():
            return
        for f in self._storage_dir.glob("job-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                job = Job(
                    id=data["id"],
                    provider=data.get("provider", ""),
                    provider_type=ProviderType(data.get("provider_type", "api")),
                    account=data.get("account", ""),
                    worker=data.get("worker"),
                    model=data.get("model", ""),
                    task=data.get("task", ""),
                    status=data.get("status", "pending"),
                    created_at=data.get("created_at", ""),
                    started_at=data.get("started_at"),
                    completed_at=data.get("completed_at"),
                    duration=data.get("duration"),
                    retry_count=data.get("retry_count", 0),
                    result=data.get("result"),
                    error=data.get("error"),
                    metadata=data.get("metadata", {}),
                )
                self._jobs[job.id] = job
            except Exception as exc:
                logger.debug(f"Failed to load persisted job {f}: {exc}")

    def _persist_job(self, job: Job) -> None:
        if not self._storage_dir:
            return
        try:
            target = self._storage_dir / f"{job.id}.json"
            # Atomic replace: a concurrent reader or a crash mid-write must never
            # leave a truncated job file, which _load_persisted_jobs would then
            # silently drop on the next start.
            fd, tmp_name = tempfile.mkstemp(dir=str(self._storage_dir), prefix=f".{job.id}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(job.to_dict(), indent=2))
                os.replace(tmp_name, target)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except Exception as exc:
            logger.warning(f"Failed to persist job {job.id}: {exc}")

    def log_audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Log an event to the audit trail with guaranteed secret redaction."""
        safe_details = self.redactor.redact_dict(details)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "details": safe_details,
        }
        self._audit_log.append(entry)
        logger.info(f"AUDIT [{event_type}]: {safe_details}")

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._audit_log[-limit:]

    def submit_job(
        self,
        task: str,
        provider: str,
        account: str | None = None,
        worker: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
        failover_chain: list[dict[str, Any]] | None = None,
    ) -> Job:
        """Create and queue a deterministic Job."""
        # Determine provider type
        prov_obj = self.registry.get_ai_provider(provider)
        if prov_obj:
            ptype = prov_obj.provider_type
        else:
            ptype = ProviderType.AGENT

        meta = dict(metadata) if metadata else {}
        if failover_chain:
            meta["failover_chain"] = failover_chain

        # Auto-build task context if not already provided
        job_context = None
        try:
            from brain.context.context_builder import ContextBuilder
            builder = ContextBuilder()
            ctx = builder.build_context(
                task=task,
                provider_id=provider,
                account_id=account or "auto",
                model_id=model or "auto"
            )
            job_context = ctx.to_dict()
        except Exception as e:
            logger.debug("ContextBuilder skipped during submit_job: %s", e)

        job = Job(
            provider=provider,
            provider_type=ptype,
            account=account or "",
            worker=worker,
            model=model or "",
            task=task,
            metadata=meta,
            context=job_context
        )

        self._jobs[job.id] = job
        self._persist_job(job)
        self.log_audit("JOB_SUBMITTED", {"job_id": job.id, "provider": provider, "account": account, "model": model})
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50, status: str | None = None) -> list[Job]:
        all_jobs = list(self._jobs.values())
        all_jobs.sort(key=lambda j: j.created_at, reverse=True)
        if status:
            all_jobs = [j for j in all_jobs if j.status == status]
        return all_jobs[:limit]

    def execute_job(
        self,
        job: Job,
        failover_chain: list[dict[str, Any]] | None = None,
    ) -> Job:
        """Execute a Job deterministically with explicit failover if configured."""
        job.mark_started()
        self._persist_job(job)
        self.log_audit("JOB_STARTED", {"job_id": job.id, "provider": job.provider, "account": job.account})

        # Chain of execution attempts: [primary, *fallbacks]
        attempts: list[dict[str, Any]] = [{"provider": job.provider, "account": job.account, "model": job.model, "worker": job.worker}]
        chain = failover_chain or job.metadata.get("failover_chain", [])
        if chain:
            for fallback in chain:
                attempts.append({
                    "provider": fallback.get("provider", job.provider),
                    "account": fallback.get("account", ""),
                    "model": fallback.get("model", job.model),
                    "worker": fallback.get("worker"),
                    "is_fallback": True,
                })

        last_error = "No execution attempted"
        for i, attempt in enumerate(attempts):
            prov_id = attempt["provider"]
            acct_id = attempt["account"]
            mdl = attempt["model"]
            wrk = attempt["worker"]

            if attempt.get("is_fallback"):
                self.log_audit("JOB_FAILOVER", {
                    "job_id": job.id,
                    "target_provider": prov_id,
                    "target_account": acct_id,
                    "target_model": mdl,
                })

            if acct_id:
                acct = self.accounts.get_account(acct_id)
                if acct and acct.status in ("OFFLINE", "DISABLED", "AUTH_ERROR", "CONFIG_ERROR"):
                    last_error = f"Skipping account {acct_id} due to status: {acct.status}"
                    self.log_audit("JOB_ATTEMPT_SKIPPED", {
                        "job_id": job.id,
                        "attempt": i + 1,
                        "account": acct_id,
                        "reason": last_error,
                    })
                    continue

            try:
                result = self._dispatch_attempt(
                    job=job,
                    provider_id=prov_id,
                    account_id=acct_id,
                    model=mdl,
                    worker=wrk,
                )
                job.provider = prov_id
                job.account = acct_id
                job.model = mdl or job.model
                job.mark_completed(result=result.to_dict(), duration=result.duration_seconds)
                self._persist_job(job)
                self.log_audit("JOB_COMPLETED", {"job_id": job.id, "provider": prov_id, "account": acct_id})
                return job
            except Exception as exc:
                last_error = str(exc)
                job.retry_count += 1
                next_attempt = attempts[i + 1] if i + 1 < len(attempts) else None
                failover_record = {
                    "attempt": i + 1,
                    "original_provider": prov_id,
                    "original_account": acct_id,
                    "error": self.redactor.redact_text(last_error),
                    "next_provider": next_attempt["provider"] if next_attempt else None,
                    "next_account": next_attempt["account"] if next_attempt else None,
                    "reason": f"Execution failed on {prov_id}/{acct_id}; triggering failover" if next_attempt else "All attempts exhausted",
                }
                job.metadata.setdefault("failover_history", []).append(failover_record)
                self.log_audit("JOB_ATTEMPT_FAILED", {
                    "job_id": job.id,
                    "attempt": i + 1,
                    "provider": prov_id,
                    "account": acct_id,
                    "error": self.redactor.redact_text(last_error),
                    "failover_record": failover_record,
                })
                # Check pool and record failure
                pool = self.accounts.get_pool(prov_id)
                if pool and acct_id:
                    pool.record_failure(acct_id, error=last_error)

        job.mark_failed(error=last_error)
        self._persist_job(job)
        self.log_audit("JOB_FAILED", {"job_id": job.id, "error": self.redactor.redact_text(last_error)})
        return job

    def _dispatch_attempt(
        self,
        job: Job,
        provider_id: str,
        account_id: str,
        model: str,
        worker: str | None,
    ) -> TaskExecutionResult:
        """Attempt single execution against an AIProvider or AgentAdapter."""
        ai_provider = self.registry.get_ai_provider(provider_id)
        if ai_provider:
            return self._execute_ai_provider(
                job=job,
                provider=ai_provider,
                account_id=account_id,
                model=model,
            )

        # Check if it's an Agent Adapter / Worker
        adapter = None
        if worker:
            adapter = self.registry.get_adapter(worker)
        elif account_id:
            adapter = self.registry.get_adapter(account_id)
        if not adapter:
            # Look up in provider's adapters
            prov_view = self.registry.get_provider(provider_id)
            if prov_view and prov_view.adapters:
                adapter = next(iter(prov_view.adapters.values()))

        if adapter:
            return self._execute_agent_adapter(job=job, adapter=adapter, model=model)

        raise ValueError(f"Provider '{provider_id}' is neither a registered AIProvider nor a known agent adapter.")

    def _execute_ai_provider(
        self,
        job: Job,
        provider: AIProvider,
        account_id: str,
        model: str,
    ) -> TaskExecutionResult:
        # Check / allocate account from pool if not specified
        pool = self.accounts.get_pool(provider.provider_id)
        target_account: Account | None = None
        if account_id:
            target_account = self.accounts.get_account(account_id)
            if not target_account and pool:
                target_account = pool.get_account(account_id)
            if not target_account:
                raise ValueError(f"Account '{account_id}' not found in registry for provider '{provider.provider_id}'")
            if not target_account.enabled:
                raise ValueError(f"Account '{account_id}' is disabled.")
            if target_account.status == AccountStatus.RATE_LIMITED:
                raise RuntimeError(f"Account '{account_id}' is currently rate-limited.")
        elif pool:
            target_account = pool.get_available_account(model=model)
            if not target_account:
                raise RuntimeError(f"No available healthy account in pool for provider '{provider.provider_id}'.")
            account_id = target_account.id

        # Acquire concurrency slot in pool
        if pool and target_account:
            pool.checkout(target_account.id)

        try:
            # Set job's account and model for execution
            job.account = account_id
            if model:
                job.model = model

            t0 = time.monotonic()
            result = provider.execute(job)
            duration = round(time.monotonic() - t0, 3)

            if not result.success:
                if pool and target_account:
                    pool.record_failure(target_account.id, error=result.error or "Provider error")
                raise RuntimeError(result.error or f"Execution failed on {provider.provider_id}")

            # Record success in pool
            if pool and target_account:
                pool.record_success(target_account.id)

            # Track usage
            self.usage_tracker.record(
                job_id=job.id,
                provider_id=provider.provider_id,
                account_id=account_id,
                model=result.actual_model or job.model,
                total_tokens=result.total_tokens,
            )
            try:
                from brain.analytics.usage_tracker import get_usage_tracker
                get_usage_tracker().record_usage(
                    job_id=job.id,
                    provider_id=provider.provider_id,
                    account_id=account_id,
                    model_id=result.actual_model or job.model,
                    success=result.success,
                    total_tokens=result.total_tokens,
                    latency=duration,
                    retries=job.retry_count,
                    failovers=len(job.metadata.get("failover_history", [])),
                )
            except Exception:
                logger.warning("Failed to record usage for job %s", job.id, exc_info=True)

            return result
        finally:
            if pool and target_account:
                pool.release(target_account.id)

    def _execute_agent_adapter(
        self,
        job: Job,
        adapter: Any,
        model: str,
    ) -> TaskExecutionResult:
        from agents.base.adapter import AgentInvocation, FileModificationPolicy
        invocation = AgentInvocation(
            prompt=job.task,
            model=model or getattr(adapter, "default_model", "auto"),
            dangerously_skip_permissions=True,
            file_policy=FileModificationPolicy.PERMISSIVE,
        )
        t0 = time.monotonic()
        res = adapter.invoke(invocation)
        duration = round(time.monotonic() - t0, 3)

        # Track agent usage
        try:
            from brain.analytics.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                job_id=job.id,
                provider_id=adapter.provider,
                account_id=adapter.agent_id,
                model_id=res.actual_model or model or getattr(adapter, "default_model", "auto"),
                success=res.success,
                total_tokens=res.total_tokens,
                latency=duration,
                retries=job.retry_count,
                failovers=len(job.metadata.get("failover_history", [])),
            )
        except Exception:
            logger.warning("Failed to record usage for job %s", job.id, exc_info=True)

        if not res.success:
            raise RuntimeError(res.error or f"Agent {adapter.agent_id} invocation failed.")

        return res

