#!/usr/bin/env python3
"""Plan-only smart swarm orchestration for Shared Brain.

No queue writes, model calls, agent execution, file edits, or cloud operations are
performed here.  Callers receive an auditable plan hash and must use a separate,
future approval-gated executor to act on it.

The plan must describe what ``brain swarm dispatch`` would actually do, so it
shares both halves of the decision with the swarm:

* the AGENT comes from ``swarm.classify_task`` (the dispatch router), and
* the MODEL comes from that agent's own catalog via the same
  :func:`selection_request` the swarm worker uses at run time.

This module only owns the action/risk/complexity assessment of a task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable

try:  # package import for tests and dashboard
    from .model_policy import (
        SELECTABLE_AGENTS, Action, AgentTarget, Complexity, ContextSize, Risk, SelectionRequest,
        recommend_for_delivery_agent, select_for_agent,
    )
except ImportError:  # direct script execution via `brain plan`
    from model_policy import (
        SELECTABLE_AGENTS, Action, AgentTarget, Complexity, ContextSize, Risk, SelectionRequest,
        recommend_for_delivery_agent, select_for_agent,
    )


class Agent(str, Enum):
    KIRO = "kiro-cli"
    CLINE = "cline"
    ANTIGRAVITY_IDE = "antigravity-ide"
    ANTIGRAVITY = "antigravity"
    # The router can name the second Antigravity account, so a plan must be able
    # to show it rather than silently relabel the task.
    ANTIGRAVITY_API = "antigravity-api"


@dataclass(frozen=True)
class PlannedTask:
    id: str
    title: str
    agent: Agent
    action: Action
    risk: Risk
    complexity: Complexity
    model: str
    fallbacks: tuple[str, ...]
    requires_approval: bool
    locks: tuple[str, ...]
    verification: tuple[str, ...]
    rationale: str
    # Companion flags of the per-agent selection, so a plan shows exactly what the
    # worker would send. Defaulted and last so positional construction still works.
    effort: str | None = None
    mode: str | None = None
    # Set only when dispatch alternates the task across Antigravity accounts: the
    # model each account would run, as (account, model) pairs. The serving account
    # is decided at dispatch, and ``model`` is the one for ``agent``.
    account_models: tuple[tuple[str, str], ...] = ()


def _task_dict(task: PlannedTask) -> dict:
    return asdict(task) | {
        "agent": task.agent.value, "action": task.action.value, "risk": task.risk.value,
        "complexity": task.complexity.value, "account_models": dict(task.account_models),
    }


@dataclass(frozen=True)
class TaskPlan:
    version: str
    execution_allowed: bool
    tasks: tuple[PlannedTask, ...]
    plan_hash: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "execution_allowed": self.execution_allowed,
            "plan_hash": self.plan_hash,
            "tasks": [_task_dict(task) for task in self.tasks],
        }


# ---------------------------------------------------------------------------
# Action / risk / complexity assessment
#
# Terms match on word boundaries, with an optional plural suffix ("tests",
# "files", "docstrings"). A hyphen counts as part of a word, so "brain-ui" never
# matches "ui" and "multi-file" never matches "file".
# ---------------------------------------------------------------------------
def _term(term: str) -> str:
    return r"(?<![\w-])" + re.escape(term) + r"(?:s|es)?(?![\w-])"


def _matches(lower: str, terms: Iterable[str]) -> bool:
    return any(re.search(_term(term), lower) for term in terms)


# Words that open an interrogative or explanatory request.
_QUESTION_LEADS = frozenset({
    "what", "why", "how", "when", "where", "which", "who", "is", "are", "was", "were",
    "does", "do", "did", "can", "could", "should", "would", "will", "explain", "describe",
    "state", "tell", "give", "walk", "summarize", "summarise", "compare", "define", "clarify",
})
# A question that asks for a design or a recommendation is planning, not analysis.
_QUESTION_PLAN_TERMS = ("how should", "should we", "recommend", "propose", "design", "architecture", "strategy")

# Imperative verbs, recognised at the start of a clause (see _clause_verbs).
_MUTATE_VERBS = frozenset({
    "delete", "purge", "deploy", "redeploy", "restart", "apply", "rotate", "destroy", "drop",
    "truncate", "prune", "rollback", "scale", "uninstall", "kill", "reset", "wipe", "clean",
    "cleanup", "evict", "drain", "cordon", "revoke", "promote", "stop", "shutdown",
    "flush", "erase", "terminate", "reboot", "recreate", "overwrite", "decommission",
    "teardown", "restore", "failover", "rm",
})
_IMPLEMENT_VERBS = frozenset({
    "add", "fix", "implement", "rename", "refactor", "write", "create", "build", "update",
    "change", "move", "split", "extract", "style", "restyle", "make", "center", "centre",
    "convert", "migrate", "install", "generate", "replace", "modify", "edit", "tidy",
    "document", "annotate", "bump", "remove", "rewrite", "port", "wire", "integrate",
    "scaffold", "configure", "patch", "format", "optimize", "optimise", "simplify", "resolve",
    "upgrade", "downgrade", "disable", "enable", "set",
})
_PLAN_VERBS = frozenset({
    "design", "plan", "propose", "draft", "architect", "outline", "sketch", "recommend", "decide",
})
_ANALYZE_VERBS = frozenset({
    "run", "execute", "report", "list", "show", "tail", "get", "check", "count", "inspect",
    "find", "search", "verify", "review", "investigate", "audit", "analyze", "analyse",
    "assess", "evaluate", "research", "diagnose", "triage", "summarize", "summarise",
    "compare", "measure", "benchmark", "trace", "monitor", "read", "fetch", "describe",
    "explain", "identify", "determine", "lint", "query", "print", "display", "scan",
    "df", "uname", "jest", "pytest", "curl", "grep", "ping",
})

# Tool invocations whose subcommand decides whether they mutate anything. They are
# matched anywhere, because they usually follow "run" ("Run helm upgrade ...").
_MUTATING_COMMANDS = (
    "kubectl delete", "kubectl apply", "kubectl rollout", "kubectl scale", "kubectl drain",
    "kubectl cordon", "kubectl patch", "kubectl edit", "helm upgrade", "helm install",
    "helm uninstall", "helm rollback", "helm delete", "terraform apply", "terraform destroy",
    "docker rm", "docker rmi", "docker system prune", "rm -rf", "drop table", "drop database",
    "force push", "force-push", "git push --force",
)
# Read-only tool invocations. "terraform plan" is here on purpose: it changes nothing.
_READ_ONLY_COMMANDS = (
    "kubectl get", "kubectl describe", "kubectl logs", "kubectl top", "helm list", "helm status",
    "terraform plan", "terraform show", "terraform validate", "git status", "git log", "git diff",
    "docker ps", "docker images", "docker logs", "df -h", "uname", "jest", "pytest", "npm test",
)
# Any change that touches production is an operations mutation, whatever its verb:
# "Remove the production deployment" is not a code edit.
_PRODUCTION = ("production", "prod")
# Polite or indirect request wrappers. "Can you delete X?" and "I need you to
# restart Y" are orders, so the wrapper is stripped before the clause's verb is
# read, and the text is not treated as a question.
_REQUEST_WRAPPERS = (
    ("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"), ("can", "we"),
    ("could", "we"), ("would", "we"), ("will", "we"), ("why", "don", "t", "you"),
    ("why", "not"), ("i", "need", "you"), ("i", "want", "you"), ("we", "need"), ("i", "need"),
    ("need",), ("go", "ahead"), ("let", "s"), ("lets",), ("please",), ("kindly",),
)
_FILLER_WORDS = frozenset({"please", "also", "then", "now", "just", "first", "finally", "kindly"})

# "apply" on a code change is an edit, not an operations action.
_APPLY_TO_CODE_RE = re.compile(
    r"(?<![\w-])apply(?![\w-])[^,;:.]*(?<![\w-])(rename|refactor|patch|fix|diff|edit|suggestion|formatting|style)"
)
# Nouns that mark code editing when no imperative verb decided the action.
_CODE_EDIT_NOUNS = ("docstring", "jsdoc", "function", "type hint", "unit test", "bugfix", "bug fix")
# Documents that are design deliverables even when the verb is "write".
_DESIGN_DOCUMENTS = ("decision record", "adr", "design note", "design doc", "design document", "rfc", "proposal")

# Scope that spans many files: this is the work that needs the strongest model.
_MULTI_FILE_SCOPE = (
    "across every file", "across all files", "every file", "all files", "every import",
    "every usage", "all usages", "every reference", "all references", "references it",
    "whole codebase", "entire codebase", "across the codebase", "across the repo",
    "throughout the codebase", "codebase-wide", "repo-wide", "project-wide", "workspace-wide",
    "multi-file", "cross-file", "monorepo", "across modules", "separate modules", "into separate",
    "common package", "shared module", "shared package", "restructure", "reorganize",
    "reorganise", "every service", "all services", "across services",
)
# Reasoning depth that deserves the strongest model regardless of action. "plan" and
# "model" are deliberately absent: on their own they say nothing about depth.
_HIGH_DEPTH = (
    "state machine", "failure mode", "threat model", "architecture", "architectural", "protocol",
    "schema", "migration", "governance", "security", "credential", "isolation", "strategy",
    "decision record", "adr", "concurrency", "distributed", "disaster recovery", "zero-downtime",
)
_MEDIUM_DEPTH = (
    "trade-off", "tradeoff", "compare", "recommend", "how should", "overview", "walk me through",
    "walk through", "lifecycle", "why does", "pros and cons", "investigate", "review", "propose",
    "design", "escalated", "escalation", "root cause", "diagnose", "audit", "in depth", "detailed",
)
# An explicit request for a short answer or a trivial edit caps the depth: "In one
# sentence, what is a security group?" is a fast question even though "security"
# is a depth noun. Multi-file scope still wins, because that scope is real work.
_BREVITY = (
    "one sentence", "one-line", "one line", "two short lines", "briefly", "brief",
    "short answer", "in short", "tl;dr", "tldr", "quick question", "typo", "trivial",
    "tiny", "minor",
)
# Small scope that only lowers complexity when nothing deeper is asked for
# ("single point of failure" is not a small task).
_LOW_SCOPE = ("single", "variable")


def _clauses(lower: str, *, split_on_to: bool = True) -> list[str]:
    """Split a task into imperative clauses ("Run X and report Y" -> two).

    "to" also separates clauses ("I need you to restart X") except inside a
    question, where it usually introduces an infinitive ("Is it safe to prune?").
    """
    joiners = r"and|then|to" if split_on_to else r"and|then"
    return [part for part in re.split(r"[,;:!?]|\.(?:\s|$)|\b(?:" + joiners + r")\b", lower) if part.strip()]


def _strip_wrappers(words: list[str]) -> list[str]:
    """Drop fillers and polite request wrappers in front of a clause's verb."""
    changed = True
    while words and changed:
        changed = False
        if words[0] in _FILLER_WORDS:
            words, changed = words[1:], True
            continue
        for wrapper in _REQUEST_WRAPPERS:
            if tuple(words[: len(wrapper)]) == wrapper:
                words, changed = words[len(wrapper):], True
                break
    return words


