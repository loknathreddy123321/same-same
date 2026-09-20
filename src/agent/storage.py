"""
src/agent/storage.py
--------------------
DynamoDB (audit + approval tables) and S3 (template snapshots).
"""

import json
import time
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"})


def _dynamo(region: str):
    return boto3.resource("dynamodb", region_name=region, config=BOTO_CONFIG)


def _s3(region: str):
    return boto3.client("s3", region_name=region, config=BOTO_CONFIG)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
def snapshot_template(
    stack_name: str,
    template_body: str,
    s3_bucket: str,
    region: str,
) -> str:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"snapshots/{stack_name}/{ts}/template.json"
    _s3(region).put_object(
        Bucket=s3_bucket,
        Key=key,
        Body=template_body.encode("utf-8"),
        ContentType="application/json",
        Metadata={"stack-name": stack_name, "snapshot-time": ts},
    )
    uri = f"s3://{s3_bucket}/{key}"
    logger.info(f"Template snapshot saved: {uri}")
    return uri


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
def write_audit_record(table_name: str, record: Dict[str, Any], region: str) -> str:
    table     = _dynamo(region).Table(table_name)
    timestamp = record.get("detection_timestamp") or datetime.now(timezone.utc).isoformat()
    record_id = hashlib.sha256(
        f"{record.get('stack_name')}:{timestamp}".encode()
    ).hexdigest()[:16]

    item = {
        "PK":                   f"STACK#{record.get('stack_name', 'UNKNOWN')}",
        "SK":                   f"DRIFT#{timestamp}",
        "record_id":            record_id,
        "stack_name":           record.get("stack_name"),
        "stack_id":             record.get("stack_id"),
        "total_drifted":        record.get("total_drifted", 0),
        "drifted_resources":    json.dumps(record.get("drifted_resources", []), default=str),
        "risk_level":           record.get("risk_level"),
        "fix_status":           record.get("fix_status"),
        "root_cause":           record.get("root_cause"),
        "change_set_name":      record.get("change_set_name"),
        "template_snapshot_s3": record.get("template_snapshot_s3"),
        "approver":             record.get("approver"),
        "approval_status":      record.get("approval_status"),
        "validation_passed":    record.get("validation_passed"),
        "dry_run":              record.get("dry_run", False),
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "ttl":                  int(time.time()) + (90 * 24 * 3600),
    }
    item = {k: v for k, v in item.items() if v is not None}
    table.put_item(Item=item)
    logger.info(f"Audit record written: {record_id}")
    return record_id


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
def write_approval_decision(
    table_name: str,
    record_id: str,
    decision: str,
    approver: str,
    region: str,
) -> None:
    table = _dynamo(region).Table(table_name)
    table.put_item(
        Item={
            "record_id":  record_id,
            "decision":   decision,
            "approver":   approver,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "ttl":        int(time.time()) + (7 * 24 * 3600),
        }
    )
    logger.info(f"Approval decision recorded: {decision} by {approver}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
def update_audit_record(
    table_name: str,
    stack_name: str,
    timestamp: str,
    updates: Dict[str, Any],
    region: str,
) -> None:
    """Updates specific fields on an existing audit record."""
    table   = _dynamo(region).Table(table_name)
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return
    set_expr    = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(updates)))
    attr_names  = {f"#f{i}": k for i, k in enumerate(updates.keys())}
    attr_values = {f":v{i}": v for i, v in enumerate(updates.values())}
    table.update_item(
        Key={"PK": f"STACK#{stack_name}", "SK": f"DRIFT#{timestamp}"},
        UpdateExpression=set_expr,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )
    logger.info(f"Audit record updated for stack: {stack_name}")
