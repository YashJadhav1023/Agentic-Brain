"""Universal Quota and Budget Alert Manager for Mission Control.

Enforces configurable daily/monthly request, token, and cost limits
per provider, account, and model. Automatically signals quota exhaustion
and provides multi-threshold budget alerts (50%, 75%, 90%, 100%).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from brain.analytics.usage_tracker import UsageTracker, get_usage_tracker

logger = logging.getLogger("MissionControl.QuotaManager")


class QuotaScope(str, Enum):
    PROVIDER = "provider"
    ACCOUNT = "account"
    MODEL = "model"


@dataclass
class QuotaRule:
    """Configured quota limit definition for a provider, account, or model."""
    target_type: str = "account"  # "provider", "account", "model"
    target_id: str = ""
    daily_request_limit: int | None = None
    daily_token_limit: int | None = None
    monthly_request_limit: int | None = None
    monthly_token_limit: int | None = None
    daily_cost_limit: float | None = None
    monthly_cost_limit: float | None = None
    enabled: bool = True
    # Aliases for flexibility
    scope: Any = None
    cost_limit_usd: float | None = None

    def __post_init__(self):
        if self.scope is not None:
            self.target_type = self.scope.value if hasattr(self.scope, "value") else str(self.scope)
        if self.cost_limit_usd is not None and not self.monthly_cost_limit:
            self.monthly_cost_limit = self.cost_limit_usd

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuotaCheckResult:
    """Outcome of a quota check against current usage."""
    allowed: bool
    target_id: str
    target_type: str
    utilization_pct: float = 0.0
    highest_threshold_alert: int | None = None  # 50, 75, 90, 100 or None
    remaining_daily_tokens: int | None = None
    remaining_daily_requests: int | None = None
    remaining_monthly_tokens: int | None = None
    remaining_monthly_requests: int | None = None
    remaining_daily_cost: float | None = None
    remaining_monthly_cost: float | None = None
    reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.allowed

    @property
    def is_exhausted(self) -> bool:
        return not self.allowed or (self.highest_threshold_alert is not None and self.highest_threshold_alert >= 100)

    @property
    def alert_threshold(self) -> int | None:
        return self.highest_threshold_alert

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_available"] = self.is_available
        d["is_exhausted"] = self.is_exhausted
        d["alert_threshold"] = self.alert_threshold
        return d


class QuotaManager:
    """Tracks and enforces limits across providers, accounts, and models."""

    ALERT_THRESHOLDS = (50, 75, 90, 100)

    def __init__(
        self,
        config_file: Path | str | None = None,
        usage_tracker: UsageTracker | None = None,
        storage_file: Path | str | None = None,
    ) -> None:
        target = config_file or storage_file or os.environ.get("BRAIN_QUOTAS_CONFIG")
        if target:
            self._config_file = Path(target)
            self._usage_tracker = usage_tracker or (UsageTracker(storage_file=False) if (config_file or storage_file) else get_usage_tracker())
        else:
            self._config_file = Path(__file__).resolve().parents[2] / "config" / "quotas.json"
            self._usage_tracker = usage_tracker or get_usage_tracker()
        self._rules: dict[str, QuotaRule] = {}  # key: f"{target_type}:{target_id}"
        self._alert_callbacks: list[Callable[[QuotaCheckResult], None]] = []
        self._load_rules()

    def _rule_key(self, target_type: str, target_id: str) -> str:
        return f"{target_type.lower()}:{target_id.lower()}"

    def _load_rules(self) -> None:
        if not self._config_file.is_file():
            return
        try:
            data = json.loads(self._config_file.read_text(encoding="utf-8"))
            for key, val in data.items():
                self._rules[key] = QuotaRule(
                    target_type=val.get("target_type", "account"),
                    target_id=val.get("target_id", ""),
                    daily_request_limit=val.get("daily_request_limit"),
                    daily_token_limit=val.get("daily_token_limit"),
                    monthly_request_limit=val.get("monthly_request_limit"),
                    monthly_token_limit=val.get("monthly_token_limit"),
                    daily_cost_limit=val.get("daily_cost_limit"),
                    monthly_cost_limit=val.get("monthly_cost_limit"),
                    enabled=val.get("enabled", True),
                )
        except Exception as exc:
            logger.warning(f"Failed to load quotas from {self._config_file}: {exc}")

    def save_rules(self) -> None:
        try:
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            out = {k: v.to_dict() for k, v in self._rules.items()}
            self._config_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to save quotas to {self._config_file}: {exc}")

    def set_quota(
        self,
        target_type: str | QuotaRule,
        target_id: str = "",
        daily_requests: int | None = None,
        daily_tokens: int | None = None,
        monthly_requests: int | None = None,
        monthly_tokens: int | None = None,
        daily_cost: float | None = None,
        monthly_cost: float | None = None,
        enabled: bool = True,
    ) -> QuotaRule:
        if isinstance(target_type, QuotaRule):
            rule = target_type
            if hasattr(rule, "scope") and rule.scope is not None and not rule.target_type:
                rule.target_type = rule.scope.value if hasattr(rule.scope, "value") else str(rule.scope)
            if hasattr(rule, "cost_limit_usd") and rule.cost_limit_usd is not None and not rule.monthly_cost_limit:
                rule.monthly_cost_limit = rule.cost_limit_usd
            self._rules[self._rule_key(rule.target_type, rule.target_id)] = rule
            self.save_rules()
            return rule

        rule = QuotaRule(
            target_type=target_type,
            target_id=target_id,
            daily_request_limit=daily_requests,
            daily_token_limit=daily_tokens,
            monthly_request_limit=monthly_requests,
            monthly_token_limit=monthly_tokens,
            daily_cost_limit=daily_cost,
            monthly_cost_limit=monthly_cost,
            enabled=enabled,
        )
        self._rules[self._rule_key(target_type, target_id)] = rule
        self.save_rules()
        return rule

    def remove_quota(self, target_type: str, target_id: str) -> bool:
        key = self._rule_key(target_type, target_id)
        if key in self._rules:
            del self._rules[key]
            self.save_rules()
            return True
        return False

    def list_quotas(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._rules.values()]

    def check_quota(self, scope: Any, target_id: str, requested_tokens: int = 0) -> QuotaCheckResult:
        stype = scope.value if hasattr(scope, "value") else str(scope)
        return self.check_target_quota(stype, target_id, requested_tokens)

    def record_usage(
        self,
        scope: Any,
        target_id: str,
        tokens: int = 0,
        requests: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        stype = scope.value if hasattr(scope, "value") else str(scope)
        kwargs = {}
        if stype == "provider":
            kwargs["provider_id"] = target_id
        elif stype == "account":
            kwargs["account_id"] = target_id
        elif stype == "model":
            kwargs["model_id"] = target_id

        from brain.analytics.usage_tracker import UsageRecord
        self._usage_tracker.record_usage(UsageRecord(
            job_id=f"quota-rec-{datetime.now(timezone.utc).timestamp()}",
            total_tokens=tokens,
            requests=requests,
            cost_usd=cost_usd,
            estimated_cost_usd=cost_usd,
            **kwargs
        ))

    def reset_counters(self) -> None:
        """Reset usage tracker in-memory records and local storage."""
        self._usage_tracker._records.clear()
        storage = getattr(self._usage_tracker, "_storage_file", None)
        if storage and storage.exists():
            try:
                storage.unlink()
            except Exception:
                pass

    def get_active_alerts(self) -> list[dict[str, Any]]:
        alerts = []
        for r in self._rules.values():
            st = self.check_target_quota(r.target_type, r.target_id)
            if st.highest_threshold_alert is not None:
                alerts.append({
                    "target_type": r.target_type,
                    "target_id": r.target_id,
                    "threshold": st.highest_threshold_alert,
                    "utilization_pct": st.utilization_pct,
                })
        return alerts

    def check_target_quota(
        self,
        target_type: str,
        target_id: str,
        requested_tokens: int = 0,
    ) -> QuotaCheckResult:
        """Check quota compliance for a single target."""
        rule = self._rules.get(self._rule_key(target_type, target_id))
        if not rule or not rule.enabled:
            return QuotaCheckResult(allowed=True, target_id=target_id, target_type=target_type)

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        current_month = now.strftime("%Y-%m")

        filter_kwargs: dict[str, Any] = {}
        if target_type == "provider":
            filter_kwargs["provider_id"] = target_id
        elif target_type == "account":
            filter_kwargs["account_id"] = target_id
        elif target_type == "model":
            filter_kwargs["model_id"] = target_id

        daily_summary = self._usage_tracker.get_summary(day=today, **filter_kwargs)
        monthly_summary = self._usage_tracker.get_summary(month=current_month, **filter_kwargs)

        utilizations: list[float] = []
        violations: list[str] = []

        # 1. Daily Requests
        rem_daily_req = None
        if rule.daily_request_limit:
            used = daily_summary["requests"]
            rem_daily_req = max(0, rule.daily_request_limit - used)
            pct = (used / rule.daily_request_limit) * 100.0
            utilizations.append(pct)
            if used >= rule.daily_request_limit:
                violations.append(f"Daily request limit exceeded ({used}/{rule.daily_request_limit})")

        # 2. Daily Tokens
        rem_daily_tok = None
        if rule.daily_token_limit:
            used = daily_summary["total_tokens"]
            rem_daily_tok = max(0, rule.daily_token_limit - used)
            pct = (used / rule.daily_token_limit) * 100.0
            utilizations.append(pct)
            if (used + requested_tokens) > rule.daily_token_limit:
                violations.append(f"Daily token limit exceeded ({used + requested_tokens}/{rule.daily_token_limit})")

        # 3. Monthly Requests
        rem_monthly_req = None
        if rule.monthly_request_limit:
            used = monthly_summary["requests"]
            rem_monthly_req = max(0, rule.monthly_request_limit - used)
            pct = (used / rule.monthly_request_limit) * 100.0
            utilizations.append(pct)
            if used >= rule.monthly_request_limit:
                violations.append(f"Monthly request limit exceeded ({used}/{rule.monthly_request_limit})")

        # 4. Monthly Tokens
        rem_monthly_tok = None
        if rule.monthly_token_limit:
            used = monthly_summary["total_tokens"]
            rem_monthly_tok = max(0, rule.monthly_token_limit - used)
            pct = (used / rule.monthly_token_limit) * 100.0
            utilizations.append(pct)
            if (used + requested_tokens) > rule.monthly_token_limit:
                violations.append(f"Monthly token limit exceeded ({used + requested_tokens}/{rule.monthly_token_limit})")

        # 5. Cost limits
        rem_daily_cost = None
        if rule.daily_cost_limit:
            used = daily_summary["estimated_cost_usd"]
            rem_daily_cost = max(0.0, rule.daily_cost_limit - used)
            pct = (used / rule.daily_cost_limit) * 100.0
            utilizations.append(pct)
            if used >= rule.daily_cost_limit:
                violations.append(f"Daily cost limit exceeded (${used:.2f}/${rule.daily_cost_limit:.2f})")

        rem_monthly_cost = None
        if rule.monthly_cost_limit:
            used = monthly_summary["estimated_cost_usd"]
            rem_monthly_cost = max(0.0, rule.monthly_cost_limit - used)
            pct = (used / rule.monthly_cost_limit) * 100.0
            utilizations.append(pct)
            if used >= rule.monthly_cost_limit:
                violations.append(f"Monthly cost limit exceeded (${used:.2f}/${rule.monthly_cost_limit:.2f})")

        max_util = max(utilizations) if utilizations else 0.0

        # Calculate threshold alerts: 50, 75, 90, 100
        highest_alert = None
        for t in self.ALERT_THRESHOLDS:
            if max_util >= t:
                highest_alert = t

        allowed = len(violations) == 0
        res = QuotaCheckResult(
            allowed=allowed,
            target_id=target_id,
            target_type=target_type,
            utilization_pct=round(max_util, 1),
            highest_threshold_alert=highest_alert,
            remaining_daily_tokens=rem_daily_tok,
            remaining_daily_requests=rem_daily_req,
            remaining_monthly_tokens=rem_monthly_tok,
            remaining_monthly_requests=rem_monthly_req,
            remaining_daily_cost=rem_daily_cost,
            remaining_monthly_cost=rem_monthly_cost,
            reason="; ".join(violations) if violations else None,
        )

        if highest_alert and self._alert_callbacks:
            for cb in self._alert_callbacks:
                try:
                    cb(res)
                except Exception:
                    pass

        return res

    def check_job_quota(
        self,
        provider_id: str,
        account_id: str,
        model_id: str = "",
        requested_tokens: int = 0,
    ) -> QuotaCheckResult:
        """Check all applicable quotas (provider, account, model). Returns most restrictive."""
        for target_type, target_id in (
            ("account", account_id),
            ("provider", provider_id),
            ("model", model_id),
        ):
            if target_id:
                res = self.check_target_quota(target_type, target_id, requested_tokens)
                if not res.allowed:
                    return res
        # If all allowed, return the highest utilization result
        res_acct = self.check_target_quota("account", account_id, requested_tokens) if account_id else None
        res_prov = self.check_target_quota("provider", provider_id, requested_tokens) if provider_id else None
        res_mod = self.check_target_quota("model", model_id, requested_tokens) if model_id else None

        candidates = [c for c in (res_acct, res_prov, res_mod) if c is not None]
        if not candidates:
            return QuotaCheckResult(allowed=True, target_id=account_id or provider_id, target_type="account")
        candidates.sort(key=lambda c: c.utilization_pct, reverse=True)
        return candidates[0]

    def register_alert_callback(self, callback: Callable[[QuotaCheckResult], None]) -> None:
        self._alert_callbacks.append(callback)


_GLOBAL_QUOTA_MANAGER: QuotaManager | None = None


def get_quota_manager() -> QuotaManager:
    global _GLOBAL_QUOTA_MANAGER
    if _GLOBAL_QUOTA_MANAGER is None:
        _GLOBAL_QUOTA_MANAGER = QuotaManager()
    return _GLOBAL_QUOTA_MANAGER
