"""
src/agent/state.py
------------------
Single source of truth for agent state.
Every node receives this and returns a partial update.
"""

from enum import Enum
from typing import TypedDict, Optional, List, Dict, Any


class RiskLevel(str, Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


class FixStatus(str, Enum):
    PENDING   = "PENDING"
    APPROVED  = "APPROVED"
    REJECTED  = "REJECTED"
    APPLIED   = "APPLIED"
    FAILED    = "FAILED"
    VALIDATED = "VALIDATED"
    SKIPPED   = "SKIPPED"
    NO_DRIFT  = "NO_DRIFT"


class DriftFixState(TypedDict):
    # ── Input ─────────────────────────────────
    stack_name:              str
    aws_region:              str
    dry_run:                 bool

    # ── Detection ─────────────────────────────
    stack_id:                Optional[str]
    detection_id:            Optional[str]
    drifted_resources:       List[Dict[str, Any]]
    total_drifted:           int
    detection_timestamp:     Optional[str]

    # ── LLM Analysis ──────────────────────────
    root_cause:              Optional[str]
    impact:                  Optional[str]
    affected_services:       List[str]
    llm_recommendation:      Optional[str]

    # ── Risk ──────────────────────────────────
    risk_level:              Optional[str]
    risk_reasoning:          Optional[str]
    approval_required:       bool

    # ── Remediation ───────────────────────────
    remediation_plan:        Optional[Dict]
    template_body:           Optional[str]   # original template
    synced_template:         Optional[str]   # template merged with actual state
    template_snapshot_s3:    Optional[str]   # S3 URI of backup before fix

    # ── Change Set ────────────────────────────
    change_set_name:         Optional[str]
    change_set_id:           Optional[str]
    change_set_diff:         Optional[str]   # human-readable diff for Slack

    # ── Human Gate ────────────────────────────
    task_token:              Optional[str]
    approval_status:         Optional[str]
    approver:                Optional[str]

    # ── Fix ───────────────────────────────────
    fix_applied:             bool
    fix_timestamp:           Optional[str]

    # ── Validation ────────────────────────────
    validation_passed:       Optional[bool]
    post_fix_drift_count:    Optional[int]

    # ── Audit ─────────────────────────────────
    fix_status:              Optional[str]
    audit_record_id:         Optional[str]
    error_message:           Optional[str]
    execution_trace:         List[str]
