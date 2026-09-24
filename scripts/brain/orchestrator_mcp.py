#!/usr/bin/env python3
"""Local FastMCP surface for approval-gated Shared Brain orchestration.

All tools are local and safe by construction.  They can plan, record an
in-memory explicit approval, and issue non-executing validation receipts.  They
never invoke agents, subprocesses, cloud APIs, or lifecycle mutations.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import date
from pathlib import Path

from fastmcp import FastMCP

try:
    from .agent_adapters import AGENT_CAPABILITIES, probe_availability
    from .execution import Approval, ApprovalRequiredError, VerificationReceipt, evaluate_execution, issue_approval
    from .lifecycle import build_archive_plan
    from .orchestrator import TaskPlan, plan_text
except ImportError:
    from agent_adapters import AGENT_CAPABILITIES, probe_availability
    from execution import Approval, ApprovalRequiredError, VerificationReceipt, evaluate_execution, issue_approval
    from lifecycle import build_archive_plan
    from orchestrator import TaskPlan, plan_text

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()
mcp = FastMCP(
    "Brain Orchestrator",
    version="1.0.0",
    instructions="Plan and validate Shared Brain work locally. Execution is intentionally disabled; approvals only authorize a dry-run validation receipt.",
)
_plans: dict[str, TaskPlan] = {}
_approvals: dict[str, Approval] = {}
_receipts: dict[str, VerificationReceipt] = {}


def plan_task(task: str) -> dict:
    plan = plan_text(task)
    _plans[plan.plan_hash] = plan
    return plan.to_dict()


def capability_report() -> list[dict]:
    result = []
    for capability in AGENT_CAPABILITIES:
        availability = probe_availability(capability.name)
        result.append({
            "name": capability.name.value,
            "capabilities": sorted(item.value for item in capability.capabilities),
            "allowed_action_risks": sorted(f"{item.action.value}:{item.risk.value}" for item in capability.allowed_action_risks),
            "available": availability.available,
            "availability_reason": availability.reason,
            "execution_mode": capability.execution_mode.value,
        })
    return result


def approve_plan(plan_hash: str, approved_by: str, explicit_confirmation: bool) -> dict:
    if plan_hash not in _plans:
        return {"status": "blocked", "reason": "plan hash is unknown to this local MCP session"}
    try:
        approval = issue_approval(plan_hash, approved_by, explicit_confirmation=explicit_confirmation)
    except ApprovalRequiredError as error:
        return {"status": "blocked", "reason": str(error)}
    _approvals[approval.id] = approval
    return {"status": "approved-for-dry-run", "approval_id": approval.id, "plan_hash": approval.plan_hash, "expires_at": approval.expires_at}


def execute_plan(plan_hash: str, approval_id: str, dry_run: bool = True) -> dict:
    if dry_run is not True:
        return {"status": "blocked", "executed": False, "reason": "actual execution is disabled in this phase; only dry_run=True is accepted"}
    plan = _plans.get(plan_hash)
    approval = _approvals.get(approval_id)
    if plan is None:
        return {"status": "blocked", "executed": False, "reason": "plan hash is unknown to this local MCP session"}
    receipt = evaluate_execution(plan, approval)
    _receipts[receipt.id] = receipt
    return receipt.to_dict()


def lifecycle_plan(slug: str) -> dict:
    current = BRAIN_DIR / "handoff" / "current.md"
    if not current.exists():
        return {"status": "blocked", "reason": "handoff/current.md does not exist"}
    return build_archive_plan(slug, current.read_bytes(), date.today()).to_dict()


@mcp.tool(name="brain_plan_task", description="Analyze an explicit task list and return a plan-only agent/model/risk plan. No work is queued or executed.")
def brain_plan_task(task: str) -> dict:
    return plan_task(task)


@mcp.tool(name="brain_agent_capabilities", description="Return closed local adapter capabilities and passive executable availability. No adapter is started.")
def brain_agent_capabilities() -> list[dict]:
    return capability_report()


@mcp.tool(name="brain_approve_plan", description="Record an in-memory explicit approval for an already planned hash. Approval authorizes dry-run validation only.")
def brain_approve_plan(plan_hash: str, approved_by: str, explicit_confirmation: bool = False) -> dict:
    return approve_plan(plan_hash, approved_by, explicit_confirmation)


@mcp.tool(name="brain_execute_plan", description="Validate an approved plan and return a verification receipt. Actual task execution is disabled; dry_run must be true.")
def brain_execute_plan(plan_hash: str, approval_id: str, dry_run: bool = True) -> dict:
    return execute_plan(plan_hash, approval_id, dry_run)


@mcp.tool(name="brain_execution_receipt", description="Retrieve an in-memory verification receipt from a prior dry-run validation.")
def brain_execution_receipt(receipt_id: str) -> dict:
    receipt = _receipts.get(receipt_id)
    return receipt.to_dict() if receipt else {"status": "not_found", "reason": "receipt id is unknown to this local MCP session"}


@mcp.tool(name="brain_lifecycle_plan", description="Create a non-mutating archive plan for the current handoff. It never moves, archives, quarantines, or purges a note.")
def brain_lifecycle_plan(slug: str) -> dict:
    return lifecycle_plan(slug)


if __name__ == "__main__":
    mcp.run(transport="stdio")
