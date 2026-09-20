"""
scripts/setup_ssm.py
--------------------
One-time setup: creates all required SSM parameters.
Run once after deploy: make setup-ssm
"""

from __future__ import annotations

import argparse
import json
import sys
import getpass
import boto3


RED   = "\033[91m"
GREEN = "\033[92m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"


def prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        val = getpass.getpass(f"  {label}{suffix}: ")
    else:
        val = input(f"  {label}{suffix}: ").strip()
    return val or default


def put_param(ssm, name: str, value: str, param_type: str = "String", description: str = "") -> None:
    ssm.put_parameter(
        Name=name, Value=value, Type=param_type,
        Description=description, Overwrite=True
    )
    masked = value[:4] + "****" if param_type == "SecureString" else value
    print(f"  {GREEN}✅ {name}{RESET} = {masked}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CFN Drift Fixer — SSM Setup")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Use env vars instead of prompts (for CI)")
    args = parser.parse_args()

    ssm = boto3.client("ssm", region_name=args.region)

    print(f"\n{CYAN}{BOLD}CFN Drift Fixer — SSM Parameter Setup{RESET}")
    print(f"Region: {args.region}\n")

    if args.non_interactive:
        import os
        params = {
            "slack_token":   os.environ["SLACK_BOT_TOKEN"],
            "slack_channel": os.environ["SLACK_CHANNEL_ID"],
            "stacks":        os.environ.get("MONITORED_STACKS", "[]"),
        }
    else:
        print("Slack Configuration:")
        params = {
            "slack_token":   prompt("Slack Bot Token (xoxb-...)", secret=True),
            "slack_channel": prompt("Slack Channel ID (C0XXXXXXX)"),
        }
        print("\nMonitored Stacks:")
        stacks_raw = prompt(
            "Stack names (comma-separated)",
            default="my-stack-1,my-stack-2"
        )
        stacks_list = [s.strip() for s in stacks_raw.split(",") if s.strip()]
        params["stacks"] = json.dumps(stacks_list)

    print("\nWriting SSM parameters...")

    put_param(ssm, "/cfn-drift-fixer/slack-bot-token",   params["slack_token"],
              "SecureString", "Slack bot token for CFN Drift Fixer")
    put_param(ssm, "/cfn-drift-fixer/slack-channel-id",  params["slack_channel"],
              "String",       "Slack channel ID for notifications")
    put_param(ssm, "/cfn-drift-fixer/monitored-stacks",  params["stacks"],
              "String",       "JSON list of stacks to monitor")

    # Multi-account config (optional)
    if not args.non_interactive:
        print("\nMulti-Account Config (optional — press Enter to skip):")
        multi = prompt("Account configs JSON (or leave empty)")
        if multi.strip():
            put_param(ssm, "/cfn-drift-fixer/accounts", multi,
                      "String", "Multi-account config for CFN Drift Fixer")

    print(f"\n{GREEN}{BOLD}SSM setup complete ✅{RESET}")
    print("You can now deploy: make deploy\n")


if __name__ == "__main__":
    main()
