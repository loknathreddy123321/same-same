"""
src/agent/nodes.py
------------------
Production flow:
  1. detect_drift       — find all drifted resources
  2. safety_gate        — kill switch / change freeze / maintenance window / stack stability / circuit breaker
  3. analyze_drift      — LLM root cause analysis
  4. classify_risk      — LOW / MEDIUM / HIGH
  5. plan_remediation   — sync template + create change set for review
  6. human_gate         — send full details to Slack, wait for approval
  7. apply_fix          — execute approved change set
  8. validate           — confirm drift is gone
  9. audit_and_notify   — write DynamoDB record + send result
"""

import json
import logging
import os
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Dict

from botocore.exceptions import ClientError
from langsmith import traceable
from agent.state import DriftFixState, RiskLevel, FixStatus
from agent.cfn import (
    trigger_drift_detection,
    wait_for_detection_complete,
    get_drifted_resources,
    get_stack_id,
    get_stack_template,
    create_change_set,
    execute_change_set,
    delete_change_set,
    sync_template_with_drift,
)
from agent.storage import (
    snapshot_template,
    write_audit_record,
    update_audit_record,
)
from agent.safety import run_all_preflight_checks, record_fix_result
from agent.observability import record_agent_result
from agent.bedrock import invoke_claude_json
from agent.prompts import DRIFT_ANALYSIS_PROMPT, REMEDIATION_PLAN_PROMPT

logger = logging.getLogger(__name__)

_HIGH_RISK = {
    "AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy",
    "AWS::IAM::InstanceProfile", "AWS::IAM::Group", "AWS::IAM::User",
    "AWS::KMS::Key", "AWS::KMS::Alias",
    "AWS::RDS::DBInstance", "AWS::RDS::DBCluster",
    "AWS::EC2::SecurityGroup",
    "AWS::SecretsManager::Secret",
    "AWS::WAFv2::WebACL",
    "AWS::Route53::HostedZone",
}

_MEDIUM_RISK = {
    "AWS::EC2::Instance", "AWS::EC2::VPC", "AWS::EC2::Subnet",
    "AWS::Lambda::Function",
    "AWS::ECS::Service", "AWS::ECS::TaskDefinition",
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::AutoScaling::AutoScalingGroup",
    "AWS::DynamoDB::Table",
}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace(state: DriftFixState, msg: str) -> list:
    return state.get("execution_trace", []) + [f"[{_ts()}] {msg}"]


def _cs_name(stack_name: str) -> str:
    h = hashlib.md5(f"{stack_name}{time.time()}".encode()).hexdigest()[:8]
    return f"drift-fix-{h}"


# ─── NODE 1: Detect Drift ─────────────────────────────────────────────────────

@traceable(name="detect_drift")
def node_detect_drift(state: DriftFixState) -> Dict[str, Any]:
    stack  = state["stack_name"]
    region = state["aws_region"]
    logger.info(f"[detect_drift] Stack: {stack}")

    try:
        stack_id     = get_stack_id(stack, region)
        detection_id = trigger_drift_detection(stack, region)
        status       = wait_for_detection_complete(stack, detection_id, region)

        if status == "DETECTION_FAILED":
            return {
                "fix_status":      FixStatus.FAILED,
                "error_message":   "AWS drift detection returned DETECTION_FAILED",
                "execution_trace": _trace(state, "DETECT: DETECTION_FAILED"),
            }

        drifted = get_drifted_resources(stack, region)
        return {
            "stack_id":            stack_id,
            "detection_id":        detection_id,
            "drifted_resources":   drifted,
            "total_drifted":       len(drifted),
            "detection_timestamp": _ts(),
            "execution_trace":     _trace(state, f"DETECT: {len(drifted)} drifted resource(s)"),
        }

    except Exception as e:
        logger.exception(f"[detect_drift] Fatal: {e}")
        return {
            "fix_status":      FixStatus.FAILED,
            "error_message":   str(e),
            "execution_trace": _trace(state, f"DETECT ERROR: {e}"),
        }


# ─── NODE 1b: Safety Gate ─────────────────────────────────────────────────────
# Runs the 5 pre-flight checks from safety.py before any LLM call or fix is
# planned: kill switch, change freeze, maintenance window, stack stability,
# circuit breaker. If any blocks, the run stops here — no analysis, no plan,
# no human notification, no fix.

