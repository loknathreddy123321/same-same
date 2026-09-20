from typing import Optional, Dict, List, Any, Tuple
"""
src/handlers/approval_callback.py
----------------------------------
API Gateway Lambda — receives Slack Approve/Reject button clicks.

Flow:
  Slack button → API Gateway → this Lambda
    → records decision in DynamoDB
    → calls sfn.send_task_success() or send_task_failure()
    → Step Functions resumes the waiting execution
    → returns 200 with a confirmation page
"""


import json
import logging
import os
import sys
import urllib.parse

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.storage import write_approval_decision

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """
    Handles Slack button callback via API Gateway GET request.
    Query params: action=APPROVE|REJECT, record_id=xxx, task_token=xxx
    """
    logger.info(f"Approval callback: {json.dumps(event, default=str)}")

    params    = event.get("queryStringParameters") or {}
    action    = params.get("action", "").upper()
    record_id = params.get("record_id", "")
    # URL-decode the task token (it may contain + and = characters)
    task_token = urllib.parse.unquote(params.get("task_token", ""))
    approver   = params.get("approver") or _extract_user_from_slack(event) or "Unknown"

    if not action or action not in ("APPROVE", "REJECT"):
        return _html_response(400, "❌ Invalid action. Must be APPROVE or REJECT.")

    if not record_id:
        return _html_response(400, "❌ Missing record_id.")

    region     = os.environ.get("AWS_REGION", "us-east-1")
    table_name = os.environ.get("DYNAMODB_APPROVAL_TABLE", "cfn-drift-approvals")

    try:
        # 1. Persist decision to DynamoDB (audit trail)
        write_approval_decision(
            table_name=table_name,
            record_id=record_id,
            decision=action,
            approver=approver,
            region=region,
        )

        # 2. Resume Step Functions execution via task token
        if task_token:
            _resume_step_functions(task_token, action, approver, region)
        else:
            logger.warning("No task_token provided — Step Functions will NOT be resumed")

        icon = "✅" if action == "APPROVE" else "🚫"
        msg  = "approved" if action == "APPROVE" else "rejected"
        return _html_response(
            200,
            f"{icon} You have <strong>{msg}</strong> the drift fix.<br>"
            f"Record ID: <code>{record_id}</code><br>"
            f"The agent will proceed accordingly. You can close this tab."
        )

    except Exception as e:
        logger.exception(f"Approval callback error: {e}")
        return _html_response(500, f"❌ Server error: {e}")


def _resume_step_functions(
    task_token: str,
    action: str,
    approver: str,
    region: str,
) -> None:
    """
    Resumes the waiting Step Functions execution.
    APPROVE → send_task_success
    REJECT  → send_task_failure
    """
    sfn = boto3.client("stepfunctions", region_name=region)

    output = json.dumps({"approval_status": action, "approver": approver})

    if action == "APPROVE":
        sfn.send_task_success(taskToken=task_token, output=output)
        logger.info(f"Step Functions resumed with APPROVE by {approver}")
    else:
        sfn.send_task_failure(
            taskToken=task_token,
            error="REJECTED",
            cause=f"Drift fix rejected by {approver}",
        )
        logger.info(f"Step Functions failed with REJECT by {approver}")


def _extract_user_from_slack(event: dict) -> Optional[str]:
    """Tries to extract Slack username from body (Slack interactive payloads)."""
    try:
        body    = event.get("body", "")
        decoded = urllib.parse.unquote(body)
        if "payload=" in decoded:
            payload = json.loads(decoded.replace("payload=", ""))
            return payload.get("user", {}).get("name")
    except Exception:
        pass
    return None


def _html_response(status: int, message: str) -> dict:
    """Returns a simple HTML page — better UX than raw JSON in a browser."""
    body = f"""<!DOCTYPE html>
<html>
<head><title>CFN Drift Fixer</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
         align-items: center; height: 100vh; margin: 0; background: #f4f4f4; }}
  .card {{ background: white; padding: 40px; border-radius: 12px; text-align: center;
           box-shadow: 0 4px 20px rgba(0,0,0,0.1); max-width: 480px; }}
  h2 {{ color: #1a1a2e; }}
  p {{ color: #555; line-height: 1.6; }}
</style>
</head>
<body>
  <div class="card">
    <h2>CFN Drift Fixer</h2>
    <p>{message}</p>
  </div>
</body>
</html>"""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html"},
        "body": body,
    }