def _clause_verbs(lower: str, *, split_on_to: bool = True) -> list[str]:
    """The leading word of every clause, i.e. its imperative verb if it has one."""
    verbs = []
    for clause in _clauses(lower, split_on_to=split_on_to):
        words = _strip_wrappers(re.findall(r"[a-z][a-z0-9-]*", clause))
        if words:
            verbs.append(words[0])
    return verbs


def _is_question(lower: str) -> bool:
    """Whether ``lower`` asks something rather than orders something.

    It is decided on the text's own leading word once a polite wrapper is
    removed, so "Can you delete X?" and "Delete X?" are orders; a trailing "?"
    only makes a question of text that does not open with an imperative verb.
    """
    words = re.findall(r"[a-z][a-z0-9-]*", lower)
    lead = _strip_wrappers(list(words))
    if not lead:
        return False
    if len(lead) != len(words) and lead[0] not in _QUESTION_LEADS:
        return False  # a wrapped order: "Could you run ...", "Please drop ..."
    if lead[0] in _QUESTION_LEADS:
        return True
    imperative = _MUTATE_VERBS | _IMPLEMENT_VERBS | _PLAN_VERBS | _ANALYZE_VERBS
    return lower.strip().endswith("?") and lead[0] not in imperative


def _runs_mutating_command(lower: str, question: bool) -> bool:
    """Whether ``lower`` asks for a mutating tool invocation to be run.

    Outside a question a mutating command anywhere counts ("Run helm upgrade ...").
    Inside one it counts only when a clause runs it ("..., then run kubectl delete
    ns x"), so "What does helm rollback do?" stays a question about the command.
    """
    if not question:
        return _matches(lower, _MUTATING_COMMANDS)
    for clause in _clauses(lower, split_on_to=False):
        words = re.findall(r"[a-z][a-z0-9-]*", clause)
        lead = _strip_wrappers(list(words))
        while lead and lead[0] in {"run", "execute", "exec", "perform", "invoke"}:
            lead = lead[1:]
        # Drop the same leading words from the raw clause, keeping its flags intact.
        rest = re.sub(r"^\W*(?:[a-z][a-z0-9-]*\W+){%d}" % (len(words) - len(lead)), "", clause)
        if any(re.match(_term(command), rest) for command in _MUTATING_COMMANDS):
            return True
    return False


