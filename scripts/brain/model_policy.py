"""Deterministic, plan-only model-selection policy.

This module deliberately performs no model discovery, provider calls, or task execution.
It maps a typed request to an auditable preferred model and ordered fallback chain
using only the locally verified model names declared in :data:`MODEL_CATALOG`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ModelName(str, Enum):
    """Locally verified Kiro CLI model identifiers.

    Every value here was read from ``kiro-cli chat --list-models``.  These ids are
    valid for Kiro only; Antigravity rejects them.  See :data:`ANTIGRAVITY_CATALOG`.
    """

    AUTO = "auto"
    GPT_5_6_TERRA = "gpt-5.6-terra"
    GPT_5_6_LUNA = "gpt-5.6-luna"
    GPT_5_6_SOL = "gpt-5.6-sol"
    CLAUDE_OPUS_5 = "claude-opus-5"
    CLAUDE_SONNET_5 = "claude-sonnet-5"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4.6"
    CLAUDE_SONNET_4_5 = "claude-sonnet-4.5"
    CLAUDE_SONNET_4 = "claude-sonnet-4"
    CLAUDE_HAIKU_4_5 = "claude-haiku-4.5"
    DEEPSEEK_3_2 = "deepseek-3.2"
    MINIMAX_M2_5 = "minimax-m2.5"
    MINIMAX_M2_1 = "minimax-m2.1"
    GLM_5 = "glm-5"
    QWEN3_CODER_NEXT = "qwen3-coder-next"


class AgentTarget(str, Enum):
    """Agents that a model decision can be resolved for.

    Model identifiers are namespaced per agent and are mutually invalid: passing
    a Kiro id to Antigravity (or the reverse) is rejected outright, so selection
    must always be resolved against one agent's catalog.

    KIRO_CLI, ANTIGRAVITY and CLINE all expose a headless CLI that accepts a
    per-invocation model flag, so a concrete model can be *selected* for them.
    ANTIGRAVITY_IDE is an editor launcher with no headless prompt mode, so its
    model is chosen in the IDE UI and it receives a *recommendation* instead.
    See :func:`recommend_for_delivery_agent`.
    """

    KIRO_CLI = "kiro-cli"
    ANTIGRAVITY = "antigravity"
    # A second Antigravity account authenticated by a Gemini API key. Requests go
    # straight to the Gemini API, so its catalog is Gemini-only and is restricted to
    # the ids proven to answer on a key.
    ANTIGRAVITY_API = "antigravity-api"
    CLINE = "cline"
    ANTIGRAVITY_IDE = "antigravity-ide"


# Agents whose model can actually be set by this process.
SELECTABLE_AGENTS = (AgentTarget.KIRO_CLI, AgentTarget.ANTIGRAVITY, AgentTarget.ANTIGRAVITY_API, AgentTarget.CLINE)
# Agents that can only be advised which model to switch to.
RECOMMEND_ONLY_AGENTS = (AgentTarget.ANTIGRAVITY_IDE,)


class Specialization(str, Enum):
    """What a model is comparatively best suited to."""

    GENERAL = "general"
    CODING = "coding"
    REASONING = "reasoning"


class ModelTier(str, Enum):
    """Policy tiers used to explain catalog intent, not provider capabilities."""

    AUTO = "auto"
    FRONTIER = "frontier"
    ADVANCED = "advanced"
    BALANCED = "balanced"
    FAST = "fast"
    CODING = "coding"


# Context window sizes that the CLI reports explicitly. A value of 0 means the
# window was not documented locally and must not be relied on for routing.
CONTEXT_1M = 1_000_000
CONTEXT_272K = 272_000
CONTEXT_UNKNOWN = 0


@dataclass(frozen=True)
class ModelSpec:
    """A verified model and the capability metadata used to route to it.

    ``strength`` is a 1-5 comparative capability rank. Where a provider publishes
    no capability ordering, the locally reported credit multiplier is used as the
    only available proxy for it. That is a proxy, not a measurement.

    ``context_tokens`` is only populated when the CLI states a window explicitly.
    ``supports_effort`` records whether the agent accepts ``--effort`` with this
    model; sending it to a model that rejects it is a hard error, not a warning.
    """

    name: str
    agent: AgentTarget
    tier: ModelTier
    strength: int
    context_tokens: int = CONTEXT_UNKNOWN
    specialization: Specialization = Specialization.GENERAL
    supports_effort: bool = False


# ---------------------------------------------------------------------------
# Kiro CLI catalog. Verified via `kiro-cli chat --list-models` on 2026-09-04.
# Context windows are those the CLI states explicitly.
# ---------------------------------------------------------------------------
KIRO_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("auto", AgentTarget.KIRO_CLI, ModelTier.AUTO, 3),
    ModelSpec("claude-opus-5", AgentTarget.KIRO_CLI, ModelTier.FRONTIER, 5, CONTEXT_1M, Specialization.REASONING),
    ModelSpec("gpt-5.6-sol", AgentTarget.KIRO_CLI, ModelTier.FRONTIER, 5, CONTEXT_272K, Specialization.REASONING),
    ModelSpec("claude-sonnet-5", AgentTarget.KIRO_CLI, ModelTier.ADVANCED, 4, CONTEXT_1M, Specialization.CODING),
    ModelSpec("claude-sonnet-4.6", AgentTarget.KIRO_CLI, ModelTier.ADVANCED, 4, CONTEXT_1M, Specialization.CODING),
    ModelSpec("gpt-5.6-terra", AgentTarget.KIRO_CLI, ModelTier.ADVANCED, 4, CONTEXT_272K),
    ModelSpec("claude-sonnet-4.5", AgentTarget.KIRO_CLI, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.CODING),
    ModelSpec("claude-sonnet-4", AgentTarget.KIRO_CLI, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.CODING),
    ModelSpec("qwen3-coder-next", AgentTarget.KIRO_CLI, ModelTier.CODING, 2, CONTEXT_UNKNOWN, Specialization.CODING),
    ModelSpec("claude-haiku-4.5", AgentTarget.KIRO_CLI, ModelTier.FAST, 2),
    ModelSpec("gpt-5.6-luna", AgentTarget.KIRO_CLI, ModelTier.FAST, 2, CONTEXT_272K),
    ModelSpec("glm-5", AgentTarget.KIRO_CLI, ModelTier.BALANCED, 2),
    ModelSpec("deepseek-3.2", AgentTarget.KIRO_CLI, ModelTier.BALANCED, 2),
    ModelSpec("minimax-m2.5", AgentTarget.KIRO_CLI, ModelTier.BALANCED, 2),
    ModelSpec("minimax-m2.1", AgentTarget.KIRO_CLI, ModelTier.FAST, 1),
)

# ---------------------------------------------------------------------------
# Antigravity catalog. Verified via `antigravity models` on 2026-09-04.
# `--effort` support is verified rejected for claude-sonnet-4-6 and verified
# accepted for gemini-3.8-flash-low; other Gemini entries are assumed to follow
# the family, and non-Gemini entries are marked unsupported conservatively.
# No cost data is exposed for this agent, so these are ranked on capability only.
# ---------------------------------------------------------------------------
ANTIGRAVITY_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("claude-opus-4-6-thinking", AgentTarget.ANTIGRAVITY, ModelTier.FRONTIER, 5, CONTEXT_UNKNOWN, Specialization.REASONING),
    ModelSpec("gemini-3.1-pro-high", AgentTarget.ANTIGRAVITY, ModelTier.FRONTIER, 5, CONTEXT_UNKNOWN, Specialization.REASONING, True),
    ModelSpec("gemini-3.1-pro-low", AgentTarget.ANTIGRAVITY, ModelTier.ADVANCED, 4, CONTEXT_UNKNOWN, Specialization.REASONING, True),
    ModelSpec("claude-sonnet-4-6", AgentTarget.ANTIGRAVITY, ModelTier.ADVANCED, 4, CONTEXT_UNKNOWN, Specialization.CODING),
    ModelSpec("gemini-3.8-flash-high", AgentTarget.ANTIGRAVITY, ModelTier.ADVANCED, 3, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gpt-oss-120b-medium", AgentTarget.ANTIGRAVITY, ModelTier.BALANCED, 3),
    ModelSpec("gemini-3.8-flash-medium", AgentTarget.ANTIGRAVITY, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.7-flash-high", AgentTarget.ANTIGRAVITY, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.8-flash-low", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.7-flash-medium", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.7-flash-low", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.6-flash-high", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.6-flash-medium", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 1, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
    ModelSpec("gemini-3.6-flash-low", AgentTarget.ANTIGRAVITY, ModelTier.FAST, 1, CONTEXT_UNKNOWN, Specialization.GENERAL, True),
)

# ---------------------------------------------------------------------------
# Cline CLI catalog. Cline ships a real headless CLI (npm package `cline`), so it
# is a selectable agent: `cline -m <model> --thinking <level> "<prompt>"`.
#
# The default `cline` provider bills paid Cline Credits, and that balance is
# $0.00 on this account, so the previous `anthropic/*`, `openai/*`, `x-ai/*`,
# `moonshotai/*`, `deepseek/*` and `z-ai/*` ids all failed with "Insufficient
# balance". Cline is therefore authenticated against the free Gemini provider
# (`cline auth -p gemini`) using the local Gemini API key, and this catalog only
# offers Gemini model ids that route to that free provider. Verified on
# 2026-09-22 by running each id: `gemini-3.6-flash` answers, while `*-pro`,
# `*-flash-lite` and the retired `gemini-2.5-flash` are rejected by the API, so
# only the working id is offered rather than offered and then failing.
#
# `--thinking` accepts none|low|medium|high|xhigh and is provider-level rather
# than per-model, so every entry supports it.
# ---------------------------------------------------------------------------
CLINE_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("gemini-3.6-flash", AgentTarget.CLINE, ModelTier.FAST, 2, CONTEXT_1M, Specialization.GENERAL, True),
)

# Antigravity IDE is a separate surface from the Antigravity CLI: it is an editor
# launcher whose only flags are --diff, --merge, --goto and window controls, so it
# has no headless prompt mode and its model is chosen in the IDE UI.
#
# These candidate ids come from `antigravity models`, i.e. the CLI for the same
# account and backend. That makes them well-founded rather than invented, but the
# IDE's own picker list was not independently enumerated, so they remain advice.
# No --effort is ever offered here because the IDE takes no such flag.
ANTIGRAVITY_IDE_CATALOG: tuple[ModelSpec, ...] = tuple(
    ModelSpec(
        spec.name,
        AgentTarget.ANTIGRAVITY_IDE,
        spec.tier,
        spec.strength,
        spec.context_tokens,
        spec.specialization,
        False,
    )
    for spec in ANTIGRAVITY_CATALOG
)

# ---------------------------------------------------------------------------
# Antigravity API-key catalog: the second Antigravity account, authenticated by a
# Gemini API key instead of a signed-in Google account.
#
# Requests go directly to the Gemini API, so Claude and GPT are unreachable here.
# Every id below was verified on 2026-09-04 by running a real prompt on an API
# key. Deliberately excluded because they were probed and rejected with "Agent
# execution terminated due to error": gemini-3.1-pro-high, gemini-3.1-pro-low and
# gemini-3.8-flash-high. A bare id such as `gemini-3.8-flash` is also rejected
# ("requires --effort"), so the effort is always carried in the id.
# ---------------------------------------------------------------------------
ANTIGRAVITY_API_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("gemini-3.8-flash-medium", AgentTarget.ANTIGRAVITY_API, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.GENERAL, False),
    ModelSpec("gemini-3.7-flash-high", AgentTarget.ANTIGRAVITY_API, ModelTier.BALANCED, 3, CONTEXT_UNKNOWN, Specialization.GENERAL, False),
    ModelSpec("gemini-3.7-flash-medium", AgentTarget.ANTIGRAVITY_API, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, False),
    ModelSpec("gemini-3.8-flash-low", AgentTarget.ANTIGRAVITY_API, ModelTier.FAST, 2, CONTEXT_UNKNOWN, Specialization.GENERAL, False),
    ModelSpec("gemini-3.7-flash-low", AgentTarget.ANTIGRAVITY_API, ModelTier.FAST, 1, CONTEXT_UNKNOWN, Specialization.GENERAL, False),
)

AGENT_CATALOGS: dict[AgentTarget, tuple[ModelSpec, ...]] = {
    AgentTarget.KIRO_CLI: KIRO_CATALOG,
    AgentTarget.ANTIGRAVITY: ANTIGRAVITY_CATALOG,
    AgentTarget.ANTIGRAVITY_API: ANTIGRAVITY_API_CATALOG,
    AgentTarget.CLINE: CLINE_CATALOG,
}

DELIVERY_CATALOGS: dict[AgentTarget, tuple[ModelSpec, ...]] = {
    AgentTarget.ANTIGRAVITY_IDE: ANTIGRAVITY_IDE_CATALOG,
}

# Retained for existing callers that reason about Kiro models only.
MODEL_CATALOG: tuple[ModelSpec, ...] = KIRO_CATALOG


class Action(str, Enum):
    """The requested work type. Selection remains a plan for every action."""

    PLAN = "plan"
    ANALYZE = "analyze"
    IMPLEMENT = "implement"
    MUTATE = "mutate"


class Risk(str, Enum):
    """Potential impact of the requested work."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Complexity(str, Enum):
    """Reasoning and coordination complexity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ContextSize(str, Enum):
    """Relative amount of relevant context to consider."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True)
