"""
src/agent/multi_account.py
--------------------------
Multi-account drift scanning.

Pattern: Hub-and-spoke.
  - The Drift Fixer Lambda runs in the HUB account.
  - It assumes a cross-account IAM role in each SPOKE account.
  - Each spoke account has a read/write role for CFN, DynamoDB, S3.

Account config comes from SSM Parameter Store (JSON list):
  /cfn-drift-fixer/accounts
  [
    {"account_id": "111111111111", "role_name": "CfnDriftFixer-CrossAccount", "stacks": ["stack-a"]},
    {"account_id": "222222222222", "role_name": "CfnDriftFixer-CrossAccount", "stacks": ["stack-b", "stack-c"]}
  ]
"""


import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"})


# ─── Account Config ───────────────────────────────────────────────────────────

def get_account_configs(region: str) -> List[Dict[str, Any]]:
    """
    Loads account + stack list from SSM.
    Falls back to empty list (single-account mode) if not configured.
    """
    ssm = boto3.client("ssm", region_name=region, config=_CONFIG)
    try:
        param    = ssm.get_parameter(Name="/cfn-drift-fixer/accounts")
        accounts = json.loads(param["Parameter"]["Value"])
        logger.info(f"Loaded {len(accounts)} account config(s) from SSM")
        return accounts
    except ssm.exceptions.ParameterNotFound:
        logger.info("No multi-account config found — running in single-account mode")
        return []
    except Exception as e:
        logger.error(f"Failed to load account config from SSM: {e}")
        return []


# ─── Role Assumption ──────────────────────────────────────────────────────────

def assume_role(
    account_id: str,
    role_name: str,
    session_name: str = "CfnDriftFixer",
    region: str = "us-east-1",
) -> Optional[Dict[str, str]]:
    """
    Assumes a cross-account IAM role.
    Returns temporary credentials dict or None on failure.
    """
    sts = boto3.client("sts", region_name=region, config=_CONFIG)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    try:
        resp  = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=3600,
        )
        creds = resp["Credentials"]
        logger.info(f"Assumed role in account {account_id}: {role_arn}")
        return {
            "aws_access_key_id":     creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token":     creds["SessionToken"],
        }
    except ClientError as e:
        logger.error(f"Failed to assume role {role_arn}: {e}")
        return None


def get_cfn_client_for_account(
    credentials: Dict[str, str],
    region: str,
):
    """Returns a CFN boto3 client using assumed-role credentials."""
    return boto3.client(
        "cloudformation",
        region_name=region,
        aws_access_key_id=credentials["aws_access_key_id"],
        aws_secret_access_key=credentials["aws_secret_access_key"],
        aws_session_token=credentials["aws_session_token"],
        config=_CONFIG,
    )


# ─── Multi-Account Scanner ────────────────────────────────────────────────────

def scan_all_accounts(region: str, dry_run: bool = False) -> List[Dict[str, Any]]:
    """
    Scans all configured accounts and stacks.
    Returns list of per-stack results.

    Each result: {account_id, stack_name, fix_status, total_drifted, error}
    """
    from agent.graph import run_agent

    account_configs = get_account_configs(region)

    # If no multi-account config, scan stacks in current account from SSM
    if not account_configs:
        return _scan_current_account(region, dry_run)

    all_results = []

    for acc in account_configs:
        account_id = acc.get("account_id")
        role_name  = acc.get("role_name", "CfnDriftFixer-CrossAccount")
        stacks     = acc.get("stacks", [])

        if not account_id or not stacks:
            logger.warning(f"Skipping invalid account config: {acc}")
            continue

        logger.info(f"Scanning account {account_id} — {len(stacks)} stack(s)")

        # Assume role in target account
        creds = assume_role(account_id, role_name, region=region)
        if not creds:
            all_results.append({
                "account_id": account_id,
                "error":      f"Could not assume role {role_name}",
            })
            continue

        # Scan each stack in this account
        for stack_name in stacks:
            try:
                # Inject cross-account credentials via env override
                # (agent.cfn uses boto3 which reads these)
                import boto3.session
                session = boto3.Session(
                    aws_access_key_id=creds["aws_access_key_id"],
                    aws_secret_access_key=creds["aws_secret_access_key"],
                    aws_session_token=creds["aws_session_token"],
                    region_name=region,
                )
                # Pass session to agent — agent uses module-level boto3 calls
                # For cross-account, we override environment temporarily
                result = _run_with_session(session, stack_name, region, dry_run)
                all_results.append({
                    "account_id":      account_id,
                    "stack_name":      stack_name,
                    "fix_status":      result.get("fix_status"),
                    "total_drifted":   result.get("total_drifted", 0),
                    "risk_level":      result.get("risk_level"),
                    "validation":      result.get("validation_passed"),
                    "audit_record_id": result.get("audit_record_id"),
                })

            except Exception as e:
                logger.error(f"Failed for {account_id}/{stack_name}: {e}")
                all_results.append({
                    "account_id": account_id,
                    "stack_name": stack_name,
                    "error":      str(e),
                })

    logger.info(f"Multi-account scan complete: {len(all_results)} result(s)")
    return all_results


