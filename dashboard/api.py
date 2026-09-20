"""
dashboard/api.py
----------------
FastAPI backend — serves the frontend and calls real AWS.
Run: python -m uvicorn dashboard.api:app --reload --port 8000
Then open: http://localhost:8000
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import json, asyncio, logging
import hmac, hashlib, base64, time, secrets
from datetime import datetime, timezone
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import ClientError
from langsmith import traceable
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
load_dotenv()

from agent.state import FixStatus
from agent.cfn import delete_change_set, get_drifted_resources
from agent.nodes import (
    node_detect_drift,
    node_safety_gate,
    node_analyze_drift,
    node_classify_risk,
    node_plan_remediation,
    node_apply_fix,
    node_validate,
    node_audit_and_notify,
)

logging.basicConfig(level=logging.INFO)
logger   = logging.getLogger(__name__)
app      = FastAPI(title="CFN Drift Fixer")
executor = ThreadPoolExecutor(max_workers=4)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

active_runs:    dict = {}
run_logs:       dict = {}
pending_states: dict = {}   # run_id -> DriftFixState paused right after plan_remediation

REGION     = os.environ.get("AWS_REGION", "ap-south-1")
AUDIT_TABLE = os.environ.get("DYNAMODB_AUDIT_TABLE", "cfn-drift-audit")

# Created once at startup and reused for every request. boto3.client(...)
# does credential-chain resolution and service-model loading on every call,
# which was adding ~100-200ms of pure overhead to every dashboard endpoint
# on top of the real network round-trip — this was a measurable chunk of
# the perceived UI latency, especially on /api/safety (5 SSM calls).
cfn_client  = boto3.client("cloudformation", region_name=REGION)
ssm_client  = boto3.client("ssm", region_name=REGION)
sts_client  = boto3.client("sts", region_name=REGION)
dynamodb_rc = boto3.resource("dynamodb", region_name=REGION)


# ── Auth ──────────────────────────────────────────────────────────────────────
# Deliberately basic: single shared username/password from env vars, signed
# session cookie. This is a stopgap access gate, not enterprise SSO/RBAC —
# it stops "anyone with the URL can approve live fixes," nothing more.
AUTH_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")
AUTH_SECRET   = os.environ.get("DASHBOARD_SECRET", "").encode() or secrets.token_bytes(32)
SESSION_TTL   = 8 * 3600  # 8 hours
PUBLIC_PATHS  = {"/login", "/api/login"}

if os.environ.get("DASHBOARD_PASSWORD") is None:
    logger.warning(
        "[auth] DASHBOARD_PASSWORD not set — using default 'admin' credentials. "
        "Set DASHBOARD_USERNAME/DASHBOARD_PASSWORD in .env before exposing this dashboard beyond localhost."
    )


def _sign(payload: str) -> str:
    return hmac.new(AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(username: str) -> str:
    expiry  = int(time.time()) + SESSION_TTL
    payload = f"{username}:{expiry}"
    raw     = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_session_token(token: str) -> Optional[str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expiry, sig = raw.rsplit(":", 2)
        if not hmac.compare_digest(_sign(f"{username}:{expiry}"), sig):
            return None
        if int(expiry) < time.time():
            return None
        return username
    except Exception:
        return None


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    token = request.cookies.get("session")
    user  = verify_session_token(token) if token else None
    if not user:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(url="/login")
    request.state.user = user
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginRequest, response: Response):
    if body.username == AUTH_USERNAME and body.password == AUTH_PASSWORD:
        token = make_session_token(body.username)
        response.set_cookie("session", token, httponly=True, samesite="lax", max_age=SESSION_TTL)
        return {"ok": True, "username": body.username}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def serve_login():
    html_path = os.path.join(os.path.dirname(__file__), "login.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ── Models ────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    stack_name: str
    dry_run:    bool = True

class ApprovalRequest(BaseModel):
    run_id:   str
    decision: str   # APPROVE | REJECT
    approver: str


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


# ── Identity / environment (backs the Profile page) ───────────────────────────

@app.get("/api/whoami")
async def whoami(request: Request):
    """
    Real AWS caller identity + environment/config status, plus the dashboard
    session's logged-in username (basic shared-credential auth — see the
    Auth section above — not per-user SSO/RBAC yet).
    """
    identity = {"account": None, "arn": None, "user_id": None, "error": None}
    try:
        who = sts_client.get_caller_identity()
        identity = {"account": who.get("Account"), "arn": who.get("Arn"), "user_id": who.get("UserId"), "error": None}
    except ClientError as e:
        identity["error"] = str(e)

    return {
        "identity": identity,
        "dashboard_user": getattr(request.state, "user", None),
        "region": REGION,
        "audit_table": AUDIT_TABLE,
        "snapshot_bucket": os.environ.get("S3_SNAPSHOT_BUCKET", ""),
        "llm_provider": (
            f"Azure OpenAI ({os.environ.get('AZURE_OPENAI_DEPLOYMENT')})" if os.environ.get("AZURE_OPENAI_API_KEY")
            else f"AWS Bedrock ({os.environ.get('BEDROCK_MODEL_ID')})" if os.environ.get("BEDROCK_MODEL_ID")
            else f"Groq ({os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')})" if os.environ.get("GROQ_API_KEY")
            else "Not configured"
        ),
        "llm_configured": bool(
            os.environ.get("AZURE_OPENAI_API_KEY")
            or os.environ.get("BEDROCK_MODEL_ID")
            or os.environ.get("GROQ_API_KEY")
        ),
        "slack_configured": bool(os.environ.get("SLACK_BOT_TOKEN")),
        "default_dry_run": os.environ.get("DRY_RUN", "true"),
    }


# ── AWS: Stacks ───────────────────────────────────────────────────────────────

@app.get("/api/stacks")
async def list_stacks():
    """List all real CFN stacks from AWS with drift status."""
    try:
        pages = cfn_client.get_paginator("list_stacks").paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE", "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE",
            ]
        )
        stacks = []
        for page in pages:
            for s in page.get("StackSummaries", []):
                stacks.append({
                    "name":         s["StackName"],
                    "status":       s["StackStatus"],
                    "drift_status": s.get("DriftInformation", {}).get("StackDriftStatus", "NOT_CHECKED"),
                    "last_updated": str(s.get("LastUpdatedTime", s.get("CreationTime", ""))),
                })
        return {"stacks": stacks, "region": REGION}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stacks/{stack_name}")
async def get_stack(stack_name: str):
    """Get details for a single stack."""
    try:
        resp  = cfn_client.describe_stacks(StackName=stack_name)
        stack = resp["Stacks"][0]
        return {
            "name":         stack["StackName"],
            "status":       stack["StackStatus"],
            "drift_status": stack.get("DriftInformation", {}).get("StackDriftStatus", "NOT_CHECKED"),
            "parameters":   stack.get("Parameters", []),
            "outputs":      stack.get("Outputs", []),
            "tags":         stack.get("Tags", []),
            "created":      str(stack.get("CreationTime", "")),
            "updated":      str(stack.get("LastUpdatedTime", "")),
        }
    except ClientError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/stacks/{stack_name}/drift")
async def get_stack_drift_details(stack_name: str):
    """
    Returns AWS's last known per-resource drift results for this stack
    (logical id, resource type, and exact property differences).

    Reads CloudFormation's cached drift results directly — does NOT trigger a
    new detection or call the LLM, so this is near-instant and safe to call
    just by clicking a badge, unlike /api/scan which runs the full agent.
    """
    try:
        resources = get_drifted_resources(stack_name, REGION)
        return {"stack_name": stack_name, "drifted_resources": resources, "count": len(resources)}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── AWS: Scan / Agent Run ─────────────────────────────────────────────────────

@app.post("/api/scan")
async def scan_stack(request: ScanRequest, background_tasks: BackgroundTasks):
    """Trigger real agent run on a stack."""
    run_id = f"{request.stack_name}-{int(datetime.now().timestamp())}"
    active_runs[run_id] = {
        "status":     "RUNNING",
        "stack":      request.stack_name,
        "dry_run":    request.dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    run_logs[run_id] = []
    if request.dry_run:
        # Safe preview mode — full single-shot run, never mutates AWS.
        background_tasks.add_task(_run_agent_task, run_id, request.stack_name, request.dry_run)
    else:
        # Live mode — pause after plan_remediation for real human review.
        background_tasks.add_task(_run_preview_task, run_id, request.stack_name)
    return {"run_id": run_id, "stack": request.stack_name, "dry_run": request.dry_run}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Get status of an agent run."""
    if run_id not in active_runs:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run_id": run_id,
        **active_runs[run_id],
        "logs": run_logs.get(run_id, []),
    }


