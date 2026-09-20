"""
src/notifications/slack.py
--------------------------
Slack integration for approval requests and result notifications.
"""

import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_RISK_EMOJI  = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}
_STATUS_ICON = {
    "VALIDATED": "✅", "APPLIED": "🔧", "FAILED": "❌",
    "SKIPPED":   "⏭️", "REJECTED": "🚫", "NO_DRIFT": "✔️",
}


def _post(blocks: list, text: str) -> Optional[str]:
    token   = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel:
        logger.warning("SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set — skipping")
        return None
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "blocks": blocks, "text": text},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"Slack API error: {data.get('error')}")
        return None
    return data.get("ts")


def send_approval_request(
    stack_name: str,
    risk_level: Optional[str],
    drifted_resources: list,
    root_cause: str,
    remediation_plan: dict,
    audit_record_id: str,
    task_token: Optional[str] = None,
    change_set_diff: Optional[str] = None,      # FIX: added parameter
    dual_approval_required: bool = False,        # FIX: added parameter
) -> Optional[str]:
    """
    Posts approval request with full drift details to Slack.
    Includes: drifted resources, root cause, change set diff, rollback info.
    """
    callback_base = os.environ.get("APPROVAL_CALLBACK_URL", "")
    emoji = _RISK_EMOJI.get(risk_level or "", "⚠️")

    resource_lines = "\n".join(
        f"• `{r['resource_type']}` — `{r.get('logical_id', r.get('resource_id', ''))}` ({r['drift_status']})"
        for r in drifted_resources[:8]
    )
    if len(drifted_resources) > 8:
        resource_lines += f"\n• _...and {len(drifted_resources) - 8} more_"

    approve_url = f"{callback_base}?action=APPROVE&record_id={audit_record_id}&task_token={task_token}"
    reject_url  = f"{callback_base}?action=REJECT&record_id={audit_record_id}&task_token={task_token}"

    dual_note = "\n⚠️ *HIGH RISK: Requires 2 approvers*" if dual_approval_required else ""

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} CFN Drift — Approval Required"}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Stack:*\n`{stack_name}`"},
                {"type": "mrkdwn", "text": f"*Risk:*\n{emoji} `{risk_level}`"},
                {"type": "mrkdwn", "text": f"*Resources Drifted:*\n`{len(drifted_resources)}`"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n`{remediation_plan.get('strategy', 'CHANGE_SET')}`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Drifted Resources:*\n{resource_lines}"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root Cause:*\n{root_cause[:400]}"}
        },
    ]

    # Add change set diff if available
    if change_set_diff:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Change Set Diff (what will change):*\n```{change_set_diff[:500]}```"}
        })

    # Add rollback info
    rollback = remediation_plan.get("rollback_procedure", "N/A")
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Rollback Plan:*\n{rollback[:300]}{dual_note}"}
    })

    # Approve/Reject buttons
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type":      "button",
                "text":      {"type": "plain_text", "text": "✅ Approve Fix"},
                "style":     "primary",
                "action_id": "approve",
                "url":       approve_url,
            },
            {
                "type":      "button",
                "text":      {"type": "plain_text", "text": "🚫 Reject"},
                "style":     "danger",
                "action_id": "reject",
                "url":       reject_url,
            },
        ],
    })

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"Audit ID: `{audit_record_id}` | Auto-rejected after 30 min if no response"}
        ],
    })

    return _post(blocks, f"{emoji} Drift fix approval needed for `{stack_name}` — Risk: {risk_level}")


def send_result_notification(
    stack_name: str,
    fix_status: Optional[str],
    validation_passed: Optional[bool],
    risk_level: Optional[str],
    total_drifted: int,
    approver: Optional[str],
    error_message: Optional[str],
    audit_record_id: str,
) -> None:
    icon    = _STATUS_ICON.get(fix_status or "", "❓")
    val_txt = "✅ Clean" if validation_passed else ("❌ Still drifted" if validation_passed is False else "—")
    appr    = f" | Approver: `{approver}`" if approver else ""
    err     = f"\n⚠️ Error: _{error_message[:200]}_" if error_message else ""

    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"{icon} *CFN Drift Fix Complete* — `{stack_name}`\n"
                f"Status: `{fix_status}` | Risk: `{risk_level}` | Drifted: `{total_drifted}`\n"
                f"Validation: {val_txt}{appr}\n"
                f"Audit ID: `{audit_record_id}`{err}"
            ),
        },
    }]

    _post(blocks, f"{icon} Drift fix complete for {stack_name} — {fix_status}")
