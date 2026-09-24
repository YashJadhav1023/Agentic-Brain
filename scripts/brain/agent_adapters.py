"""Closed, side-effect-free agent capability and eligibility registry.

This module describes the known local agent adapters.  It deliberately does not
start agents, create tasks, execute commands, or query external services.
Availability is limited to locating an explicitly declared executable on PATH.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from shutil import which
from typing import Callable

try:  # Package import for tests and dashboard callers.
    from .model_policy import Action, Risk
except ImportError:  # Direct script execution from this directory.
    from model_policy import Action, Risk


class AgentName(str, Enum):
    """The only adapters recognized by the registry."""

    KIRO_CLI = "kiro-cli"
    ANTIGRAVITY = "antigravity"
    # The second Antigravity account, authenticated by a Gemini API key. Same
    # executable, isolated data dir, Gemini-only models, its own quota. It is a
    # real headless worker so it can run *concurrently* with ANTIGRAVITY rather
    # than only serving as its fallback.
    ANTIGRAVITY_API = "antigravity-api"
    CLINE = "cline"
    ANTIGRAVITY_IDE = "antigravity-ide"


class Capability(str, Enum):
    """Explicit, provider-neutral capabilities used for planning and routing."""

    TERMINAL_OPERATIONS = "terminal-operations"
    LOCAL_VALIDATION = "local-validation"
    BUILD_AND_TEST = "build-and-test"
    CLOUD_READ_ONLY = "cloud-read-only"
    KUBERNETES_READ_ONLY = "kubernetes-read-only"
    ARCHITECTURE = "architecture"
    PROTOCOL_DESIGN = "protocol-design"
    GOVERNANCE = "governance"
    DEEP_REASONING = "deep-reasoning"
    EDITOR_REFACTORING = "editor-refactoring"
    FRONTEND_STYLING = "frontend-styling"
    COMPONENT_REFACTORING = "component-refactoring"
    CODE_REVIEW = "code-review"
    CODE_COMPLETION = "code-completion"
    DOCUMENTATION = "documentation"
    TEST_SCAFFOLDING = "test-scaffolding"


class ExecutionMode(str, Enum):
    """How an adapter can actually be reached on this machine.

    ``HEADLESS`` adapters expose a verified command-line entrypoint. ``DELIVERY``
    adapters live inside an editor chat session and can only be handed a durable
    task record that the agent itself must acknowledge.
    """

    HEADLESS = "headless"
    DELIVERY = "delivery"


@dataclass(frozen=True)
class ActionRisk:
    """One explicitly allow-listed action and risk combination."""

    action: Action
    risk: Risk


@dataclass(frozen=True)
class AgentCapability:
    """Immutable declaration for one local agent adapter.

    ``executable`` is metadata for a passive availability probe only.  It is
    never executed by this module.  ``execution_mode`` records whether the
    adapter can be reached headlessly at all; delivery adapters are routable but
    must never be described as executing on their own.
    """

    name: AgentName
    executable: str
    capabilities: frozenset[Capability]
    allowed_action_risks: frozenset[ActionRisk]
    execution_mode: ExecutionMode = ExecutionMode.HEADLESS


@dataclass(frozen=True)
class Availability:
    """Result of a passive executable-discovery probe."""

    agent: AgentName | None
    available: bool
    executable: str | None
    path: str | None
    reason: str


@dataclass(frozen=True)
class Eligibility:
    """Auditable decision for one requested agent/action/risk combination."""

    agent: AgentName | None
    action: Action | None
    risk: Risk | None
    eligible: bool
    reason: str
    availability: Availability


def _pairs(*actions: Action) -> frozenset[ActionRisk]:
    """Allow low and medium risk for the supplied plan-only actions."""

    return frozenset(
        ActionRisk(action, risk)
        for action in actions
        for risk in (Risk.LOW, Risk.MEDIUM)
    )


# This tuple is the complete, closed adapter catalog.  Mutation and critical
# risk are absent intentionally: a future approval-gated executor needs a
# separate policy and may not infer permission from this planning registry.
AGENT_CAPABILITIES: tuple[AgentCapability, ...] = (
    AgentCapability(
        name=AgentName.KIRO_CLI,
        executable="kiro-cli",
        capabilities=frozenset(
            {
                Capability.TERMINAL_OPERATIONS,
                Capability.LOCAL_VALIDATION,
                Capability.BUILD_AND_TEST,
                Capability.CLOUD_READ_ONLY,
                Capability.KUBERNETES_READ_ONLY,
            }
        ),
        allowed_action_risks=_pairs(Action.PLAN, Action.ANALYZE, Action.IMPLEMENT),
    ),
    AgentCapability(
        name=AgentName.ANTIGRAVITY,
        executable="antigravity",
        capabilities=frozenset(
            {
                Capability.ARCHITECTURE,
                Capability.PROTOCOL_DESIGN,
                Capability.GOVERNANCE,
                Capability.DEEP_REASONING,
            }
        ),
        allowed_action_risks=frozenset(
            {
                *_pairs(Action.PLAN, Action.ANALYZE),
                ActionRisk(Action.PLAN, Risk.HIGH),
                ActionRisk(Action.ANALYZE, Risk.HIGH),
            }
        ),
    ),
    AgentCapability(
        name=AgentName.ANTIGRAVITY_API,
        executable="antigravity",
        capabilities=frozenset(
            {
                Capability.ARCHITECTURE,
                Capability.PROTOCOL_DESIGN,
                Capability.DEEP_REASONING,
            }
        ),
        allowed_action_risks=frozenset(
            {
                *_pairs(Action.PLAN, Action.ANALYZE),
                ActionRisk(Action.PLAN, Risk.HIGH),
                ActionRisk(Action.ANALYZE, Risk.HIGH),
            }
        ),
    ),
    AgentCapability(
        name=AgentName.CLINE,
        executable="cline",
        capabilities=frozenset(
            {
                Capability.EDITOR_REFACTORING,
                Capability.FRONTEND_STYLING,
                Capability.COMPONENT_REFACTORING,
                Capability.CODE_REVIEW,
            }
        ),
        allowed_action_risks=_pairs(Action.PLAN, Action.ANALYZE, Action.IMPLEMENT),
    ),
    # Antigravity IDE reaches the shared brain through its own MCP client. Its
    # binary is an editor launcher with no headless prompt mode, so it is a
    # delivery adapter: tasks are handed to it and it acknowledges them.
    AgentCapability(
        name=AgentName.ANTIGRAVITY_IDE,
        executable="antigravity-ide",
        capabilities=frozenset(
            {
                Capability.EDITOR_REFACTORING,
                Capability.COMPONENT_REFACTORING,
                Capability.ARCHITECTURE,
                Capability.DEEP_REASONING,
            }
        ),
        allowed_action_risks=_pairs(Action.PLAN, Action.ANALYZE, Action.IMPLEMENT),
        execution_mode=ExecutionMode.DELIVERY,
    ),
)

_REGISTRY: dict[AgentName, AgentCapability] = {
    record.name: record for record in AGENT_CAPABILITIES
}
PathLookup = Callable[[str], str | None]


def _coerce(enum_type: type[AgentName] | type[Action] | type[Risk], value: object):
    """Convert enum values and their strings without raising for caller input."""

    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            return None
    return None


def get_agent_capability(agent: AgentName | str) -> AgentCapability | None:
    """Return the declared record for a known agent; unknown names return ``None``."""

    name = _coerce(AgentName, agent)
    return _REGISTRY.get(name) if name is not None else None


def is_delivery_agent(agent: AgentName | str) -> bool:
    """Return whether an adapter is reachable only through a delivery lifecycle."""

    record = get_agent_capability(agent)
    return record is not None and record.execution_mode is ExecutionMode.DELIVERY


def probe_availability(
    agent: AgentName | str, *, path_lookup: PathLookup = which
) -> Availability:
    """Passively inspect PATH for an adapter executable without executing it.

    ``path_lookup`` is injectable for deterministic tests.  It must only be a
    lookup callable; no command execution API is used or accepted here.

    Delivery adapters are reported unavailable on purpose: they live inside an
    editor chat session, so a PATH hit would not be evidence that they can be
    invoked.  Route work to them through their durable delivery lifecycle.
    """

    record = get_agent_capability(agent)
    if record is None:
        return Availability(None, False, None, None, "unknown agent")
    if record.execution_mode is ExecutionMode.DELIVERY:
        return Availability(
            record.name,
            False,
            record.executable,
            None,
            "delivery adapter: reached through an editor chat session, so it has no headless entrypoint to probe",
        )
    path = path_lookup(record.executable)
    if path:
        return Availability(record.name, True, record.executable, path, "executable found on PATH")
    return Availability(record.name, False, record.executable, None, "executable not found on PATH")


def evaluate_eligibility(
    agent: AgentName | str,
    action: Action | str,
    risk: Risk | str,
    *,
    availability: Availability | None = None,
    path_lookup: PathLookup = which,
) -> Eligibility:
    """Reject unknown, unavailable, or non-allow-listed agent requests.

    The result is only a routing decision.  A positive result grants neither
    execution permission nor approval for a mutation.
    """

    name = _coerce(AgentName, agent)
    requested_action = _coerce(Action, action)
    requested_risk = _coerce(Risk, risk)
    record = _REGISTRY.get(name) if name is not None else None
    probe = availability if availability is not None else probe_availability(agent, path_lookup=path_lookup)

    if record is None:
        return Eligibility(None, requested_action, requested_risk, False, "unknown agent", probe)
    if probe.agent is not record.name:
        return Eligibility(record.name, requested_action, requested_risk, False, "availability result belongs to a different agent", probe)
    if not probe.available:
        return Eligibility(record.name, requested_action, requested_risk, False, "agent is unavailable", probe)
    if requested_action is None:
        return Eligibility(record.name, None, requested_risk, False, "unsupported action", probe)
    if requested_risk is None:
        return Eligibility(record.name, requested_action, None, False, "unsupported risk", probe)
    if ActionRisk(requested_action, requested_risk) not in record.allowed_action_risks:
        return Eligibility(record.name, requested_action, requested_risk, False, "action/risk combination is not allow-listed", probe)
    return Eligibility(record.name, requested_action, requested_risk, True, "agent is available and action/risk is allow-listed", probe)


def is_eligible(
    agent: AgentName | str,
    action: Action | str,
    risk: Risk | str,
    *,
    availability: Availability | None = None,
    path_lookup: PathLookup = which,
) -> bool:
    """Return only the eligibility boolean for callers that do not need rationale."""

    return evaluate_eligibility(
        agent,
        action,
        risk,
        availability=availability,
        path_lookup=path_lookup,
    ).eligible