@traceable(name="safety_gate")
def node_safety_gate(state: DriftFixState) -> Dict[str, Any]:
    stack       = state["stack_name"]
    region      = state["aws_region"]
    dry_run     = state.get("dry_run", False)
    audit_table = os.environ.get("DYNAMODB_AUDIT_TABLE", "cfn-drift-audit")

    logger.info(f"[safety_gate] Running pre-flight checks for {stack}")
    can_proceed, blockers = run_all_preflight_checks(stack, region, audit_table, dry_run=dry_run)

    if not can_proceed:
        reason = "; ".join(blockers)
        logger.warning(f"[safety_gate] BLOCKED: {reason}")
        return {
            "fix_status":      FixStatus.SKIPPED,
            "error_message":   f"Safety gate blocked: {reason}",
            "execution_trace": _trace(state, f"SAFETY GATE: Blocked — {reason}"),
        }

    return {"execution_trace": _trace(state, "SAFETY GATE: All checks passed ✅")}


# ─── NODE 2: Analyze Drift ────────────────────────────────────────────────────

@traceable(name="analyze_drift")
def node_analyze_drift(state: DriftFixState) -> Dict[str, Any]:
    logger.info("[analyze_drift] Calling LLM for root cause")

    if not state.get("drifted_resources"):
        return {
            "fix_status":      FixStatus.NO_DRIFT,
            "root_cause":      "No drift detected — stack is in sync",
            "execution_trace": _trace(state, "ANALYZE: No drift"),
        }

    prompt = DRIFT_ANALYSIS_PROMPT.format(
        stack_name=state["stack_name"],
        total_drifted=state["total_drifted"],
        drifted_json=json.dumps(state["drifted_resources"], indent=2, default=str),
    )

    try:
        parsed = invoke_claude_json(prompt)
        return {
            "root_cause":         parsed.get("root_cause", "Unknown"),
            "impact":             parsed.get("impact", ""),
            "affected_services":  parsed.get("affected_services", []),
            "llm_recommendation": parsed.get("recommendation", "CHANGE_SET"),
            "execution_trace":    _trace(state, f"ANALYZE: {parsed.get('root_cause','')[:80]}"),
        }
    except Exception as e:
        logger.warning(f"[analyze_drift] LLM fallback: {e}")
        return {
            "root_cause":         "LLM analysis unavailable — manual review required",
            "llm_recommendation": "CHANGE_SET",
            "affected_services":  [],
            "execution_trace":    _trace(state, f"ANALYZE FALLBACK: {e}"),
        }


# ─── NODE 3: Classify Risk ────────────────────────────────────────────────────

@traceable(name="classify_risk")
def node_classify_risk(state: DriftFixState) -> Dict[str, Any]:
    logger.info("[classify_risk] Classifying risk level")

    resource_types = {r["resource_type"] for r in state.get("drifted_resources", [])}
    high_matched   = resource_types & _HIGH_RISK
    medium_matched = resource_types & _MEDIUM_RISK

    if high_matched:
        risk              = RiskLevel.HIGH
        reason            = f"HIGH risk resources detected: {high_matched}"
        approval_required = True
    elif medium_matched:
        risk              = RiskLevel.MEDIUM
        reason            = f"MEDIUM risk resources detected: {medium_matched}"
        approval_required = True
    else:
        risk              = RiskLevel.LOW
        reason            = "All resources are low-risk — requires approval before fix"
        approval_required = True  # Always require approval — stakeholder must see the diff

    logger.info(f"[classify_risk] {risk} | approval_required={approval_required}")

    return {
        "risk_level":        risk,
        "risk_reasoning":    reason,
        "approval_required": approval_required,
        "approval_status":   FixStatus.PENDING,  # Always pending until human approves
        "execution_trace":   _trace(state, f"RISK: {risk} — {reason[:80]}"),
    }


# ─── NODE 4: Plan Remediation ─────────────────────────────────────────────────
# KEY STEP:
#   1. Snapshot current template to S3 (rollback safety)
#   2. Build synced template (merge actual props into template)
#   3. Create change set from synced template
#   4. Get change set diff (exact list of changes)
#   5. Store everything for Slack notification

