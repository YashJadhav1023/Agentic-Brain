"""Autonomous Task Planner and Execution Controller for Mission Control Phase 18.

Orchestrates multi-step planning, capability matching, approval gates,
and DAG execution with rollback support.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.planner.plan_model import ExecutionGraph, Plan, PlanStatus, PlanStep, StepStatus
from brain.planner.task_decomposer import TaskDecomposer
from brain.planner.verification_engine import RollbackManager, VerificationEngine
from brain.resources.resource_registry import ResourceRegistry
from brain.router.classification import TaskClassifier
from brain.router.smart_router import SmartRouter
from providers.registry.bootstrap import create_default_registry

logger = logging.getLogger(__name__)


class Planner:
    """Universal Task Planner generating inspectable, verifiable execution plans."""

    def __init__(
        self,
        resource_registry: Optional[ResourceRegistry] = None,
        router: Optional[SmartRouter] = None,
        storage_dir: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.resource_registry = resource_registry or ResourceRegistry()
        self.prov_registry = create_default_registry()
        self.router = router or SmartRouter(self.prov_registry)
        self.decomposer = TaskDecomposer(self.resource_registry)
        self.classifier = TaskClassifier()
        self.storage_dir = storage_dir or Path("runtime/plans")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._plans: Dict[str, Plan] = {}
        self._load_persisted_plans()

    def _load_persisted_plans(self) -> None:
        try:
            for p_file in self.storage_dir.glob("*.json"):
                with open(p_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                plan_id = data.get("plan_id")
                if plan_id:
                    steps = []
                    for s in data.get("steps", []):
                        steps.append(PlanStep(
                            step_id=s["step_id"],
                            title=s["title"],
                            description=s.get("description", ""),
                            dependencies=s.get("dependencies", []),
                            required_capabilities=s.get("required_capabilities", []),
                            agent=s.get("agent", "antigravity"),
                            model=s.get("model", "auto"),
                            provider=s.get("provider", "auto"),
                            account=s.get("account", "auto"),
                            tool=s.get("tool"),
                            mcp_server=s.get("mcp_server"),
                            command=s.get("command"),
                            risk_level=s.get("risk_level", "READ_ONLY"),
                            requires_approval=s.get("requires_approval", False),
                            approved=s.get("approved", False),
                            status=StepStatus(s.get("status", "PENDING")),
                            output=s.get("output"),
                            error=s.get("error"),
                            verification_rules=s.get("verification_rules", []),
                            rollback_action=s.get("rollback_action"),
                            duration_seconds=s.get("duration_seconds", 0.0),
                        ))
                    self._plans[plan_id] = Plan(
                        plan_id=plan_id,
                        task=data.get("task", ""),
                        domain=data.get("domain", "General"),
                        steps=steps,
                        status=PlanStatus(data.get("status", "DRAFT")),
                        created_at=data.get("created_at", time.time()),
                        is_dry_run=data.get("is_dry_run", False),
                        requires_approval=data.get("requires_approval", False),
                        metadata=data.get("metadata", {})
                    )
        except Exception as e:
            logger.debug("Failed loading persisted plans: %s", e)

    def _persist_plan(self, plan: Plan) -> None:
        try:
            p_file = self.storage_dir / f"{plan.plan_id}.json"
            with open(p_file, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, indent=2)
        except Exception as e:
            logger.debug("Failed persisting plan %s: %s", plan.plan_id, e)

    def create_plan(self, task: str, dry_run: bool = False) -> Plan:
        """Generate a full Plan with dependency-linked steps and safety classification."""
        with self._lock:
            # 1. Classification
            cls_res = self.classifier.classify(task)
            domain = cls_res.category.value

            # 2. Decompose into PlanSteps
            steps = self.decomposer.decompose(task)

            # 3. Route and attach capabilities to each step. The decomposer's
            # agent is only a hint: pinning it made plan creation crash with
            # "Requested agent 'antigravity' is not registered" on any machine
            # without that agent (a supported fresh install). Fall back to open
            # routing, and if no agent is available at all keep the step
            # unrouted rather than failing the whole plan.
            routing_warnings: List[str] = []
            for s in steps:
                text = s.description or s.title
                try:
                    decision = self.router.route(text, preferred_agent=s.agent)
                except RuntimeError as pinned_err:
                    try:
                        decision = self.router.route(text)
                    except RuntimeError as open_err:
                        routing_warnings.append(f"{s.step_id}: {open_err}")
                        continue
                    routing_warnings.append(f"{s.step_id}: {pinned_err}; re-routed to {decision.agent_id}")
                s.agent = decision.agent_id
                s.account = decision.account_id
                s.provider = decision.provider_id
                s.model = decision.model

            # 4. Check if any step requires human approval
            has_approval_gate = any(s.requires_approval for s in steps)
            initial_status = PlanStatus.BLOCKED_ON_APPROVAL if has_approval_gate else PlanStatus.READY

            # 5. Generate deterministic Plan ID
            plan_hash = hashlib.sha256(f"{task}:{time.time()}".encode("utf-8")).hexdigest()[:8]
            plan_id = f"plan-{plan_hash}"

            plan = Plan(
                plan_id=plan_id,
                task=task,
                domain=domain,
                steps=steps,
                status=initial_status,
                created_at=time.time(),
                is_dry_run=dry_run,
                requires_approval=has_approval_gate,
                metadata={
                    "complexity": cls_res.suggested_complexity.value,
                    "keywords": cls_res.keywords_matched,
                    **({"routing_warnings": routing_warnings} if routing_warnings else {}),
                }
            )

            self._plans[plan_id] = plan
            self._persist_plan(plan)
            return plan

    def approve_plan(self, plan_id: str) -> bool:
        """Grant user approval for high-risk or destructive steps in a plan."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                return False

            for s in plan.steps:
                if s.requires_approval:
                    s.approved = True
                    s.status = StepStatus.PENDING

            plan.status = PlanStatus.READY
            plan.requires_approval = False
            self._persist_plan(plan)
            return True

    def execute_plan(self, plan_id: str, auto_approve_safe: bool = True) -> Dict[str, Any]:
        """Execute a plan following its DAG dependencies with verification and rollback."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                return {"error": f"Plan {plan_id} not found."}

            if plan.requires_approval:
                return {
                    "status": "BLOCKED_ON_APPROVAL",
                    "plan_id": plan.plan_id,
                    "message": "Plan requires explicit approval before execution. Use: brain plan approve <id>"
                }

            if plan.is_dry_run:
                return {
                    "status": "DRY_RUN_COMPLETED",
                    "plan_id": plan.plan_id,
                    "steps_preview": [s.to_dict() for s in plan.steps]
                }

            plan.status = PlanStatus.IN_PROGRESS
            graph = ExecutionGraph(plan.steps)
            topological = graph.get_topological_order()
            rollback_mgr = RollbackManager()
            executed_steps = []

            for step in topological:
                if step.requires_approval and not step.approved:
                    step.status = StepStatus.APPROVAL_REQUIRED
                    plan.status = PlanStatus.BLOCKED_ON_APPROVAL
                    return {
                        "status": "BLOCKED_ON_APPROVAL",
                        "blocked_step": step.step_id,
                        "plan_id": plan.plan_id
                    }

                step.status = StepStatus.RUNNING
                t0 = time.time()

                # Rollbacks are deliberately NOT registered here. Steps in this
                # path are simulated (nothing is executed), so there is nothing to
                # compensate, while running a step's rollback_action (the decomposer
                # emits "git checkout .") would discard the user's uncommitted work
                # in the process CWD. A failed verification on a simulated step --
                # e.g. a task title containing "fatal:" tripping "no_errors" --
                # must never do that. Real rollbacks are applied by
                # ExecutionFabric.execute_step, which actually runs step.command.

                # Simulated safe step execution (or dispatch to agent)
                # In Phase 18, steps complete with simulated execution and verification
                step_output = f"Executed step '{step.title}' via agent {step.agent} ({step.model})"
                step_err = None
                returncode = 0

                # Verify
                v_res = VerificationEngine.verify_step(
                    command_returncode=returncode,
                    output=step_output,
                    rules=step.verification_rules
                )

                step.duration_seconds = round(time.time() - t0, 3)
                if v_res.success:
                    step.status = StepStatus.COMPLETED
                    step.output = step_output
                    executed_steps.append(step.step_id)
                else:
                    step.status = StepStatus.FAILED
                    step.error = v_res.details
                    plan.status = PlanStatus.FAILED

                    # Trigger Rollback
                    rollback_results = rollback_mgr.execute_rollback()
                    return {
                        "status": "FAILED",
                        "failed_step": step.step_id,
                        "error": v_res.details,
                        "rollback_results": rollback_results,
                        "plan_id": plan.plan_id
                    }

            plan.status = PlanStatus.COMPLETED
            self._persist_plan(plan)
            return {
                "status": "COMPLETED",
                "plan_id": plan.plan_id,
                "executed_steps": executed_steps,
                "total_steps": len(plan.steps)
            }

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        """Retrieve plan by ID."""
        with self._lock:
            return self._plans.get(plan_id)

    def list_plans(self) -> List[Plan]:
        """List all tracked plans."""
        with self._lock:
            return list(self._plans.values())
