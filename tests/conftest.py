"""
tests/conftest.py
-----------------
Shared pytest fixtures. All AWS mocked with moto.
Tests never hit real AWS.
"""

import os
import json
import pytest
import boto3

# Set env vars BEFORE any imports that might use them
os.environ.setdefault("AWS_DEFAULT_REGION",       "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",        "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY",    "test")
os.environ.setdefault("AWS_REGION",               "us-east-1")
os.environ.setdefault("DYNAMODB_AUDIT_TABLE",     "cfn-drift-audit-test")
os.environ.setdefault("DYNAMODB_APPROVAL_TABLE",  "cfn-drift-approvals-test")
os.environ.setdefault("S3_SNAPSHOT_BUCKET",       "cfn-drift-snapshots-test")
os.environ.setdefault("BEDROCK_MODEL_ID",         "anthropic.claude-3-sonnet-20240229-v1:0")
os.environ.setdefault("DRY_RUN",                  "true")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def aws_region():
    return "us-east-1"


@pytest.fixture
def sample_drifted_resources():
    return [
        {
            "logical_id":           "MyBucket",
            "resource_id":          "my-bucket-12345",
            "resource_type":        "AWS::S3::Bucket",
            "drift_status":         "MODIFIED",
            "expected_properties":  {"Tags": []},
            "actual_properties":    {"Tags": [{"Key": "Manual", "Value": "True"}]},
            "property_differences": [
                {
                    "PropertyPath": "/Tags",
                    "ExpectedValue": "[]",
                    "ActualValue":   '[{"Key":"Manual","Value":"True"}]',
                    "DifferenceType": "ADD",
                }
            ],
            "timestamp": "2024-01-15T10:00:00+00:00",
        }
    ]


@pytest.fixture
def sample_iam_drifted_resources():
    return [
        {
            "logical_id":           "MyRole",
            "resource_id":          "arn:aws:iam::123:role/MyRole",
            "resource_type":        "AWS::IAM::Role",
            "drift_status":         "MODIFIED",
            "expected_properties":  {},
            "actual_properties":    {},
            "property_differences": [],
            "timestamp":            "2024-01-15T10:00:00+00:00",
        }
    ]


@pytest.fixture
def base_state(aws_region):
    """Minimal valid DriftFixState for testing."""
    return {
        "stack_name":           "test-stack",
        "aws_region":           aws_region,
        "dry_run":              True,
        "stack_id":             None,
        "detection_id":         None,
        "drifted_resources":    [],
        "total_drifted":        0,
        "detection_timestamp":  None,
        "root_cause":           None,
        "impact":               None,
        "affected_services":    [],
        "llm_recommendation":   None,
        "risk_level":           None,
        "risk_reasoning":       None,
        "approval_required":    False,
        "remediation_plan":     None,
        "template_snapshot_s3": None,
        "template_body":        None,
        "task_token":           None,
        "approval_status":      None,
        "approver":             None,
        "change_set_id":        None,
        "change_set_name":      None,
        "fix_applied":          False,
        "fix_timestamp":        None,
        "validation_passed":    None,
        "post_fix_drift_count": None,
        "fix_status":           None,
        "audit_record_id":      None,
        "error_message":        None,
        "execution_trace":      [],
    }