class SelectionRequest:
    """Inputs to deterministic policy evaluation.

    ``approval_granted`` documents approval supplied by a caller.  It does not
    perform, authorize, or execute a mutation itself.
    """

    action: Action
    risk: Risk
    complexity: Complexity
    context: ContextSize
    approval_granted: bool = False


@dataclass(frozen=True)
class SelectionResult:
    """A provider-neutral plan containing an ordered selection chain."""

    preferred: ModelName
    fallbacks: tuple[ModelName, ...]
    requires_approval: bool
    approval_granted: bool
    rationale: str

    @property
    def chain(self) -> tuple[ModelName, ...]:
        """Return preferred and fallback models in deterministic order."""

        return (self.preferred, *self.fallbacks)


@dataclass(frozen=True)
class AgentModelSelection:
    """A concrete, agent-valid model choice plus any companion flags.

    ``model`` is an identifier from that agent's own catalog and is safe to pass
    to its ``--model`` flag. ``effort`` is populated only when the chosen model
    accepts it. ``mode`` carries an agent execution mode where one applies.
    """

    agent: AgentTarget
    model: str
    fallbacks: tuple[str, ...]
    effort: str | None
    mode: str | None
    requires_approval: bool
    approval_granted: bool
    rationale: str

    @property
    def chain(self) -> tuple[str, ...]:
        return (self.model, *self.fallbacks)


