"""
src/agent/cfn.py
----------------
All CloudFormation boto3 operations.
- Retries with exponential backoff (tenacity)
- get_stack_template handles BOTH string and dict responses
- execute_change_set takes explicit stack_name (no ARN parsing)
- describe_stack_resource_drifts uses direct call (not paginator)
- sync_template_with_drift: auto-syncs template with actual resource state
"""

import json
import time
import logging
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, WaiterError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

_RETRYABLE = (ClientError,)

BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=60,
)


def _cfn(region: str):
    return boto3.client("cloudformation", region_name=region, config=BOTO_CONFIG)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def trigger_drift_detection(stack_name: str, region: str) -> str:
    client = _cfn(region)
    resp = client.detect_stack_drift(StackName=stack_name)
    detection_id = resp["StackDriftDetectionId"]
    logger.info(f"Drift detection started: {detection_id}")
    return detection_id


def wait_for_detection_complete(
    stack_name: str,
    detection_id: str,
    region: str,
    max_wait_seconds: int = 240,
    poll_interval: int = 3,
) -> str:
    client  = _cfn(region)
    elapsed = 0
    while elapsed < max_wait_seconds:
        try:
            resp   = client.describe_stack_drift_detection_status(
                StackDriftDetectionId=detection_id
            )
            status = resp["DetectionStatus"]
            logger.info(f"Detection status after {elapsed}s: {status}")
            if status in ("DETECTION_COMPLETE", "DETECTION_FAILED"):
                return status
        except ClientError as e:
            logger.warning(f"Poll error (will retry): {e}")
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Drift detection timed out after {max_wait_seconds}s for: {stack_name}")


def get_drifted_resources(stack_name: str, region: str) -> List[Dict[str, Any]]:
    """
    FIX: describe_stack_resource_drifts does NOT support pagination.
    Use direct call instead of get_paginator().
    """
    client  = _cfn(region)
    drifted = []

    resp = client.describe_stack_resource_drifts(
        StackName=stack_name,
        StackResourceDriftStatusFilters=["MODIFIED", "DELETED"],
    )

    for r in resp.get("StackResourceDrifts", []):
        drifted.append({
            "logical_id":           r.get("LogicalResourceId", ""),
            "resource_id":          r.get("PhysicalResourceId", ""),
            "resource_type":        r.get("ResourceType", ""),
            "drift_status":         r.get("StackResourceDriftStatus", ""),
            "expected_properties":  _parse_props(r.get("ExpectedProperties", "{}")),
            "actual_properties":    _parse_props(r.get("ActualProperties", "{}")),
            "property_differences": r.get("PropertyDifferences", []),
            "timestamp":            str(r.get("Timestamp", "")),
        })

    logger.info(f"Found {len(drifted)} drifted resource(s) in {stack_name}")
    return drifted


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)
def get_stack_id(stack_name: str, region: str) -> str:
    client = _cfn(region)
    resp   = client.describe_stacks(StackName=stack_name)
    return resp["Stacks"][0]["StackId"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)
def get_stack_template(stack_name: str, region: str) -> str:
    """FIX: CFN returns TemplateBody as EITHER string OR dict. Handle both."""
    client = _cfn(region)
    resp   = client.get_template(StackName=stack_name, TemplateStage="Original")
    body   = resp["TemplateBody"]
    if isinstance(body, dict):
        return json.dumps(body, indent=2)
    return body


def create_change_set(
    stack_name: str,
    template_body: str,
    region: str,
    change_set_name: str,
    dry_run: bool = False,
) -> Optional[str]:
    if dry_run:
        logger.info("DRY RUN — skipping change set creation")
        return None
    client = _cfn(region)
    try:
        resp = client.create_change_set(
            StackName=stack_name,
            TemplateBody=template_body,
            ChangeSetName=change_set_name,
            ChangeSetType="UPDATE",
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            Description="Automated drift remediation — CFN Drift Fixer Agent",
        )
        change_set_id = resp["Id"]
        logger.info(f"Change set created: {change_set_id}")
        _wait_change_set_ready(stack_name, change_set_name, region)
        return change_set_id
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            logger.info("Stack already in sync — no changes needed")
            return None
        raise


def execute_change_set(
    stack_name: str,
    change_set_name: str,
    region: str,
    dry_run: bool = False,
) -> bool:
    if dry_run:
        logger.info("DRY RUN — skipping change set execution")
        return True
    client = _cfn(region)
    try:
        client.execute_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        logger.info(f"Executing change set: {change_set_name}")
        waiter = client.get_waiter("stack_update_complete")
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        logger.info(f"Stack update complete: {stack_name}")
        return True
    except WaiterError as e:
        raise RuntimeError(f"Stack update failed: {stack_name}") from e