@app.get("/api/runs/{run_id}/stream")
async def stream_run_logs(run_id: str):
    """
    SSE stream of live agent logs. Stays open across the human-review pause:
    once planning finishes, emits one `awaiting_approval` event with the full
    review payload (diff, risk, root cause), then keeps streaming logs from
    the apply/validate phase after /approve is called. Only closes on a
    terminal status.
    """
    TERMINAL = {"COMPLETE", "ERROR"}

    async def event_stream():
        sent = 0
        announced_review = False
        while True:
            logs = run_logs.get(run_id, [])
            while sent < len(logs):
                yield f"data: {json.dumps(logs[sent])}\n\n"
                sent += 1

            run = active_runs.get(run_id, {})
            status = run.get("status")

            if status == "AWAITING_APPROVAL" and not announced_review:
                announced_review = True
                yield f"data: {json.dumps({'type': 'awaiting_approval', 'run': run})}\n\n"

            if status in TERMINAL:
                yield f"data: {json.dumps({'type': 'done', 'run': run})}\n\n"
                break

            await asyncio.sleep(0.3)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/approve")
async def approve_run(run_id: str, request: ApprovalRequest, background_tasks: BackgroundTasks):
    """
    Called from UI when a stakeholder approves or rejects a paused fix.
    APPROVE resumes apply_fix -> validate -> audit_and_notify in the background.
    REJECT records the rejection and cleans up the preview change set — nothing
    further runs.
    """
    if run_id not in pending_states:
        raise HTTPException(status_code=404, detail="No run awaiting approval for this run_id")

    state = pending_states.pop(run_id)

    if request.decision == "APPROVE":
        state["approval_status"] = FixStatus.APPROVED
        state["approver"]        = request.approver
        active_runs[run_id]["status"]   = "APPLYING"
        active_runs[run_id]["approver"] = request.approver
        background_tasks.add_task(_resume_apply_task, run_id, state)
    else:
        state["approval_status"] = FixStatus.REJECTED
        state["approver"]        = request.approver
        state["fix_status"]      = FixStatus.REJECTED
        cs_name = state.get("change_set_name")
        if cs_name:
            try:
                delete_change_set(state["stack_name"], cs_name, REGION)
            except Exception:
                pass
        _finalize_run(run_id, state, final_status="COMPLETE")

    return {"run_id": run_id, "decision": request.decision, "approver": request.approver}


