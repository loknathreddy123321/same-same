"""
tests/test_safety.py
--------------------
Unit tests for all 10 safety measures.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError


import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent.safety import (
    check_stack_is_stable,
    is_kill_switch_active,
    is_change_freeze_active,
    is_within_maintenance_window,
    enforce_blast_radius,
    filter_excluded_resources,
    format_diff_for_slack,
    needs_dual_approval,
    run_all_preflight_checks,
)


# ─── 1. Stack State Check ─────────────────────────────────────────────────────

class TestStackStateCheck:

    @patch("agent.safety.boto3.client")
    def test_stable_stack_returns_true(self, mock_boto):
        mock_boto.return_value.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]
        }
        ok, status = check_stack_is_stable("my-stack", "us-east-1")
        assert ok is True
        assert status == "UPDATE_COMPLETE"

    @patch("agent.safety.boto3.client")
    def test_in_progress_stack_returns_false(self, mock_boto):
        mock_boto.return_value.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]
        }
        ok, status = check_stack_is_stable("my-stack", "us-east-1")
        assert ok is False
        assert status == "UPDATE_IN_PROGRESS"

    @patch("agent.safety.boto3.client")
    def test_api_error_returns_false(self, mock_boto):
        mock_boto.return_value.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack not found"}}, "describe_stacks"
        )
        ok, status = check_stack_is_stable("missing-stack", "us-east-1")
        assert ok is False


# ─── 5. Kill Switch ───────────────────────────────────────────────────────────

class TestKillSwitch:

    @patch("agent.safety.boto3.client")
    def test_kill_switch_active(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "true"}
        }
        assert is_kill_switch_active("us-east-1") is True

    @patch("agent.safety.boto3.client")
    def test_kill_switch_inactive(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "false"}
        }
        assert is_kill_switch_active("us-east-1") is False

    @patch("agent.safety.boto3.client")
    def test_kill_switch_not_set_returns_false(self, mock_boto):
        err = ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "get_parameter")
        mock_boto.return_value.get_parameter.side_effect = err
        mock_boto.return_value.exceptions.ParameterNotFound = type(err)
        assert is_kill_switch_active("us-east-1") is False

    @patch("agent.safety.boto3.client")
    def test_api_error_fails_safe(self, mock_boto):
        # ParameterNotFound must be a real exception class for the `except`
        # clause in is_kill_switch_active to evaluate, even though this test
        # raises a different exception.
        mock_boto.return_value.exceptions.ParameterNotFound = ClientError
        mock_boto.return_value.get_parameter.side_effect = Exception("Network error")
        # Should return True (fail safe — block if can't check)
        assert is_kill_switch_active("us-east-1") is True


# ─── 10. Change Freeze ────────────────────────────────────────────────────────

class TestChangeFreeze:

    @patch("agent.safety.boto3.client")
    def test_freeze_active(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "true"}
        }
        frozen, reason = is_change_freeze_active("us-east-1")
        assert frozen is True

    @patch("agent.safety.boto3.client")
    def test_freeze_inactive(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "false"}
        }
        frozen, _ = is_change_freeze_active("us-east-1")
        assert frozen is False


# ─── 4. Maintenance Window ────────────────────────────────────────────────────

class TestMaintenanceWindow:

    @patch("agent.safety.boto3.client")
    @patch("agent.safety.datetime")
    def test_within_window(self, mock_dt, mock_boto):
        from datetime import datetime, timezone
        # Monday 03:00 UTC — inside window [Mon-Fri, 02:00-06:00].
        # A real datetime is used (rather than a MagicMock) so that
        # .isoweekday()/.hour behave correctly inside is_within_maintenance_window.
        mock_dt.now.return_value = datetime(2024, 1, 15, 3, 0, 0, tzinfo=timezone.utc)

        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({
                "days": [1, 2, 3, 4, 5],
                "start_hour_utc": 2,
                "end_hour_utc": 6,
            })}
        }
        allowed, reason = is_within_maintenance_window("us-east-1")
        assert allowed is True
        assert "Within window" in reason

    @patch("agent.safety.boto3.client")
    def test_no_window_configured_allows_all(self, mock_boto):
        err = ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "get_parameter")
        mock_boto.return_value.get_parameter.side_effect = err
        mock_boto.return_value.exceptions.ParameterNotFound = type(err)
        ok, reason = is_within_maintenance_window("us-east-1")
        assert ok is True
        assert "No maintenance window" in reason


# ─── 6. Blast Radius ─────────────────────────────────────────────────────────

class TestBlastRadius:

    def test_within_limit_passes_all(self):
        stacks = ["stack-a", "stack-b", "stack-c"]
        allowed, blocked = enforce_blast_radius(stacks)
        assert len(allowed) == 3
        assert len(blocked) == 0

    def test_over_limit_blocks_excess(self):
        stacks = [f"stack-{i}" for i in range(10)]
        allowed, blocked = enforce_blast_radius(stacks)
        assert len(allowed) == 5       # MAX_STACKS_PER_RUN default
        assert len(blocked) == 5
        assert set(allowed) | set(blocked) == set(stacks)

    def test_exactly_at_limit_passes_all(self):
        stacks = [f"stack-{i}" for i in range(5)]
        allowed, blocked = enforce_blast_radius(stacks)
        assert len(allowed) == 5
        assert len(blocked) == 0


# ─── 8. Resource Exclusion ───────────────────────────────────────────────────

class TestResourceExclusion:

    @patch("agent.safety.boto3.client")
    def test_excludes_by_logical_id(self, mock_boto, sample_drifted_resources):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps(["MyBucket"])}
        }
        mock_boto.return_value.exceptions.ParameterNotFound = Exception

        to_fix, excluded = filter_excluded_resources(sample_drifted_resources, "us-east-1")
        assert len(excluded) == 1
        assert excluded[0]["logical_id"] == "MyBucket"
        assert len(to_fix) == 0

    @patch("agent.safety.boto3.client")
    def test_excludes_by_resource_type(self, mock_boto, sample_drifted_resources):
        mock_boto.return_value.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps(["AWS::S3::Bucket"])}
        }
        mock_boto.return_value.exceptions.ParameterNotFound = Exception

        to_fix, excluded = filter_excluded_resources(sample_drifted_resources, "us-east-1")
        assert len(excluded) == 1

    @patch("agent.safety.boto3.client")
    def test_no_exclusions_when_ssm_empty(self, mock_boto, sample_drifted_resources):
        err = ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "get_parameter")
        mock_boto.return_value.get_parameter.side_effect = err
        mock_boto.return_value.exceptions.ParameterNotFound = type(err)

        to_fix, excluded = filter_excluded_resources(sample_drifted_resources, "us-east-1")
        assert len(to_fix)   == len(sample_drifted_resources)
        assert len(excluded) == 0


# ─── 9. Dual Approval ────────────────────────────────────────────────────────

class TestDualApproval:

    def test_high_risk_needs_dual_approval(self):
        assert needs_dual_approval("HIGH")   is True
        assert needs_dual_approval("MEDIUM") is False
        assert needs_dual_approval("LOW")    is False


# ─── Change Set Diff Formatting ──────────────────────────────────────────────

class TestDiffFormatting:

    def test_format_diff_shows_action(self):
        changes = [
            {"logical_id": "MyRole", "resource_type": "AWS::IAM::Role",
             "action": "Modify", "replacement": "False", "details": []},
        ]
        result = format_diff_for_slack(changes)
        assert "MyRole" in result
        assert "Modify" in result

    def test_format_diff_warns_replacement(self):
        changes = [
            {"logical_id": "MyDB", "resource_type": "AWS::RDS::DBInstance",
             "action": "Modify", "replacement": "True", "details": []},
        ]
        result = format_diff_for_slack(changes)
        assert "REPLACEMENT" in result

    def test_format_empty_diff(self):
        result = format_diff_for_slack([])
        assert "No changes" in result

    def test_caps_at_10_changes(self):
        changes = [
            {"logical_id": f"Res{i}", "resource_type": "AWS::S3::Bucket",
             "action": "Modify", "replacement": "False", "details": []}
            for i in range(15)
        ]
        result = format_diff_for_slack(changes)
        assert "5 more" in result