def delete_change_set(stack_name: str, change_set_name: str, region: str) -> None:
    try:
        _cfn(region).delete_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        logger.info(f"Change set deleted: {change_set_name}")
    except ClientError as e:
        logger.warning(f"Could not delete change set: {e}")


def sync_template_with_drift(
    stack_name: str,
    drifted_resources: list,
    region: str,
    dry_run: bool = False,
) -> str:
    """
    AUTO-SYNC: Updates the CFN template to match actual resource state.
    This fixes drift by accepting the real-world state into the template.

    Use case: S3 tags added manually, Lambda env vars changed, etc.
    Instead of removing the changes — we accept them into the template.

    Returns the updated template body.
    """
    client        = _cfn(region)
    template_body = get_stack_template(stack_name, region)

    try:
        template = json.loads(template_body)
    except json.JSONDecodeError:
        logger.error("[SYNC] Template is not valid JSON — cannot auto-sync")
        raise

    resources = template.get("Resources", {})

    for drifted in drifted_resources:
        logical_id   = drifted.get("logical_id")
        actual_props = drifted.get("actual_properties", {})
        prop_diffs   = drifted.get("property_differences", [])

        if logical_id not in resources:
            logger.warning(f"[SYNC] {logical_id} not in template — skipping")
            continue

        current_props = resources[logical_id].get("Properties", {})
        logger.info(f"[SYNC] Syncing {logical_id} — {len(prop_diffs)} difference(s)")

        for diff in prop_diffs:
            path      = diff.get("PropertyPath", "")
            actual    = diff.get("ActualValue")
            diff_type = diff.get("DifferenceType", "")

            # ── Tags (most common drift) ────────────────────────────────────
            if "Tags" in path and diff_type in ("ADD", "NOT_EQUAL", "REMOVE"):
                try:
                    actual_tags = json.loads(actual) if isinstance(actual, str) else actual
                    if isinstance(actual_tags, list):
                        current_props["Tags"] = actual_tags
                        logger.info(f"[SYNC] Tags synced for {logical_id}: {actual_tags}")
                except Exception as e:
                    logger.warning(f"[SYNC] Could not parse tags: {e}")

            # ── Nested property (e.g. /VersioningConfiguration/Status) ─────
            elif "/" in path and diff_type in ("NOT_EQUAL", "ADD"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 1:
                    try:
                        current_props[parts[0]] = json.loads(actual)
                    except Exception:
                        current_props[parts[0]] = actual
                    logger.info(f"[SYNC] Property {parts[0]} synced for {logical_id}")
                elif len(parts) >= 2:
                    target = current_props
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    try:
                        target[parts[-1]] = json.loads(actual)
                    except Exception:
                        target[parts[-1]] = actual
                    logger.info(f"[SYNC] Nested property {path} synced for {logical_id}")

        resources[logical_id]["Properties"] = current_props

    updated_template = json.dumps(template, indent=2)
    logger.info(f"[SYNC] Template updated for {stack_name}")

    if not dry_run:
        try:
            client.update_stack(
                StackName=stack_name,
                TemplateBody=updated_template,
                Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            )
            logger.info(f"[SYNC] Stack update initiated: {stack_name}")

            waiter = client.get_waiter("stack_update_complete")
            waiter.wait(
                StackName=stack_name,
                WaiterConfig={"Delay": 15, "MaxAttempts": 40}
            )
            logger.info(f"[SYNC] Stack update complete ✅: {stack_name}")

        except ClientError as e:
            if "No updates are to be performed" in str(e):
                logger.info("[SYNC] Stack already in sync — no update needed")
            else:
                raise

    return updated_template


def _parse_props(value: Any) -> Dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _wait_change_set_ready(
    stack_name: str,
    change_set_name: str,
    region: str,
    max_wait: int = 120,
) -> None:
    client  = _cfn(region)
    elapsed = 0
    while elapsed < max_wait:
        resp   = client.describe_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        status = resp["Status"]
        reason = resp.get("StatusReason", "")
        if status == "CREATE_COMPLETE":
            return
        if status in ("FAILED", "DELETE_COMPLETE"):
            no_change = ("didn't contain changes", "No updates are to be performed")
            if any(m in reason for m in no_change):
                return
            raise RuntimeError(f"Change set failed: {reason}")
        time.sleep(3)
        elapsed += 3
    raise TimeoutError("Change set not ready")
