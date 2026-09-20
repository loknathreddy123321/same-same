// dashboard/src/App.jsx
// React dashboard for CFN Drift Fixer
// Run: npm install && npm run dev

import { useState, useEffect, useRef, useCallback, memo } from "react";

const API = "http://localhost:8000/api";

const STATUS_COLOR = {
  VALIDATED:  "#3B6D11", APPLIED: "#3B6D11", NO_DRIFT: "#3B6D11",
  FAILED:     "#A32D2D", REJECTED: "#A32D2D",
  SKIPPED:    "#854F0B", PENDING: "#854F0B",
};

const DRIFT_COLOR = {
  DRIFTED:     "#A32D2D",
  IN_SYNC:     "#3B6D11",
  NOT_CHECKED: "#888780",
};

const RISK_COLOR = { HIGH: "#A32D2D", MEDIUM: "#854F0B", LOW: "#3B6D11" };

const Badge = memo(function Badge({ label, color }) {
  const bg = { "#A32D2D": "#FCEBEB", "#3B6D11": "#EAF3DE", "#854F0B": "#FAEEDA", "#888780": "#F1EFE8" };
  return (
    <span style={{
      display: "inline-block", fontSize: 11, fontWeight: 500,
      padding: "2px 8px", borderRadius: 99,
      background: bg[color] || "#F1EFE8", color: color || "#888780",
    }}>{label}</span>
  );
});

const MetricCard = memo(function MetricCard({ label, value, color }) {
  return (
    <div style={{ background: "#F1EFE8", borderRadius: 8, padding: "12px 16px", flex: 1 }}>
      <div style={{ fontSize: 12, color: "#5F5E5A", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 500, color: color || "#2C2C2A" }}>{value}</div>
    </div>
  );
});

