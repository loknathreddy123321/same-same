"""
src/agent/observability.py
--------------------------
Langfuse tracing for all LLM calls.
Wraps bedrock.invoke_claude_json() with trace/span/generation recording.

If LANGFUSE_SECRET_KEY is not set, tracing is silently disabled
so the app still works without Langfuse configured.
"""


import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Langfuse client (lazy init) ───────────────────────────────────────────────
_langfuse = None


def _get_langfuse():
    global _langfuse
    if _langfuse is not None:
        return _langfuse

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    host       = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    if not secret_key or not public_key:
        logger.info("Langfuse not configured — tracing disabled")
        return None

    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            secret_key=secret_key,
            public_key=public_key,
            host=host,
        )
        logger.info("Langfuse tracing enabled")
        return _langfuse
    except ImportError:
        logger.warning("langfuse package not installed — tracing disabled")
        return None
    except Exception as e:
        logger.warning(f"Langfuse init failed: {e} — tracing disabled")
        return None


# ── Traced LLM call ───────────────────────────────────────────────────────────

def invoke_claude_traced(
    prompt: str,
    node_name: str,
    stack_name: str,
    session_id: Optional[str] = None,
    max_tokens: int = 2048,
) -> dict:
    """
    Calls Bedrock Claude with full Langfuse tracing.
    Records: prompt, response, latency, model, token counts.
    Falls back to un-traced call if Langfuse is unavailable.
    """
    from agent.bedrock import invoke_claude_json  # local import to avoid circular

    lf      = _get_langfuse()
    trace   = None
    span    = None
    t_start = time.time()

    # ── Start trace ───────────────────────────────────────────────────────────
    if lf:
        try:
            trace = lf.trace(
                name=f"cfn-drift-fixer:{node_name}",
                session_id=session_id or stack_name,
                metadata={"stack_name": stack_name, "node": node_name},
                tags=["cfn-drift-fixer", node_name],
            )
            span = trace.span(name=node_name, input={"prompt_length": len(prompt)})
        except Exception as e:
            logger.warning(f"Langfuse trace start failed: {e}")
            trace = span = None

    # ── Call Bedrock ──────────────────────────────────────────────────────────
    error = None
    result: dict = {}

    try:
        result = invoke_claude_json(prompt, max_tokens=max_tokens)
    except Exception as e:
        error = e
        raise
    finally:
        latency_ms = int((time.time() - t_start) * 1000)

        # ── Record generation in Langfuse ─────────────────────────────────────
        if lf and trace:
            try:
                model_id = os.environ.get(
                    "BEDROCK_MODEL_ID",
                    "anthropic.claude-3-sonnet-20240229-v1:0",
                )
                generation = trace.generation(
                    name=f"{node_name}-generation",
                    model=model_id,
                    model_parameters={
                        "temperature": 0.1,
                        "max_tokens":  max_tokens,
                    },
                    input=prompt,
                    output=str(result) if result else str(error),
                    metadata={
                        "latency_ms":  latency_ms,
                        "stack_name":  stack_name,
                        "success":     error is None,
                    },
                )
                if span:
                    span.end(
                        output={"latency_ms": latency_ms, "success": error is None},
                        level="ERROR" if error else "DEFAULT",
                    )
                lf.flush()
            except Exception as lf_err:
                logger.warning(f"Langfuse generation record failed: {lf_err}")

        logger.info(f"[{node_name}] Bedrock call: {latency_ms}ms | error={error is not None}")

    return result


# ── CloudWatch custom metrics ─────────────────────────────────────────────────

def emit_metric(
    metric_name: str,
    value: float,
    unit: str = "Count",
    dimensions: Optional[dict] = None,
) -> None:
    """
    Emits a custom CloudWatch metric.
    Useful for: fix_success_rate, mttr, llm_latency_p95, fallback_rate.
    Non-fatal — never crashes the agent.
    """
    try:
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        cw     = boto3.client("cloudwatch", region_name=region)

        dim_list = [
            {"Name": k, "Value": str(v)}
            for k, v in (dimensions or {}).items()
        ]

        cw.put_metric_data(
            Namespace="CFNDriftFixer",
            MetricData=[{
                "MetricName": metric_name,
                "Value":      value,
                "Unit":       unit,
                "Dimensions": dim_list,
            }],
        )
        logger.debug(f"Metric emitted: {metric_name}={value}")
    except Exception as e:
        logger.warning(f"CloudWatch metric failed (non-fatal): {e}")


# ── Convenience: emit standard agent metrics ──────────────────────────────────

def record_agent_result(state: dict) -> None:
    """
    Call at end of every agent run to emit standard metrics.
    Tracks: fix_success, drift_count, validation_pass.
    """
    stack      = state.get("stack_name", "unknown")
    fix_status = state.get("fix_status", "UNKNOWN")
    dimensions = {"StackName": stack, "FixStatus": fix_status}

    emit_metric("DriftFixAttempt",   1,     "Count", dimensions)
    emit_metric("DriftedResources",  state.get("total_drifted", 0), "Count", {"StackName": stack})

    if state.get("validation_passed") is True:
        emit_metric("ValidationPassed", 1, "Count", {"StackName": stack})
    elif state.get("validation_passed") is False:
        emit_metric("ValidationFailed", 1, "Count", {"StackName": stack})

    if state.get("fix_status") == "FAILED":
        emit_metric("FixFailed", 1, "Count", {"StackName": stack})