# ── AWS: Audit Trail ──────────────────────────────────────────────────────────

@app.get("/api/audit")
async def get_audit(stack_name: Optional[str] = None, limit: int = 20):
    """Read real audit records from DynamoDB."""
    try:
        table    = dynamodb_rc.Table(AUDIT_TABLE)
        if stack_name:
            resp = table.query(
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": f"STACK#{stack_name}"},
                ScanIndexForward=False,
                Limit=limit,
            )
        else:
            resp = table.scan(Limit=limit)
        items = resp.get("Items", [])
        # Convert Decimal to int/float for JSON
        for item in items:
            for k, v in item.items():
                try:
                    from decimal import Decimal
                    if isinstance(v, Decimal):
                        item[k] = int(v)
                except Exception:
                    pass
        return {"records": items, "count": len(items)}
    except ClientError as e:
        return {"records": [], "error": str(e)}


# ── AWS: Metrics ──────────────────────────────────────────────────────────────

@app.get("/api/metrics")
async def get_metrics():
    """Real metrics from DynamoDB audit table."""
    try:
        table     = dynamodb_rc.Table(AUDIT_TABLE)
        resp      = table.scan(Limit=200)
        items     = resp.get("Items", [])
        total     = len(items)
        validated = sum(1 for i in items if i.get("fix_status") == "VALIDATED")
        failed    = sum(1 for i in items if i.get("fix_status") == "FAILED")
        no_drift  = sum(1 for i in items if i.get("fix_status") == "NO_DRIFT")
        return {
            "total_runs":   total,
            "validated":    validated,
            "failed":       failed,
            "no_drift":     no_drift,
            "fix_rate_pct": round(validated / total * 100) if total else 0,
            "region":       REGION,
        }
    except ClientError as e:
        return {"total_runs":0, "validated":0, "failed":0, "no_drift":0, "fix_rate_pct":0}


