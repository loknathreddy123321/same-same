"""
tests/test_nodes.py
-------------------
Unit tests for all agent nodes. Uses pytest-mock for Bedrock (not in moto).
"""

import json
import pytest
import boto3
from moto import mock_aws
from unittest.mock import patch, MagicMock

from agent.state import RiskLevel, FixStatus
from agent.nodes import (
    node_classify_risk,
    node_detect_drift,
    node_apply_fix,
    node_validate,
    node_analyze_drift,
)


# ─── classify_risk ────────────────────────────────────────────────────────────

class TestClassifyRisk:

    def _make_resource(self, resource_type, logical_id="MyResource"):
        return {
            "logical_id": logical_id,
            "resource_id": f"id-{logical_id}",
            "resource_type": resource_type,
            "drift_status": "MODIFIED",
            "expected_properties": {},
            "actual_properties": {},
            "property_differences": [],
            "timestamp": ""
        }

    def test_iam_role_is_high_risk(self, base_state, sample_iam_drifted_resources):
        state = {**base_state, "drifted_resources": sample_iam_drifted_resources}
        result = node_classify_risk(state)
        assert result["risk_level"]        == RiskLevel.HIGH.value
        assert result["approval_required"] is True
        assert result["approval_status"]   == FixStatus.PENDING.value

    def test_kms_key_is_high_risk(self, base_state):
        state = {**base_state, "drifted_resources": [
            self._make_resource("AWS::KMS::Key", "MyKey")
        ]}
        result = node_classify_risk(state)
        assert result["risk_level"] == RiskLevel.HIGH.value

    def test_s3_bucket_is_low_risk(self, base_state, sample_drifted_resources):
        state = {**base_state, "drifted_resources": sample_drifted_resources}
        result = node_classify_risk(state)
        assert result["risk_level"]        == RiskLevel.LOW.value
        # Approval is always required, even for LOW risk — a human must see
        # the diff before anything is applied (see nodes.py node_classify_risk).
        assert result["approval_required"] is True
        assert result["approval_status"]   == FixStatus.PENDING.value

    def test_ec2_instance_is_medium_risk(self, base_state):
        state = {**base_state, "drifted_resources": [
            self._make_resource("AWS::EC2::Instance", "MyInstance")
        ]}
        result = node_classify_risk(state)
        assert result["risk_level"]        == RiskLevel.MEDIUM.value
        assert result["approval_required"] is True

    def test_mixed_high_and_low_takes_high(self, base_state, sample_drifted_resources, sample_iam_drifted_resources):
        mixed = sample_drifted_resources + sample_iam_drifted_resources
        state = {**base_state, "drifted_resources": mixed}
        result = node_classify_risk(state)
        assert result["risk_level"] == RiskLevel.HIGH.value

    def test_empty_resources_is_low_risk(self, base_state):
        state  = {**base_state, "drifted_resources": []}
        result = node_classify_risk(state)
        assert result["risk_level"] == RiskLevel.LOW.value

    def test_security_group_is_high_risk(self, base_state):
        state = {**base_state, "drifted_resources": [
            self._make_resource("AWS::EC2::SecurityGroup", "MySG")
        ]}
        result = node_classify_risk(state)
        assert result["risk_level"] == RiskLevel.HIGH.value

    def test_lambda_is_medium_risk(self, base_state):
        state = {**base_state, "drifted_resources": [
            self._make_resource("AWS::Lambda::Function", "MyFn")
        ]}
        result = node_classify_risk(state)
        assert result["risk_level"] == RiskLevel.MEDIUM.value


# ─── detect_drift ─────────────────────────────────────────────────────────────

class TestDetectDrift:

    @patch("agent.nodes.get_stack_id",                return_value="arn:aws:cloudformation:us-east-1:123:stack/test/abc")
    @patch("agent.nodes.trigger_drift_detection",     return_value="det-id-001")
    @patch("agent.nodes.wait_for_detection_complete", return_value="DETECTION_COMPLETE")
    @patch("agent.nodes.get_drifted_resources")
    def test_detects_drift_successfully(self, mock_get, mock_wait, mock_trigger, mock_id, base_state, sample_drifted_resources):
        mock_get.return_value = sample_drifted_resources
        result = node_detect_drift(base_state)
        assert result["total_drifted"]          == 1
        assert result["detection_id"]           == "det-id-001"
        assert len(result["drifted_resources"]) == 1

    @patch("agent.nodes.get_stack_id",                return_value="arn:aws:cloudformation:us-east-1:123:stack/test/abc")
    @patch("agent.nodes.trigger_drift_detection",     return_value="det-id-002")
    @patch("agent.nodes.wait_for_detection_complete", return_value="DETECTION_COMPLETE")
    @patch("agent.nodes.get_drifted_resources",       return_value=[])
    def test_no_drift_returns_zero(self, mock_get, mock_wait, mock_trigger, mock_id, base_state):
        result = node_detect_drift(base_state)
        assert result["total_drifted"]    == 0
        assert result["drifted_resources"] == []

    @patch("agent.nodes.get_stack_id",                return_value="arn:aws:cloudformation:us-east-1:123:stack/test/abc")
    @patch("agent.nodes.trigger_drift_detection",     return_value="det-id-003")
    @patch("agent.nodes.wait_for_detection_complete", return_value="DETECTION_FAILED")
    def test_detection_failed_sets_failed_status(self, mock_wait, mock_trigger, mock_id, base_state):
        result = node_detect_drift(base_state)
        assert result["fix_status"] == FixStatus.FAILED.value

    @patch("agent.nodes.get_stack_id", side_effect=Exception("Stack does not exist"))
    def test_exception_sets_failed_status(self, mock_id, base_state):
        result = node_detect_drift(base_state)
        assert result["fix_status"]    == FixStatus.FAILED.value
        assert "Stack does not exist" in result["error_message"]