@traceable(name="plan_remediation")
def node_plan_remediation(state: DriftFixState) -> Dict[str, Any]:
    stack   = state["stack_name"]
    region  = state["aws_region"]
    dry_run = state.get("dry_run", False)
    logger.info(f"[plan_remediation] Planning for {stack}")

    try:
        # Step 1: Fetch current template
        original_template = get_stack_template(stack, region)

        # Step 2: Snapshot original template to S3 (safety net)
        s3_uri = None
        if not dry_run:
            bucket = os.environ.get("S3_SNAPSHOT_BUCKET", "")
            if bucket:
                s3_uri = snapshot_template(stack, original_template, bucket, region)
                logger.info(f"[plan_remediation] Snapshot saved: {s3_uri}")
            else:
                logger.warning("[plan_remediation] S3_SNAPSHOT_BUCKET not set — skipping snapshot")

        # Step 3: Build synced template (merge actual resource state into template)
        logger.info("[plan_remediation] Building synced template from actual resource state")
        synced_template = _build_synced_template(
            original_template=original_template,
            drifted_resources=state.get("drifted_resources", []),
        )

        # Step 4: Create change set from synced template (for review — not executed yet)
        cs_name       = _cs_name(stack)
        change_set_id = None
        change_set_diff = "Dry run — no change set created"

        if not dry_run:
            import boto3
            cfn = boto3.client("cloudformation", region_name=region)

            try:
                # Read actual parameter values from the live stack and pass them explicitly
                # This is the same as what a normal CFN deploy does
                existing_params = []
                try:
                    stack_desc = cfn.describe_stacks(StackName=stack)
                    existing_params = [
                        {
                            "ParameterKey":   p["ParameterKey"],
                            "ParameterValue": p["ParameterValue"],
                        }
                        for p in stack_desc["Stacks"][0].get("Parameters", [])
                    ]
                    if existing_params:
                        logger.info(
                            f"[plan_remediation] Passing {len(existing_params)} parameter(s): "
                            f"{[p['ParameterKey'] for p in existing_params]}"
                        )
                except Exception as pe:
                    logger.warning(f"[plan_remediation] Could not fetch params: {pe}")

                resp = cfn.create_change_set(
                    StackName=stack,
                    TemplateBody=synced_template,
                    ChangeSetName=cs_name,
                    ChangeSetType="UPDATE",
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
                    Description="Drift fix — sync template with actual resource state",
                    Parameters=existing_params,
                )
                change_set_id = resp["Id"]
                logger.info(f"[plan_remediation] Change set created: {change_set_id}")

                # Wait for change set to be ready
                _wait_for_change_set(cfn, stack, cs_name)

                # Step 5: Get exact diff for stakeholder review
                change_set_diff = _get_change_set_diff(cfn, stack, cs_name)
                logger.info(f"[plan_remediation] Change set diff ready")

            except ClientError as e:
                if "No updates are to be performed" in str(e):
                    logger.info("[plan_remediation] Template already matches — no changes needed")
                    change_set_diff = "No changes required — stack is already in sync"
                else:
                    raise

        # Step 6: LLM remediation plan
        prompt = REMEDIATION_PLAN_PROMPT.format(
            stack_name=stack,
            root_cause=state.get("root_cause", "Unknown"),
            recommendation="CHANGE_SET",
            drifted_json=json.dumps(state.get("drifted_resources", []), indent=2, default=str),
        )
        try:
            plan = invoke_claude_json(prompt)
        except Exception as e:
            logger.warning(f"LLM plan fallback: {e}")
            plan = {
                "strategy":           "CHANGE_SET",
                "reasoning":          "Sync template with actual resource state",
                "estimated_risk":     state.get("risk_level", "LOW"),
                "rollback_procedure": f"Restore from S3 snapshot: {s3_uri}",
                "pre_fix_checks":     ["Stack must be in stable state"],
            }

        return {
            "template_body":        original_template,
            "synced_template":      synced_template,
            "template_snapshot_s3": s3_uri,
            "remediation_plan":     plan,
            "change_set_name":      cs_name,
            "change_set_id":        change_set_id,
            "change_set_diff":      change_set_diff,
            "execution_trace":      _trace(
                state,
                f"PLAN: synced template created | change set={cs_name} | snapshot={s3_uri}"
            ),
        }

    except Exception as e:
        logger.exception(f"[plan_remediation] Error: {e}")
        return {
            "fix_status":      FixStatus.FAILED,
            "error_message":   str(e),
            "execution_trace": _trace(state, f"PLAN ERROR: {e}"),
        }


def _add_deletion_policy_retain(template: dict) -> dict:
    """
    SAFETY MEASURE: Adds DeletionPolicy: Retain to EVERY resource
    in the template before applying any fix.

    Why this matters:
    - If the change set accidentally removes a resource from CFN control
    - Or if a rollback happens unexpectedly
    - DeletionPolicy: Retain ensures the REAL AWS resource is NEVER deleted
    - The resource just becomes unmanaged — much safer than deletion
    - This is mandatory for production before any update_stack call

    Resources that already have a DeletionPolicy are left unchanged.
    """
    resources = template.get("Resources", {})
    count = 0

    for logical_id, resource in resources.items():
        existing_policy = resource.get("DeletionPolicy")
        if not existing_policy:
            resource["DeletionPolicy"] = "Retain"
            count += 1
            logger.info(f"[RETAIN] Added DeletionPolicy: Retain to {logical_id}")
        else:
            logger.info(f"[RETAIN] {logical_id} already has DeletionPolicy: {existing_policy} — keeping")

    if count > 0:
        logger.info(f"[RETAIN] DeletionPolicy: Retain added to {count} resource(s) — no resource will be deleted")

    return template