# ── AWS: Safety Controls ──────────────────────────────────────────────────────

@app.get("/api/safety")
async def get_safety():
    """
    Read real safety control values from SSM.

    One batched get_parameters call instead of 5 sequential get_parameter
    round-trips — this endpoint alone was previously ~700ms of pure
    back-to-back network latency for something that's polled repeatedly.
    """
    params = [
        "/cfn-drift-fixer/kill-switch",
        "/cfn-drift-fixer/change-freeze",
        "/cfn-drift-fixer/change-freeze-reason",
        "/cfn-drift-fixer/maintenance-window",
        "/cfn-drift-fixer/circuit-breaker-count",
    ]
    result = {name.split("/")[-1].replace("-", "_"): ("0" if "count" in name else "false") for name in params}
    resp = ssm_client.get_parameters(Names=params)
    for p in resp.get("Parameters", []):
        key = p["Name"].split("/")[-1].replace("-", "_")
        result[key] = p["Value"]
    return result


@app.post("/api/safety/kill-switch")
async def set_kill_switch(active: bool):
    """Toggle kill switch in SSM."""
    ssm_client.put_parameter(
        Name="/cfn-drift-fixer/kill-switch",
        Value="true" if active else "false",
        Type="String", Overwrite=True,
    )
    return {"kill_switch": active}


@app.post("/api/safety/change-freeze")
async def set_change_freeze(active: bool, reason: str = "Manual freeze from dashboard"):
    """Toggle change freeze in SSM."""
    ssm_client.put_parameter(Name="/cfn-drift-fixer/change-freeze",
                      Value="true" if active else "false", Type="String", Overwrite=True)
    if active:
        ssm_client.put_parameter(Name="/cfn-drift-fixer/change-freeze-reason",
                          Value=reason, Type="String", Overwrite=True)
    return {"change_freeze": active, "reason": reason}


# ── Background: Real Agent Runner ─────────────────────────────────────────────

def _run_agent_task(run_id: str, stack_name: str, dry_run: bool):
    """Runs the real LangGraph agent and captures logs."""

    class LiveLogHandler(logging.Handler):
        def emit(self, record):
            run_logs[run_id].append({
                "time":    datetime.now(timezone.utc).isoformat(),
                "level":   record.levelname,
                "message": record.getMessage(),
                "logger":  record.name,
                "type":    "log",
            })

    handler = LiveLogHandler()
    logging.getLogger("agent").addHandler(handler)
    logging.getLogger("agent").setLevel(logging.INFO)

    try:
        from agent.graph import run_agent
        from agent.state import FixStatus

        result = run_agent(
            stack_name=stack_name,
            aws_region=REGION,
            dry_run=dry_run,
            approval_status=FixStatus.APPROVED if dry_run else None,
        )

        active_runs[run_id].update({
            "status":          "COMPLETE",
            "fix_status":      result.get("fix_status"),
            "risk_level":      result.get("risk_level"),
            "total_drifted":   result.get("total_drifted", 0),
            "root_cause":      result.get("root_cause"),
            "audit_record_id": result.get("audit_record_id"),
            "validation":      result.get("validation_passed"),
            "error":           result.get("error_message"),
            "change_set_diff": result.get("change_set_diff"),
            "drifted_resources": result.get("drifted_resources", []),
        })

    except Exception as e:
        logger.exception(f"Agent run failed: {e}")
        active_runs[run_id].update({"status": "ERROR", "error": str(e)})
    finally:
        logging.getLogger("agent").removeHandler(handler)


# ── Background: Pause-for-review flow (live mode) ─────────────────────────────
# Live scans no longer auto-apply. plan_remediation runs for real (real change
# set + S3 snapshot), then the run pauses in AWAITING_APPROVAL until a human
# calls /api/runs/{run_id}/approve. Only then does apply_fix ever run.

def _make_log_handler(run_id: str) -> logging.Handler:
    class LiveLogHandler(logging.Handler):
        def emit(self, record):
            run_logs[run_id].append({
                "time":    datetime.now(timezone.utc).isoformat(),
                "level":   record.levelname,
                "message": record.getMessage(),
                "logger":  record.name,
                "type":    "log",
            })
    return LiveLogHandler()