_EFFORT_BY_COMPLEXITY = {
    Complexity.LOW: "low",
    Complexity.MEDIUM: "medium",
    Complexity.HIGH: "high",
}


def _requires_approval(request: SelectionRequest) -> bool:
    return request.action is Action.MUTATE and request.risk in {
        Risk.HIGH,
        Risk.CRITICAL,
    }


def _needs_strongest(request: SelectionRequest) -> bool:
    return (
        request.risk in {Risk.HIGH, Risk.CRITICAL}
        or request.complexity is Complexity.HIGH
    )


def needs_strongest_tier(request: SelectionRequest) -> bool:
    """Whether ``request`` must be served by the strongest model an agent has.

    Public so account balancing can keep such work off a capped account.
    """
    return _needs_strongest(request)


_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high)$")


def effort_encoded_in_model_id(model: str) -> bool:
    """Whether ``model`` already carries an effort level in its id.

    Antigravity rejects `--effort` when it disagrees with the id's own suffix
    (`gemini-3.8-flash-low --effort=medium` is a hard error), so such an id must
    never be paired with a separate effort.
    """
    return bool(_EFFORT_SUFFIX_RE.search(model or ""))


def _rank_for_fit(spec: ModelSpec, index: int, request: SelectionRequest) -> tuple:
    """Order candidates by fit for this request. Lower sorts first.

    Cost is deliberately not a factor. The axes are capability, whether the
    context actually fits, and whether the model specializes in the work type.
    Ties break on catalog declaration order, which is curated strongest-first
    within each tier, so a newer generation is preferred over an older one.
    """
    wants_code = request.action in {Action.IMPLEMENT, Action.MUTATE}
    wants_reasoning = request.action in {Action.PLAN, Action.ANALYZE} and _needs_strongest(request)

    # A large context must not be routed to a model with a known-smaller window.
    # Unknown windows are treated as unproven rather than disqualifying.
    if request.context is ContextSize.LARGE:
        fits_context = 0 if spec.context_tokens >= CONTEXT_1M else (1 if spec.context_tokens == CONTEXT_UNKNOWN else 2)
    else:
        fits_context = 0

    if wants_code:
        specialization_fit = 0 if spec.specialization is Specialization.CODING else 1
    elif wants_reasoning:
        specialization_fit = 0 if spec.specialization is Specialization.REASONING else 1
    else:
        # Planning or analysis that is not hard, i.e. questions and read-only
        # runs. A coding specialist is the wrong fit for them: with no penalty
        # here, catalog order handed qwen3-coder-next every one-line question.
        specialization_fit = 1 if spec.specialization is Specialization.CODING else 0

    if _needs_strongest(request):
        strength_fit = -spec.strength  # strongest first
    elif request.complexity is Complexity.MEDIUM:
        strength_fit = abs(spec.strength - 4)  # capable but not maximal
    else:
        strength_fit = abs(spec.strength - 2)  # fast, low-latency work

    # `auto` is a reasonable last resort but never a deliberate choice.
    auto_penalty = 1 if spec.tier is ModelTier.AUTO else 0

    # For high-risk or highly complex work, raw capability outranks
    # specialization: a critical mutation must not land on a merely
    # code-specialized model when a stronger one is available.
    if _needs_strongest(request):
        return (fits_context, strength_fit, specialization_fit, auto_penalty, index)
    return (fits_context, specialization_fit, strength_fit, auto_penalty, index)


