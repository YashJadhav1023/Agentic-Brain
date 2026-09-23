"""Universal Usage Tracking System for Mission Control.

Tracks requests, tokens, latency, retries, and costs per provider, account,
model, job, day, and month with lightweight local persistence.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brain.analytics.cost_tracker import CostTracker, get_cost_tracker

logger = logging.getLogger("MissionControl.UsageTracker")


@dataclass
class UsageRecord:
    """A single execution usage record."""
    job_id: str = ""
    provider_id: str = ""
    account_id: str = ""
    model_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    date: str = ""  # YYYY-MM-DD
    month: str = ""  # YYYY-MM
    success: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency: float = 0.0
    latency_ms: float = 0.0
    retries: int = 0
    failovers: int = 0
    cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    pricing_type: str = "unknown"
    error_type: str | None = None
    requests: int = 1

    def __post_init__(self) -> None:
        if not self.date or not self.month:
            try:
                dt = datetime.fromisoformat(self.timestamp)
                self.date = dt.strftime("%Y-%m-%d")
                self.month = dt.strftime("%Y-%m")
            except Exception:
                pass
        if not self.total_tokens:
            self.total_tokens = self.input_tokens + self.output_tokens
        if self.latency_ms and not self.latency:
            self.latency = self.latency_ms
        elif self.latency and not self.latency_ms:
            self.latency_ms = self.latency
        if self.estimated_cost_usd is not None and self.cost_usd is None:
            self.cost_usd = self.estimated_cost_usd
        elif self.cost_usd is not None and self.estimated_cost_usd is None:
            self.estimated_cost_usd = self.cost_usd

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageTracker:
    """High-performance in-memory and persistent usage tracking."""

    def __init__(
        self,
        storage_file: Path | str | bool | None = None,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        if storage_file is False:
            self._storage_file = None
        elif storage_file:
            self._storage_file = Path(storage_file)
        else:
            self._storage_file = (
                Path(__file__).resolve().parents[2] / "runtime" / "analytics" / "usage.jsonl"
            )
        self._cost_tracker = cost_tracker or get_cost_tracker()
        self._records: list[UsageRecord] = []
        self._load_records()

    def _load_records(self) -> None:
        if not self._storage_file or not self._storage_file.is_file():
            return
        try:
            with open(self._storage_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        rec = UsageRecord(
                            job_id=data.get("job_id", ""),
                            provider_id=data.get("provider_id", ""),
                            account_id=data.get("account_id", ""),
                            model_id=data.get("model_id", ""),
                            timestamp=data.get("timestamp", ""),
                            date=data.get("date", ""),
                            month=data.get("month", ""),
                            success=data.get("success", True),
                            input_tokens=data.get("input_tokens", 0),
                            output_tokens=data.get("output_tokens", 0),
                            total_tokens=data.get("total_tokens", 0),
                            latency=float(data.get("latency", 0.0)),
                            retries=int(data.get("retries", 0)),
                            failovers=int(data.get("failovers", 0)),
                            cost_usd=data.get("cost_usd"),
                            pricing_type=data.get("pricing_type", "unknown"),
                        )
                        self._records.append(rec)
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning(f"Error loading usage records from {self._storage_file}: {exc}")

    def record_usage(
        self,
        job_id_or_record: UsageRecord | str | None = None,
        provider_id: str = "",
        account_id: str = "",
        model_id: str = "",
        success: bool = True,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        latency: float = 0.0,
        retries: int = 0,
        failovers: int = 0,
        cost_usd: float | None = None,
        **kwargs: Any,
    ) -> UsageRecord:
        """Record usage and compute cost if known."""
        # Callers pass the id as `job_id=`; accept it as an alias.
        if job_id_or_record is None:
            job_id_or_record = kwargs.pop("job_id", None)
        if job_id_or_record is None:
            raise TypeError("record_usage() requires a UsageRecord or a job id")
        if isinstance(job_id_or_record, UsageRecord):
            rec = job_id_or_record
        else:
            job_id = str(job_id_or_record)
            tot_tok = total_tokens or (input_tokens + output_tokens)

            # Compute cost if not explicitly passed
            pricing_type = kwargs.get("pricing_type", "unknown")
            if cost_usd is None and ("estimated_cost_usd" not in kwargs or kwargs["estimated_cost_usd"] is None):
                cost_calc = self._cost_tracker.calculate_cost(
                    provider_id=provider_id,
                    model_id=model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                cost_usd = cost_calc["total_cost"]
                pricing_type = cost_calc["pricing_type"]
            elif cost_usd is None and "estimated_cost_usd" in kwargs:
                cost_usd = kwargs["estimated_cost_usd"]

            rec = UsageRecord(
                job_id=job_id,
                provider_id=provider_id,
                account_id=account_id,
                model_id=model_id,
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=tot_tok,
                latency=latency,
                retries=retries,
                failovers=failovers,
                cost_usd=cost_usd,
                pricing_type=pricing_type,
                **kwargs,
            )
        self._records.append(rec)

        # Append to disk if storage file configured
        if self._storage_file:
            try:
                self._storage_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._storage_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec.to_dict()) + "\n")
            except Exception as exc:
                logger.warning(f"Failed to persist usage record: {exc}")

        return rec

    def get_summary(
        self,
        provider_id: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        day: str | None = None,
        month: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate metrics matching the given filter."""
        filtered = self._filter_records(
            provider_id=provider_id,
            account_id=account_id,
            model_id=model_id,
            day=day,
            month=month,
        )
        req_count = len(filtered)
        succ_count = sum(1 for r in filtered if r.success)
        fail_count = req_count - succ_count
        in_tokens = sum(r.input_tokens for r in filtered)
        out_tokens = sum(r.output_tokens for r in filtered)
        tot_tokens = sum(r.total_tokens for r in filtered)
        retries = sum(r.retries for r in filtered)
        failovers = sum(r.failovers for r in filtered)
        latencies = [r.latency for r in filtered if r.latency > 0]
        avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else 0.0

        known_costs = [r.cost_usd for r in filtered if r.cost_usd is not None]
        total_cost = round(sum(known_costs), 6) if known_costs else 0.0

        return {
            "requests": req_count,
            "successful_requests": succ_count,
            "failed_requests": fail_count,
            "success_rate": round((succ_count / req_count * 100.0) if req_count else 100.0, 1),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "total_tokens": tot_tokens,
            "estimated_cost_usd": total_cost,
            "has_unknown_costs": len(known_costs) < len(filtered),
            "avg_latency": avg_latency,
            "retries": retries,
            "failovers": failovers,
        }

    def _filter_records(
        self,
        provider_id: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        day: str | None = None,
        month: str | None = None,
    ) -> list[UsageRecord]:
        res = self._records
        if provider_id:
            res = [r for r in res if r.provider_id == provider_id]
        if account_id:
            res = [r for r in res if r.account_id == account_id]
        if model_id:
            res = [r for r in res if r.model_id == model_id]
        if day:
            res = [r for r in res if r.date == day]
        if month:
            res = [r for r in res if r.month == month]
        return res

    def get_breakdown(self, group_by: str = "provider") -> dict[str, dict[str, Any]]:
        """Group usage by 'provider', 'account', or 'model'."""
        groups: dict[str, list[UsageRecord]] = {}
        for r in self._records:
            if group_by == "provider":
                k = r.provider_id or "unknown"
            elif group_by == "account":
                k = r.account_id or "unknown"
            elif group_by == "model":
                k = r.model_id or "unknown"
            elif group_by == "day":
                k = r.date or "unknown"
            elif group_by == "month":
                k = r.month or "unknown"
            else:
                k = "all"
            groups.setdefault(k, []).append(r)

        result: dict[str, dict[str, Any]] = {}
        for k, recs in groups.items():
            reqs = len(recs)
            succ = sum(1 for r in recs if r.success)
            tokens = sum(r.total_tokens for r in recs)
            costs = [r.cost_usd for r in recs if r.cost_usd is not None]
            cost = round(sum(costs), 6) if costs else 0.0
            lats = [r.latency for r in recs if r.latency > 0]
            avg_lat = round(sum(lats) / len(lats), 3) if lats else 0.0
            result[k] = {
                "requests": reqs,
                "successful_requests": succ,
                "failed_requests": reqs - succ,
                "total_tokens": tokens,
                "estimated_cost_usd": cost,
                "avg_latency": avg_lat,
            }
        return result

    def get_usage_by_provider(self) -> dict[str, dict[str, Any]]:
        """Get usage breakdown grouped by provider."""
        return self.get_breakdown("provider")

    def get_usage_by_account(self) -> dict[str, dict[str, Any]]:
        """Get usage breakdown grouped by account."""
        return self.get_breakdown("account")

    def get_usage_by_model(self) -> dict[str, dict[str, Any]]:
        """Get usage breakdown grouped by model."""
        return self.get_breakdown("model")

    def get_timeseries(self, days: int = 30) -> list[dict[str, Any]]:
        """Get daily aggregation for the last N days."""
        daily_groups: dict[str, list[UsageRecord]] = {}
        for r in self._records:
            if r.date:
                daily_groups.setdefault(r.date, []).append(r)

        sorted_days = sorted(daily_groups.keys())[-days:]
        timeseries = []
        for d in sorted_days:
            recs = daily_groups[d]
            tokens = sum(r.total_tokens for r in recs)
            reqs = len(recs)
            costs = [r.cost_usd for r in recs if r.cost_usd is not None]
            cost = round(sum(costs), 6) if costs else 0.0
            fails = sum(1 for r in recs if not r.success)
            lats = [r.latency for r in recs if r.latency > 0]
            avg_lat = round(sum(lats) / len(lats), 3) if lats else 0.0
            timeseries.append({
                "date": d,
                "requests": reqs,
                "tokens": tokens,
                "cost": cost,
                "failures": fails,
                "avg_latency": avg_lat,
            })
        return timeseries

    def get_job_records(self, job_id: str) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records if r.job_id == job_id]

    def clear(self) -> None:
        self._records.clear()
        if self._storage_file.exists():
            self._storage_file.unlink()


_GLOBAL_USAGE_TRACKER: UsageTracker | None = None


def get_usage_tracker() -> UsageTracker:
    global _GLOBAL_USAGE_TRACKER
    if _GLOBAL_USAGE_TRACKER is None:
        _GLOBAL_USAGE_TRACKER = UsageTracker()
    return _GLOBAL_USAGE_TRACKER