export default function App() {
  const [stacks,     setStacks]     = useState([]);
  const [audit,      setAudit]      = useState([]);
  const [metrics,    setMetrics]    = useState({});
  const [safety,     setSafety]     = useState({});
  const [activeRun,  setActiveRun]  = useState(null);
  const [runLogs,    setRunLogs]    = useState([]);
  const [loading,    setLoading]    = useState({});
  const [pendingRun, setPendingRun] = useState(null);   // paused run awaiting approval
  const [approverName, setApproverName] = useState("");
  const [deciding,   setDeciding]   = useState(false);
  const logRef = useRef(null);
  const esRef  = useRef(null);

  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [runLogs]);
  useEffect(() => () => esRef.current?.close(), []); // close SSE on unmount

  const refresh = useCallback(async () => {
    try {
      const [s, a, m, sf] = await Promise.all([
        fetch(`${API}/stacks`).then(r => r.json()),
        fetch(`${API}/audit`).then(r => r.json()),
        fetch(`${API}/metrics`).then(r => r.json()),
        fetch(`${API}/safety`).then(r => r.json()),
      ]);
      setStacks(s.stacks || []);
      setAudit(a.records || []);
      setMetrics(m);
      setSafety(sf);
    } catch (e) {
      console.error("API error:", e);
    }
  }, []);

  const scanStack = useCallback(async (stackName, dryRun = false) => {
    setLoading(l => ({ ...l, [stackName]: true }));
    setRunLogs([]);
    setPendingRun(null);
    try {
      const resp = await fetch(`${API}/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stack_name: stackName, dry_run: dryRun }),
      }).then(r => r.json());

      const runId = resp.run_id;
      setActiveRun(runId);

      // Stream logs via SSE. For a live (non-dry-run) fix, this stream stays
      // open across the human-review pause: it announces `awaiting_approval`
      // once planning finishes, then keeps streaming apply-phase logs after
      // /approve is called, only closing on true completion.
      esRef.current?.close();
      const es = new EventSource(`${API}/runs/${runId}/stream`);
      esRef.current = es;
      es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === "done") {
          es.close();
          setLoading(l => ({ ...l, [stackName]: false }));
          setPendingRun(null);
          refresh();
        } else if (data.type === "awaiting_approval") {
          setPendingRun({ runId, stackName, ...data.run });
        } else {
          setRunLogs(logs => [...logs, data]);
        }
      };
      es.onerror = () => { es.close(); setLoading(l => ({ ...l, [stackName]: false })); };
    } catch (err) {
      setLoading(l => ({ ...l, [stackName]: false }));
    }
  }, [refresh]);

  const decideRun = useCallback(async (decision) => {
    if (!pendingRun || deciding) return;
    setDeciding(true);
    try {
      await fetch(`${API}/runs/${pendingRun.runId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: pendingRun.runId,
          decision,
          approver: approverName.trim() || "dashboard-user",
        }),
      });
      // Don't clear pendingRun's log stream here — the same SSE connection
      // keeps delivering apply-phase logs (APPROVE) or the rejection (REJECT)
      // until it sends `done`. Just drop the review card itself.
      setPendingRun(null);
    } finally {
      setDeciding(false);
    }
  }, [pendingRun, approverName, deciding]);

  async function toggleKillSwitch() {
    const current = safety.kill_switch === "true";
    await fetch(`${API}/safety/kill-switch?active=${!current}`, { method: "POST" });
    refresh();
  }

  async function toggleFreeze() {
    const current = safety.change_freeze === "true";
    await fetch(`${API}/safety/change-freeze?active=${!current}&reason=Manual+freeze`, { method: "POST" });
    refresh();
  }

  const killActive   = safety.kill_switch   === "true";
  const freezeActive = safety.change_freeze === "true";

  return (
    <div style={{ padding: "24px 28px", maxWidth: 1100, margin: "0 auto", fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 500 }}>CFN Drift Fixer</div>
          <div style={{ fontSize: 12, color: "#888780", marginTop: 2 }}>
            {process.env.REACT_APP_REGION || "ap-south-1"} · Real-time drift monitoring
          </div>
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          {/* Kill switch */}
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
            <div onClick={toggleKillSwitch} style={{
              width: 36, height: 20, borderRadius: 99, cursor: "pointer",
              background: killActive ? "#E24B4A" : "#D3D1C7", position: "relative", transition: "background .2s",
            }}>
              <div style={{
                position: "absolute", top: 2, left: killActive ? 18 : 2,
                width: 16, height: 16, borderRadius: "50%", background: "#fff", transition: "left .2s",
              }} />
            </div>
            <span style={{ color: killActive ? "#A32D2D" : "#888780" }}>
              {killActive ? "Kill switch ACTIVE" : "Kill switch"}
            </span>
          </label>
          {/* Change freeze */}
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
            <div onClick={toggleFreeze} style={{
              width: 36, height: 20, borderRadius: 99, cursor: "pointer",
              background: freezeActive ? "#BA7517" : "#D3D1C7", position: "relative", transition: "background .2s",
            }}>
              <div style={{
                position: "absolute", top: 2, left: freezeActive ? 18 : 2,
                width: 16, height: 16, borderRadius: "50%", background: "#fff", transition: "left .2s",
              }} />
            </div>
            <span style={{ color: freezeActive ? "#854F0B" : "#888780" }}>
              {freezeActive ? "Change freeze" : "No freeze"}
            </span>
          </label>
          <button onClick={refresh} style={{
            padding: "6px 16px", borderRadius: 8, border: "0.5px solid #B4B2A9",
            background: "transparent", cursor: "pointer", fontSize: 13,
          }}>Refresh</button>
        </div>
      </div>

      {/* Metrics */}
      <div style={{ display: "flex", gap: 12, marginBottom: 20 }}>
        <MetricCard label="Total runs"    value={metrics.total_runs    || 0} />
        <MetricCard label="Fixed"         value={metrics.validated     || 0} color="#3B6D11" />
        <MetricCard label="Failed"        value={metrics.failed        || 0} color="#A32D2D" />
        <MetricCard label="Fix rate"      value={`${metrics.fix_rate_pct || 0}%`} color="#185FA5" />
        <MetricCard label="Stacks total"  value={stacks.length} />
      </div>

      {/* Main grid */}
      <div style={{ display: "grid", gridColumns: "1fr 340px", gap: 16 }}>

        {/* Stacks table */}
        <div style={{ border: "0.5px solid #D3D1C7", borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: "0.5px solid #D3D1C7", display: "flex", justifyContent: "space-between" }}>
            <span style={{ fontWeight: 500, fontSize: 14 }}>Stacks</span>
            <button onClick={() => stacks.forEach(s => scanStack(s.name, true))} style={{
              padding: "4px 12px", borderRadius: 6, border: "0.5px solid #B5D4F4",
              background: "#E6F1FB", color: "#185FA5", cursor: "pointer", fontSize: 12,
            }}>Scan all (dry run)</button>
          </div>
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: "#F1EFE8" }}>
                {["Stack name", "Status", "Drift", "Actions"].map(h => (
                  <th key={h} style={{ padding: "8px 16px", textAlign: "left", fontWeight: 500, fontSize: 11, color: "#5F5E5A" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {stacks.map(s => (
                <tr key={s.name} style={{ borderTop: "0.5px solid #D3D1C7" }}>
                  <td style={{ padding: "10px 16px", fontWeight: 500 }}>{s.name}</td>
                  <td style={{ padding: "10px 16px" }}>
                    <Badge label={s.status} color="#888780" />
                  </td>
                  <td style={{ padding: "10px 16px" }}>
                    <Badge
                      label={s.drift_status}
                      color={DRIFT_COLOR[s.drift_status] || "#888780"}
                    />
                  </td>
                  <td style={{ padding: "10px 16px" }}>
                    <div style={{ display: "flex", gap: 6 }}>
                      <button onClick={() => scanStack(s.name, true)} disabled={loading[s.name]} style={{
                        padding: "3px 10px", borderRadius: 6, border: "0.5px solid #B4B2A9",
                        background: "transparent", cursor: "pointer", fontSize: 12,
                        opacity: loading[s.name] ? 0.5 : 1,
                      }}>
                        {loading[s.name] ? "Running..." : "Dry run"}
                      </button>
                      {s.drift_status === "DRIFTED" && (
                        <button onClick={() => scanStack(s.name, false)} disabled={loading[s.name]} style={{
                          padding: "3px 10px", borderRadius: 6, border: "0.5px solid #B5D4F4",
                          background: "#E6F1FB", color: "#185FA5", cursor: "pointer", fontSize: 12,
                        }}>Fix (review first)</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {stacks.length === 0 && (
                <tr><td colSpan={4} style={{ padding: 24, textAlign: "center", color: "#888780", fontSize: 13 }}>
                  No stacks found. Make sure AWS credentials are configured.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pending approval — the real pause point, backed by a real change set */}
      {pendingRun && (
        <div style={{ marginTop: 16, border: "1px solid #E2A83A", borderRadius: 12, overflow: "hidden", background: "#FFFBF0" }}>
          <div style={{ padding: "10px 16px", borderBottom: "1px solid #E2A83A", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14 }}>
              Review required — {pendingRun.stackName}
            </span>
            <Badge label={pendingRun.risk_level || "—"} color={RISK_COLOR[pendingRun.risk_level] || "#888780"} />
          </div>
          <div style={{ padding: 16 }}>
            <div style={{ fontSize: 12, color: "#5F5E5A", marginBottom: 10 }}>
              <strong>Root cause:</strong> {pendingRun.root_cause || "—"}
            </div>
            <div style={{ fontSize: 11, fontWeight: 500, color: "#5F5E5A", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.4 }}>
              Change set diff — what will actually change
            </div>
            <pre style={{
              background: "#2C2C2A", color: "#EDEBE4", padding: 12, borderRadius: 8,
              fontSize: 12, lineHeight: 1.6, overflowX: "auto", margin: 0, marginBottom: 12,
            }}>{pendingRun.change_set_diff || "Not available"}</pre>
            {pendingRun.template_snapshot_s3 && (
              <div style={{ fontSize: 11, color: "#888780", marginBottom: 12 }}>
                Rollback snapshot: <code>{pendingRun.template_snapshot_s3}</code>
              </div>
            )}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                value={approverName}
                onChange={(e) => setApproverName(e.target.value)}
                placeholder="Your name (for audit trail)"
                style={{ flex: 1, padding: "8px 10px", borderRadius: 6, border: "0.5px solid #B4B2A9", fontSize: 13 }}
              />
              <button onClick={() => decideRun("APPROVE")} disabled={deciding} style={{
                padding: "8px 18px", borderRadius: 6, border: "none", background: "#3B6D11",
                color: "#fff", cursor: "pointer", fontSize: 13, fontWeight: 500, opacity: deciding ? 0.6 : 1,
              }}>✓ Approve & apply</button>
              <button onClick={() => decideRun("REJECT")} disabled={deciding} style={{
                padding: "8px 18px", borderRadius: 6, border: "none", background: "#A32D2D",
                color: "#fff", cursor: "pointer", fontSize: 13, fontWeight: 500, opacity: deciding ? 0.6 : 1,
              }}>✗ Reject</button>
            </div>
          </div>
        </div>
      )}

      {/* Live logs */}
      {runLogs.length > 0 && (
        <div style={{ marginTop: 16, border: "0.5px solid #D3D1C7", borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: "10px 16px", borderBottom: "0.5px solid #D3D1C7", fontWeight: 500, fontSize: 14 }}>
            Live agent logs
          </div>
          <div ref={logRef} style={{ padding: 16, fontFamily: "monospace", fontSize: 12, maxHeight: 200, overflowY: "auto", background: "#F1EFE8" }}>
            {runLogs.map((log, i) => (
              <div key={i} style={{
                color: log.level === "ERROR" ? "#A32D2D" : log.level === "WARNING" ? "#854F0B" : "#444441",
                padding: "1px 0",
              }}>
                {log.time?.slice(11, 19)} | {log.level?.padEnd(7)} | {log.message}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Audit table */}
      <div style={{ marginTop: 16, border: "0.5px solid #D3D1C7", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "12px 16px", borderBottom: "0.5px solid #D3D1C7", fontWeight: 500, fontSize: 14 }}>
          Audit trail
        </div>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "#F1EFE8" }}>
              {["Stack", "Result", "Risk", "Drifted", "Audit ID", "Time"].map(h => (
                <th key={h} style={{ padding: "8px 16px", textAlign: "left", fontWeight: 500, fontSize: 11, color: "#5F5E5A" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {audit.slice(0, 10).map((r, i) => (
              <tr key={i} style={{ borderTop: "0.5px solid #D3D1C7" }}>
                <td style={{ padding: "10px 16px", fontWeight: 500 }}>{r.stack_name}</td>
                <td style={{ padding: "10px 16px" }}>
                  <Badge label={r.fix_status || "—"} color={STATUS_COLOR[r.fix_status] || "#888780"} />
                </td>
                <td style={{ padding: "10px 16px" }}>
                  <Badge label={r.risk_level || "—"} color={RISK_COLOR[r.risk_level] || "#888780"} />
                </td>
                <td style={{ padding: "10px 16px", color: "#5F5E5A" }}>{r.total_drifted ?? "—"}</td>
                <td style={{ padding: "10px 16px", fontFamily: "monospace", color: "#5F5E5A", fontSize: 11 }}>{r.record_id}</td>
                <td style={{ padding: "10px 16px", color: "#5F5E5A" }}>{r.created_at?.slice(11, 19) || "—"}</td>
              </tr>
            ))}
            {audit.length === 0 && (
              <tr><td colSpan={6} style={{ padding: 24, textAlign: "center", color: "#888780", fontSize: 13 }}>
                No audit records found.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

    </div>
  );
}