def _convert_cfn_tags(obj):
    """
    Recursively fixes CFN intrinsic functions after YAML parsing.

    PyYAML SafeLoader already handles !Ref -> {"Ref": "X"} correctly.
    The add_multi_constructor strips ! from other tags, giving:
      {"GetAtt": "X.Y"}     -> needs {"Fn::GetAtt": ["X", "Y"]}
      {"Sub": "X"}          -> needs {"Fn::Sub": "X"}
      {"Join": [...]}       -> needs {"Fn::Join": [...]}
      {"If": [...]}         -> needs {"Fn::If": [...]}
      etc.
    {"Ref": "X"} is already correct — leave it alone.
    """
    # These keys came from !TAG shorthand — need Fn:: prefix
    # GetAtt also needs value split from "A.B" to ["A", "B"]
    NEEDS_FN_PREFIX = {
        "Sub", "GetAtt", "Join", "Select", "If", "Not",
        "And", "Or", "Equals", "Base64", "FindInMap",
        "ImportValue", "Split", "Cidr", "Transform",
    }

    if isinstance(obj, dict):
        if len(obj) == 1:
            key = list(obj.keys())[0]
            val = list(obj.values())[0]

            if key == "GetAtt":
                # !GetAtt Resource.Attr -> {"Fn::GetAtt": ["Resource", "Attr"]}
                if isinstance(val, str) and "." in val:
                    parts = val.split(".", 1)
                    return {"Fn::GetAtt": parts}
                return {"Fn::GetAtt": _convert_cfn_tags(val)}

            if key in NEEDS_FN_PREFIX:
                return {f"Fn::{key}": _convert_cfn_tags(val)}

        # Regular dict — recurse into all values
        return {k: _convert_cfn_tags(v) for k, v in obj.items()}

    elif isinstance(obj, list):
        return [_convert_cfn_tags(i) for i in obj]

    return obj


# Ingress/egress rule lists where AWS's drift-detection ActualProperties has
# been observed to omit FromPort/ToPort for a TCP/UDP rule — CloudFormation's
# UpdateStack rejects that combination outright (HandlerErrorCode: InvalidRequest,
# "Must specify both from and to ports with TCP/UDP"). Validate before syncing
# rather than discover it via a rolled-back stack update.
_RULE_LIST_PROPS = {"SecurityGroupIngress", "SecurityGroupEgress"}


def _is_safe_rule_list(key: str, value) -> bool:
    if key not in _RULE_LIST_PROPS:
        return True
    if not isinstance(value, list):
        return True
    for rule in value:
        if not isinstance(rule, dict):
            continue
        protocol = str(rule.get("IpProtocol", "")).lower()
        if protocol in ("tcp", "udp", "6", "17"):
            if rule.get("FromPort") is None or rule.get("ToPort") is None:
                return False
    return True


