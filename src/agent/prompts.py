"""
src/agent/prompts.py
--------------------
All prompts for Bedrock Claude.
Explicit JSON-only instruction prevents markdown wrapping.
"""

DRIFT_ANALYSIS_PROMPT = """\
You are an AWS CloudFormation expert. Analyze the drift report below.

Stack: {stack_name}
Drifted Resources ({total_drifted} total):
{drifted_json}

Return ONLY valid JSON — no preamble, no markdown, no code fences:
{{
  "root_cause": "one-sentence explanation of why this drift likely occurred",
  "impact": "operational impact of leaving this drift unfixed",
  "affected_services": ["list", "of", "aws", "services"],
  "recommendation": "CHANGE_SET or IMPORT or MANUAL",
  "recommendation_reasoning": "why this strategy is safest for these specific resources"
}}
"""

REMEDIATION_PLAN_PROMPT = """\
You are an AWS CloudFormation engineer. Generate a remediation plan.

Stack: {stack_name}
Root Cause: {root_cause}
Recommendation: {recommendation}

Drifted Resources:
{drifted_json}

Return ONLY valid JSON — no preamble, no markdown, no code fences:
{{
  "strategy": "CHANGE_SET or IMPORT or MANUAL",
  "reasoning": "why this strategy",
  "estimated_risk": "LOW or MEDIUM or HIGH",
  "rollback_procedure": "step-by-step rollback if fix fails",
  "pre_fix_checks": ["check before applying fix", "another check"]
}}
"""