def select_for_agent(
    agent: AgentTarget | str, request: SelectionRequest
) -> AgentModelSelection:
    """Choose a model from ``agent``'s own catalog based on capability fit.

    The result is deterministic and contains only identifiers that the named
    agent accepts. High-risk mutations are still flagged as requiring approval.
    """
    target = agent if isinstance(agent, AgentTarget) else AgentTarget(agent)
    catalog = AGENT_CATALOGS[target]
    ordered = [
        spec
        for _, spec in sorted(
            enumerate(catalog),
            key=lambda item: _rank_for_fit(item[1], item[0], request),
        )
    ]
    chosen = ordered[0]

    # `--effort` is rejected outright by models that do not support it, so it is
    # only emitted when the chosen model accepts it. An id that already encodes
    # its effort (`gemini-3.1-pro-low`) gets none either: a separate level that
    # disagrees with the suffix is rejected by the CLI.
    encoded = effort_encoded_in_model_id(chosen.name)
    effort = _EFFORT_BY_COMPLEXITY[request.complexity] if chosen.supports_effort and not encoded else None
    mode = "plan" if (target is AgentTarget.ANTIGRAVITY and request.action is Action.PLAN) else None

    reasons = [f"strength {chosen.strength}", f"tier {chosen.tier.value}"]
    if request.context is ContextSize.LARGE:
        reasons.append(
            f"{chosen.context_tokens or 'unstated'} context window for large-context work"
        )
    if chosen.specialization is not Specialization.GENERAL:
        reasons.append(f"{chosen.specialization.value} specialization")
    if chosen.supports_effort and encoded:
        reasons.append("effort fixed by the model id")
    rationale = "selected on capability fit: " + ", ".join(reasons)

    # The approval note is appended, never substituted: the capability-fit reasons
    # are what make the model choice auditable, and a mutation needs them most.
    requires_approval = _requires_approval(request)
    if requires_approval and not request.approval_granted:
        rationale += "; high-risk mutation requires approval before execution"
    elif requires_approval:
        rationale += "; high-risk mutation is approved"

    return AgentModelSelection(
        agent=target,
        model=chosen.name,
        fallbacks=tuple(spec.name for spec in ordered[1:]),
        effort=effort,
        mode=mode,
        requires_approval=requires_approval,
        approval_granted=request.approval_granted,
        rationale=rationale,
    )