def _build_synced_template(original_template: str, drifted_resources: list) -> str:
    """
    Builds a synced template:
    1. Parses the original template
    2. Adds DeletionPolicy: Retain to ALL resources (safety first)
    3. Syncs drifted resource properties with actual AWS state
    4. Returns updated template in the SAME format as the input

    For YAML input, uses ruamel.yaml round-trip mode: comments, key order,
    and quote style are preserved exactly, and CFN short-hand tags
    (!Ref, !Sub, !GetAtt, ...) are kept as-is rather than expanded to
    Fn::/Ref dict form. Only the drifted properties actually change —
    everything else in the file is untouched.

    DeletionPolicy: Retain is added BEFORE any property changes
    so that even if something goes wrong, no real resource is deleted.
    """
    is_yaml = False
    ruamel_yaml = None
    try:
        template = json.loads(original_template)
    except json.JSONDecodeError:
        try:
            from ruamel.yaml import YAML
            from ruamel.yaml.constructor import RoundTripConstructor
            from ruamel.yaml.nodes import ScalarNode, SequenceNode, MappingNode
            from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar

            def _cfn_tag_constructor(loader, tag_suffix, node):
                """Preserves CFN short-hand tags exactly as-is — never expanded."""
                if isinstance(node, ScalarNode):
                    return TaggedScalar(loader.construct_scalar(node), style=node.style, tag=node.tag)
                elif isinstance(node, SequenceNode):
                    data = CommentedSeq(loader.construct_sequence(node, deep=True))
                    data.yaml_set_tag(node.tag)
                    return data
                elif isinstance(node, MappingNode):
                    data = CommentedMap(loader.construct_mapping(node, deep=True))
                    data.yaml_set_tag(node.tag)
                    return data

            class CFNConstructor(RoundTripConstructor):
                pass

            CFNConstructor.add_multi_constructor("!", _cfn_tag_constructor)

            ruamel_yaml = YAML()
            ruamel_yaml.Constructor = CFNConstructor
            ruamel_yaml.preserve_quotes = True
            ruamel_yaml.width = 10_000   # never wrap long lines (ARNs, policy JSON, etc.)
            ruamel_yaml.indent(mapping=2, sequence=4, offset=2)

            template = ruamel_yaml.load(original_template)
            is_yaml  = True
            logger.info("[SYNC] Template parsed as YAML (ruamel round-trip) — CFN tags preserved as-is")
        except Exception as e:
            raise ValueError(f"Template is not valid JSON or YAML: {e}")

    # SAFETY FIRST: Add DeletionPolicy: Retain to every resource
    template = _add_deletion_policy_retain(template)

    resources = template.get("Resources", {})

    for drifted in drifted_resources:
        logical_id   = drifted.get("logical_id")
        actual_props = drifted.get("actual_properties", {})
        drift_status = drifted.get("drift_status", "")

        if not logical_id or logical_id not in resources:
            logger.warning(f"[SYNC] {logical_id} not in template — skipping")
            continue

        if drift_status == "DELETED":
            # Resource deleted outside CFN — keep in template with Retain policy
            # DO NOT remove it — the Retain policy means the resource still exists in AWS
            logger.warning(
                f"[SYNC] {logical_id} was DELETED in CFN but DeletionPolicy=Retain "
                f"means the real AWS resource still exists. Keeping in template."
            )
            continue

        if not actual_props:
            logger.warning(f"[SYNC] No actual_properties for {logical_id} — skipping")
            continue

        # Properties that are SAFE to sync — CFN can always update these
        # We intentionally only sync Tags and mutable properties.
        # Other properties (network config, engine version etc) can cause rollback
        SAFE_TO_SYNC = {"Tags", "Description"}

        # Properties that are NEVER safe — always computed/read-only
        READ_ONLY_PROPS = {
            "Arn", "Id", "CreatedTime", "LastModified",
            "DomainName", "WebsiteURL", "DualStackDomainName",
            "RegionalDomainName", "BucketName", "Outputs",
            "AvailabilityZone", "PublicIp", "PrivateIp",
            "GroupId", "VpcId", "OwnerId",
        }

        # Start from original template properties
        current_props = resources[logical_id].get("Properties", {}).copy()
        synced_keys = []

        for key, value in actual_props.items():
            if key in READ_ONLY_PROPS:
                continue  # Never sync read-only

            if not _is_safe_rule_list(key, value):
                # AWS's own drift-detection ActualProperties for security group
                # rule lists is not always UpdateStack-safe — e.g. a rule can
                # come back missing FromPort/ToPort while IpProtocol is tcp/udp,
                # which CloudFormation rejects outright. Guessing a port range
                # would be a security decision we're not willing to make
                # silently, so skip this key and leave the template's existing
                # rule in place rather than risk a rejected/rolled-back update.
                logger.warning(
                    f"[SYNC] Skipping {key} for {logical_id} — AWS reported a rule "
                    f"missing FromPort/ToPort for a TCP/UDP protocol; syncing it would "
                    f"fail CloudFormation validation. Leaving existing rule in template."
                )
                continue

            if key in SAFE_TO_SYNC:
                # Always safe to sync these
                current_props[key] = value
                synced_keys.append(key)
            else:
                # For other properties — only sync if they already exist
                # in the original template (don't add new ones that weren't there)
                if key in current_props:
                    current_props[key] = value
                    synced_keys.append(key)
                else:
                    logger.info(f"[SYNC] Skipping {key} for {logical_id} — not in original template")

        resources[logical_id]["Properties"] = current_props
        logger.info(
            f"[SYNC] {logical_id}: DeletionPolicy=Retain. "
            f"Synced properties: {synced_keys}"
        )

    # Return in same format as original — YAML stays YAML, JSON stays JSON
    if is_yaml:
        from io import StringIO
        logger.info("[SYNC] Returning synced template as YAML — same format, only the fix applied")
        buf = StringIO()
        ruamel_yaml.dump(template, buf)
        return buf.getvalue()
    return json.dumps(template, indent=2)


