"""Model selection must vary with the task, not collapse onto one model.

Production evidence that prompted this suite (2026-09-22): of 354 recorded jobs,
320 ran on `gemini-3.8-flash-medium` and 34 on `auto` — nothing else, across every
agent. The selector was never at fault; it maps complexity to a strength tier
correctly. Two upstream defects starved it:

1. The classifier matched only 10 narrow keyword rules, so most real task phrasing
   fell through to GENERAL/STANDARD, which resolves to strength 3 every time.
2. `select_model()` accepts a `risk` argument and escalates to the frontier tier on
   high/critical, but no call site ever passed one, so blast radius could never
   influence the choice — a destructive task could be handed to a fast model.

These tests assert the *outcome* (which model a realistic task resolves to), so a
regression that re-collapses routing onto one model fails here.
"""
from __future__ import annotations

import unittest

from brain.router.classification import TaskClassifier, infer_risk
from models.policies.model_policy import (
    AGENT_CATALOGS,
    Complexity,
    Risk,
    select_model,
)

#: A realistic spread of work, the kind that produced the 320-job monoculture.
CORPUS = [
    "run git status",
    "fix a typo in the README",
    "summarise this file in one sentence",
    "reformat the imports in utils.py",
    "add a unit test for the parser",
    "implement the user profile endpoint",
    "write integration tests for the queue",
    "code review the new payment module",
    "design a distributed multi-region consensus protocol",
    "plan the migration off the legacy scheduler",
    "why does this race condition happen under load?",
    "root cause the flaky checkout test",
    "rotate the production API credentials",
    "drop the production database and recreate all tables",
]


def _model_for(agent: str, text: str) -> str:
    r = TaskClassifier().classify(text)
    return select_model(agent, complexity=r.suggested_complexity, risk=r.risk).preferred_model


class TestRoutingIsNotAMonoculture(unittest.TestCase):
    def test_multiple_distinct_models_are_selected_across_the_corpus(self):
        for agent in ("antigravity", "kiro-cli"):
            chosen = {_model_for(agent, t) for t in CORPUS}
            self.assertGreaterEqual(
                len(chosen),
                3,
                f"{agent} collapsed onto {chosen}; routing is not varying by task",
            )

    def test_no_single_model_dominates_the_corpus(self):
        # The regression signature was one model taking ~90% of traffic.
        for agent in ("antigravity", "kiro-cli"):
            picks = [_model_for(agent, t) for t in CORPUS]
            top = max(set(picks), key=picks.count)
            share = picks.count(top) / len(picks)
            self.assertLess(
                share,
                0.75,
                f"{agent}: {top} took {share:.0%} of the corpus; that is the monoculture bug",
            )

    def test_a_trivial_task_and_a_hard_task_do_not_get_the_same_model(self):
        for agent in ("antigravity", "kiro-cli"):
            trivial = _model_for(agent, "fix a typo in the README")
            hard = _model_for(agent, "why does this race condition happen under load?")
            self.assertNotEqual(
                trivial, hard, f"{agent} routed a typo and a race-condition analysis identically"
            )


class TestComplexityCoverage(unittest.TestCase):
    """Phrasing that previously fell through to GENERAL/STANDARD."""

    def test_root_cause_questions_are_reasoning_work(self):
        for text in (
            "why does this race condition happen under load?",
            "root cause the flaky checkout test",
            "this is non-deterministic, reason carefully",
        ):
            r = TaskClassifier().classify(text)
            self.assertEqual(r.suggested_complexity, Complexity.REASONING, text)

    def test_summarisation_and_mechanical_edits_take_the_lightest_tier(self):
        for text in (
            "summarise this file in one sentence",
            "reformat the imports in utils.py",
            "bump version to 2.1.0",
        ):
            r = TaskClassifier().classify(text)
            self.assertEqual(r.suggested_complexity, Complexity.FAST, text)

    def test_planning_and_review_take_a_strong_tier(self):
        for text in (
            "plan the migration off the legacy scheduler",
            "code review the new payment module",
            "critique this approach",
        ):
            r = TaskClassifier().classify(text)
            self.assertIn(
                r.suggested_complexity,
                (Complexity.STRONG, Complexity.REASONING),
                text,
            )

    def test_test_authoring_is_standard_not_frontier(self):
        # Well-specified work against known criteria: mid tier, not the most expensive.
        r = TaskClassifier().classify("add a unit test for the parser")
        self.assertEqual(r.suggested_complexity, Complexity.STANDARD)

    def test_the_general_fallback_is_no_longer_the_common_case(self):
        from brain.router.classification import TaskCategory

        generals = [
            t for t in CORPUS if TaskClassifier().classify(t).category == TaskCategory.GENERAL
        ]
        self.assertLessEqual(
            len(generals),
            len(CORPUS) // 3,
            f"too many tasks still fall through to GENERAL: {generals}",
        )


class TestRiskInference(unittest.TestCase):
    def test_destructive_operations_are_critical(self):
        for text in (
            "drop the production database and recreate all tables",
            "drop database",
            "truncate table users",
            "rm -rf /var/data",
            "force-push the release branch",
        ):
            self.assertEqual(infer_risk(text), Risk.CRITICAL, text)

    def test_production_and_secret_handling_is_high(self):
        for text in (
            "update the firewall rule",
            "change the DNS record",
            "deploy the payment service to production",
        ):
            self.assertEqual(infer_risk(text), Risk.HIGH, text)

    def test_credential_rotation_is_critical_not_merely_high(self):
        # Rotation invalidates live sessions, so it is not reversible in practice.
        self.assertEqual(infer_risk("rotate the production API credentials"), Risk.CRITICAL)

    def test_reversible_mutations_are_medium(self):
        for text in ("restart the pod", "disable the feature flag"):
            self.assertEqual(infer_risk(text), Risk.MEDIUM, text)

    def test_read_only_work_is_low(self):
        for text in ("fix a typo", "summarise this file", "run git status"):
            self.assertEqual(infer_risk(text), Risk.LOW, text)

    def test_severity_is_ordered_most_severe_first(self):
        # Mentions both a destructive verb and production: critical, not high.
        self.assertEqual(infer_risk("drop the production database"), Risk.CRITICAL)


