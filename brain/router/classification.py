"""Deterministic Task Classification for Intelligent Routing.

Classifies incoming tasks into canonical operational domains without requiring
an external LLM call, with an extensible interface for future model classifiers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agents.base.adapter import Capability
from models.policies.model_policy import Complexity, Risk


class TaskCategory(str, Enum):
    CODING = "coding"
    DEBUGGING = "debugging"
    ARCHITECTURE = "architecture"
    DEVOPS = "DevOps"
    DOCUMENTATION = "documentation"
    RESEARCH = "research"
    REASONING = "reasoning"
    QUICK_QUESTION = "quick_question"
    LONG_CONTEXT = "long_context"
    AUTOMATION = "automation"
    GENERAL = "general"


TaskDomain = TaskCategory


@dataclass
class ClassificationResult:
    """Outcome of task classification."""
    category: TaskCategory
    confidence: float
    required_capabilities: list[Capability]
    suggested_complexity: Complexity
    keywords_matched: list[str]
    explanation: str
    #: Blast radius of the task, independent of how hard it is. A one-line command
    #: can be critical ("drop the production database") while a long refactor is
    #: low risk. Defaults to LOW so older callers keep working.
    risk: Risk = Risk.LOW

    @property
    def primary_domain(self) -> TaskDomain:
        return self.category

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "primary_domain": self.category.value,
            "confidence": round(self.confidence, 2),
            "required_capabilities": [c.value for c in self.required_capabilities],
            "suggested_complexity": self.suggested_complexity.value,
            "risk": self.risk.value,
            "keywords_matched": self.keywords_matched,
            "explanation": self.explanation,
        }


#: Deterministic classification rules (patterns, category, capabilities, complexity, base confidence)
RULES: list[tuple[re.Pattern[str], TaskCategory, list[Capability], Complexity, float, str]] = [
    (
        re.compile(r"\b(architecture|architect|microservice|system design|rfc|protocol|spec|interface contract|topology)\b", re.I),
        TaskCategory.ARCHITECTURE,
        [Capability.ARCHITECTURE, Capability.PROTOCOL_DESIGN, Capability.GOVERNANCE],
        Complexity.STRONG,
        0.95,
        "Architectural specifications and system governance design",
    ),
    (
        re.compile(r"\b(debug|exception|null pointer|traceback|error|crash|failing test|bug|segfault|500 internal|regression|memory leak)\b", re.I),
        TaskCategory.DEBUGGING,
        [Capability.CODE_REVIEW, Capability.BUILD_AND_TEST, Capability.DEEP_REASONING],
        Complexity.STANDARD,
        0.90,
        "Software defect analysis and debugging",
    ),
    (
        re.compile(r"\b(docker|kubernetes|k8s|ci/cd|pipeline|deploy|aks|container|terraform|ansible|kubectl|helm)\b", re.I),
        TaskCategory.DEVOPS,
        [Capability.TERMINAL_OPERATIONS, Capability.KUBERNETES_READ_ONLY, Capability.CLOUD_READ_ONLY],
        Complexity.STANDARD,
        0.92,
        "DevOps, container, and infrastructure operations",
    ),
    (
        re.compile(r"\b(document|documentation|readme|docs|changelog|manual|guide|api reference|docstring)\b", re.I),
        TaskCategory.DOCUMENTATION,
        [Capability.DOCUMENTATION],
        Complexity.FAST,
        0.88,
        "Technical documentation and specification writing",
    ),
    (
        re.compile(r"\b(research|investigate|compare|benchmark|survey|literature|whitepaper|trade-?off|academic)\b", re.I),
        TaskCategory.RESEARCH,
        [Capability.DEEP_REASONING, Capability.LOCAL_VALIDATION],
        Complexity.STRONG,
        0.80,
        "Technical research and solution exploration",
    ),
    (
        re.compile(r"\b(deep reasoning|proof|prove|formal verification|cryptographic|consensus|byzantine|theorem|mathematical logic)\b", re.I),
        TaskCategory.REASONING,
        [Capability.DEEP_REASONING],
        Complexity.REASONING,
        0.95,
        "Complex logical deduction and mathematical reasoning",
    ),
    (
        re.compile(r"\b(quick|what is|how do i|explain briefly|sanity check|typo|fix spelling|capital of)\b", re.I),
        TaskCategory.QUICK_QUESTION,
        [Capability.LOCAL_VALIDATION],
        Complexity.FAST,
        0.80,
        "Quick informational response or minor modification",
    ),
    (
        re.compile(r"\b(entire repo|full codebase|all files|across all|corpus|huge diff|long document|500-page|book repository|entire\b.*?\brepository)\b", re.I),
        TaskCategory.LONG_CONTEXT,
        [Capability.CODE_REVIEW, Capability.ARCHITECTURE],
        Complexity.STRONG,
        0.85,
        "Long-context comprehension across extensive codebase",
    ),
    (
        re.compile(r"\b(batch|cron|automate|auto-sync|nightly|backup|scrape|bulk|reconcile)\b", re.I),
        TaskCategory.AUTOMATION,
        [Capability.TERMINAL_OPERATIONS, Capability.BUILD_AND_TEST],
        Complexity.STANDARD,
        0.80,
        "Automated background processing and scripting",
    ),
    (
        re.compile(r"\b(refactor|implement|code|feature|component|class|function|method|clean code|endpoint|python|script|parse|parse logs)\b", re.I),
        TaskCategory.CODING,
        [Capability.EDITOR_REFACTORING, Capability.COMPONENT_REFACTORING, Capability.CODE_REVIEW],
        Complexity.STANDARD,
        0.85,
        "Software implementation and code refactoring",
    ),
]


class TaskClassifier:
    """Classifies task instructions using deterministic pattern matching."""

    def classify(self, text: str) -> ClassificationResult:
        matched_rules = []
        for pattern, cat, caps, comp, conf, expl in RULES:
            m = pattern.findall(text)
            if m:
                matched_rules.append((len(m), pattern, cat, caps, comp, conf, expl, m))

        if matched_rules:
            # Confidence is the primary signal; match count only nudges.
            #
            # This previously sorted by raw match count first, which rewarded
            # verbosity: a padded string repeating cheap keywords ("summary
            # summary summary recap rename") outranked a precise 0.95-confidence
            # rule matching once, so "deploy the service to kubernetes" could be
            # classified as a FAST quick_question and routed to the lightest model.
            # The count bonus saturates at 3 matches and is capped well below the
            # confidence gaps between rules, so it can break ties without
            # overturning a more certain classification.
            matched_rules.sort(key=lambda x: (x[5] + 0.01 * min(x[0], 3), x[0]), reverse=True)
            top = matched_rules[0]
            words = [str(w) for w in top[7]]
            return ClassificationResult(
                category=top[2],
                confidence=top[5],
                required_capabilities=top[3],
                suggested_complexity=top[4],
                keywords_matched=words[:5],
                explanation=top[6],
                risk=infer_risk(text),
            )

        # Fallback to General
        return ClassificationResult(
            category=TaskCategory.GENERAL,
            confidence=0.50,
            required_capabilities=[Capability.LOCAL_VALIDATION],
            suggested_complexity=Complexity.STANDARD,
            keywords_matched=[],
            explanation="General task without specialized domain keywords",
            risk=infer_risk(text),
        )

# ---------------------------------------------------------------------------
# Additional coverage rules
# ---------------------------------------------------------------------------
# Production evidence (2026-09-22): 320 of 354 recorded jobs ran on the single
# model `gemini-3.8-flash-medium`. The selector was never the problem — it maps
# complexity to a strength tier correctly. The classifier simply did not match
# most real task phrasing, so `classify()` fell through to GENERAL/STANDARD,
# which resolves to strength 3 every time.
#
# These rules close the gaps that mattered, keeping patterns specific because
# `classify()` ranks by match *count* first: a broad rule full of common words
# would outrank a precise one.
EXTRA_RULES: list[tuple] = [
    # Root-cause work. "why does X happen", races and deadlocks are genuine
    # reasoning, not standard implementation, and were landing in GENERAL.
    (
        re.compile(
            r"\b(root cause|why (?:does|is|do|did|would)|race condition|deadlock|"
            r"livelock|heisenbug|flaky|intermittent|non-?deterministic|"
            r"under (?:heavy )?load|only in production|cannot reproduce|"
            r"reason (?:carefully|step by step)|think through)\b",
            re.IGNORECASE,
        ),
        TaskCategory.REASONING,
        [Capability.DEEP_REASONING],
        Complexity.REASONING,
        0.93,
        "Root-cause analysis of non-deterministic behaviour requires deep reasoning",
    ),
    # Planning and critique. Research consensus is that planning errors cascade
    # into every downstream task, so these justify a stronger tier.
    (
        re.compile(
            r"\b(plan|roadmap|break down|decompose|design doc|adr|"
            r"code review|review the|critique|assess the|evaluate the|"
            r"trade ?off analysis|pros and cons)\b",
            re.IGNORECASE,
        ),
        TaskCategory.ARCHITECTURE,
        [Capability.ARCHITECTURE],
        Complexity.STRONG,
        0.88,
        "Planning and critique errors cascade downstream, so a stronger tier is used",
    ),
    # Security-sensitive change. Needs care even when the diff is small.
    (
        re.compile(
            r"\b(auth|authentication|authorisation|authorization|rbac|"
            r"permission|credential|secret|api key|token rotation|"
            r"vulnerability|cve|injection|xss|csrf|ssrf|privilege escalation|"
            r"encryption|tls|certificate)\b",
            re.IGNORECASE,
        ),
        TaskCategory.REASONING,
        [Capability.CODE_REVIEW],
        Complexity.STRONG,
        0.9,
        "Security-sensitive change; correctness matters more than speed",
    ),
    # Schema and data migrations are irreversible in ways code is not.
    (
        re.compile(
            r"\b(migration|migrate|schema change|alter table|drop column|"
            r"backfill|reindex|data model change)\b",
            re.IGNORECASE,
        ),
        TaskCategory.ARCHITECTURE,
        [Capability.ARCHITECTURE],
        Complexity.STRONG,
        0.89,
        "Data migrations are hard to reverse, so they are not routed to a fast tier",
    ),
    # Test authoring: well-specified work against known criteria -> mid tier.
    (
        re.compile(
            r"\b(unit test|integration test|write tests?|add tests?|"
            r"test coverage|pytest|jest|testcase|test suite|assertion)\b",
            re.IGNORECASE,
        ),
        TaskCategory.CODING,
        [Capability.CODE_REVIEW],
        Complexity.STANDARD,
        0.85,
        "Test authoring evaluates against known criteria rather than inventing design",
    ),
    # Summarisation and trivial edits: language competence, minimal reasoning.
    (
        re.compile(
            r"\b(summari[sz]e|summary|tl;?dr|in one sentence|one-?liner|"
            r"recap|brief overview|rename|reformat|format|lint|"
            r"whitespace|import order|bump version)\b",
            re.IGNORECASE,
        ),
        TaskCategory.QUICK_QUESTION,
        [Capability.LOCAL_VALIDATION],
        Complexity.FAST,
        0.86,
        "Summarisation and mechanical edits need the lightest viable model",
    ),
]

RULES = list(RULES) + EXTRA_RULES


# ---------------------------------------------------------------------------
# Risk inference
# ---------------------------------------------------------------------------
# `select_model()` accepts a `risk` argument and escalates to the frontier tier on
# high/critical, but nothing ever computed one: every call site passed complexity
# alone, so risk could never influence the choice. Blast radius is orthogonal to
# difficulty — "drop the production database" is trivial to write and catastrophic
# to run — so it is derived separately and passed alongside complexity.
# Risk is only meaningful when the task *performs* the operation. Prose that merely
# mentions it must not escalate: an audit found "document the production checklist"
# and "add a secret santa feature" both jumping to the strength-5 frontier tier,
# which is the most expensive model in the catalogue. Over-escalation on ordinary
# docs and UI work is a real cost, so the signals below require an operational
# object, and descriptive intent suppresses escalation entirely.
_RISK_CRITICAL = re.compile(
    # Destructive verb applied to a real data object. Qualifiers may sit between them
    # ("drop the production database") and objects may be plural ("rotate the
    # production API credentials"), but the object itself must be present.
    r"\b("
    r"drop\s+(?:\w+\s+){0,3}(?:database|table|schema)s?"
    r"|truncate\s+tables?"
    r"|delete\s+(?:all|everything)"
    r"|delete\s+(?:\w+\s+){0,3}(?:database|table|schema)s?"
    r"|rm\s+-rf"
    r"|force[- ]push"
    r"|reset\s+--hard"
    r"|git\s+clean\s+-f"
    r"|revoke\s+(?:\w+\s+){0,3}(?:access|credential|token|key|cert)s?"
    r"|rotate\s+(?:\w+\s+){0,3}(?:credential|secret|key|token|password)s?"
    r"|wipe|decommission"
    r"|tear\s+down\s+(?:the\s+)?(?:cluster|environment|stack)s?"
    r")\b",
    re.IGNORECASE,
)
_RISK_HIGH = re.compile(
    # An environment or sensitive subsystem being acted on, not merely named. Bare
    # "production" and "secret" are excluded: they collide with docs and feature work
    # ("production checklist", "secret santa") far too often.
    r"\b("
    r"(?:deploy|release|roll\s?out|promote|apply|migrate|patch|hotfix|scale"
    r"|failover|cut\s?over)\s+(?:\w+\s+){0,3}(?:to\s+)?(?:production|prod|live)"
    r"|(?:production|prod|live)\s+(?:database|cluster|deploy(?:ment)?|traffic"
    r"|incident|outage)s?"
    r"|(?:update|change|modify|edit|add|remove)\s+(?:\w+\s+){0,3}"
    r"(?:firewall|dns\s+record|iam\s+(?:policy|role)|rbac|security\s+group"
    r"|key\s?vault|tls\s+cert(?:ificate)?)s?"
    r"|schema\s+migration|data\s+migration|customer\s+data"
    r"|payment\s+(?:flow|gateway|processing)"
    r")\b",
    re.IGNORECASE,
)
_RISK_MEDIUM = re.compile(
    # Reversible operational mutations, scoped to infrastructure objects so that UI
    # and code-level wording ("delete the unused import", "add a reset password
    # link") stays low.
    r"\b("
    r"(?:delete|remove|drop|purge|disable|revert|downgrade|uninstall|reset)\s+"
    r"(?:the\s+)?(?:\w+\s+){0,2}"
    r"(?:pod|deployment|service|container|volume|bucket|queue|index|branch|tag"
    r"|release|record|user|account|role|policy|rule|job|cron"
    r"|feature\s?flag|flag|config|configuration|setting|env\s?var|environment\s?variable)s?"
    r"|restart\s+(?:the\s+)?(?:\w+\s+){0,2}"
    r"(?:pod|service|server|container|daemon|cluster|node|process)s?"
    r"|redeploy|roll\s?back"
    r")\b",
    re.IGNORECASE,
)

#: Wording that describes or explains rather than performs. A task about a risky
#: subject is not itself risky, so these suppress escalation to LOW.
_DESCRIPTIVE_INTENT = re.compile(
    r"\b(document|documentation|readme|docs|explain|describe|summari[sz]e|"
    r"tutorial|guide|example|checklist|comment|docstring|changelog|"
    r"what (?:is|does)|how (?:do|does)|write (?:a |an )?(?:doc|note|post))\b",
    re.IGNORECASE,
)


def infer_risk(text: str) -> Risk:
    """Blast radius of a task, independent of how hard it is.

    Deliberately ordered most-severe-first: a task that both drops a table and
    mentions production is critical, not high. Keyword matching is coarse, but the
    failure mode is asymmetric — over-estimating risk costs a more expensive model,
    under-estimating it runs a destructive change on a weak one.
    """
    # Describing a dangerous operation is not performing it. Checked before the
    # severity ladder so a docs task never reaches the frontier tier on keywords.
    if _DESCRIPTIVE_INTENT.search(text) and not _RISK_CRITICAL.search(text):
        return Risk.LOW
    if _RISK_CRITICAL.search(text):
        return Risk.CRITICAL
    if _RISK_HIGH.search(text):
        return Risk.HIGH
    if _RISK_MEDIUM.search(text):
        return Risk.MEDIUM
    return Risk.LOW
