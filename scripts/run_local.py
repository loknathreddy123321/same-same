"""
scripts/run_local.py
--------------------
Local runner for the CFN Drift Fixer agent.

Flow:
  1. Detect drift + analyze + classify + plan
  2. Show stakeholder the full details (drifted resources, diff, root cause)
  3. Ask for approval in terminal (simulates Slack approval)
  4. If approved → execute change set → validate → confirm IN SYNC
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

RISK_COLOR = {"LOW": GREEN, "MEDIUM": YELLOW, "HIGH": RED}
STATUS_COLOR = {
    "VALIDATED": GREEN, "APPLIED": GREEN, "NO_DRIFT": GREEN,
    "SKIPPED":   YELLOW, "PENDING": YELLOW,
    "FAILED":    RED,    "REJECTED": RED,
}

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("agent").setLevel(logging.INFO)


def c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def banner(title: str) -> str:
    line = "─" * 54
    return f"\n{CYAN}{BOLD}{line}\n  {title}\n{line}{RESET}"


def main():
    parser = argparse.ArgumentParser(description="CFN Drift Fixer — Local Runner")
    parser.add_argument("--stack",   required=True)
    parser.add_argument("--region",  default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(banner("CFN Drift Fixer Agent"))
    print(f"  Region:  {c(args.region, CYAN)}")
    print(f"  Stacks:  {c(args.stack, CYAN)}")
    mode = c("DRY RUN — no changes will be made", GREEN) if args.dry_run else c("LIVE — changes WILL be applied", RED)
    print(f"  Mode:    {mode}")

    if not args.dry_run:
        confirm = input(f"\n  {c('WARNING', RED)}: This will apply real changes.\n  Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("  Aborted.")
            return

    from agent.graph import run_agent
    from agent.state import FixStatus

    # ── PHASE 1: Detect, Analyze, Plan ──────────────────────────────────────
    print(f"\n{BOLD}Phase 1 — Detecting and planning...{RESET}")

    # Run with dry_run=True first to get the plan without executing
    state = run_agent(
        stack_name=args.stack,
        aws_region=args.region,
        dry_run=True,   # Always plan in dry run first
        approval_status=FixStatus.REJECTED,  # Don't execute yet
    )

    # Show drift details
    _print_drift_details(state)

    # If no drift found — done
    if state.get("total_drifted", 0) == 0 or state.get("fix_status") == FixStatus.NO_DRIFT:
        print(f"\n  {c('✅ Stack is IN SYNC — no drift detected', GREEN)}")
        return

    if state.get("fix_status") == FixStatus.FAILED:
        print(f"\n  {c('❌ Planning failed:', RED)} {state.get('error_message')}")
        return

    if state.get("fix_status") == FixStatus.SKIPPED:
        print(f"\n  {c('🚨 Blocked by safety gate:', RED)} {state.get('error_message')}")
        return

    # ── PHASE 2: Stakeholder Approval ────────────────────────────────────────
    print(banner("Stakeholder Approval Required"))

    risk_level = state.get("risk_level") or "UNKNOWN"
    risk_color = RISK_COLOR.get(risk_level, RESET)

    print(f"\n  Stack:       {c(args.stack, CYAN)}")
    print(f"  Risk Level:  {c(risk_level, risk_color)}")
    print(f"  Drifted:     {c(str(state.get('total_drifted', 0)), YELLOW)} resource(s)")

    print(f"\n  {BOLD}Root Cause:{RESET}")
    print(f"  {state.get('root_cause', 'Unknown')}")

    print(f"\n  {BOLD}Drifted Resources:{RESET}")
    for r in state.get("drifted_resources", []):
        print(f"    • {c(r['resource_type'], YELLOW)} — {r.get('logical_id', '')} ({r['drift_status']})")
        for diff in r.get("property_differences", [])[:3]:
            path   = diff.get("PropertyPath", "")
            actual = diff.get("ActualValue", "")[:60]
            dtype  = diff.get("DifferenceType", "")
            print(f"      └ {path} [{dtype}] actual: {actual}")

    print(f"\n  {BOLD}Change Set Diff (what will change):{RESET}")
    diff_text = state.get("change_set_diff") or "Not available"
    for line in diff_text.split("\n"):
        print(f"  {line}")

    print(f"\n  {BOLD}Snapshot:{RESET} {state.get('template_snapshot_s3') or 'Not created (dry run)'}")

    plan = state.get("remediation_plan", {})
    if plan:
        print(f"\n  {BOLD}Rollback Plan:{RESET}")
        print(f"  {plan.get('rollback_procedure', 'N/A')}")

    if args.dry_run:
        print(f"\n  {c('DRY RUN — stopping here. Remove --dry-run to actually fix.', YELLOW)}")
        _print_result(args.stack, state, 0)
        return

    # Ask for approval
    print(f"\n  {c('─' * 50, CYAN)}")
    decision = input(f"\n  Approve this fix? {c('[yes/no]', BOLD)}: ").strip().lower()

    if decision != "yes":
        print(f"\n  {c('🚫 Fix REJECTED by stakeholder', RED)}")
        return

    approver = input(f"  Your name (for audit trail): ").strip() or "local-user"

    # ── PHASE 3: Execute Fix ──────────────────────────────────────────────────
    print(f"\n{BOLD}Phase 2 — Executing approved fix...{RESET}")

    t0 = time.time()
    final_state = run_agent(
        stack_name=args.stack,
        aws_region=args.region,
        dry_run=False,
        approval_status=FixStatus.APPROVED,
        approver=approver,
    )
    elapsed = round(time.time() - t0, 1)

    _print_result(args.stack, final_state, elapsed)


def _print_drift_details(state: dict):
    total = state.get("total_drifted", 0)
    if total == 0:
        return
    print(f"\n  Found {c(str(total), YELLOW)} drifted resource(s)")
    print(f"  Root Cause: {state.get('root_cause', 'Analyzing...')}")


def _print_result(stack_name: str, state: dict, elapsed: float):
    fix_status = state.get("fix_status") or "UNKNOWN"
    risk_level = state.get("risk_level") or "N/A"
    status_col = STATUS_COLOR.get(fix_status, RESET)
    risk_col   = RISK_COLOR.get(risk_level, RESET)

    print(f"\n{banner('Result')}")
    print(f"  Stack:         {c(stack_name, CYAN)}")
    print(f"  Fix Status:    {c(fix_status, status_col)}")
    print(f"  Risk Level:    {c(risk_level, risk_col)}")
    print(f"  Drifted:       {state.get('total_drifted', 0)}")
    print(f"  Validation:    {_fmt_val(state.get('validation_passed'))}")
    print(f"  Audit ID:      {state.get('audit_record_id') or '—'}")
    print(f"  Snapshot:      {state.get('template_snapshot_s3') or '—'}")
    if elapsed:
        print(f"  Elapsed:       {elapsed}s")

    if state.get("error_message"):
        print(f"\n  {c('Error:', RED)} {state['error_message']}")

    print(f"\n  Execution Trace:")
    for step in state.get("execution_trace", []):
        print(f"    {step}")

    if state.get("fix_status") == "VALIDATED":
        print(f"\n  {c('✅ Stack is now IN SYNC — drift fixed successfully!', GREEN)}")
    elif state.get("fix_status") == "NO_DRIFT":
        print(f"\n  {c('✅ Stack is IN SYNC — no drift detected', GREEN)}")


def _fmt_val(val) -> str:
    if val is True:  return c("✅ PASSED", GREEN)
    if val is False: return c("❌ FAILED", RED)
    return "—"


if __name__ == "__main__":
    main()