def _wait_for_change_set(cfn, stack_name: str, cs_name: str, max_wait: int = 120) -> None:
    elapsed = 0
    while elapsed < max_wait:
        resp   = cfn.describe_change_set(StackName=stack_name, ChangeSetName=cs_name)
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
    raise TimeoutError("Change set not ready after timeout")


def _get_change_set_diff(cfn, stack_name: str, cs_name: str) -> str:
    """Returns a human-readable summary of what the change set will change."""
    try:
        resp    = cfn.describe_change_set(StackName=stack_name, ChangeSetName=cs_name)
        changes = resp.get("Changes", [])

        if not changes:
            return "No changes in change set"

        lines = []
        ACTION_ICON = {"Add": "➕", "Modify": "✏️", "Remove": "🗑️"}

        for change in changes:
            rc          = change.get("ResourceChange", {})
            icon        = ACTION_ICON.get(rc.get("Action", ""), "•")
            logical_id  = rc.get("LogicalResourceId", "")
            res_type    = rc.get("ResourceType", "")
            action      = rc.get("Action", "")
            replacement = rc.get("Replacement", "False")
            replace_warn = " ⚠️ REPLACEMENT REQUIRED" if replacement == "True" else ""

            lines.append(f"{icon} {logical_id} ({res_type}) — {action}{replace_warn}")

            for detail in rc.get("Details", [])[:3]:
                attr = detail.get("Target", {}).get("Attribute", "")
                name = detail.get("Target", {}).get("Name", "")
                if name:
                    lines.append(f"   └ {attr}.{name}")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"Could not get change set diff: {e}")
        return "Could not retrieve change details"


# ─── NODE 5: Human Gate ───────────────────────────────────────────────────────
# Sends FULL details to Slack:
#   - Stack name + risk level
#   - All drifted resources
#   - Root cause (from LLM)
#   - Exact change set diff (what will change)
#   - Snapshot S3 URI (rollback info)
#   - Approve / Reject buttons

@traceable(name="human_gate")
def node_human_gate(state: DriftFixState) -> Dict[str, Any]:
    stack = state["stack_name"]

    # Already approved (passed in from run_local.py)
    if state.get("approval_status") == FixStatus.APPROVED:
        logger.info("[human_gate] Pre-approved — skipping Slack")
        return {"execution_trace": _trace(state, "GATE: Pre-approved ✅")}

    # Slack not configured — auto-approve for local testing
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.info("[human_gate] Slack not configured — auto-approving (local test mode)")
        return {
            "approval_status": FixStatus.APPROVED,
            "approver":        "LOCAL_TEST",
            "execution_trace": _trace(state, "GATE: No Slack — auto-approved for local test"),
        }

    # Slack configured — send full details for approval
    from notifications.slack import send_approval_request
    try:
        msg_ts = send_approval_request(
            stack_name=stack,
            risk_level=state.get("risk_level"),
            drifted_resources=state.get("drifted_resources", []),
            root_cause=state.get("root_cause", ""),
            remediation_plan=state.get("remediation_plan", {}),
            audit_record_id=state.get("audit_record_id", ""),
            task_token=state.get("task_token"),
            change_set_diff=state.get("change_set_diff", ""),
        )
        logger.info(f"[human_gate] Slack approval sent: ts={msg_ts}")
        return {
            "execution_trace": _trace(state, f"GATE: Details sent to Slack ts={msg_ts}"),
        }
    except Exception as e:
        logger.error(f"[human_gate] Slack failed: {e}")
        return {
            "approval_status": FixStatus.REJECTED,
            "error_message":   f"Slack failed: {e}",
            "execution_trace": _trace(state, f"GATE ERROR: {e} — rejected for safety"),
        }


# ─── NODE 6: Apply Fix ────────────────────────────────────────────────────────
# Executes the pre-created change set (approved by stakeholder)