class TestRiskReachesTheSelector(unittest.TestCase):
    def test_high_risk_escalates_to_the_strongest_tier(self):
        for agent in ("antigravity", "kiro-cli"):
            low = select_model(agent, complexity=Complexity.STANDARD, risk=Risk.LOW)
            crit = select_model(agent, complexity=Complexity.STANDARD, risk=Risk.CRITICAL)
            self.assertNotEqual(
                low.preferred_model,
                crit.preferred_model,
                f"{agent}: risk did not change the model for identical complexity",
            )
            self.assertGreater(crit.strength, low.strength, agent)

    def test_a_destructive_task_never_lands_on_the_weakest_model(self):
        for agent in ("antigravity", "kiro-cli"):
            catalog = AGENT_CATALOGS[agent]
            weakest = min(s.strength for s in catalog)
            r = TaskClassifier().classify("drop the production database and recreate all tables")
            decision = select_model(agent, complexity=r.suggested_complexity, risk=r.risk)
            self.assertGreater(
                decision.strength,
                weakest,
                f"{agent} routed a critical-risk task to its weakest tier",
            )

    def test_classification_result_exposes_risk(self):
        r = TaskClassifier().classify("drop the production database")
        self.assertEqual(r.risk, Risk.CRITICAL)
        self.assertEqual(r.to_dict()["risk"], "critical")


class TestSmartRouterThreadsRisk(unittest.TestCase):
    def test_no_select_model_call_omits_risk(self):
        # Guards the actual defect: every call site passed complexity only.
        import re
        from pathlib import Path

        src = Path("brain/router/smart_router.py").read_text(encoding="utf-8")
        # Strip comments so prose mentioning select_model() is not counted.
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        calls = re.findall(r"select_model\((.*?)\)", code, re.DOTALL)
        self.assertTrue(calls, "no select_model calls found; test would pass vacuously")
        missing = [c.strip()[:70] for c in calls if "risk=" not in c]
        self.assertEqual(missing, [], f"select_model call(s) omit risk: {missing}")


class TestAuditFindings(unittest.TestCase):
    """Cases an independent audit found wrong in the first version of this fix.

    Two defects were reported and are pinned here: benign work escalating to the
    most expensive tier, and category hijacking by keyword padding.
    """

    #: Ordinary docs, UI and refactor work. None of it performs a risky operation,
    #: so none of it may reach the frontier tier — that is money spent for nothing.
    BENIGN = [
        "document the production checklist",
        "add a production-ready logging config example to the readme",
        "add a secret santa feature to the app",
        "delete the unused import in utils.py",
        "restart the tutorial when the user clicks replay",
        "write docs explaining how we drop duplicate rows in pandas",
        "the dropdown should remove focus on blur",
        "revert is a git command; explain what it does",
        "disable the newsletter checkbox by default",
        "downgrade the tooltip animation duration",
        "add a 'reset password' link to the login form",
    ]

    def test_benign_work_never_escalates_to_high_or_critical(self):
        for text in self.BENIGN:
            self.assertIn(
                infer_risk(text),
                (Risk.LOW, Risk.MEDIUM),
                f"benign task escalated to an expensive tier: {text!r}",
            )

    def test_describing_a_dangerous_operation_is_not_performing_it(self):
        self.assertEqual(infer_risk("document the production checklist"), Risk.LOW)
        self.assertEqual(
            infer_risk("write docs explaining how we drop duplicate rows in pandas"), Risk.LOW
        )

    def test_genuine_risk_still_escalates_including_plural_objects(self):
        for text, expected in [
            ("drop the production database", Risk.CRITICAL),
            ("drop the production databases", Risk.CRITICAL),
            ("drop the tables", Risk.CRITICAL),
            ("truncate table users", Risk.CRITICAL),
            ("rotate the production API credentials", Risk.CRITICAL),
            ("revoke user access tokens", Risk.CRITICAL),
            ("rm -rf /var/data", Risk.CRITICAL),
            ("deploy the payment service to production", Risk.HIGH),
            ("update the firewall rule", Risk.HIGH),
            ("delete the pods", Risk.MEDIUM),
            ("restart the pod", Risk.MEDIUM),
        ]:
            self.assertEqual(infer_risk(text), expected, text)

    def test_keyword_padding_cannot_hijack_the_category(self):
        from brain.router.classification import TaskCategory

        # classify() once ranked by raw match count, so repeating cheap keywords
        # outranked a precise rule and routed a real deploy to the lightest model.
        padded_deploy = (
            "deploy the service to kubernetes. summary summary summary recap rename "
            "reformat format lint whitespace one-liner brief overview tl;dr"
        )
        r = TaskClassifier().classify(padded_deploy)
        self.assertNotEqual(
            r.suggested_complexity,
            Complexity.FAST,
            "padding hijacked a deploy task onto the fastest tier",
        )
        self.assertEqual(r.category, TaskCategory.DEVOPS)

        padded_security = (
            "critical security auth vulnerability in token rotation. review the "
            "review the critique assess the evaluate the plan roadmap decompose"
        )
        r2 = TaskClassifier().classify(padded_security)
        self.assertIn(r2.suggested_complexity, (Complexity.STRONG, Complexity.REASONING))


if __name__ == "__main__":
    unittest.main()
