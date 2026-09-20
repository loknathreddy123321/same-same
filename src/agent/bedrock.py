"""
src/agent/bedrock.py
--------------------
LLM client. Priority order:
  1. Azure OpenAI  — if AZURE_OPENAI_API_KEY is set
  2. AWS Bedrock   — if BEDROCK_MODEL_ID is set
  3. Groq (free)   — if GROQ_API_KEY is set
"""

import json
import logging
import os
from typing import Optional

from langsmith import traceable

logger = logging.getLogger(__name__)


def _region() -> str:
    # Lambda reserves AWS_REGION as a read-only runtime var, so
    # infra/template.yaml injects the region as AWS_REGION_NAME instead.
    return os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")


@traceable(run_type="llm", name="invoke-llm")
def invoke_claude(prompt: str, max_tokens: int = 2048, temperature: float = 0.1, region: Optional[str] = None) -> str:
    azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if azure_key:
        return _call_azure_openai(prompt, azure_key, max_tokens, temperature)

    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if model_id:
        return _call_bedrock(prompt, model_id, max_tokens, temperature, region or _region())

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        return _call_groq(prompt, groq_key, max_tokens)

    raise RuntimeError(
        "No LLM configured. Set AZURE_OPENAI_API_KEY, BEDROCK_MODEL_ID, or GROQ_API_KEY in .env"
    )


def invoke_claude_json(prompt: str, max_tokens: int = 2048) -> dict:
    raw   = invoke_claude(prompt, max_tokens=max_tokens)
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned non-JSON: {clean[:300]}")
        raise ValueError(f"LLM response not valid JSON: {e}") from e


def _call_azure_openai(prompt: str, api_key: str, max_tokens: int, temperature: float) -> str:
    from openai import AzureOpenAI

    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

    client   = AzureOpenAI(api_key=api_key, azure_endpoint=endpoint, api_version=api_version)
    response = client.chat.completions.create(
        model=deployment,  # Azure routes by deployment name, not model name
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = (response.choices[0].message.content or "").strip()
    logger.info(f"Azure OpenAI response (first 100 chars): {text[:100]}")
    return text


def _call_bedrock(prompt: str, model_id: str, max_tokens: int, temperature: float, region: str) -> str:
    import boto3
    client   = boto3.client("bedrock-runtime", region_name=region)
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    logger.info(f"Bedrock response (first 100 chars): {text[:100]}")
    return text


def _call_groq(prompt: str, api_key: str, max_tokens: int) -> str:
    from groq import Groq
    model_id = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    client   = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    text = (response.choices[0].message.content or "").strip()
    logger.info(f"Groq response (first 100 chars): {text[:100]}")
    return text