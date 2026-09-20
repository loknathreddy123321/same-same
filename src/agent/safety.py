"""
src/agent/safety.py
--------------------
All safety guardrails for the CFN Drift Fixer agent.

Measures implemented:
  1.  Pre-flight stack state check       — stack must be STABLE before any fix
  2.  Change set diff review             — show exact property changes before apply
  3.  Auto-rollback on validation fail   — restore from S3 snapshot if fix fails
  4.  Maintenance window enforcement     — only fix during approved hours
  5.  Emergency kill switch              — SSM flag halts all agent activity
  6.  Blast radius limiter              — max N stacks per run
  7.  Circuit breaker                   — halt after N consecutive failures
  8.  Resource exclusion list           — never touch specified resources
  9.  Dual approval for HIGH risk        — requires 2 approvers
  10. Change freeze detection            — respect freeze windows from SSM/DynamoDB
"""


import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"})


# ─────────────────────────────────────────────────────────────────────────────
# 1. PRE-FLIGHT STACK STATE CHECK
# A stack in any non-stable state must NEVER be touched.
# ─────────────────────────────────────────────────────────────────────────────

# These are the ONLY states where it is safe to apply a change set
STABLE_STACK_STATES = {
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
}

# These states mean someone else is already touching the stack
UNSTABLE_STACK_STATES = {
    "CREATE_IN_PROGRESS",
    "UPDATE_IN_PROGRESS",
    "DELETE_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "ROLLBACK_IN_PROGRESS",
    "REVIEW_IN_PROGRESS",
    "IMPORT_IN_PROGRESS",
}


def check_stack_is_stable(stack_name: str, region: str) -> Tuple[bool, str]:
    """
    Returns (is_stable, current_status).
    If not stable, caller must abort — never apply to an in-progress stack.
    """
    cfn = boto3.client("cloudformation", region_name=region, config=_CONFIG)
    try:
        resp   = cfn.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]

        if status in STABLE_STACK_STATES:
            logger.info(f"[SAFETY] Stack {stack_name} is STABLE: {status}")
            return True, status

        if status in UNSTABLE_STACK_STATES:
            logger.warning(f"[SAFETY] Stack {stack_name} is UNSTABLE: {status} — aborting")
            return False, status

        # Unknown state — treat as unsafe
        logger.warning(f"[SAFETY] Stack {stack_name} unknown state: {status} — treating as unsafe")
        return False, status

    except ClientError as e:
        logger.error(f"[SAFETY] Could not check stack state: {e}")
        return False, "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# 2. CHANGE SET DIFF REVIEW
# Extract human-readable diff from change set before execution.
# ─────────────────────────────────────────────────────────────────────────────

def get_change_set_diff(
    stack_name: str,
    change_set_name: str,
    region: str,
) -> List[Dict[str, Any]]:
    """
    Returns a list of human-readable changes from a change set.
    Include this in Slack approval messages so approvers see EXACTLY what changes.

    Each item: {logical_id, resource_type, action, replacement, details}
    """
    cfn     = boto3.client("cloudformation", region_name=region, config=_CONFIG)
    changes = []

    try:
        resp = cfn.describe_change_set(
            StackName=stack_name,
            ChangeSetName=change_set_name,
        )

        for change in resp.get("Changes", []):
            rc = change.get("ResourceChange", {})
            changes.append({
                "logical_id":    rc.get("LogicalResourceId", ""),
                "resource_type": rc.get("ResourceType", ""),
                "action":        rc.get("Action", ""),          # Add | Modify | Remove
                "replacement":   rc.get("Replacement", "False"), # True | False | Conditional
                "details":       [
                    {
                        "attribute":    d.get("Target", {}).get("Attribute", ""),
                        "name":         d.get("Target", {}).get("Name", ""),
                        "requires_recreate": d.get("Target", {}).get("RequiresRecreation", "Never"),
                        "cause":        d.get("ChangeSource", ""),
                    }
                    for d in rc.get("Details", [])
                ],
            })

    except ClientError as e:
        logger.error(f"[SAFETY] Could not get change set diff: {e}")

    return changes


