"""
web_ui.py — Sovereign Claw Web UI Operator Console
===================================================

Lightweight HTML-served operator console for monitoring and controlling
the Sovereign Claw platform. Serves a single-page dashboard via the
Gateway WebSocket connection.

Features:
- Real-time drift monitoring
- Session management
- Proof Vault inspection
- Skills overview
- Channel status
- Scheduler job management
- Configuration viewer
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Dashboard data models ─────────────────────────────────────────────────────


@dataclass
class DashboardMetrics:
    """Aggregated metrics for the operator console."""

    active_sessions: int = 0
    total_messages: int = 0
    current_drift: float = 0.0
    proof_vault_entries: int = 0
    active_skills: int = 0
    connected_channels: int = 0
    scheduled_jobs: int = 0
    uptime_seconds: float = 0.0
    model_router_calls: int = 0
    policy_evaluations: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_sessions": self.active_sessions,
            "total_messages": self.total_messages,
            "current_drift": self.current_drift,
            "proof_vault_entries": self.proof_vault_entries,
            "active_skills": self.active_skills,
            "connected_channels": self.connected_channels,
            "scheduled_jobs": self.scheduled_jobs,
            "uptime_seconds": self.uptime_seconds,
            "model_router_calls": self.model_router_calls,
            "policy_evaluations": self.policy_evaluations,
            "timestamp": self.timestamp,
        }


@dataclass
class DashboardAlert:
    """Alert for the operator console."""

    alert_id: str = ""
    severity: str = "info"  # info, warning, error, critical
    message: str = ""
    source: str = ""
    timestamp: float = field(default_factory=time.time)
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "timestamp": self.timestamp,
            "acknowledged": self.acknowledged,
        }


@dataclass
class ProofVaultEntry:
    """Simplified proof vault entry for UI display."""

    step_index: int = 0
    trace_id: str = ""
    tool: str = ""
    status: str = ""
    drift: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "trace_id": self.trace_id,
            "tool": self.tool,
            "status": self.status,
            "drift": self.drift,
            "timestamp": self.timestamp,
        }


# ── Dashboard state manager ──────────────────────────────────────────────────


class DashboardState:
    """
    Manages the state for the Web UI operator console.
    Collects metrics from all platform subsystems.
    """

    def __init__(self) -> None:
        self._start_time = time.time()
        self._alerts: List[DashboardAlert] = []
        self._max_alerts = 100
        self._metrics = DashboardMetrics()
        self._proof_vault_cache: List[ProofVaultEntry] = []
        self._max_vault_cache = 50

    def update_metrics(
        self,
        active_sessions: int = 0,
        total_messages: int = 0,
        current_drift: float = 0.0,
        proof_vault_entries: int = 0,
        active_skills: int = 0,
        connected_channels: int = 0,
        scheduled_jobs: int = 0,
        model_router_calls: int = 0,
        policy_evaluations: int = 0,
    ) -> DashboardMetrics:
        """Update dashboard metrics from subsystem data."""
        self._metrics = DashboardMetrics(
            active_sessions=active_sessions,
            total_messages=total_messages,
            current_drift=current_drift,
            proof_vault_entries=proof_vault_entries,
            active_skills=active_skills,
            connected_channels=connected_channels,
            scheduled_jobs=scheduled_jobs,
            uptime_seconds=time.time() - self._start_time,
            model_router_calls=model_router_calls,
            policy_evaluations=policy_evaluations,
        )
        return self._metrics

    def get_metrics(self) -> DashboardMetrics:
        """Return current metrics snapshot."""
        self._metrics.uptime_seconds = time.time() - self._start_time
        self._metrics.timestamp = time.time()
        return self._metrics

    def add_alert(self, alert: DashboardAlert) -> None:
        """Add an alert to the dashboard."""
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts :]

    def get_alerts(
        self,
        severity: Optional[str] = None,
        unacknowledged_only: bool = False,
    ) -> List[DashboardAlert]:
        """Get alerts, optionally filtered."""
        alerts = self._alerts
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if unacknowledged_only:
            alerts = [a for a in alerts if not a.acknowledged]
        return alerts

    def acknowledge_alert(self, alert_id: str) -> bool:
        """Acknowledge an alert by ID."""
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                return True
        return False

    def add_vault_entry(self, entry: ProofVaultEntry) -> None:
        """Cache a proof vault entry for display."""
        self._proof_vault_cache.append(entry)
        if len(self._proof_vault_cache) > self._max_vault_cache:
            self._proof_vault_cache = self._proof_vault_cache[-self._max_vault_cache :]

    def get_vault_entries(self, limit: int = 20) -> List[ProofVaultEntry]:
        """Get recent proof vault entries."""
        return self._proof_vault_cache[-limit:]

    def get_full_state(self) -> Dict[str, Any]:
        """Return complete dashboard state as JSON-serializable dict."""
        return {
            "metrics": self.get_metrics().to_dict(),
            "alerts": [a.to_dict() for a in self.get_alerts()],
            "vault_entries": [e.to_dict() for e in self.get_vault_entries()],
        }


# ── HTML template ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sovereign Claw — Operator Console</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --yellow: #d29922; --red: #f85149; --orange: #db6d28;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5; }
header { background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; }
header h1 { font-size: 1.25rem; font-weight: 600; }
header .version { color: var(--muted); font-size: 0.85rem; }
header .status { margin-left: auto; display: flex; align-items: center; gap: 0.5rem; }
header .status .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem; padding: 1.5rem 2rem; }
.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 1.25rem; }
.card .label { color: var(--muted); font-size: 0.8rem; text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 0.25rem; }
.card .value { font-size: 1.75rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.card .value.green { color: var(--green); }
.card .value.yellow { color: var(--yellow); }
.card .value.red { color: var(--red); }
section { padding: 0 2rem 1.5rem; }
section h2 { font-size: 1rem; font-weight: 600; margin-bottom: 0.75rem;
  padding-bottom: 0.5rem; border-bottom: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th { text-align: left; color: var(--muted); font-weight: 500;
  padding: 0.5rem; border-bottom: 1px solid var(--border); }
td { padding: 0.5rem; border-bottom: 1px solid var(--border); }
.badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 12px;
  font-size: 0.75rem; font-weight: 500; }
.badge.info { background: #1f6feb33; color: var(--accent); }
.badge.warning { background: #d2992233; color: var(--yellow); }
.badge.error { background: #f8514933; color: var(--red); }
.badge.critical { background: #f8514966; color: #fff; }
.drift-bar { height: 6px; background: var(--border); border-radius: 3px;
  overflow: hidden; margin-top: 0.5rem; }
.drift-bar .fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
footer { text-align: center; color: var(--muted); font-size: 0.75rem;
  padding: 1.5rem; border-top: 1px solid var(--border); }
@media (max-width: 768px) { .grid { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Sovereign Claw</h1>
  <span class="version">v3.0.0</span>
  <div class="status"><div class="dot" id="status-dot"></div><span id="status-text">Connected</span></div>
</header>
<div class="grid">
  <div class="card">
    <div class="label">Active Sessions</div>
    <div class="value" id="m-sessions">0</div>
  </div>
  <div class="card">
    <div class="label">Messages</div>
    <div class="value" id="m-messages">0</div>
  </div>
  <div class="card">
    <div class="label">Current Drift</div>
    <div class="value" id="m-drift">0.0000</div>
    <div class="drift-bar"><div class="fill" id="drift-fill" style="width:0%;background:var(--green)"></div></div>
  </div>
  <div class="card">
    <div class="label">Proof Vault</div>
    <div class="value" id="m-vault">0</div>
  </div>
  <div class="card">
    <div class="label">Active Skills</div>
    <div class="value green" id="m-skills">0</div>
  </div>
  <div class="card">
    <div class="label">Channels</div>
    <div class="value" id="m-channels">0</div>
  </div>
  <div class="card">
    <div class="label">Scheduled Jobs</div>
    <div class="value" id="m-jobs">0</div>
  </div>
  <div class="card">
    <div class="label">Uptime</div>
    <div class="value" id="m-uptime">0s</div>
  </div>
</div>
<section>
  <h2>Alerts</h2>
  <table><thead><tr><th>Time</th><th>Severity</th><th>Source</th><th>Message</th></tr></thead>
  <tbody id="alerts-body"><tr><td colspan="4" style="color:var(--muted)">No alerts</td></tr></tbody></table>
</section>
<section>
  <h2>Recent Proof Vault Entries</h2>
  <table><thead><tr><th>#</th><th>Trace ID</th><th>Tool</th><th>Status</th><th>Drift</th></tr></thead>
  <tbody id="vault-body"><tr><td colspan="5" style="color:var(--muted)">No entries</td></tr></tbody></table>
</section>
<footer>
  Sovereign Claw v3.0.0 — Constraint-First Governance — Isomorphic Closure Invariant
</footer>
<script>
const WS_URL = `ws://${location.hostname}:8765`;
let ws;
function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { document.getElementById('status-dot').style.background='var(--green)';
    document.getElementById('status-text').textContent='Connected'; };
  ws.onclose = () => { document.getElementById('status-dot').style.background='var(--red)';
    document.getElementById('status-text').textContent='Disconnected';
    setTimeout(connect, 3000); };
  ws.onmessage = (e) => { try { update(JSON.parse(e.data)); } catch(err) {} };
}
function update(data) {
  if (!data.metrics) return;
  const m = data.metrics;
  document.getElementById('m-sessions').textContent = m.active_sessions;
  document.getElementById('m-messages').textContent = m.total_messages;
  const drift = m.current_drift.toFixed(4);
  const driftEl = document.getElementById('m-drift');
  driftEl.textContent = drift;
  driftEl.className = 'value ' + (m.current_drift < 0.3 ? 'green' : m.current_drift < 0.7 ? 'yellow' : 'red');
  const pct = Math.min(m.current_drift * 100, 100);
  const fill = document.getElementById('drift-fill');
  fill.style.width = pct + '%';
  fill.style.background = pct < 30 ? 'var(--green)' : pct < 70 ? 'var(--yellow)' : 'var(--red)';
  document.getElementById('m-vault').textContent = m.proof_vault_entries;
  document.getElementById('m-skills').textContent = m.active_skills;
  document.getElementById('m-channels').textContent = m.connected_channels;
  document.getElementById('m-jobs').textContent = m.scheduled_jobs;
  const secs = Math.floor(m.uptime_seconds);
  const h = Math.floor(secs/3600), mn = Math.floor((secs%3600)/60), s = secs%60;
  document.getElementById('m-uptime').textContent = h > 0 ? `${h}h ${mn}m` : `${mn}m ${s}s`;
}
connect();
</script>
</body>
</html>"""


def get_dashboard_html() -> str:
    """Return the operator console HTML."""
    return DASHBOARD_HTML


def render_dashboard_json(state: DashboardState) -> str:
    """Render dashboard state as JSON for WebSocket push."""
    return json.dumps(state.get_full_state())