def _action(lower: str) -> Action:
    # Mutation is decided FIRST, before any question or read-only rule, so no
    # phrasing can talk an operations action down to analysis. A mutating verb
    # leading any clause, a mutating tool invocation anywhere, or production
    # together with a mutating verb anywhere is a mutation. Only a genuine
    # question may carry a mutating word mid-sentence and stay a question
    # ("Explain why prune is safer than truncate", "What does helm rollback do?").
    question = _is_question(lower)
    verbs = set(_clause_verbs(lower, split_on_to=not question))
    mutate_verbs = verbs & _MUTATE_VERBS
    if mutate_verbs == {"apply"} and _APPLY_TO_CODE_RE.search(lower):
        mutate_verbs = set()
        verbs.add("apply-code")
    if mutate_verbs or _runs_mutating_command(lower, question):
        return Action.MUTATE
    production = _matches(lower, _PRODUCTION)
    if production and not question and _matches(lower, _MUTATE_VERBS):
        return Action.MUTATE
    if question:
        # A question is answered, never executed.
        return Action.PLAN if _matches(lower, _QUESTION_PLAN_TERMS) else Action.ANALYZE
    if _matches(lower, _DESIGN_DOCUMENTS) or verbs & _PLAN_VERBS:
        return Action.PLAN
    if verbs & _IMPLEMENT_VERBS or "apply-code" in verbs:
        # An edit applied to production is an operations change, not a code edit.
        return Action.MUTATE if production else Action.IMPLEMENT
    if verbs & _ANALYZE_VERBS or _matches(lower, _READ_ONLY_COMMANDS):
        return Action.ANALYZE
    if _matches(lower, _CODE_EDIT_NOUNS):
        return Action.MUTATE if production else Action.IMPLEMENT
    return Action.PLAN