# ─── analyze_drift ────────────────────────────────────────────────────────────

class TestAnalyzeDrift:

    @patch("agent.nodes.invoke_claude_json")
    def test_successful_analysis(self, mock_llm, base_state, sample_drifted_resources):
        mock_llm.return_value = {
            "root_cause":        "Manual tag added via console",
            "impact":            "Tagging policy drift",
            "affected_services": ["S3"],
            "recommendation":    "CHANGE_SET",
        }
        state  = {**base_state, "drifted_resources": sample_drifted_resources, "total_drifted": 1}
        result = node_analyze_drift(state)
        assert result["root_cause"]         == "Manual tag added via console"
        assert result["llm_recommendation"] == "CHANGE_SET"

    @patch("agent.nodes.invoke_claude_json", side_effect=Exception("Bedrock timeout"))
    def test_llm_failure_uses_fallback(self, mock_llm, base_state, sample_drifted_resources):
        state  = {**base_state, "drifted_resources": sample_drifted_resources, "total_drifted": 1}
        result = node_analyze_drift(state)
        assert result.get("root_cause")         is not None
        assert result.get("llm_recommendation") == "CHANGE_SET"

    def test_no_drift_skips_llm(self, base_state):
        result = node_analyze_drift(base_state)
        assert result["fix_status"] == FixStatus.NO_DRIFT.value


# ─── apply_fix ────────────────────────────────────────────────────────────────

class TestApplyFix:

    @mock_aws
    def test_applies_fix_when_approved(self, base_state):
        # node_apply_fix calls update_stack directly (not the preview change
        # set) — see the WHY note on node_apply_fix — so this exercises a real
        # (moto-mocked) CloudFormation stack rather than mocking functions the
        # current implementation no longer calls.
        region = "us-east-1"
        template = json.dumps({
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "MyBucket": {"Type": "AWS::S3::Bucket", "Properties": {}}
            },
        })
        cfn = boto3.client("cloudformation", region_name=region)
        cfn.create_stack(StackName="test-stack", TemplateBody=template)
        cfn.get_waiter("stack_create_complete").wait(StackName="test-stack")

        state = {
            **base_state,
            "aws_region":       region,
            "dry_run":          False,
            "approval_status":  FixStatus.APPROVED.value,
            "template_body":    template,
            "synced_template":  template,
        }
        result = node_apply_fix(state)
        assert result["fix_status"]  == FixStatus.APPLIED.value
        assert result["fix_applied"] is True

    def test_skips_when_rejected(self, base_state):
        state  = {**base_state, "approval_status": FixStatus.REJECTED.value}
        result = node_apply_fix(state)
        assert result["fix_status"]  == FixStatus.SKIPPED.value
        assert result["fix_applied"] is False

    @patch("agent.nodes.get_stack_template",  return_value='{"AWSTemplateFormatVersion": "2010-09-09"}')
    @patch("agent.nodes.create_change_set",   return_value=None)
    @patch("agent.nodes.execute_change_set",  return_value=True)
    def test_dry_run_does_not_set_fix_applied(self, mock_exec, mock_cs, mock_tpl, base_state):
        state = {
            **base_state,
            "dry_run":         True,
            "approval_status": FixStatus.APPROVED.value,
            "change_set_name": "drift-fix-dry",
            "template_body":   '{"AWSTemplateFormatVersion": "2010-09-09"}',
        }
        result = node_apply_fix(state)
        assert result["fix_applied"] is False

    def test_skips_when_previous_node_failed(self, base_state):
        state  = {
            **base_state,
            "approval_status": FixStatus.APPROVED.value,
            "fix_status":      FixStatus.FAILED.value
        }
        result = node_apply_fix(state)
        assert result["fix_applied"] is False


# ─── validate ─────────────────────────────────────────────────────────────────

class TestValidate:

    @patch("agent.nodes.trigger_drift_detection",     return_value="det-val-001")
    @patch("agent.nodes.wait_for_detection_complete", return_value="DETECTION_COMPLETE")
    @patch("agent.nodes.get_drifted_resources",       return_value=[])
    def test_validation_passes_when_no_drift(self, mock_get, mock_wait, mock_trigger, base_state):
        state  = {**base_state, "fix_applied": True}
        result = node_validate(state)
        assert result["validation_passed"]    is True
        assert result["post_fix_drift_count"] == 0
        assert result["fix_status"]           == FixStatus.VALIDATED.value

    @patch("agent.nodes.trigger_drift_detection",     return_value="det-val-002")
    @patch("agent.nodes.wait_for_detection_complete", return_value="DETECTION_COMPLETE")
    @patch("agent.nodes.get_drifted_resources")
    def test_validation_fails_when_drift_remains(self, mock_get, mock_wait, mock_trigger, base_state, sample_drifted_resources):
        mock_get.return_value = sample_drifted_resources
        state  = {**base_state, "fix_applied": True}
        result = node_validate(state)
        assert result["validation_passed"]    is False
        assert result["post_fix_drift_count"] == 1
        assert result["fix_status"]           == FixStatus.FAILED.value

    def test_skips_validation_when_not_applied(self, base_state):
        state  = {**base_state, "fix_applied": False}
        result = node_validate(state)
        assert result["validation_passed"] is None