@traceable(name="apply_fix")
def node_apply_fix(state: DriftFixState) -> Dict[str, Any]:
    """
    Applies the drift fix using update_stack with the synced template.

    WHY update_stack instead of executing the pre-created change set:
    - The change set in plan_remediation was created for PREVIEW (showing diff)
    - It may be in FAILED status if actual_properties had read-only fields
    - update_stack is simpler and more reliable for actually applying the fix
    - We rebuild the synced template here to ensure it is correct
    """
    stack   = state["stack_name"]
    region  = state["aws_region"]
    dry_run = state.get("dry_run", False)

    logger.info(f"[apply_fix] approval={state.get('approval_status')}")

    if state.get("approval_status") != FixStatus.APPROVED:
        return {
            "fix_status":      FixStatus.SKIPPED,
            "fix_applied":     False,
            "execution_trace": _trace(state, f"FIX: Skipped — {state.get('approval_status')}"),
        }

    if state.get("fix_status") == FixStatus.FAILED:
        return {
            "fix_status":      FixStatus.FAILED,
            "fix_applied":     False,
            "execution_trace": _trace(state, "FIX: Skipped — previous node failed"),
        }

    if dry_run:
        logger.info("[apply_fix] DRY RUN — skipping")
        return {
            "fix_status":      FixStatus.APPLIED,
            "fix_applied":     False,
            "fix_timestamp":   _ts(),
            "execution_trace": _trace(state, "FIX: DRY RUN — would apply synced template"),
        }

    try:
        import boto3
        cfn = boto3.client("cloudformation", region_name=region)

        # Get synced template — prefer cached, rebuild if not available
        synced_template = state.get("synced_template")
        if not synced_template:
            logger.info("[apply_fix] Rebuilding synced template")
            original = get_stack_template(stack, region)
            synced_template = _build_synced_template(
                original_template=original,
                drifted_resources=state.get("drifted_resources", []),
            )

        logger.info(f"[apply_fix] Applying synced template via update_stack")

        # Cleanup any old failed change set first
        old_cs = state.get("change_set_name")
        if old_cs:
            try:
                delete_change_set(stack, old_cs, region)
            except Exception:
                pass

        # Apply using update_stack — direct and reliable
        try:
            # Read actual parameter values from the live stack and pass them explicitly
            existing_params = []
            try:
                stack_desc = cfn.describe_stacks(StackName=stack)
                existing_params = [
                    {
                        "ParameterKey":   p["ParameterKey"],
                        "ParameterValue": p["ParameterValue"],
                    }
                    for p in stack_desc["Stacks"][0].get("Parameters", [])
                ]
                if existing_params:
                    logger.info(
                        f"[apply_fix] Passing {len(existing_params)} parameter(s): "
                        f"{[p['ParameterKey'] for p in existing_params]}"
                    )
            except Exception as pe:
                logger.warning(f"[apply_fix] Could not fetch params: {pe}")

            cfn.update_stack(
                StackName=stack,
                TemplateBody=synced_template,
                Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
                Parameters=existing_params,
            )
            logger.info(f"[apply_fix] update_stack initiated for {stack}")

            # Wait for completion
            waiter = cfn.get_waiter("stack_update_complete")
            try:
                waiter.wait(
                    StackName=stack,
                    WaiterConfig={"Delay": 15, "MaxAttempts": 40},
                )
                logger.info(f"[apply_fix] Stack update complete ✅")

            except Exception as waiter_err:
                # Stack rolled back — get the real failure reason from stack events
                rollback_reason = _get_rollback_reason(cfn, stack)
                logger.error(f"[apply_fix] Stack rolled back: {rollback_reason}")
                raise RuntimeError(
                    f"Stack update rolled back. Reason: {rollback_reason}\n"
                    f"The synced template contained a property that CloudFormation "
                    f"cannot update on this resource type. "
                    f"The stack has been restored to its previous state automatically."
                ) from waiter_err

        except ClientError as e:
            if "No updates are to be performed" in str(e):
                logger.info("[apply_fix] Stack already in sync — no update needed")
            else:
                raise

        return {
            "fix_status":      FixStatus.APPLIED,
            "fix_applied":     True,
            "fix_timestamp":   _ts(),
            "execution_trace": _trace(state, "FIX: Template synced — stack updated ✅"),
        }

    except Exception as e:
        logger.exception(f"[apply_fix] Error: {e}")
        return {
            "fix_status":      FixStatus.FAILED,
            "fix_applied":     False,
            "error_message":   str(e),
            "execution_trace": _trace(state, f"FIX ERROR: {e}"),
        }


# ─── NODE 7: Validate ─────────────────────────────────────────────────────────