def _complexity(lower: str, action: Action, length: int) -> Complexity:
    if _matches(lower, _MULTI_FILE_SCOPE) or length > 240:
        return Complexity.HIGH
    if _matches(lower, _BREVITY):
        return Complexity.LOW
    if _matches(lower, _HIGH_DEPTH):
        return Complexity.HIGH
    if _matches(lower, _MEDIUM_DEPTH):
        return Complexity.MEDIUM
    if _matches(lower, _LOW_SCOPE):
        return Complexity.LOW
    # Planning and code edits need some coordination by default; answering a
    # question, running a read-only command or an operations verb does not.
    return Complexity.MEDIUM if action in {Action.PLAN, Action.IMPLEMENT} else Complexity.LOW


def _risk(lower: str, action: Action) -> Risk:
    production = _matches(lower, _PRODUCTION)
    if action is Action.MUTATE:
        return Risk.CRITICAL if production else Risk.HIGH
    if production and (_matches(lower, _MUTATE_VERBS) or _matches(lower, _MUTATING_COMMANDS)):
        # A question about changing production ("Is it safe to delete the prod
        # namespace?") is answered, not executed, but it is still high risk: an
        # agent answering it holds production access, so it needs the strongest
        # model and is kept out of the dashboard's low/medium execution scope.
        return Risk.HIGH
    return Risk.MEDIUM if action is Action.IMPLEMENT else Risk.LOW


def assess(text: str) -> tuple[Action, Risk, Complexity]:
    """Classify what a task *is*: its action, risk and complexity."""
    lower = text.lower()
    action = _action(lower)
    return action, _risk(lower, action), _complexity(lower, action, len(text))


def context_size(text: str) -> ContextSize:
    return ContextSize.LARGE if len(text) > 4000 else (ContextSize.MEDIUM if len(text) > 800 else ContextSize.SMALL)


def selection_request(text: str, *, approval_granted: bool = False) -> SelectionRequest:
    """The model-selection inputs for ``text``.

    This is the single source used by the planner and by the swarm worker at run
    time, so a plan's model is the model the worker would send for the same task.
    """
    action, risk, complexity = assess(text)
    return SelectionRequest(action=action, risk=risk, complexity=complexity, context=context_size(text), approval_granted=approval_granted)


def _swarm():
    """The swarm module, imported lazily.

    swarm creates its queue directories under BRAIN_DIR at import time, so
    importing it at module load would do that for every consumer of this module,
    including ones that never plan.
    """
    try:
        from . import swarm  # type: ignore[attr-defined]
    except ImportError:
        import swarm  # type: ignore[no-redef]
    return swarm


def route(text: str) -> tuple[Agent, str]:
    """Choose the agent with the dispatch router, so plan and dispatch agree."""
    agent, _confidence, rationale = _swarm().classify_task(text)
    return Agent(agent), rationale