def _scan_current_account(region: str, dry_run: bool) -> List[Dict[str, Any]]:
    """Falls back to SSM stack list in current account."""
    from agent.graph import run_agent
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    try:
        param  = ssm.get_parameter(Name="/cfn-drift-fixer/monitored-stacks")
        stacks = json.loads(param["Parameter"]["Value"])
    except Exception:
        logger.warning("No stacks configured in SSM")
        return []

    results = []
    for stack_name in stacks:
        try:
            result = run_agent(stack_name=stack_name, aws_region=region, dry_run=dry_run)
            results.append({
                "stack_name":  stack_name,
                "fix_status":  result.get("fix_status"),
                "total_drifted": result.get("total_drifted", 0),
            })
        except Exception as e:
            results.append({"stack_name": stack_name, "error": str(e)})

    return results


def _run_with_session(session, stack_name: str, region: str, dry_run: bool) -> dict:
    """
    Temporarily patches boto3.client/resource to use assumed-role session.
    Restores after run.
    NOTE: Not thread-safe — single-threaded Lambda only.
    """
    import boto3 as _boto3
    original_client   = _boto3.client
    original_resource = _boto3.resource

    def patched_client(service, **kwargs):
        kwargs.setdefault("region_name", region)
        return session.client(service, **kwargs)

    def patched_resource(service, **kwargs):
        kwargs.setdefault("region_name", region)
        return session.resource(service, **kwargs)

    _boto3.client   = patched_client
    _boto3.resource = patched_resource

    try:
        from agent.graph import run_agent
        return run_agent(stack_name=stack_name, aws_region=region, dry_run=dry_run)
    finally:
        # Always restore
        _boto3.client   = original_client
        _boto3.resource = original_resource


# ─── Cross-Account Role Template ──────────────────────────────────────────────

CROSS_ACCOUNT_ROLE_TEMPLATE = """
# Deploy this in EACH spoke account that you want to monitor.
# The hub account Lambda will assume this role.

AWSTemplateFormatVersion: "2010-09-09"
Description: CFN Drift Fixer - Cross-Account Role (deploy in each spoke account)

Parameters:
  HubAccountId:
    Type: String
    Description: Account ID of the hub account running the Drift Fixer Lambda

Resources:
  CfnDriftFixerRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: CfnDriftFixer-CrossAccount
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              AWS: !Sub "arn:aws:iam::{HubAccountId}:root"
            Action: sts:AssumeRole
            Condition:
              StringEquals:
                "sts:ExternalId": "cfn-drift-fixer"
      Policies:
        - PolicyName: CfnDriftFixerAccess
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action:
                  - cloudformation:DetectStackDrift
                  - cloudformation:DetectStackResourceDrift
                  - cloudformation:DescribeStackDriftDetectionStatus
                  - cloudformation:DescribeStackResourceDrifts
                  - cloudformation:DescribeStacks
                  - cloudformation:GetTemplate
                  - cloudformation:CreateChangeSet
                  - cloudformation:ExecuteChangeSet
                  - cloudformation:DeleteChangeSet
                  - cloudformation:DescribeChangeSet
                  - s3:PutObject
                  - iam:PassRole
                Resource: "*"

Outputs:
  RoleArn:
    Value: !GetAtt CfnDriftFixerRole.Arn
"""
