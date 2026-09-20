#!/bin/bash
# scripts/safety_controls.sh
# ---------------------------
# Quick CLI commands for all safety controls.
# Run any of these instantly from terminal during an incident.

REGION=${AWS_REGION:-ap-south-1}

echo ""
echo "CFN Drift Fixer — Safety Controls"
echo "==================================="
echo ""

# ── EMERGENCY KILL SWITCH ─────────────────────────────────────────────────────
kill_switch_on() {
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/kill-switch" \
    --value "true" --type String --overwrite \
    --region $REGION
  echo "🚨 Kill switch ACTIVATED — agent will halt on next run"
}

kill_switch_off() {
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/kill-switch" \
    --value "false" --type String --overwrite \
    --region $REGION
  echo "✅ Kill switch deactivated"
}

# ── CHANGE FREEZE ─────────────────────────────────────────────────────────────
freeze_on() {
  REASON=${1:-"Manual change freeze"}
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/change-freeze" \
    --value "true" --type String --overwrite \
    --region $REGION
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/change-freeze-reason" \
    --value "$REASON" --type String --overwrite \
    --region $REGION
  echo "❄️  Change freeze ACTIVATED: $REASON"
}

freeze_off() {
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/change-freeze" \
    --value "false" --type String --overwrite \
    --region $REGION
  echo "✅ Change freeze lifted"
}

# ── MAINTENANCE WINDOW ────────────────────────────────────────────────────────
set_maintenance_window() {
  # Weekdays 02:00-06:00 UTC
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/maintenance-window" \
    --value '{"days":[1,2,3,4,5],"start_hour_utc":2,"end_hour_utc":6}' \
    --type String --overwrite \
    --region $REGION
  echo "✅ Maintenance window set: Mon-Fri 02:00-06:00 UTC"
}

# ── EXCLUSION LIST ────────────────────────────────────────────────────────────
set_exclusions() {
  # Add logical IDs or resource types to never touch
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/excluded-resources" \
    --value '["MyLegacyRole","AWS::RDS::DBInstance","ProductionKMSKey"]' \
    --type String --overwrite \
    --region $REGION
  echo "✅ Exclusion list updated"
}

# ── MONITORED STACKS ──────────────────────────────────────────────────────────
set_stacks() {
  aws ssm put-parameter \
    --name "/cfn-drift-fixer/monitored-stacks" \
    --value '["prod-app-stack","prod-infra-stack","prod-data-stack"]' \
    --type String --overwrite \
    --region $REGION
  echo "✅ Monitored stacks updated"
}

# ── STATUS CHECK ──────────────────────────────────────────────────────────────
status() {
  echo ""
  echo "Current Safety Status:"
  echo "----------------------"

  KS=$(aws ssm get-parameter --name "/cfn-drift-fixer/kill-switch" \
    --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "not set")
  CF=$(aws ssm get-parameter --name "/cfn-drift-fixer/change-freeze" \
    --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "not set")
  DR=$(aws ssm get-parameter --name "/cfn-drift-fixer/dry-run" \
    --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "not set")

  echo "  Kill switch:    $KS"
  echo "  Change freeze:  $CF"
  echo "  Dry run:        $DR"
  echo ""
}

# ── USAGE ─────────────────────────────────────────────────────────────────────
usage() {
  echo "Usage:"
  echo "  source scripts/safety_controls.sh"
  echo ""
  echo "  kill_switch_on              # Emergency halt"
  echo "  kill_switch_off             # Resume"
  echo "  freeze_on 'Q4 release'      # Freeze with reason"
  echo "  freeze_off                  # Lift freeze"
  echo "  set_maintenance_window      # Weekdays 02-06 UTC"
  echo "  set_exclusions              # Update exclusion list"
  echo "  set_stacks                  # Update monitored stacks"
  echo "  status                      # Show current state"
}

usage