@traceable(name="validate")
def node_validate(state: DriftFixState) -> Dict[str, Any]:
    stack  = state["stack_name"]
    region = state["aws_region"]
    logger.info("[validate] Re-checking drift post-fix")

    if not state.get("fix_applied"):
        return {
            "validation_passed": None,
            "execution_trace":   _trace(state, "VALIDATE: Skipped — fix not applied"),
        }

    try:
        det_id    = trigger_drift_detection(stack, region)
        wait_for_detection_complete(stack, det_id, region)
        remaining = get_drifted_resources(stack, region)
        passed    = len(remaining) == 0

        logger.info(f"[validate] {'PASSED ✅' if passed else 'FAILED ❌'} — {len(remaining)} remaining")

        return {
            "validation_passed":    passed,
            "post_fix_drift_count": len(remaining),
            "fix_status":           FixStatus.VALIDATED if passed else FixStatus.FAILED,
            "execution_trace":      _trace(
                state,
                f"VALIDATE: {'PASSED ✅' if passed else f'FAILED — {len(remaining)} still drifted'}"
            ),
        }

    except Exception as e:
        logger.exception(f"[validate] Error: {e}")
        return {
            "validation_passed": False,
            "error_message":     str(e),
            "execution_trace":   _trace(state, f"VALIDATE ERROR: {e}"),
        }


# ─── NODE 8: Audit and Notify ─────────────────────────────────────────────────

@traceable(name="audit_and_notify")
def node_audit_and_notify(state: DriftFixState) -> Dict[str, Any]:
    from notifications.slack import send_result_notification

    logger.info("[audit_and_notify] Writing final audit record")

    region     = state["aws_region"]
    table_name = os.environ.get("DYNAMODB_AUDIT_TABLE", "cfn-drift-audit")

    try:
        record_id = write_audit_record(
            table_name=table_name,
            record={
                "stack_name":           state["stack_name"],
                "stack_id":             state.get("stack_id"),
                "detection_timestamp":  state.get("detection_timestamp"),
                "total_drifted":        state.get("total_drifted", 0),
                "drifted_resources":    state.get("drifted_resources", []),
                "risk_level":           state.get("risk_level"),
                "fix_status":           state.get("fix_status"),
                "root_cause":           state.get("root_cause"),
                "change_set_name":      state.get("change_set_name"),
                "change_set_diff":      state.get("change_set_diff"),
                "template_snapshot_s3": state.get("template_snapshot_s3"),
                "approval_status":      state.get("approval_status"),
                "approver":             state.get("approver"),
                "validation_passed":    state.get("validation_passed"),
                "dry_run":              state.get("dry_run", False),
                "execution_trace":      state.get("execution_trace", []),
                "error_message":        state.get("error_message"),
            },
            region=region,
        )

        # Send result notification to Slack
        try:
            send_result_notification(
                stack_name=state["stack_name"],
                fix_status=state.get("fix_status"),
                validation_passed=state.get("validation_passed"),
                risk_level=state.get("risk_level"),
                total_drifted=state.get("total_drifted", 0),
                approver=state.get("approver"),
                error_message=state.get("error_message"),
                audit_record_id=record_id,
            )
        except Exception as slack_err:
            logger.warning(f"Slack result notification failed (non-fatal): {slack_err}")

        # Feed the circuit breaker — only counts runs where a fix was actually attempted
        fix_status = state.get("fix_status")
        if fix_status in (FixStatus.VALIDATED, FixStatus.FAILED) and state.get("fix_applied"):
            try:
                record_fix_result(
                    table_name=table_name,
                    stack_name=state["stack_name"],
                    success=(fix_status == FixStatus.VALIDATED),
                    region=region,
                )
            except Exception as cb_err:
                logger.warning(f"Circuit breaker record failed (non-fatal): {cb_err}")

        # Emit CloudWatch custom metrics — DriftFixAttempt, DriftedResources,
        # ValidationPassed/Failed, FixFailed under the CFNDriftFixer namespace
        record_agent_result(state)

        return {
            "audit_record_id": record_id,
            "execution_trace": _trace(state, f"AUDIT: Record {record_id} written ✅"),
        }

    except Exception as e:
        logger.exception(f"[audit_and_notify] Error: {e}")
        return {
            "error_message":   str(e),
            "execution_trace": _trace(state, f"AUDIT ERROR: {e}"),
        }


def _get_rollback_reason(cfn, stack_name: str) -> str:
    """
    Reads CloudFormation stack events to find the real reason
    a stack update failed and rolled back.
    Returns the first FAILED event reason found.
    """
    try:
        resp   = cfn.describe_stack_events(StackName=stack_name)
        events = resp.get("StackEvents", [])
        for event in events:
            status = event.get("ResourceStatus", "")
            reason = event.get("ResourceStatusReason", "")
            if "FAILED" in status and reason and "User Initiated" not in reason:
                resource = event.get("LogicalResourceId", "unknown")
                res_type = event.get("ResourceType", "")
                return f"{resource} ({res_type}): {reason}"
        return "Unknown — check AWS CloudFormation console for stack events"
    except Exception as e:
        return f"Could not retrieve rollback reason: {e}"