def _result_summary(state: dict) -> dict:
    return {
        "fix_status":           state.get("fix_status"),
        "risk_level":            state.get("risk_level"),
        "total_drifted":         state.get("total_drifted", 0),
        "root_cause":            state.get("root_cause"),
        "audit_record_id":       state.get("audit_record_id"),
        "validation":            state.get("validation_passed"),
        "error":                 state.get("error_message"),
        "change_set_diff":       state.get("change_set_diff"),
        "drifted_resources":     state.get("drifted_resources", []),
        "remediation_plan":      state.get("remediation_plan"),
        "template_snapshot_s3":  state.get("template_snapshot_s3"),
        "approver":              state.get("approver"),
    }


def _finalize_run(run_id: str, state: dict, final_status: str = "COMPLETE") -> None:
    """Writes the final audit record (if not already written) and marks the run done."""
    if not state.get("audit_record_id"):
        state.update(node_audit_and_notify(state))
    active_runs[run_id]["status"] = final_status
    active_runs[run_id].update(_result_summary(state))


def _new_state(stack_name: str) -> dict:
    return {
        "stack_name": stack_name, "aws_region": REGION, "dry_run": False,
        "task_token": None, "stack_id": None, "detection_id": None,
        "drifted_resources": [], "total_drifted": 0, "detection_timestamp": None,
        "root_cause": None, "impact": None, "affected_services": [], "llm_recommendation": None,
        "risk_level": None, "risk_reasoning": None, "approval_required": False,
        "remediation_plan": None, "template_body": None, "synced_template": None,
        "template_snapshot_s3": None, "change_set_name": None, "change_set_id": None,
        "change_set_diff": None, "approval_status": None, "approver": None,
        "fix_applied": False, "fix_timestamp": None, "validation_passed": None,
        "post_fix_drift_count": None, "fix_status": None, "audit_record_id": None,
        "error_message": None, "execution_trace": [],
    }


@traceable(name="drift-fix-preview")
def _run_preview_task(run_id: str, stack_name: str) -> None:
    """Runs detect -> safety_gate -> analyze -> classify -> plan, then pauses."""
    handler = _make_log_handler(run_id)
    logging.getLogger("agent").addHandler(handler)
    logging.getLogger("agent").setLevel(logging.INFO)

    state = _new_state(stack_name)

    try:
        state.update(node_detect_drift(state))
        if (state.get("error_message") or state.get("fix_status") == FixStatus.FAILED
                or state.get("total_drifted", 0) == 0):
            _finalize_run(run_id, state)
            return

        state.update(node_safety_gate(state))
        if state.get("fix_status") == FixStatus.SKIPPED:
            _finalize_run(run_id, state)
            return

        state.update(node_analyze_drift(state))
        state.update(node_classify_risk(state))
        state.update(node_plan_remediation(state))
        if state.get("fix_status") == FixStatus.FAILED:
            _finalize_run(run_id, state)
            return

        # Genuinely paused — waiting on a human decision, nothing more runs.
        pending_states[run_id] = state
        active_runs[run_id]["status"] = "AWAITING_APPROVAL"
        active_runs[run_id].update(_result_summary(state))

    except Exception as e:
        logger.exception(f"Preview run failed: {e}")
        active_runs[run_id].update({"status": "ERROR", "error": str(e)})
    finally:
        logging.getLogger("agent").removeHandler(handler)


@traceable(name="drift-fix-apply")
def _resume_apply_task(run_id: str, state: dict) -> None:
    """Resumes after approval: apply_fix -> validate -> audit_and_notify."""
    handler = _make_log_handler(run_id)
    logging.getLogger("agent").addHandler(handler)
    logging.getLogger("agent").setLevel(logging.INFO)

    try:
        state.update(node_apply_fix(state))
        if state.get("fix_applied"):
            state.update(node_validate(state))
        _finalize_run(run_id, state)
    except Exception as e:
        logger.exception(f"Apply run failed: {e}")
        active_runs[run_id].update({"status": "ERROR", "error": str(e)})
    finally:
        logging.getLogger("agent").removeHandler(handler)
