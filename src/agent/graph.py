"""
src/agent/graph.py
------------------
LangGraph state graph.

Flow:
  detect_drift
      ↓ (no drift → audit)
  analyze_drift
      ↓
  classify_risk
      ↓
  plan_remediation  ← builds synced template + change set
      ↓ (plan failed → audit)
  human_gate        ← sends ALL details to Slack, waits for approval
      ↓ (approved → fix | rejected → audit)
  apply_fix         ← executes approved change set
      ↓
  validate          ← confirms drift is gone
      ↓
  audit_and_notify  ← DynamoDB + Mail result
"""

import logging
from typing import Optional
from langgraph.graph import StateGraph, END

from agent.state import DriftFixState, FixStatus
from agent.nodes import (
    node_detect_drift,
    node_safety_gate,
    node_analyze_drift,
    node_classify_risk,
    node_plan_remediation,
    node_human_gate,
    node_apply_fix,
    node_validate,
    node_audit_and_notify,
)

logger = logging.getLogger(__name__)


def _route_post_detect(state: DriftFixState) -> str:
    if state.get("error_message") or state.get("fix_status") == FixStatus.FAILED:
        return "audit_and_notify"
    if state.get("total_drifted", 0) == 0:
        return "audit_and_notify"
    return "safety_gate"


def _route_post_safety_gate(state: DriftFixState) -> str:
    if state.get("fix_status") == FixStatus.SKIPPED:
        return "audit_and_notify"
    return "analyze_drift"


def _route_post_plan(state: DriftFixState) -> str:
    if state.get("fix_status") == FixStatus.FAILED:
        return "audit_and_notify"
    return "human_gate"  # Always go through human gate


def _route_post_gate(state: DriftFixState) -> str:
    if state.get("approval_status") == FixStatus.APPROVED:
        return "apply_fix"
    return "audit_and_notify"


def _route_post_fix(state: DriftFixState) -> str:
    if state.get("fix_applied"):
        return "validate"
    return "audit_and_notify"


def build_graph() -> StateGraph:
    g = StateGraph(DriftFixState)

    g.add_node("detect_drift",     node_detect_drift)
    g.add_node("safety_gate",      node_safety_gate)
    g.add_node("analyze_drift",    node_analyze_drift)
    g.add_node("classify_risk",    node_classify_risk)
    g.add_node("plan_remediation", node_plan_remediation)
    g.add_node("human_gate",       node_human_gate)
    g.add_node("apply_fix",        node_apply_fix)
    g.add_node("validate",         node_validate)
    g.add_node("audit_and_notify", node_audit_and_notify)

    g.set_entry_point("detect_drift")

    g.add_conditional_edges(
        "detect_drift", _route_post_detect,
        {"safety_gate": "safety_gate", "audit_and_notify": "audit_and_notify"},
    )
    g.add_conditional_edges(
        "safety_gate", _route_post_safety_gate,
        {"analyze_drift": "analyze_drift", "audit_and_notify": "audit_and_notify"},
    )
    g.add_conditional_edges(
        "plan_remediation", _route_post_plan,
        {"human_gate": "human_gate", "audit_and_notify": "audit_and_notify"},
    )
    g.add_conditional_edges(
        "human_gate", _route_post_gate,
        {"apply_fix": "apply_fix", "audit_and_notify": "audit_and_notify"},
    )
    g.add_conditional_edges(
        "apply_fix", _route_post_fix,
        {"validate": "validate", "audit_and_notify": "audit_and_notify"},
    )

    g.add_edge("analyze_drift",    "classify_risk")
    g.add_edge("classify_risk",    "plan_remediation")
    g.add_edge("validate",         "audit_and_notify")
    g.add_edge("audit_and_notify", END)

    return g.compile()


def run_agent(
    stack_name: str,
    aws_region: str = "us-east-1",
    dry_run: bool = False,
    task_token: Optional[str] = None,
    approval_status: Optional[str] = None,
    approver: Optional[str] = None,
) -> DriftFixState:
    graph = build_graph()

    initial_state = DriftFixState(
        stack_name=stack_name,
        aws_region=aws_region,
        dry_run=dry_run,
        task_token=task_token,
        stack_id=None,
        detection_id=None,
        drifted_resources=[],
        total_drifted=0,
        detection_timestamp=None,
        root_cause=None,
        impact=None,
        affected_services=[],
        llm_recommendation=None,
        risk_level=None,
        risk_reasoning=None,
        approval_required=False,
        remediation_plan=None,
        template_body=None,
        synced_template=None,
        template_snapshot_s3=None,
        change_set_name=None,
        change_set_id=None,
        change_set_diff=None,
        approval_status=approval_status,
        approver=approver,
        fix_applied=False,
        fix_timestamp=None,
        validation_passed=None,
        post_fix_drift_count=None,
        fix_status=None,
        audit_record_id=None,
        error_message=None,
        execution_trace=[],
    )

    logger.info(f"Agent starting | stack={stack_name} | dry_run={dry_run}")
    final = graph.invoke(initial_state)
    logger.info(f"Agent complete | status={final.get('fix_status')}")
    return final