@dataclass(frozen=True)
class ModelRecommendation:
    """Advice for an agent whose model this process cannot set.

    In-editor agents choose their model in their own UI. The brain therefore
    states the capability the task needs and, where a model list is discoverable,
    offers ranked candidates. ``candidates`` may be empty, which means no list
    could be read locally and the agent should match ``tier`` in its own picker.

    This is advice, never a claim that the model was changed. The agent records
    what it actually used when it reports its lifecycle.
    """

    agent: AgentTarget
    tier: ModelTier
    specialization: Specialization
    needs_large_context: bool
    candidates: tuple[str, ...]
    rationale: str
    requires_approval: bool

    @property
    def top_candidate(self) -> str | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict:
        return {
            "agent": self.agent.value,
            "tier": self.tier.value,
            "specialization": self.specialization.value,
            "needs_large_context": self.needs_large_context,
            "candidates": list(self.candidates),
            "rationale": self.rationale,
            "requires_approval": self.requires_approval,
            "settable": False,
        }


def _needed_tier(request: SelectionRequest) -> ModelTier:
    """Map task attributes onto the capability tier the work deserves."""
    if _needs_strongest(request):
        return ModelTier.FRONTIER
    if request.complexity is Complexity.MEDIUM or request.context is ContextSize.LARGE:
        return ModelTier.ADVANCED
    if request.action in {Action.IMPLEMENT, Action.MUTATE}:
        return ModelTier.CODING
    return ModelTier.FAST


