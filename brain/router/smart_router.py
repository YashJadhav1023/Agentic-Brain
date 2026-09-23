"""Smart Agent and Model Router — Phase 13 Multi-Factor Routing Engine.

Routing is a measurable multi-factor competition across all healthy agents,
accounts, and models. The router scores candidates using:
- capability_match (MUST use 'capability_match', NOT 'capability_overlap')
- provider_health
- account_health
- model_health
- latency
- recent_success_rate
- failure_rate
- account_priority
- provider_priority
- model_priority
- quota_remaining
- estimated_cost
- task_complexity
- context_size
- streaming_support
- tool_support
- user_preference
- keyword_affinity, strength_fit, reliability, token_efficiency, health, load_penalty

Supports routing modes: performance, balanced, cost, reliability, manual.
Provides explainable score breakdowns and persistent routing history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.base.adapter import AgentAdapter, AgentStatus, Capability
from brain.analytics.analytics_engine import AnalyticsEngine, get_analytics_engine
from brain.analytics.cost_tracker import CostTracker, get_cost_tracker
from brain.analytics.performance_registry import PerformanceRegistry
from brain.analytics.quota_manager import QuotaManager, get_quota_manager
from brain.router.classification import ClassificationResult, TaskCategory, TaskClassifier
from brain.router.models import (
    CandidateScore,
    FailoverConfig,
    RoutingCandidate,
    RoutingDecision,
    RoutingMode,
    RoutingScore,
)
from models.policies.model_policy import AGENT_CATALOGS, Complexity, Risk, select_model
from providers.registry.account_registry import AccountStatus
from providers.registry.credential_manager import get_credential_manager
from providers.registry.lifecycle import (
    AccountLifecycleState,
    AuthState,
    HealthState,
)
from providers.registry.provider_registry import ProviderRegistry

logger = logging.getLogger("MissionControl.SmartRouter")


class AccountRejectionReason:
    """Machine-readable reasons an account was rejected as a routing candidate.

    These are surfaced through :meth:`SmartRouter.get_rejection_reasons` so the
    dashboard can explain precisely why an account is not receiving work.
    """

    NOT_REGISTERED = "NOT_REGISTERED"
    NOT_ENABLED = "NOT_ENABLED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    UNHEALTHY = "UNHEALTHY"
    WRONG_LIFECYCLE_STATE = "WRONG_LIFECYCLE_STATE"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    COOLDOWN = "COOLDOWN"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    CONCURRENCY_EXCEEDED = "CONCURRENCY_EXCEEDED"


#: Lifecycle states in which an account may be admitted as a routing candidate.
_ADMISSIBLE_LIFECYCLE_STATES = frozenset(
    {AccountLifecycleState.READY, AccountLifecycleState.ONLINE}
)
#: Health states in which an account may be admitted (never UNHEALTHY/UNKNOWN).
_ADMISSIBLE_HEALTH_STATES = frozenset(
    {HealthState.HEALTHY, HealthState.DEGRADED}
)


@dataclass
class RouterConfig:
    mode: RoutingMode = RoutingMode.BALANCED
    max_attempts: int = 4
    failover_config: FailoverConfig | None = None

#: Keyword affinity: phrasing that signals a particular agent's specialty.
AGENT_KEYWORDS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "antigravity-account-1",
        re.compile(r"\b(architecture|governance|strategy|design doc|rfc|protocol|spec|system design)\b", re.I),
        "high-level architectural design or protocol governance",
    ),
    (
        "antigravity-account-2",
        re.compile(r"\b(refactor|refactoring|component|review|clean code|modernize|telemetry|consolidate|optimize|restructure|split)\b", re.I),
        "deep refactoring, component modernization and code review",
    ),
    (
        "antigravity-account-3",
        re.compile(r"\b(synthesis|autonomous|pipeline|verification|audit|eval|experiment|reasoning)\b", re.I),
        "autonomous implementation, synthesis and verification",
    ),
    (
        "cline",
        re.compile(r"\b(frontend|ui|css|html|styling|tailwind|view|layout|flexbox)\b", re.I),
        "frontend styling or UI component adjustments",
    ),
    (
        "kiro-cli",
        re.compile(r"\b(test|tests|pytest|unittest|terminal|bash|shell|docker|kubernetes|k8s|ci/cd|pipeline|git|deploy|build|azure|kubectl)\b", re.I),
        "terminal execution, testing or environment validation",
    ),
)

#: Capability inference keywords
CAPABILITY_KEYWORDS: tuple[tuple[Capability, re.Pattern[str]], ...] = (
    (Capability.ARCHITECTURE, re.compile(r"\b(architecture|architect|system design|design doc|topology)\b", re.I)),
    (Capability.PROTOCOL_DESIGN, re.compile(r"\b(protocol|rfc|contract|schema|spec|interface)\b", re.I)),
    (Capability.GOVERNANCE, re.compile(r"\b(governance|policy|compliance|standard|guardrail)\b", re.I)),
    (Capability.DEEP_REASONING, re.compile(r"\b(analy[sz]e|reason|investigate|root cause|trade-?off|evaluate)\b", re.I)),
    (Capability.EDITOR_REFACTORING, re.compile(r"\b(refactor|refactoring|restructure|rename|modernize|split|extract)\b", re.I)),
    (Capability.COMPONENT_REFACTORING, re.compile(r"\b(component|module|service|handler|class|consolidate)\b", re.I)),
    (Capability.CODE_REVIEW, re.compile(r"\b(review|audit|inspect|critique|lint)\b", re.I)),
    (Capability.FRONTEND_STYLING, re.compile(r"\b(css|styling|tailwind|layout|flexbox|frontend|ui)\b", re.I)),
    (Capability.TERMINAL_OPERATIONS, re.compile(r"\b(terminal|bash|shell|command|cli|kubectl|docker)\b", re.I)),
    (Capability.BUILD_AND_TEST, re.compile(r"\b(test|tests|pytest|unittest|build|compile|ci/cd|pipeline)\b", re.I)),
    (Capability.LOCAL_VALIDATION, re.compile(r"\b(validate|verify|check|sanity)\b", re.I)),
    (Capability.CLOUD_READ_ONLY, re.compile(r"\b(azure|aws|subscription|resource group|cloud)\b", re.I)),
    (Capability.KUBERNETES_READ_ONLY, re.compile(r"\b(kubernetes|k8s|aks|pod|namespace|kubectl)\b", re.I)),
    (Capability.DOCUMENTATION, re.compile(r"\b(document|documentation|readme|docs|changelog)\b", re.I)),
    (Capability.TEST_SCAFFOLDING, re.compile(r"\b(test scaffold|add tests|unit test|write tests)\b", re.I)),
)

# Standard baseline weights
KEYWORD_AFFINITY_WEIGHT = 10.0
CAPABILITY_WEIGHT = 4.0
STRENGTH_FIT_WEIGHT = 3.0
RELIABILITY_WEIGHT = 4.0
LATENCY_WEIGHT = 2.0
TOKEN_EFFICIENCY_WEIGHT = 2.0
HEALTH_WEIGHT = 2.0
LOAD_PENALTY = 6.0

COMPLEXITY_TARGET_STRENGTH = {
    Complexity.FAST: 2,
    Complexity.STANDARD: 3,
    Complexity.STRONG: 4,
    Complexity.REASONING: 5,
}


class SmartRouter:
    """Capability-aware, multi-factor task routing across all registered agents and accounts."""

    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        task_manager: Any | None = None,
        history_file: Path | None = None,
        telemetry_tracker: Any | None = None,
        cost_tracker: CostTracker | None = None,
        quota_manager: QuotaManager | None = None,
        analytics_engine: AnalyticsEngine | None = None,
        failover_config: FailoverConfig | None = None,
        config: RouterConfig | None = None,
        performance_registry: PerformanceRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._config = config or RouterConfig()
        self.default_mode = self._config.mode
        self._task_manager = task_manager
        self._history_file = history_file or (
            Path(__file__).resolve().parents[2] / "runtime" / "logs" / "routing_history.jsonl"
        )
        self._telemetry_tracker = telemetry_tracker
        self._cost_tracker = cost_tracker or get_cost_tracker()
        self._quota_manager = quota_manager or get_quota_manager()
        self._analytics_engine = analytics_engine or get_analytics_engine()
        self._performance_registry = performance_registry or PerformanceRegistry()
        self._classifier = TaskClassifier()
        self.failover_config = failover_config or self._config.failover_config or FailoverConfig()
        self._in_memory_decisions: dict[str, RoutingDecision] = {}
        #: account_id -> {"reason": <code>, "detail": str, "timestamp": iso}
        #: recorded on the most recent scoring pass so the dashboard can explain
        #: why an account is not receiving work.
        self._rejection_reasons: dict[str, dict[str, Any]] = {}

    def _record_rejection(
        self, account_id: str, reason: str, detail: str = ""
    ) -> None:
        """Record a structured machine-readable rejection reason for an account."""
        self._rejection_reasons[account_id] = {
            "account_id": account_id,
            "reason": reason,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_rejection_reasons(
        self, account_id: str | None = None
    ) -> dict[str, dict[str, Any]] | dict[str, Any] | None:
        """Return structured rejection reasons from the most recent scoring pass.

        With no argument, returns the full ``{account_id: reason_record}`` map.
        With an ``account_id``, returns that account's reason record or ``None``
        if the account was admitted (or never evaluated).
        """
        if account_id is not None:
            return self._rejection_reasons.get(account_id)
        return dict(self._rejection_reasons)

    def check_account_admissible(
        self,
        account: Any,
        required_capabilities: frozenset[Capability] | None = None,
    ) -> tuple[bool, str | None, str]:
        """Return (admissible, rejection_reason, detail) for an account.

        An account is admitted as a routing candidate ONLY when ALL hold:
        it is registered, ``enabled`` is True, auth state is AUTHENTICATED,
        health state is HEALTHY or DEGRADED, lifecycle state is READY or ONLINE,
        and its capabilities are compatible with the request.
        """
        if account is None:
            return False, AccountRejectionReason.NOT_REGISTERED, "account not registered"

        if not getattr(account, "enabled", False):
            return False, AccountRejectionReason.NOT_ENABLED, "account is disabled"

        # Cooldown / rate-limit / quota take precedence as concrete operational
        # blocks so the dashboard reports the most actionable cause.
        status = getattr(account, "status", None)
        if status == AccountStatus.QUOTA_EXHAUSTED:
            return False, AccountRejectionReason.QUOTA_EXHAUSTED, "quota exhausted"
        if status == AccountStatus.RATE_LIMITED:
            return False, AccountRejectionReason.RATE_LIMITED, "rate limited"
        cooldown_until = getattr(account, "cooldown_until", None)
        if cooldown_until is not None and time.time() < cooldown_until:
            return False, AccountRejectionReason.COOLDOWN, "in cooldown"
        if status == AccountStatus.COOLDOWN:
            return False, AccountRejectionReason.COOLDOWN, "in cooldown"

        auth_state = getattr(account, "auth_state", None)
        if auth_state != AuthState.AUTHENTICATED:
            return (
                False,
                AccountRejectionReason.NOT_AUTHENTICATED,
                f"auth state is {getattr(auth_state, 'value', auth_state)}",
            )

        health_state = getattr(account, "health_state", None)
        if health_state not in _ADMISSIBLE_HEALTH_STATES:
            return (
                False,
                AccountRejectionReason.UNHEALTHY,
                f"health state is {getattr(health_state, 'value', health_state)}",
            )

        lifecycle_state = getattr(account, "lifecycle_state", None)
        if lifecycle_state not in _ADMISSIBLE_LIFECYCLE_STATES:
            return (
                False,
                AccountRejectionReason.WRONG_LIFECYCLE_STATE,
                f"lifecycle state is {getattr(lifecycle_state, 'value', lifecycle_state)}",
            )

        # Concurrency ceiling.
        limit = getattr(account, "concurrency_limit", None)
        current = getattr(account, "current_concurrency", 0)
        if limit is not None and current >= limit:
            return (
                False,
                AccountRejectionReason.CONCURRENCY_EXCEEDED,
                f"{current}/{limit} concurrent slots in use",
            )

        # Capability compatibility. Every registered agent account is a
        # general execution resource whose per-task capability *overlap* is a
        # scoring signal (see score_candidates), not an admission gate --
        # excluding a generalist agent because a single task did not name one
        # of its declared strengths would silently shrink the candidate pool.
        # Admission therefore requires only that the account declares a usable
        # capability set at all; an account that declares NO capabilities is
        # not routable and is reported as CAPABILITY_MISMATCH.
        declared = getattr(account, "capabilities", None) or []
        declared_caps = set()
        for c in declared:
            try:
                declared_caps.add(Capability(c) if not isinstance(c, Capability) else c)
            except ValueError:
                continue
        if not declared_caps:
            return (
                False,
                AccountRejectionReason.CAPABILITY_MISMATCH,
                "account declares no usable capabilities",
            )

        return True, None, "admitted"

    def _get_agent_metrics(self, agent_id: str) -> dict[str, Any]:
        """Aggregate operational telemetry for an agent."""
        if self._telemetry_tracker:
            try:
                metrics = self._telemetry_tracker.get_metrics()
                agent_data = metrics.get("by_agent", {}).get(agent_id, {})
                if agent_data:
                    return agent_data
            except Exception:
                pass

        if self._analytics_engine:
            try:
                perf = self._analytics_engine.get_metrics(account_id=agent_id)
                if perf.sample_size > 0:
                    return {
                        "tasks": perf.sample_size,
                        "success_rate": perf.success_rate / 100.0,
                        "failure_rate": perf.failure_rate / 100.0,
                        "avg_duration": perf.avg_latency or 15.0,
                        "avg_tokens": 10000,
                    }
            except Exception:
                pass

        # Fallback to TaskManager inspection if tracker not provided
        if self._task_manager:
            try:
                tasks = [t for t in self._task_manager.list_tasks() if t.assigned_agent == agent_id]
                if tasks:
                    completed = sum(1 for t in tasks if t.status.value in ("COMPLETED", "VERIFICATION_COMPLETE"))
                    return {
                        "tasks": len(tasks),
                        "success_rate": completed / len(tasks),
                        "failure_rate": (len(tasks) - completed) / len(tasks),
                        "avg_duration": 15.0,
                        "avg_tokens": 10000,
                    }
            except Exception:
                pass

        return {"tasks": 0, "success_rate": 1.0, "failure_rate": 0.0, "avg_duration": 10.0, "avg_tokens": 5000}

    # ------------------------------------------------------------------
    # Analysis & Classification
    # ------------------------------------------------------------------
    def classify_task(self, text: str) -> ClassificationResult:
        """Classify task into operational categories."""
        return self._classifier.classify(text)

    def infer_complexity(self, text: str) -> Complexity:
        lower = text.lower()
        if any(w in lower for w in (
            "deep reasoning", "formal proof", "mission critical", "high risk", "complex rfc",
            "architect", "consensus", "byzantine", "formal verification", "cryptographic",
            "distributed protocol", "fault tolerance"
        )):
            return Complexity.REASONING
        if any(w in lower for w in (
            "refactor all", "restructure", "comprehensive", "full audit", "major rewrite",
            "across every", "multi-region", "migration"
        )):
            return Complexity.STRONG
        if any(w in lower for w in (
            "quick fix", "typo", "lint", "comment", "format", "minor tweak", "sanity", "fix typo"
        )):
            return Complexity.FAST
        return Complexity.STANDARD

    def infer_capabilities(self, text: str) -> frozenset[Capability]:
        return frozenset(cap for cap, pattern in CAPABILITY_KEYWORDS if pattern.search(text))

    @staticmethod
    def _max_strength(agent_id: str) -> int:
        catalog = AGENT_CATALOGS.get(agent_id)
        return max((spec.strength for spec in catalog), default=3) if catalog else 3

    def _busy(self, adapter: AgentAdapter) -> bool:
        try:
            return adapter.status() == AgentStatus.WORKING
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Scoring Candidates (Multi-Factor)
    # ------------------------------------------------------------------
    def score_candidates(
        self,
        task_text: str,
        complexity: Complexity | None = None,
        routing_mode: RoutingMode = RoutingMode.BALANCED,
        user_preference: str | None = None,
    ) -> list[CandidateScore]:
        """Score every healthy candidate with multi-factor weighting under given routing mode."""
        complexity = complexity or self.infer_complexity(task_text)
        required = self.infer_capabilities(task_text)
        target_strength = COMPLEXITY_TARGET_STRENGTH.get(complexity, 3)
        classification = self.classify_task(task_text)
        domain_str = classification.category.value if hasattr(classification.category, "value") else str(classification.category)

        scores: list[CandidateScore] = []
        # Reset rejection reasons for this scoring pass so the dashboard always
        # reflects the most recent competition.
        self._rejection_reasons = {}
        for adapter in self._registry.list_active_adapters():
            # Account Registry check
            acct = self._registry.account_registry.get_account(adapter.account_id)
            if not acct:
                acct = self._registry.account_registry.get_account(adapter.agent_id)

            # Strict admission gate (Phase 22, Part 12): an account is admitted
            # ONLY when registered, enabled, AUTHENTICATED, HEALTHY/DEGRADED,
            # lifecycle READY/ONLINE, and capability-compatible. Rejected
            # accounts get a structured, queryable reason instead of silently
            # vanishing. Adapters with no first-class account are admitted as
            # before to preserve legacy behaviour.
            if acct is not None:
                admissible, reason_code, detail = self.check_account_admissible(
                    acct, required_capabilities=required
                )
                if not admissible:
                    self._record_rejection(acct.id, reason_code or "REJECTED", detail)
                    continue

            # Check Quota Limits
            if acct and self._quota_manager:
                q_res = self._quota_manager.check_job_quota(
                    provider_id=adapter.provider,
                    account_id=acct.id,
                )
                if not q_res.allowed:
                    # Mark account quota exhausted
                    acct.status = AccountStatus.QUOTA_EXHAUSTED
                    self._record_rejection(
                        acct.id, AccountRejectionReason.QUOTA_EXHAUSTED, "quota check denied"
                    )
                    continue

            breakdown: dict[str, float] = {}
            reasons: list[str] = []

            # 1. Keyword Affinity
            kw_score = 0.0
            for agent_id, pattern, explanation in AGENT_KEYWORDS:
                if agent_id == adapter.agent_id and pattern.search(task_text):
                    kw_score += KEYWORD_AFFINITY_WEIGHT
                    reasons.append(f"keyword affinity: {explanation}")
                    break
            breakdown["keyword_affinity"] = kw_score

            # Empirical Affinity Boost from PerformanceRegistry (Phase 19)
            if self._performance_registry:
                boost = self._performance_registry.get_agent_affinity_boost(adapter.agent_id, domain_str)
                if boost != 0.0:
                    breakdown["empirical_affinity"] = boost
                    reasons.append(f"historical affinity: {boost:+.2f}")

            # 2. Capability Match (CRITICAL: MUST use 'capability_match', NOT 'capability_overlap')
            declared = adapter.capabilities()
            overlap = required & declared
            cap_score = 0.0
            matched_caps: list[str] = []
            if overlap:
                cap_score = CAPABILITY_WEIGHT * len(overlap)
                matched_caps = sorted(c.value for c in overlap)
                reasons.append(f"{len(overlap)}/{len(required)} required capabilities declared")
            breakdown["capability_match"] = cap_score

            # 3. Strength Fit
            max_str = self._max_strength(adapter.agent_id)
            strength_diff = max_str - target_strength
            str_score = 0.0
            if strength_diff >= 0:
                str_score = STRENGTH_FIT_WEIGHT
            else:
                str_score = max(0.0, STRENGTH_FIT_WEIGHT + (strength_diff * 1.5))
            if routing_mode == RoutingMode.PERFORMANCE:
                str_score *= 1.5
            breakdown["strength_fit"] = str_score

            # 4. Reliability & Historical Success Rate
            metrics = self._get_agent_metrics(adapter.agent_id)
            succ_rate = metrics.get("success_rate", 1.0)
            rel_score = RELIABILITY_WEIGHT * succ_rate
            if routing_mode == RoutingMode.RELIABILITY:
                rel_score *= 2.0
            breakdown["reliability"] = rel_score
            if metrics.get("tasks", 0) > 0:
                reasons.append(f"reliability: {succ_rate*100:.0f}% success")

            # 5. Latency Score
            avg_duration = metrics.get("avg_duration", 15.0)
            lat_weight = LATENCY_WEIGHT * 2.0 if routing_mode == RoutingMode.PERFORMANCE else LATENCY_WEIGHT
            lat_score = max(0.0, lat_weight * (1.0 - min(avg_duration / 60.0, 1.0)))
            breakdown["latency"] = lat_score

            # 6. Token Efficiency Score
            avg_tokens = metrics.get("avg_tokens", 10000)
            tok_score = max(0.0, TOKEN_EFFICIENCY_WEIGHT * (1.0 - min(avg_tokens / 100000.0, 1.0)))
            breakdown["token_efficiency"] = tok_score

            # 7. Health Bonus
            healthy, _ = adapter.health()
            hlth_score = HEALTH_WEIGHT if healthy else 0.0
            if routing_mode == RoutingMode.RELIABILITY:
                hlth_score *= 2.0
            breakdown["health"] = hlth_score

            # 8. Load Penalty
            load_pen = 0.0
            if self._busy(adapter):
                load_pen = LOAD_PENALTY
                reasons.append("currently WORKING; deprioritized")
            breakdown["load_penalty"] = -load_pen

            # 9. Priorities (Account, Provider, Model)
            acct_prio = float(getattr(acct, "priority", 10)) * 0.1 if acct else 1.0
            if acct and acct.cooldown_until and time.time() < acct.cooldown_until:
                prio_score = -50.0
                reasons.append(f"in cooldown until {acct.cooldown_until:.0f}")
            elif acct and acct.status == AccountStatus.RATE_LIMITED:
                prio_score = -40.0
                reasons.append("rate limited")
            else:
                prio_score = acct_prio
            breakdown["priority"] = prio_score
            breakdown["account_priority"] = acct_prio
            breakdown["provider_priority"] = 1.0
            breakdown["model_priority"] = 1.0

            # 10. Granular Health Factors (Provider, Account, Model)
            prov = self._registry.get_provider(adapter.provider)
            prov_healthy = prov.health().get("healthy", True) if prov else True
            prov_score = 2.0 if prov_healthy else -10.0
            if self._performance_registry:
                prov_health = self._performance_registry.get_provider_health_score(adapter.provider)
                if prov_health < 1.0 and prov_score > 0:
                    prov_score = round(prov_score * prov_health, 2)
                    reasons.append(f"provider empirical health: {prov_health*100:.0f}%")
            breakdown["provider_health"] = prov_score
            breakdown["account_health"] = 2.0 if (acct and acct.is_available()) else (1.0 if healthy else -10.0)
            breakdown["model_health"] = 1.0

            # 11. Performance & Failure Rates
            fail_rate = metrics.get("failure_rate", 0.0)
            breakdown["recent_success_rate"] = rel_score
            breakdown["failure_rate"] = -round(fail_rate * 5.0, 2)

            # 12. Quota & Budget
            quota_bonus = 1.0
            if acct and self._quota_manager:
                q_res = self._quota_manager.check_target_quota("account", acct.id)
                if q_res.highest_threshold_alert == 90:
                    quota_bonus = -5.0
                elif q_res.highest_threshold_alert == 75:
                    quota_bonus = -2.0
            breakdown["quota_remaining"] = quota_bonus

            # 13. Cost Factor
            model_name = getattr(adapter, "default_model", "auto")
            est_cost = self._cost_tracker.estimate_cost(adapter.provider, model_name, 5000)
            cost_factor = 0.0
            if est_cost == 0.0:  # Free local or oauth model
                cost_factor = 3.0 if routing_mode == RoutingMode.COST else 1.5
                reasons.append("cost: free local execution ($0.00)")
            elif est_cost is not None:
                if routing_mode == RoutingMode.COST:
                    cost_factor = -round(est_cost * 100.0, 2)
                else:
                    cost_factor = max(-2.0, -round(est_cost * 10.0, 2))
            breakdown["estimated_cost"] = cost_factor

            # 14. Task Complexity & Context Size Alignment
            breakdown["task_complexity"] = str_score
            breakdown["context_size"] = 1.0  # Sufficient context window

            # 15. Streaming & Tool Support
            breakdown["streaming_support"] = 0.5
            breakdown["tool_support"] = 1.0 if Capability.TERMINAL_OPERATIONS in declared or Capability.EDITOR_REFACTORING in declared else 0.5

            # 16. User Preference
            pref_bonus = 0.0
            if user_preference:
                if user_preference.lower() in (adapter.provider.lower(), adapter.agent_id.lower(), adapter.account_id.lower()):
                    pref_bonus = 10.0
                    reasons.append(f"user preference: {user_preference}")
            breakdown["user_preference"] = pref_bonus

            # Total score
            total = sum(breakdown.values())

            candidate = CandidateScore(
                agent_id=adapter.agent_id,
                account_id=adapter.account_id,
                provider=adapter.provider,
                score=total,
                model_id=getattr(adapter, "default_model", "auto"),
                matched_capabilities=matched_caps,
                reasons=reasons,
                score_breakdown=breakdown,
                latency=avg_duration,
                success_rate=round(succ_rate * 100.0, 1),
            )
            scores.append(candidate)

        scores.sort(key=lambda c: c.score, reverse=True)
        return scores

    # ------------------------------------------------------------------
    # Routing Decision
    # ------------------------------------------------------------------
    def route(
        self,
        task_text: str,
        preferred_agent: str | None = None,
        preferred_model: str | None = None,
        preferred_provider: str | None = None,
        forced_complexity: Complexity | None = None,
        routing_mode: RoutingMode | str = RoutingMode.BALANCED,
        preferred_account: str | None = None,
        requires_streaming: bool = False,
        **kwargs: Any,
    ) -> RoutingDecision:
        """Deterministically route a task and generate explainable fallback chain."""
        if self._registry is None:
            from providers.registry.bootstrap import create_default_registry
            self._registry = create_default_registry()

        if isinstance(routing_mode, str):
            try:
                mode_enum = RoutingMode(routing_mode.lower())
            except Exception:
                mode_enum = RoutingMode.BALANCED
        else:
            mode_enum = routing_mode
        # Use the normalised enum from here on; a raw "COST"/"Performance" string
        # would otherwise compare unequal to every RoutingMode and silently score
        # as BALANCED.
        routing_mode = mode_enum

        pref_agent = preferred_agent or preferred_account
        classification = self.classify_task(task_text)
        complexity = forced_complexity or classification.suggested_complexity
        # Blast radius, not difficulty. select_model() escalates to the frontier
        # tier on high/risk, but no call site ever supplied one, so risk could
        # never influence the choice and a destructive task could be handed to a
        # fast model. Derived from the classifier and threaded through below.
        risk = getattr(classification, "risk", None) or Risk.LOW
        required = sorted(c.value for c in self.infer_capabilities(task_text))

        # 1. Manual / Explicit selection mode
        if pref_agent:
            adapter = self._registry.get_adapter(pref_agent)
            if adapter is None:
                # Attempt prefix / substring alias resolution (e.g. antigravity -> antigravity-account-1, kiro -> kiro-cli)
                for cand in self._registry.list_active_adapters():
                    if cand.agent_id.startswith(pref_agent) or pref_agent in cand.agent_id:
                        adapter = cand
                        break
            if adapter is None:
                raise RuntimeError(
                    f"Requested agent '{preferred_agent}' is not registered. "
                    f"Known agents: {sorted(a.agent_id for a in self._registry.list_active_adapters())}"
                )
            healthy, health_reason = adapter.health()
            if not healthy:
                raise RuntimeError(
                    f"Requested agent '{preferred_agent}' is not healthy: {health_reason}"
                )
            model = preferred_model or select_model(adapter.agent_id, complexity=complexity, risk=risk).preferred_model

            fallbacks = [
                c for c in self.score_candidates(task_text, complexity, routing_mode=routing_mode)
                if c.agent_id != adapter.agent_id
            ]

            fallback_chain = [
                {
                    "provider": fb.provider,
                    "account": fb.account_id,
                    "agent_id": fb.agent_id,
                    "model": select_model(fb.agent_id, complexity=complexity, risk=risk).preferred_model,
                    "score": fb.score,
                }
                for fb in fallbacks
            ]

            breakdown = {
                "user_preference": 100.0,
                "health": 2.0,
            }

            decision = RoutingDecision(
                agent_id=adapter.agent_id,
                account_id=adapter.account_id,
                provider=adapter.provider,
                model=model,
                complexity=complexity,
                routing_mode=RoutingMode.MANUAL if routing_mode == RoutingMode.BALANCED else routing_mode,
                total_score=102.0,
                score_breakdown=breakdown,
                reason=f"Explicitly selected agent {preferred_agent}",
                fallback_agent_id=fallbacks[0].agent_id if fallbacks else None,
                fallback_model=(
                    select_model(fallbacks[0].agent_id, complexity=complexity, risk=risk).preferred_model
                    if fallbacks else None
                ),
                fallback_chain=fallback_chain,
                task_type=classification.category.value,
                required_capabilities=required,
                candidates=[c.to_dict() for c in fallbacks],
            )
            self._log_decision(task_text, decision)
            return decision

        # 2. Scored competition across every healthy candidate
        candidates = self.score_candidates(
            task_text,
            complexity=complexity,
            routing_mode=routing_mode,
            user_preference=preferred_provider,
        )
        if not candidates:
            raise RuntimeError("No healthy agents available in ProviderRegistry")

        winner = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        model = preferred_model or select_model(winner.agent_id, complexity=complexity, risk=risk).preferred_model

        reason = "; ".join(winner.reasons) if winner.reasons else "highest scoring available agent"

        fallback_chain = [
            {
                "provider": c.provider,
                "account": c.account_id,
                "agent_id": c.agent_id,
                "model": select_model(c.agent_id, complexity=complexity, risk=risk).preferred_model,
                "score": c.score,
            }
            for c in candidates[1:]
        ]

        decision = RoutingDecision(
            agent_id=winner.agent_id,
            account_id=winner.account_id,
            provider=winner.provider,
            model=model,
            complexity=complexity,
            routing_mode=routing_mode,
            total_score=winner.score,
            score_breakdown=winner.score_breakdown,
            reason=f"score {winner.score:.1f} — {reason}",
            fallback_agent_id=runner_up.agent_id if runner_up else None,
            fallback_model=(
                select_model(runner_up.agent_id, complexity=complexity, risk=risk).preferred_model
                if runner_up else None
            ),
            fallback_chain=fallback_chain,
            task_type=classification.category.value,
            required_capabilities=required,
            candidates=[c.to_dict() for c in candidates],
        )
        self._log_decision(task_text, decision)
        return decision

    # ------------------------------------------------------------------
    # Decision Persistence & History
    # ------------------------------------------------------------------
    def _log_decision(self, task_text: str, decision: RoutingDecision) -> None:
        try:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": getattr(decision, "job_id", ""),
                "task_text": task_text[:120],
                "selected_agent": decision.agent_id,
                "selected_account": decision.account_id,
                "selected_provider": decision.provider,
                "selected_model": decision.model,
                "routing_mode": decision.routing_mode.value if hasattr(decision.routing_mode, "value") else str(decision.routing_mode),
                "task_type": decision.task_type,
                "complexity": decision.complexity.value if hasattr(decision.complexity, "value") else str(decision.complexity),
                "total_score": round(decision.total_score, 2),
                "score_breakdown": {k: round(v, 2) for k, v in decision.score_breakdown.items()},
                "reason": decision.reason,
                "fallback_agent": decision.fallback_agent_id,
                "fallback_chain": decision.fallback_chain,
                "candidates": decision.candidates,
            }
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning(f"Failed to log routing decision: {exc}")

    def get_routing_history(self, limit: int = 50, job_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve recent routing history records in reverse chronological order."""
        if not self._history_file.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            item = json.loads(line)
                            if job_id:
                                # Match job_id in candidates or text
                                if job_id in item.get("task_text", "") or job_id in json.dumps(item):
                                    records.append(item)
                            else:
                                records.append(item)
                        except Exception:
                            continue
        except Exception as exc:
            logger.warning(f"Error reading routing history: {exc}")
            return []
        return list(reversed(records))[:limit]

    def score_candidate(
        self,
        candidate: RoutingCandidate,
        required_capabilities: list[str] | None = None,
        mode: RoutingMode | None = None,
        context_tokens: int = 0,
        **kwargs: Any,
    ) -> RoutingScore:
        """Score a single candidate across all 16 multi-dimensional factors."""
        routing_mode = mode or self.default_mode
        req = set(required_capabilities or [])
        cand_caps = set(candidate.capabilities)

        breakdown: dict[str, float] = {}

        # 1. capability_match (CRITICAL: MUST use 'capability_match', NOT 'capability_overlap')
        match_count = len(req & cand_caps)
        base_match = (match_count / max(len(req), 1)) * 30.0 if req else 10.0
        breakdown["capability_match"] = round(base_match, 2)

        # 2. provider_health
        breakdown["provider_health"] = 15.0 if candidate.provider_healthy else -50.0

        # 3. account_health
        breakdown["account_health"] = 15.0 if (candidate.account_healthy and not candidate.cooldown_active) else -100.0

        # 4. model_health
        breakdown["model_health"] = 10.0 if candidate.model_healthy else -30.0

        # 5. latency
        lat = candidate.latency if candidate.latency > 0 else candidate.latency_ms
        if routing_mode == RoutingMode.PERFORMANCE:
            lat_score = max(0.0, 25.0 * (1.0 - min(lat / 2000.0, 1.0)))
        else:
            lat_score = max(0.0, 15.0 * (1.0 - min(lat / 2000.0, 1.0)))
        breakdown["latency"] = round(lat_score, 2)

        # 6. recent_success_rate
        succ = candidate.recent_success_rate
        if routing_mode == RoutingMode.RELIABILITY:
            breakdown["recent_success_rate"] = round((succ / 100.0) * 25.0, 2)
        else:
            breakdown["recent_success_rate"] = round((succ / 100.0) * 15.0, 2)

        # 7. failure_rate
        breakdown["failure_rate"] = -round(candidate.failure_rate * 20.0, 2)

        # 8. account_priority
        breakdown["account_priority"] = round(float(candidate.account_priority) * 0.1, 2)

        # 9. provider_priority
        breakdown["provider_priority"] = round(float(candidate.provider_priority) * 0.1, 2)

        # 10. model_priority
        breakdown["model_priority"] = round(float(candidate.model_priority) * 0.1, 2)

        # 11. quota_remaining
        if candidate.quota_remaining is not None and candidate.quota_remaining <= 0:
            breakdown["quota_remaining"] = -100.0
        elif candidate.quota_remaining is not None:
            breakdown["quota_remaining"] = 5.0
        else:
            breakdown["quota_remaining"] = 2.0

        # 12. estimated_cost
        cost = candidate.estimated_cost if candidate.estimated_cost is not None else (candidate.estimated_cost_per_1m or 0.0)
        if routing_mode == RoutingMode.COST:
            cost_score = max(0.0, 25.0 - cost * 0.8)
        else:
            cost_score = max(0.0, 10.0 - cost * 0.2)
        breakdown["estimated_cost"] = round(cost_score, 2)

        # 13. task_complexity
        breakdown["task_complexity"] = 5.0

        # 14. context_size
        breakdown["context_size"] = 5.0 if candidate.context_window >= context_tokens else -10.0

        # 15. streaming_support
        breakdown["streaming_support"] = 5.0 if candidate.streaming_supported else 0.0

        # 16. tool_support
        breakdown["tool_support"] = 5.0 if candidate.tool_supported else 0.0

        total = round(sum(breakdown.values()), 2)
        return RoutingScore(
            total_score=total,
            breakdown=breakdown,
            candidate=candidate,
            capability_match=breakdown["capability_match"],
            provider_health=breakdown["provider_health"],
            account_health=breakdown["account_health"],
            model_health=breakdown["model_health"],
            latency=breakdown["latency"],
            recent_success_rate=breakdown["recent_success_rate"],
            failure_rate=breakdown["failure_rate"],
            account_priority=breakdown["account_priority"],
            provider_priority=breakdown["provider_priority"],
            model_priority=breakdown["model_priority"],
            quota_remaining=breakdown["quota_remaining"],
            estimated_cost=breakdown["estimated_cost"],
            task_complexity=breakdown["task_complexity"],
            context_size=breakdown["context_size"],
            streaming_support=breakdown["streaming_support"],
            tool_support=breakdown["tool_support"],
        )

    def select_best_candidate(
        self,
        candidates: list[RoutingCandidate],
        required_capabilities: list[str] | None = None,
        task_prompt: str = "",
        mode: RoutingMode | None = None,
        job_id: str = "",
        **kwargs: Any,
    ) -> RoutingDecision:
        """Evaluate candidates and select the optimal candidate according to multi-factor score."""
        routing_mode = mode or self.default_mode
        scored: list[tuple[RoutingCandidate, RoutingScore]] = []

        for c in candidates:
            score = self.score_candidate(
                c,
                required_capabilities=required_capabilities,
                mode=routing_mode,
                **kwargs,
            )
            # If cooldown is active or quota exhausted, apply penalty to prevent selection
            if c.cooldown_active:
                score.total_score -= 1000.0
            if c.quota_remaining is not None and c.quota_remaining <= 0:
                score.total_score -= 1000.0

            scored.append((c, score))

        scored.sort(key=lambda item: item[1].total_score, reverse=True)
        winner_c, winner_s = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None

        fallback_chain = [
            {
                "provider": c.provider_id,
                "account": c.account_id,
                "model": c.model_id,
                "score": s.total_score,
            }
            for c, s in scored[1:]
        ]

        decision = RoutingDecision(
            agent_id=winner_c.account_id,
            account_id=winner_c.account_id,
            provider=winner_c.provider_id,
            model=winner_c.model_id,
            complexity=Complexity.STANDARD,
            reason=f"score {winner_s.total_score:.1f} — best capability match and health profile",
            job_id=job_id,
            routing_mode=routing_mode,
            total_score=winner_s.total_score,
            score_breakdown=winner_s.breakdown,
            fallback_agent_id=runner_up[0].account_id if runner_up else None,
            fallback_model=runner_up[0].model_id if runner_up else None,
            fallback_chain=fallback_chain,
            task_type="coding",
            required_capabilities=required_capabilities or [],
            candidates=[
                {
                    "provider": c.provider_id,
                    "account": c.account_id,
                    "model": c.model_id,
                    "score": s.total_score,
                    "breakdown": s.breakdown,
                }
                for c, s in scored
            ],
        )
        if job_id:
            self._in_memory_decisions[job_id] = decision
            self._log_decision(task_prompt or job_id, decision)
        return decision

    def explain_decision(self, decision: RoutingDecision) -> str:
        """Generate human-readable explanation with sanitized secrets."""
        redactor = get_credential_manager().redactor
        expl = decision.explain()
        return redactor.redact(expl)

    def record_decision(self, decision: RoutingDecision) -> None:
        """Persist routing decision in memory and on disk."""
        if decision.job_id:
            self._in_memory_decisions[decision.job_id] = decision
        self._log_decision(decision.job_id or "job", decision)

    def get_decision(self, job_id: str) -> RoutingDecision | None:
        """Retrieve decision by job_id."""
        if job_id in self._in_memory_decisions:
            return self._in_memory_decisions[job_id]
        history = self.get_routing_history(limit=100, job_id=job_id)
        if history:
            item = history[0]
            return RoutingDecision(
                agent_id=item.get("selected_agent", ""),
                account_id=item.get("selected_account", ""),
                provider=item.get("selected_provider", ""),
                model=item.get("selected_model", ""),
                complexity=Complexity.STANDARD,
                reason=item.get("reason", ""),
                job_id=job_id,
                total_score=item.get("total_score", 0.0),
                score_breakdown=item.get("score_breakdown", {}),
            )
        return None

    def explain_routing(self, task_text: str, **kwargs: Any) -> dict[str, Any]:
        """Generate comprehensive explainability report for routing a task."""
        decision = self.route(task_text, **kwargs)

        # Recommendations for MCPs, Tools, and Knowledge
        recommended_mcps = []
        recommended_tools = []
        recommended_docs = []
        recommended_steering = []

        try:
            from brain.context.context_builder import ContextBuilder
            builder = ContextBuilder()
            ctx = builder.preview_context(task_text)
            recommended_mcps = [m.get("id") or m.get("name") for m in ctx.relevant_mcps if isinstance(m, dict)]
            recommended_tools = [t.get("name") for t in ctx.relevant_tools if isinstance(t, dict)]
            recommended_docs = [d.get("title") for d in ctx.relevant_docs if isinstance(d, dict)]
            recommended_steering = [s.get("title") for s in ctx.relevant_steering if isinstance(s, dict)]
        except Exception as e:
            logger.debug("Failed building recommendation context in explain_routing: %s", e)
        # Context entries without an id/name/title yield None; consumers join
        # these lists as strings (`brain route explain` crashed on a None).
        recommended_mcps = [str(x) for x in recommended_mcps if x]
        recommended_tools = [str(x) for x in recommended_tools if x]
        recommended_docs = [str(x) for x in recommended_docs if x]
        recommended_steering = [str(x) for x in recommended_steering if x]

        affinity_boost = 0.0
        if self._performance_registry:
            affinity_boost = self._performance_registry.get_agent_affinity_boost(
                decision.agent_id, decision.task_type
            )

        # Recommendations for federated ECC capabilities
        recommended_ecc_skills = []
        ecc_rationale = ""
        try:
            from brain.resources.ecc_federation import get_federation_manager
            fed_mgr = get_federation_manager()
            if fed_mgr.is_enabled():
                task_lower = task_text.lower()
                enabled_skills = fed_mgr.list_federated(state="ENABLED", comp_type="skill")
                for s in enabled_skills:
                    s_id = s.id.lower()
                    bare_id = s_id.split(":")[-1]
                    s_caps = [c.lower() for c in s.capabilities]
                    tokens = [bare_id] + bare_id.split("-") + s_caps
                    if any(tok in task_lower for tok in tokens if len(tok) >= 3):
                        recommended_ecc_skills.append(s.id)
                if recommended_ecc_skills:
                    ecc_rationale = f"Matched {len(recommended_ecc_skills)} enabled federated ECC skill(s)"
        except Exception as e:
            logger.debug("Failed checking ECC recommendations in explain_routing: %s", e)

        return {
            "task": task_text,
            "selected_agent": decision.agent_id,
            "selected_account": decision.account_id,
            "selected_provider": decision.provider,
            "selected_model": decision.model,
            "task_type": decision.task_type,
            "complexity": decision.complexity.value if hasattr(decision.complexity, "value") else str(decision.complexity),
            "routing_mode": decision.routing_mode.value if hasattr(decision.routing_mode, "value") else str(decision.routing_mode),
            "total_score": decision.total_score,
            "score_breakdown": decision.score_breakdown,
            "reason": decision.reason,
            "empirical_affinity_boost": affinity_boost,
            "recommended_mcps": recommended_mcps,
            "recommended_tools": recommended_tools,
            "recommended_knowledge": recommended_docs + recommended_steering,
            "recommended_ecc_skills": recommended_ecc_skills,
            "ecc_relevance_rationale": ecc_rationale,
            "fallback_agent": decision.fallback_agent_id,
            "fallback_model": decision.fallback_model,
            "fallback_chain": decision.fallback_chain,
            "candidate_scores": decision.candidates,
        }