def format_diff_for_slack(changes: List[Dict[str, Any]]) -> str:
    """Formats change set diff as readable Slack text."""
    if not changes:
        return "_No changes detected_"

    ACTION_EMOJI = {"Add": "➕", "Modify": "✏️", "Remove": "🗑️"}
    lines = []

    for c in changes[:10]:  # Cap at 10 to avoid Slack message limits
        emoji = ACTION_EMOJI.get(c["action"], "•")
        replacement_warn = " ⚠️ *REPLACEMENT*" if c["replacement"] == "True" else ""
        lines.append(
            f"{emoji} `{c['logical_id']}` ({c['resource_type']}) — "
            f"{c['action']}{replacement_warn}"
        )
        for d in c.get("details", [])[:3]:
            if d.get("name"):
                lines.append(f"   └ `{d['attribute']}.{d['name']}`")

    if len(changes) > 10:
        lines.append(f"_...and {len(changes) - 10} more changes_")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 3. AUTO-ROLLBACK ON VALIDATION FAILURE
# If post-fix drift check finds remaining drift, restore from S3 snapshot.
# ─────────────────────────────────────────────────────────────────────────────

def rollback_from_snapshot(
    stack_name: str,
    snapshot_s3_uri: str,
    region: str,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Restores a stack to its pre-fix state using the S3 template snapshot.
    Returns (success, message).
    """
    if dry_run:
        logger.info("[SAFETY] DRY RUN — skipping rollback")
        return True, "DRY RUN — rollback skipped"

    if not snapshot_s3_uri:
        return False, "No snapshot URI available — cannot rollback"

    try:
        # Parse S3 URI
        s3_path    = snapshot_s3_uri.replace("s3://", "")
        bucket     = s3_path.split("/")[0]
        key        = "/".join(s3_path.split("/")[1:])

        # Download snapshot template
        s3       = boto3.client("s3", region_name=region, config=_CONFIG)
        response = s3.get_object(Bucket=bucket, Key=key)
        template = response["Body"].read().decode("utf-8")

        # Apply as new change set
        cfn           = boto3.client("cloudformation", region_name=region, config=_CONFIG)
        rollback_name = f"drift-rollback-{int(time.time())}"

        cfn.create_change_set(
            StackName=stack_name,
            TemplateBody=template,
            ChangeSetName=rollback_name,
            ChangeSetType="UPDATE",
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            Description="Auto-rollback by CFN Drift Fixer after failed validation",
        )

        # Wait for change set ready
        _wait_change_set(cfn, stack_name, rollback_name)

        # Execute rollback
        cfn.execute_change_set(StackName=stack_name, ChangeSetName=rollback_name)

        # Wait for update complete
        waiter = cfn.get_waiter("stack_update_complete")
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 15, "MaxAttempts": 40},
        )

        msg = f"Rollback complete — restored from {snapshot_s3_uri}"
        logger.info(f"[SAFETY] {msg}")
        return True, msg

    except Exception as e:
        msg = f"Rollback FAILED: {e}"
        logger.error(f"[SAFETY] {msg}")
        return False, msg


def _wait_change_set(cfn, stack_name: str, change_set_name: str, max_wait: int = 60) -> None:
    elapsed = 0
    while elapsed < max_wait:
        resp   = cfn.describe_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        status = resp["Status"]
        reason = resp.get("StatusReason", "")
        if status == "CREATE_COMPLETE":
            return
        if status in ("FAILED", "DELETE_COMPLETE"):
            if "No updates" in reason or "didn't contain changes" in reason:
                return
            raise RuntimeError(f"Rollback change set failed: {reason}")
        time.sleep(3)
        elapsed += 3
    raise TimeoutError("Rollback change set timed out")


# ─────────────────────────────────────────────────────────────────────────────
# 4. MAINTENANCE WINDOW ENFORCEMENT
# Only allow fixes during approved hours. Configurable via SSM.
# ─────────────────────────────────────────────────────────────────────────────

def is_within_maintenance_window(region: str) -> Tuple[bool, str]:
    """
    Checks if current UTC time falls within the approved maintenance window.
    Window config stored in SSM as JSON:
      /cfn-drift-fixer/maintenance-window
      {"days": [1,2,3,4,5], "start_hour_utc": 2, "end_hour_utc": 6}
      days: 1=Mon ... 7=Sun. Above = weekdays 02:00-06:00 UTC.

    Returns (allowed, reason).
    """
    ssm = boto3.client("ssm", region_name=region, config=_CONFIG)

    try:
        param   = ssm.get_parameter(Name="/cfn-drift-fixer/maintenance-window")
        config  = json.loads(param["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        # No window configured → always allowed
        logger.info("[SAFETY] No maintenance window configured — allowing")
        return True, "No maintenance window configured"
    except Exception as e:
        logger.warning(f"[SAFETY] Could not read maintenance window: {e} — blocking fix")
        return False, f"Could not verify maintenance window: {e}"

    now      = datetime.now(timezone.utc)
    cur_day  = now.isoweekday()     # 1=Mon, 7=Sun
    cur_hour = now.hour

    allowed_days      = config.get("days", [1, 2, 3, 4, 5])
    start_hour        = config.get("start_hour_utc", 2)
    end_hour          = config.get("end_hour_utc", 6)

    if cur_day not in allowed_days:
        reason = f"Today (day={cur_day}) not in allowed days {allowed_days}"
        logger.warning(f"[SAFETY] Outside maintenance window: {reason}")
        return False, reason

    if not (start_hour <= cur_hour < end_hour):
        reason = f"Current hour {cur_hour} UTC not in window [{start_hour}-{end_hour})"
        logger.warning(f"[SAFETY] Outside maintenance window: {reason}")
        return False, reason

    logger.info(f"[SAFETY] Within maintenance window: day={cur_day} hour={cur_hour}")
    return True, f"Within window: day={cur_day} hour={cur_hour}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. EMERGENCY KILL SWITCH
# SSM parameter /cfn-drift-fixer/kill-switch = "true" halts all activity.
# Set it instantly from console or CLI during an incident.
# ─────────────────────────────────────────────────────────────────────────────

def is_kill_switch_active(region: str) -> bool:
    """
    Reads /cfn-drift-fixer/kill-switch from SSM.
    Returns True if agent should halt immediately.

    To activate:  aws ssm put-parameter --name /cfn-drift-fixer/kill-switch --value "true" --overwrite
    To deactivate: aws ssm put-parameter --name /cfn-drift-fixer/kill-switch --value "false" --overwrite
    """
    ssm = boto3.client("ssm", region_name=region, config=_CONFIG)
    try:
        param = ssm.get_parameter(Name="/cfn-drift-fixer/kill-switch")
        value = param["Parameter"]["Value"].strip().lower()
        active = value == "true"
        if active:
            logger.critical("[SAFETY] 🚨 KILL SWITCH IS ACTIVE — halting all agent activity")
        return active
    except ssm.exceptions.ParameterNotFound:
        return False  # Not set = not active
    except Exception as e:
        logger.error(f"[SAFETY] Kill switch check failed: {e} — treating as ACTIVE for safety")
        return True   # Fail safe: if we can't check, assume active


# ─────────────────────────────────────────────────────────────────────────────
# 6. BLAST RADIUS LIMITER
# Hard cap: no more than N stacks fixed in a single scheduled run.
# ─────────────────────────────────────────────────────────────────────────────

MAX_STACKS_PER_RUN = int(os.environ.get("MAX_STACKS_PER_RUN", "5"))


def enforce_blast_radius(stacks: List[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (allowed_stacks, blocked_stacks).
    Blocked stacks are logged and skipped — not silently dropped.
    """
    if len(stacks) <= MAX_STACKS_PER_RUN:
        return stacks, []

    allowed = stacks[:MAX_STACKS_PER_RUN]
    blocked = stacks[MAX_STACKS_PER_RUN:]

    logger.warning(
        f"[SAFETY] Blast radius limit: {MAX_STACKS_PER_RUN} max. "
        f"Blocked: {blocked}"
    )
    return allowed, blocked


# ─────────────────────────────────────────────────────────────────────────────
# 7. CIRCUIT BREAKER
# If N consecutive stack fixes fail, halt all further fixes this run.
# State stored in DynamoDB so it persists across Lambda invocations.
# ─────────────────────────────────────────────────────────────────────────────

CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_RESET_MINUTES = int(os.environ.get("CIRCUIT_BREAKER_RESET_MINUTES", "60"))


def record_fix_result(
    table_name: str,
    stack_name: str,
    success: bool,
    region: str,
) -> None:
    """Records fix outcome to DynamoDB for circuit breaker tracking."""
    dynamodb = boto3.resource("dynamodb", region_name=region, config=_CONFIG)
    table    = dynamodb.Table(table_name)

    table.put_item(Item={
        "PK":         "CIRCUIT_BREAKER",
        "SK":         f"FIX#{int(time.time())}",
        "stack_name": stack_name,
        "success":    success,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "ttl":        int(time.time()) + (CIRCUIT_BREAKER_RESET_MINUTES * 60),
    })


def is_circuit_open(table_name: str, region: str) -> Tuple[bool, str]:
    """
    Returns (circuit_open, reason).
    Circuit is open (blocking) if there are N+ consecutive failures
    within the reset window.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region, config=_CONFIG)
    table    = dynamodb.Table(table_name)

    cutoff = int(time.time()) - (CIRCUIT_BREAKER_RESET_MINUTES * 60)

    try:
        resp  = table.query(
            KeyConditionExpression="PK = :pk AND SK > :sk",
            ExpressionAttributeValues={
                ":pk": "CIRCUIT_BREAKER",
                ":sk": f"FIX#{cutoff}",
            },
            ScanIndexForward=False,  # Most recent first
            Limit=CIRCUIT_BREAKER_THRESHOLD + 1,
        )
        items = resp.get("Items", [])

        if len(items) < CIRCUIT_BREAKER_THRESHOLD:
            return False, "Circuit closed"

        # Check if last N are all failures
        last_n  = items[:CIRCUIT_BREAKER_THRESHOLD]
        all_fail = all(not item.get("success") for item in last_n)

        if all_fail:
            reason = (
                f"Circuit OPEN — {CIRCUIT_BREAKER_THRESHOLD} consecutive failures "
                f"in last {CIRCUIT_BREAKER_RESET_MINUTES} min"
            )
            logger.critical(f"[SAFETY] 🔴 {reason}")
            return True, reason

        return False, "Circuit closed"

    except Exception as e:
        logger.error(f"[SAFETY] Circuit breaker check failed: {e} — treating as OPEN")
        return True, f"Circuit check error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. RESOURCE EXCLUSION LIST
# Some resources may be intentionally drifted (manual overrides, experiments).
# Never touch them — ever.
# ─────────────────────────────────────────────────────────────────────────────

def filter_excluded_resources(
    drifted_resources: List[Dict],
    region: str,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Removes excluded resources from the drift list.
    Exclusion list stored in SSM as JSON list of logical IDs or resource types:
      /cfn-drift-fixer/excluded-resources
      ["MyLegacyRole", "AWS::RDS::DBInstance", "ManualOverrideBucket"]

    Returns (to_fix, excluded).
    """
    ssm = boto3.client("ssm", region_name=region, config=_CONFIG)
    exclusions = []

    try:
        param      = ssm.get_parameter(Name="/cfn-drift-fixer/excluded-resources")
        exclusions = json.loads(param["Parameter"]["Value"])
        logger.info(f"[SAFETY] Exclusion list: {exclusions}")
    except ssm.exceptions.ParameterNotFound:
        return drifted_resources, []
    except Exception as e:
        logger.warning(f"[SAFETY] Could not read exclusion list: {e}")
        return drifted_resources, []

    to_fix   = []
    excluded = []

    for resource in drifted_resources:
        logical_id    = resource.get("logical_id", "")
        resource_type = resource.get("resource_type", "")

        if logical_id in exclusions or resource_type in exclusions:
            logger.info(f"[SAFETY] Excluding: {logical_id} ({resource_type})")
            excluded.append(resource)
        else:
            to_fix.append(resource)

    if excluded:
        logger.warning(f"[SAFETY] {len(excluded)} resource(s) excluded from fix")

    return to_fix, excluded


# ─────────────────────────────────────────────────────────────────────────────
# 9. DUAL APPROVAL FOR HIGH RISK
# HIGH risk changes require TWO distinct approvers in DynamoDB.
# ─────────────────────────────────────────────────────────────────────────────

def check_dual_approval(
    audit_record_id: str,
    approval_table: str,
    region: str,
    required_approvals: int = 2,
) -> Tuple[bool, List[str]]:
    """
    Checks if enough distinct approvers have approved this record.
    Returns (approved, list_of_approvers).

    Each approver clicking the Slack button calls write_approval_decision()
    with their username. This function counts distinct approvers.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region, config=_CONFIG)
    table    = dynamodb.Table(approval_table)

    try:
        resp  = table.query(
            KeyConditionExpression="record_id = :rid AND decision = :d",
            ExpressionAttributeValues={
                ":rid": audit_record_id,
                ":d":   "APPROVE",
            },
        )
        items     = resp.get("Items", [])
        approvers = list({item.get("approver", "") for item in items if item.get("approver")})

        approved = len(approvers) >= required_approvals
        logger.info(
            f"[SAFETY] Dual approval: {len(approvers)}/{required_approvals} "
            f"approver(s): {approvers}"
        )
        return approved, approvers

    except Exception as e:
        logger.error(f"[SAFETY] Dual approval check failed: {e}")
        return False, []


def needs_dual_approval(risk_level: str) -> bool:
    """HIGH risk always requires dual approval. Others need only one."""
    return risk_level == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# 10. CHANGE FREEZE DETECTION
# Respect change freeze windows stored in SSM or passed via event.
# ─────────────────────────────────────────────────────────────────────────────

def is_change_freeze_active(region: str) -> Tuple[bool, str]:
    """
    Checks if a change freeze is currently active.
    Freeze flag in SSM: /cfn-drift-fixer/change-freeze = "true"
    With optional reason: /cfn-drift-fixer/change-freeze-reason = "Q4 release freeze"

    Returns (freeze_active, reason).

    To activate:   aws ssm put-parameter --name /cfn-drift-fixer/change-freeze --value "true" --overwrite
    To deactivate: aws ssm put-parameter --name /cfn-drift-fixer/change-freeze --value "false" --overwrite
    """
    ssm = boto3.client("ssm", region_name=region, config=_CONFIG)

    try:
        param  = ssm.get_parameter(Name="/cfn-drift-fixer/change-freeze")
        active = param["Parameter"]["Value"].strip().lower() == "true"

        if active:
            reason = "Change freeze active"
            try:
                r_param = ssm.get_parameter(Name="/cfn-drift-fixer/change-freeze-reason")
                reason  = r_param["Parameter"]["Value"]
            except Exception:
                pass
            logger.warning(f"[SAFETY] ❄️ Change freeze: {reason}")
            return True, reason

        return False, "No change freeze"

    except ssm.exceptions.ParameterNotFound:
        return False, "No change freeze configured"
    except Exception as e:
        logger.error(f"[SAFETY] Change freeze check failed: {e} — blocking fix")
        return True, f"Could not verify change freeze: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# MASTER SAFETY GATE
# Call this ONCE at the start of every agent run.
# Returns (go_ahead, list_of_blocking_reasons).
# ─────────────────────────────────────────────────────────────────────────────

def run_all_preflight_checks(
    stack_name: str,
    region: str,
    audit_table: str,
    dry_run: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Runs every safety check before the agent does anything destructive.
    Returns (can_proceed, blocking_reasons).
    If can_proceed is False, the agent should go straight to audit_and_notify.
    """
    blockers = []

    # 1. Kill switch
    if is_kill_switch_active(region):
        blockers.append("🚨 Emergency kill switch is ACTIVE")

    # 2. Change freeze
    frozen, freeze_reason = is_change_freeze_active(region)
    if frozen:
        blockers.append(f"❄️ Change freeze: {freeze_reason}")

    # 3. Maintenance window (skip in dry-run)
    if not dry_run:
        in_window, window_reason = is_within_maintenance_window(region)
        if not in_window:
            blockers.append(f"🕐 Outside maintenance window: {window_reason}")

    # 4. Stack state
    stable, stack_status = check_stack_is_stable(stack_name, region)
    if not stable:
        blockers.append(f"⚙️ Stack not stable: {stack_status}")

    # 5. Circuit breaker
    circuit_open, cb_reason = is_circuit_open(audit_table, region)
    if circuit_open:
        blockers.append(f"🔴 Circuit breaker: {cb_reason}")

    can_proceed = len(blockers) == 0

    if can_proceed:
        logger.info(f"[SAFETY] ✅ All pre-flight checks passed for {stack_name}")
    else:
        logger.warning(f"[SAFETY] ❌ Pre-flight FAILED for {stack_name}: {blockers}")

    return can_proceed, blockers
