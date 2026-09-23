"""Verification Engine and Rollback Manager for Mission Control Phase 18.

Verifies step completion outcomes and provides safe compensations upon failure.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of step verification."""
    success: bool
    details: str
    checks_performed: List[str] = field(default_factory=list)


class VerificationEngine:
    """Engine verifying plan step completion before advancing the execution graph."""

    @staticmethod
    def verify_step(
        command_returncode: Optional[int] = None,
        output: Optional[str] = None,
        expected_files: Optional[List[str]] = None,
        rules: Optional[List[str]] = None
    ) -> VerificationResult:
        """Verify step execution output against success invariants."""
        checks = []

        # 1. Exit code check
        if command_returncode is not None:
            checks.append(f"exit_code == 0 (actual: {command_returncode})")
            if command_returncode != 0:
                return VerificationResult(
                    success=False,
                    details=f"Command exited with non-zero code {command_returncode}",
                    checks_performed=checks
                )

        # 2. File creation check
        if expected_files:
            for f in expected_files:
                p = Path(f)
                exists = p.exists()
                checks.append(f"file_exists({f}) == {exists}")
                if not exists:
                    return VerificationResult(
                        success=False,
                        details=f"Expected output file not found: {f}",
                        checks_performed=checks
                    )

        # 3. Rule checks
        if rules and output:
            out_lower = output.lower()
            for r in rules:
                checks.append(f"rule: {r}")
                if r == "no_errors" and ("error:" in out_lower or "fatal:" in out_lower or "exception:" in out_lower):
                    return VerificationResult(
                        success=False,
                        details=f"Output contained error signals violating rule 'no_errors'",
                        checks_performed=checks
                    )

        return VerificationResult(
            success=True,
            details="All verification checks passed.",
            checks_performed=checks
        )


class RollbackManager:
    """Manages and applies compensatory undo operations in reverse order upon failure."""

    def __init__(self) -> None:
        self._rollback_stack: List[Tuple[str, str]] = []  # (step_id, action_command)

    def register_rollback(self, step_id: str, rollback_action: str) -> None:
        """Register a rollback action for a step."""
        if rollback_action:
            self._rollback_stack.append((step_id, rollback_action))

    def execute_rollback(self) -> List[Dict[str, Any]]:
        """Execute registered rollback actions in LIFO order."""
        results = []
        while self._rollback_stack:
            step_id, action = self._rollback_stack.pop()
            try:
                # Execute action safely
                logger.info("Executing rollback for step %s: %s", step_id, action)
                # shell=True is intentional: rollback actions are shell command
                # lines from the in-process caller or a local plan, never task
                # text. ExecutionFabric.execute_step guardrail-checks them before
                # registering; Planner.execute_plan no longer runs any.
                proc = subprocess.run(  # nosec B602
                    action,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                results.append({
                    "step_id": step_id,
                    "action": action,
                    "success": proc.returncode == 0,
                    "stdout": proc.stdout[:200],
                    "stderr": proc.stderr[:200]
                })
            except Exception as e:
                logger.error("Rollback failed for step %s: %s", step_id, e)
                results.append({
                    "step_id": step_id,
                    "action": action,
                    "success": False,
                    "error": str(e)
                })
        return results