def recommend_for_delivery_agent(
    agent: AgentTarget | str, request: SelectionRequest
) -> ModelRecommendation:
    """Recommend, but never set, a model for an in-editor chat agent.

    Candidates are ranked with the same capability-fit ordering used for
    selectable agents, so advice and selection stay consistent.
    """
    target = agent if isinstance(agent, AgentTarget) else AgentTarget(agent)
    if target not in DELIVERY_CATALOGS:
        raise ValueError(f"{target.value} is not a recommend-only agent")
    catalog = DELIVERY_CATALOGS[target]
    ordered = [
        spec
        for _, spec in sorted(
            enumerate(catalog),
            key=lambda item: _rank_for_fit(item[1], item[0], request),
        )
    ]

    tier = _needed_tier(request)
    if request.action in {Action.IMPLEMENT, Action.MUTATE}:
        specialization = Specialization.CODING
    elif _needs_strongest(request):
        specialization = Specialization.REASONING
    else:
        specialization = Specialization.GENERAL
    needs_large_context = request.context is ContextSize.LARGE

    reasons = [f"needs {tier.value} capability"]
    if specialization is not Specialization.GENERAL:
        reasons.append(f"{specialization.value} work")
    if needs_large_context:
        reasons.append("large context, prefer a 1M-token window")
    if not ordered:
        reasons.append("no local model list is discoverable for this agent, so pick the closest match in its own model picker")

    return ModelRecommendation(
        agent=target,
        tier=tier,
        specialization=specialization,
        needs_large_context=needs_large_context,
        candidates=tuple(spec.name for spec in ordered[:5]),
        rationale="recommended on capability fit: " + ", ".join(reasons),
        requires_approval=_requires_approval(request),
    )


def select_model(request: SelectionRequest) -> SelectionResult:
    """Return a deterministic Kiro-model selection plan for ``request``.

    Retained for callers that predate per-agent selection. It delegates to
    :func:`select_for_agent` against the Kiro catalog, so both paths agree.
    """

    selection = select_for_agent(AgentTarget.KIRO_CLI, request)
    return SelectionResult(
        preferred=ModelName(selection.model),
        fallbacks=tuple(ModelName(name) for name in selection.fallbacks),
        requires_approval=selection.requires_approval,
        approval_granted=selection.approval_granted,
        rationale=selection.rationale,
    )
