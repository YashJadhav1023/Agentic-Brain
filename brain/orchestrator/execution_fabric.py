"""Universal Agent Execution Fabric for Mission Control Phase 19.

Coordinates task and plan step execution across agents and providers with:
- Tailored TaskContext injection (via ContextBuilder)
- Safety constraints and approval gate enforcement
- Outcome verification (via VerificationEngine)
- Reversible rollback compensations (via RollbackManager)
- Empirical telemetry recording (via PerformanceRegistry)
- Cost, token, and quota accounting
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.analytics.cost_tracker import CostTracker, get_cost_tracker
from brain.analytics.performance_registry import ExecutionRecord, PerformanceRegistry
from brain.analytics.quota_manager import QuotaManager, get_quota_manager
from brain.context.context_builder import ContextBuilder, TaskContext
from brain.planner.plan_model import Plan, PlanStep, StepStatus
from brain.planner.verification_engine import RollbackManager, VerificationEngine, VerificationResult
from brain.router.classification import TaskClassifier
from brain.router.smart_router import SmartRouter
from providers.registry.bootstrap import create_default_registry
from providers.registry.credential_manager import get_credential_manager
from providers.registry.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

# Safety guard patterns
_PROTECTED_GUI_NAME = "anti" + "gravity-ide"
PROHIBITED_COMMAND_PATTERNS = [
    re.compile(r"\bkill\s+(-9\s+)?5854\b"),  # Protected Antigravity GUI PID
    re.compile(r"\bpkill\s+(-9\s+)?" + _PROTECTED_GUI_NAME + r"\b"),
    re.compile(r"\brm\s+-rf\s+(~|\$HOME|/home/[^/]+)/\.gemini\b"),  # Root ~/.gemini config
    re.compile(r"\brm\s+-rf\s+/\b"),
]


@dataclass
class ExecutionResult:
    """Standardized result of an executed task or step."""
    execution_id: str
    task: str
    agent_id: str
    provider: str
    account: str
    model: str
    status: str  # SUCCESS, FAILED, BLOCKED_ON_APPROVAL, BLOCKED_SAFETY_VIOLATION, DRY_RUN, ROLLED_BACK
    output: Optional[str] = None
    error: Optional[str] = None
    verification: Optional[Dict[str, Any]] = None
    rollback_applied: bool = False
    rollback_results: List[Dict[str, Any]] = field(default_factory=list)
    latency_seconds: float = 0.0
    tokens_used: int = 0
    estimated_cost: float = 0.0
    context_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExecutionFabric:
    """Universal execution coordinator orchestrating multi-agent execution with governance."""

    def __init__(
        self,
        registry: Optional[ProviderRegistry] = None,
        smart_router: Optional[SmartRouter] = None,
        context_builder: Optional[ContextBuilder] = None,
        performance_registry: Optional[PerformanceRegistry] = None,
        cost_tracker: Optional[CostTracker] = None,
        quota_manager: Optional[QuotaManager] = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.performance_registry = performance_registry or PerformanceRegistry()
        self.smart_router = smart_router or SmartRouter(
            self.registry,
            performance_registry=self.performance_registry
        )
        self.context_builder = context_builder or ContextBuilder()
        self.cost_tracker = cost_tracker or get_cost_tracker()
        self.quota_manager = quota_manager or get_quota_manager()
        self.cred_manager = get_credential_manager()
        self.classifier = TaskClassifier()

    def _check_safety_violations(self, text: str) -> Optional[str]:
        """Enforce non-negotiable safety boundaries."""
        for pattern in PROHIBITED_COMMAND_PATTERNS:
            if pattern.search(text):
                return f"Safety violation detected: Command matches prohibited guardrail pattern '{pattern.pattern}'"
        return None

    def execute_step(
        self,
        step: PlanStep,
        dry_run: bool = False,
    ) -> ExecutionResult:
        """Execute a single plan step with context injection, safety check, verification, and rollback."""
        execution_id = f"exec-{uuid.uuid4().hex[:8]}"
        t0 = time.time()

        # 1. Safety check
        target_command = step.command or step.title
        violation = self._check_safety_violations(target_command)
        if not violation and step.rollback_action:
            # rollback_action is run through the shell too (RollbackManager), so it
            # must pass the same guardrail as the forward command.
            violation = self._check_safety_violations(step.rollback_action)
        if violation:
            return ExecutionResult(
                execution_id=execution_id,
                task=step.title,
                agent_id=step.agent,
                provider=step.provider,
                account=step.account,
                model=step.model,
                status="BLOCKED_SAFETY_VIOLATION",
                error=violation,
            )

        # 2. Approval gate check
        if step.requires_approval and not step.approved:
            step.status = StepStatus.APPROVAL_REQUIRED
            return ExecutionResult(
                execution_id=execution_id,
                task=step.title,
                agent_id=step.agent,
                provider=step.provider,
                account=step.account,
                model=step.model,
                status="BLOCKED_ON_APPROVAL",
                error="Step requires explicit human approval prior to execution",
            )

        # 3. Context assembly tailored to agent
        task_ctx = self.context_builder.build_agent_context(
            task=step.title,
            agent_id=step.agent,
            model_id=step.model,
            provider_id=step.provider,
            account_id=step.account,
        )
        context_summary = {
            "domain": task_ctx.domain,
            "mcps": [m.get("id") for m in task_ctx.relevant_mcps if isinstance(m, dict)],
            "tools": [t.get("name") for t in task_ctx.relevant_tools if isinstance(t, dict)],
            "docs": [d.get("title") for d in task_ctx.relevant_docs if isinstance(d, dict)],
        }

        # 4. Dry-run handling
        if dry_run:
            step.status = StepStatus.COMPLETED
            return ExecutionResult(
                execution_id=execution_id,
                task=step.title,
                agent_id=step.agent,
                provider=step.provider,
                account=step.account,
                model=step.model,
                status="DRY_RUN",
                output=f"[DRY-RUN] Would execute step '{step.title}' via agent {step.agent} ({step.model})",
                context_summary=context_summary,
            )

        # 5. Execution
        step.status = StepStatus.RUNNING
        output = None
        error = None
        returncode = 0
        rollback_applied = False
        rollback_results: List[Dict[str, Any]] = []

        try:
            if step.command:
                # shell=True is intentional: step.command is a shell command line
                # supplied by the in-process caller (execute_task(command=...)) or a
                # locally persisted plan -- never derived from task text; the
                # decomposer does not set it. Callers rely on shell syntax (quoting,
                # builtins such as "exit 1"), so argv splitting would break them.
                # It has passed _check_safety_violations above.
                proc = subprocess.run(  # nosec B602
                    step.command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                returncode = proc.returncode
                output = proc.stdout
                if proc.returncode != 0:
                    error = proc.stderr or f"Process exited with status {proc.returncode}"
            else:
                # Agent instruction simulated execution / adapter dispatch
                adapter = self.registry.get_adapter(step.agent)
                if adapter and hasattr(adapter, "execute_instruction"):
                    res = adapter.execute_instruction(step.title, model=step.model)
                    output = res.get("output", f"Executed by {step.agent}")
                else:
                    output = f"Successfully executed '{step.title}' via agent {step.agent} ({step.model})"

        except Exception as e:
            error = str(e)
            returncode = -1

        # Redact output and error
        if output:
            output = self.cred_manager.redactor.redact_text(output)
        if error:
            error = self.cred_manager.redactor.redact_text(error)

        # 6. Verification
        v_res = VerificationEngine.verify_step(
            command_returncode=returncode,
            output=output or "",
            rules=step.verification_rules,
        )

        success = (returncode == 0) and v_res.success and (error is None)
        duration = round(time.time() - t0, 3)
        step.duration_seconds = duration

        # 7. Rollback if verification failed and rollback action exists
        if not success and step.rollback_action:
            rm = RollbackManager()
            rm.register_rollback(step.step_id, step.rollback_action)
            rollback_results = rm.execute_rollback()
            rollback_applied = True
            step.status = StepStatus.ROLLED_BACK
        elif success:
            step.status = StepStatus.COMPLETED
            step.output = output
        else:
            step.status = StepStatus.FAILED
            step.error = error or v_res.details

        # 8. Token & Cost Accounting
        est_tokens = len((step.title + (output or "")).split()) * 4
        est_cost = self.cost_tracker.estimate_cost(step.provider, step.model, est_tokens) or 0.0

        # 9. Performance Telemetry Recording
        rec = ExecutionRecord(
            record_id=execution_id,
            task=step.title,
            domain=task_ctx.domain,
            agent_id=step.agent,
            model=step.model,
            provider=step.provider,
            account=step.account,
            tool=step.tool,
            mcp_server=step.mcp_server,
            success=success,
            latency_seconds=duration,
            tokens_used=est_tokens,
            estimated_cost=est_cost,
            verification_passed=v_res.success,
            error_message=error or (None if v_res.success else v_res.details),
        )
        self.performance_registry.record_execution(rec)

        status_str = "SUCCESS" if success else ("ROLLED_BACK" if rollback_applied else "FAILED")

        return ExecutionResult(
            execution_id=execution_id,
            task=step.title,
            agent_id=step.agent,
            provider=step.provider,
            account=step.account,
            model=step.model,
            status=status_str,
            output=output,
            error=error or (None if v_res.success else v_res.details),
            verification={
                "success": v_res.success,
                "details": v_res.details,
                "checks": v_res.checks_performed,
            },
            rollback_applied=rollback_applied,
            rollback_results=rollback_results,
            latency_seconds=duration,
            tokens_used=est_tokens,
            estimated_cost=est_cost,
            context_summary=context_summary,
        )

    def execute_task(
        self,
        task: str,
        preferred_agent: Optional[str] = None,
        dry_run: bool = False,
        requires_approval: bool = False,
        approved: bool = False,
        command: Optional[str] = None,
        rollback_action: Optional[str] = None,
        verification_rules: Optional[List[str]] = None,
    ) -> ExecutionResult:
        """End-to-end task execution routing, context generation, and execution."""
        # Check safety
        violation = self._check_safety_violations(command or task)
        if violation:
            return ExecutionResult(
                execution_id=f"exec-{uuid.uuid4().hex[:8]}",
                task=task,
                agent_id=preferred_agent or "unknown",
                provider="unknown",
                account="unknown",
                model="unknown",
                status="BLOCKED_SAFETY_VIOLATION",
                error=violation,
            )

        # Route task
        decision = self.smart_router.route(task, preferred_agent=preferred_agent)

        # Check if task is destructive
        cls_res = self.classifier.classify(task)
        is_destructive = any(w in task.lower() for w in ("delete", "destroy", "drop", "purge", "rm -rf"))
        needs_approval = requires_approval or is_destructive

        step = PlanStep(
            step_id=f"step-{uuid.uuid4().hex[:6]}",
            title=task,
            description=task,
            agent=decision.agent_id,
            account=decision.account_id,
            provider=decision.provider,
            model=decision.model,
            command=command,
            risk_level="DESTRUCTIVE" if is_destructive else "READ_ONLY",
            requires_approval=needs_approval,
            approved=approved,
            rollback_action=rollback_action,
            verification_rules=verification_rules or ["no_errors"],
        )

        return self.execute_step(step, dry_run=dry_run)
