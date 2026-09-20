"""
src/handlers/drift_scanner.py
------------------------------
Lambda handler for drift detection and agent execution.

Triggered by:
  - EventBridge schedule (every N hours)
  - EventBridge custom event (on-demand)
  - Direct Lambda invocation (testing)

Step Functions injects task_token when using .waitForTaskToken pattern.
"""


import json
import logging
import os
import sys

# Ensure src/ is on the path when running in Lambda
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph import run_agent

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """
    Entry point for the drift scanner Lambda.

    Supported event shapes:
      {"stack_name": "my-stack"}
      {"stack_name": "my-stack", "dry_run": true}
      {"stacks": ["stack-a", "stack-b"]}
      {"source": "aws.events"}  → reads stack list from SSM
    """
    logger.info(f"Event: {json.dumps(event, default=str)}")

    region  = os.environ.get("AWS_REGION", "us-east-1")
    dry_run = event.get("dry_run", os.environ.get("DRY_RUN", "false").lower() == "true")

    # ── Single stack ─────────────────────────────────────────────────────────
    if "stack_name" in event:
        result = run_agent(
            stack_name=event["stack_name"],
            aws_region=region,
            dry_run=dry_run,
            task_token=event.get("task_token"),
        )
        return _format_response(event["stack_name"], result)

    # ── Multi-stack (scheduled scan) ─────────────────────────────────────────
    stacks = event.get("stacks") or _get_stacks_from_ssm(region)
    if not stacks:
        logger.warning("No stacks to scan")
        return {"statusCode": 200, "body": "No stacks configured"}

    results = []
    for stack_name in stacks:
        try:
            result = run_agent(stack_name=stack_name, aws_region=region, dry_run=dry_run)
            results.append(_format_response(stack_name, result))
        except Exception as e:
            logger.error(f"Agent failed for {stack_name}: {e}")
            results.append({"stack_name": stack_name, "error": str(e)})

    return {"statusCode": 200, "body": json.dumps(results, default=str)}


def _format_response(stack_name: str, state: dict) -> dict:
    return {
        "statusCode":      200,
        "stack_name":      stack_name,
        "fix_status":      state.get("fix_status"),
        "total_drifted":   state.get("total_drifted", 0),
        "risk_level":      state.get("risk_level"),
        "validation":      state.get("validation_passed"),
        "audit_record_id": state.get("audit_record_id"),
        "error":           state.get("error_message"),
    }


def _get_stacks_from_ssm(region: str) -> list:
    import boto3
    try:
        ssm   = boto3.client("ssm", region_name=region)
        param = ssm.get_parameter(Name="/cfn-drift-fixer/monitored-stacks")
        return json.loads(param["Parameter"]["Value"])
    except Exception as e:
        logger.warning(f"Could not read stacks from SSM: {e}")
        return []