def dispatch_assignment(agent: Agent, request: SelectionRequest) -> tuple[Agent, tuple[str, ...], str]:
    """Apply dispatch's Antigravity account rule to a routed agent.

    ``create_task`` pins hard work to the all-models ``antigravity`` pool and
    alternates low/medium work across attached accounts, so the router's pick is
    not always the agent that runs. The plan applies the same rule, via the same
    side-effect-free function, so its agent and model are the dispatched ones.
    """
    assigned, accounts, note = _swarm().antigravity_assignment(agent.value, request)
    return Agent(assigned), tuple(accounts), note


@dataclass(frozen=True)
class ModelChoice:
    model: str
    fallbacks: tuple[str, ...]
    effort: str | None
    mode: str | None
    requires_approval: bool
    rationale: str


def model_for_agent(agent: Agent | str, request: SelectionRequest) -> ModelChoice:
    """Resolve a model from ``agent``'s own catalog; never a cross-agent id."""
    target = AgentTarget(agent.value if isinstance(agent, Agent) else agent)
    if target in SELECTABLE_AGENTS:
        selection = select_for_agent(target, request)
        return ModelChoice(selection.model, selection.fallbacks, selection.effort, selection.mode, selection.requires_approval, selection.rationale)
    # A delivery agent's model is chosen by a human in its own UI: advise only.
    advice = recommend_for_delivery_agent(target, request)
    return ModelChoice(advice.top_candidate or "", advice.candidates[1:], None, None, advice.requires_approval, advice.rationale + " (advice only; set in the IDE)")


def _classify(text: str) -> tuple[Agent, Action, Risk, Complexity, tuple[str, ...], tuple[str, ...], str]:
    lower = text.lower()
    agent, reason = route(text)
    action, risk, complexity = assess(text)
    locks = tuple(sorted({"cloud" if _matches(lower, ("azure", "kubectl", "deploy")) else "", "workspace" if action is not Action.PLAN else ""} - {""}))
    verification = ("review plan output",) if action is Action.PLAN else ("run targeted validation", "review diff and verification receipt")
    return agent, action, risk, complexity, locks, verification, reason


def _split_explicit_tasks(text: str) -> list[str]:
    """Split only explicit newline bullets; prose/semicolons remain one task."""
    lines = [line.strip().lstrip("- ").strip() for line in text.splitlines()]
    explicit = [line for line in lines if line]
    return explicit if len(explicit) > 1 else ([text.strip()] if text.strip() else [])


def plan_tasks(tasks: Iterable[str], *, approval_granted: bool = False) -> TaskPlan:
    planned: list[PlannedTask] = []
    for index, title in enumerate((task.strip() for task in tasks if task.strip()), start=1):
        agent, action, risk, complexity, locks, verification, reason = _classify(title)
        request = selection_request(title, approval_granted=approval_granted)
        agent, accounts, assignment_note = dispatch_assignment(agent, request)
        choice = model_for_agent(agent, request)
        rationale = reason + (f"; {assignment_note}" if assignment_note else "")
        account_models: tuple[tuple[str, str], ...] = ()
        if len(accounts) > 1:
            account_models = tuple((account, model_for_agent(account, request).model) for account in accounts)
            rationale += "; the model follows the serving account (" + ", ".join(f"{a}: {m}" for a, m in account_models) + ")"
        planned.append(PlannedTask(
            str(index), title, agent, action, risk, complexity, choice.model, tuple(choice.fallbacks),
            choice.requires_approval, locks, ("plan-only: no execution performed", *verification),
            rationale + "; " + choice.rationale, choice.effort, choice.mode, account_models,
        ))
    # Must stay byte-compatible with execution.canonical_plan_hash, which recomputes
    # it to reject altered plans, so ``account_models`` is hashed as its tuple here.
    payload = json.dumps([asdict(task) | {"agent": task.agent.value, "action": task.action.value, "risk": task.risk.value, "complexity": task.complexity.value} for task in planned], sort_keys=True, default=str)
    return TaskPlan("1", False, tuple(planned), hashlib.sha256(payload.encode()).hexdigest()[:16])


def plan_text(text: str, *, approval_granted: bool = False) -> TaskPlan:
    return plan_tasks(_split_explicit_tasks(text), approval_granted=approval_granted)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a plan-only Smart Brain swarm plan")
    parser.add_argument("task", help="One task, or newline-separated explicit tasks")
    parser.add_argument("--approved", action="store_true", help="Record already-granted approval in a plan; execution remains disabled")
    args = parser.parse_args()
    print(json.dumps(plan_text(args.task, approval_granted=args.approved).to_dict(), indent=2))


if __name__ == "__main__":
    main()
