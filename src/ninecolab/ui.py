"""Web UI dashboard server and REST API for 9colab."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ninecolab.accounts import (
    DEFAULT_TOKEN_URI,
    AccountStore,
    fetch_account_status,
    fetch_email,
    validate_account_name,
)
from ninecolab.cli import build_account_env, find_colab_bin, run_colab_stream
from ninecolab.pick import normalize_mode, run_with_failover

PORT_SEARCH_RANGE = 20

OAUTH_CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
)
OAUTH_CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"
OAUTH_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
OAUTH_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class DashboardService:
    def __init__(self, store: AccountStore | None = None) -> None:
        self.store = store or AccountStore()
        self._pending_oauth: dict[str, str] = {}

    def get_overview(self, now: float | None = None) -> dict[str, Any]:
        ts = time.time() if now is None else now
        accounts = self.store.list_accounts()
        state = self.store.load_state()

        rows: list[dict[str, Any]] = []
        if accounts:
            with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
                futs = {
                    pool.submit(fetch_account_status, self.store, acct, ts): acct
                    for acct in accounts
                }
                for fut in as_completed(futs):
                    rows.append(fut.result())
        rows.sort(key=lambda r: r["account"])

        sessions: list[dict[str, Any]] = []
        for acct in accounts:
            local_map = self.store.read_local_sessions(acct)
            for s_name, s_data in local_map.items():
                sessions.append(
                    {
                        "account": acct,
                        "name": s_data.get("name", s_name),
                        "endpoint": s_data.get("endpoint", "-"),
                        "variant": s_data.get("variant", "DEFAULT"),
                        "accelerator": s_data.get("accelerator", "NONE"),
                        "machine_shape": s_data.get("machine_shape", "STANDARD"),
                    }
                )

        total_units = round(sum(r["balance"] for r in rows), 2)
        total_rate = round(sum(r["rate"] for r in rows), 2)
        total_sessions = max(sum(r["assignments"] for r in rows), len(sessions))
        ready_count = sum(
            1 for r in rows if not r["error"] and r["cooldown_s"] == 0
        )
        gpus_set = sorted({g for r in rows for g in r.get("gpus", [])})

        return {
            "mode": state.mode,
            "accounts": rows,
            "sessions": sessions,
            "totals": {
                "accounts": len(rows),
                "ready": ready_count,
                "units": total_units,
                "rate": total_rate,
                "sessions": total_sessions,
                "gpus": gpus_set,
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def set_mode(self, raw_mode: str) -> dict[str, Any]:
        mode = normalize_mode(raw_mode)
        state = self.store.load_state()
        state.mode = mode
        self.store.save_state(state)
        return {"ok": True, "mode": mode}

    def clear_cooldown(self, account: str | None = None) -> dict[str, Any]:
        state = self.store.load_state()
        if account:
            state.cooldowns.pop(account, None)
        else:
            state.cooldowns.clear()
        self.store.save_state(state)
        return {"ok": True}

    def remove_account(self, account: str) -> dict[str, Any]:
        self.store.remove_account(validate_account_name(account))
        return {"ok": True}

    def stop_session(self, account: str, session: str) -> dict[str, Any]:
        colab_bin = find_colab_bin()
        env = build_account_env(self.store, validate_account_name(account))
        proc = subprocess.run(
            [colab_bin, "stop", "-s", session],
            env=env,
            capture_output=True,
            text=True,
        )
        return {
            "ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr).strip(),
        }

    def create_session(
        self,
        gpu: str = "T4",
        session_name: str | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        args = ["new"]
        if gpu and gpu.upper() != "CPU":
            args.extend(["--gpu", gpu.upper()])
        if session_name:
            args.extend(["-s", session_name.strip()])

        if account:
            code, out = run_colab_stream(
                self.store, validate_account_name(account), args
            )
            return {"ok": code == 0, "account": account, "output": out.strip()}

        accounts = self.store.list_accounts()
        state = self.store.load_state()
        now = time.time()

        def _runner(acct: str) -> tuple[int, str]:
            return run_colab_stream(self.store, acct, args)

        picked, code, out, next_state = run_with_failover(
            accounts, state, now, _runner
        )
        self.store.save_state(next_state)
        return {"ok": code == 0, "account": picked, "output": out.strip()}

    def start_oauth(self, name: str | None = None) -> dict[str, Any]:
        acct = validate_account_name(name) if name else self.store.next_account_name()
        verifier, challenge = make_pkce_pair()
        self._pending_oauth[acct] = verifier
        params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": " ".join(OAUTH_SCOPES),
            "state": secrets.token_urlsafe(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
            "token_usage": "remote",
            "access_type": "offline",
        }
        auth_url = f"{OAUTH_AUTH_URI}?{urllib.parse.urlencode(params)}"
        return {"ok": True, "account": acct, "auth_url": auth_url}

    def finish_oauth(self, account: str, code: str) -> dict[str, Any]:
        acct = validate_account_name(account)
        verifier = self._pending_oauth.get(acct)
        if not verifier:
            raise ValueError(
                f"No active login flow for '{acct}'. Click 'Get Auth Link' first."
            )
        form = urllib.parse.urlencode(
            {
                "code": code.strip(),
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }
        ).encode()
        req = urllib.request.Request(
            DEFAULT_TOKEN_URI,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            tok_resp = json.loads(resp.read().decode())

        self.store.ensure_profile(acct)
        tpath = self.store.token_path(acct)
        token_doc = {
            "token": tok_resp["access_token"],
            "refresh_token": tok_resp.get("refresh_token", ""),
            "token_uri": DEFAULT_TOKEN_URI,
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "scopes": OAUTH_SCOPES,
            "universe_domain": "googleapis.com",
            "account": "",
        }
        tpath.write_text(json.dumps(token_doc))
        os.chmod(tpath, 0o600)
        self._pending_oauth.pop(acct, None)

        try:
            email = fetch_email(tok_resp["access_token"])
            self.store.save_meta(acct, {"email": email})
        except Exception:
            email = "-"
        return {"ok": True, "account": acct, "email": email}


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>9colab Dashboard — Multi-Account Colab Controller</title>
<style>
  :root {
    --bg: #0b0f17;
    --panel: #131a27;
    --panel-2: #192234;
    --border: #25324c;
    --text: #e6edf7;
    --muted: #8ea0c0;
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --green: #10b981;
    --amber: #f59e0b;
    --red: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 24px; }
  header {
    display: flex; flex-wrap: wrap; gap: 16px;
    justify-content: space-between; align-items: center;
    padding-bottom: 18px; border-bottom: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo {
    width: 38px; height: 38px; border-radius: 10px;
    background: linear-gradient(135deg, #f97316, #3b82f6);
    display: grid; place-items: center; font-weight: 800; font-size: 18px;
  }
  h1 { margin: 0; font-size: 20px; }
  .sub { color: var(--muted); font-size: 13px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  button, select, input {
    font: inherit; font-size: 13px; color: var(--text);
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 12px;
  }
  button { cursor: pointer; font-weight: 600; transition: 0.15s; }
  button:hover { border-color: var(--accent); }
  .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-danger { color: #fca5a5; border-color: #7f1d1d; background: #2a1215; }
  .mode-group {
    display: inline-flex; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  }
  .mode-group button { border: 0; border-radius: 0; background: transparent; color: var(--muted); }
  .mode-group button.active { background: var(--accent); color: #fff; }
  .kpis {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 14px; margin: 20px 0;
  }
  .kpi {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px;
  }
  .kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .kpi .val { font-size: 26px; font-weight: 700; margin-top: 6px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; margin-bottom: 18px;
  }
  .card-head {
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; margin-bottom: 14px; flex-wrap: wrap;
  }
  .card-title { margin: 0; font-size: 16px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 600; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600; margin-right: 4px;
    background: var(--panel-2); border: 1px solid var(--border);
  }
  .badge.gpu { color: #60a5fa; border-color: #1e40af; background: #172554; }
  .badge.tpu { color: #c084fc; border-color: #581c87; background: #2e1065; }
  .badge.ready { color: #34d399; border-color: #065f46; background: #064e3b; }
  .badge.cd { color: #fbbf24; border-color: #92400e; background: #451a03; }
  .badge.err { color: #f87171; border-color: #991b1b; background: #450a0a; }
  .row-actions { display: flex; gap: 6px; }
  .oauth-box {
    display: none; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; margin-top: 12px;
  }
  .oauth-box.open { display: block; }
  .flex-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
  .toast {
    position: fixed; right: 20px; bottom: 20px; background: #1e293b;
    border: 1px solid var(--accent); padding: 10px 14px; border-radius: 8px;
    font-size: 13px; display: none;
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">9C</div>
      <div>
        <h1>9colab Dashboard</h1>
        <div class="sub">Multi-Account Google Colab Pool &bull; <span id="updatedAt">Loading...</span></div>
      </div>
    </div>
    <div class="toolbar">
      <div class="mode-group">
        <button id="btnModeB" onclick="setMode('round-robin')">Mode B: Round-Robin</button>
        <button id="btnModeC" onclick="setMode('fill-first')">Mode C: Fill-First</button>
      </div>
      <button onclick="clearCooldown('')">Clear All Cooldowns</button>
      <button onclick="toggleOAuth()">+ Add Account</button>
      <button class="btn-primary" onclick="loadOverview()">Refresh</button>
    </div>
  </header>

  <div id="oauthBox" class="oauth-box">
    <strong>Add Google Colab Account (OAuth2)</strong>
    <div class="flex-row">
      <input id="oauthName" placeholder="Account name (optional, auto: acc2, acc3...)" style="width:260px">
      <button class="btn-primary" onclick="startOAuth()">1. Get Google Auth Link</button>
      <a id="oauthLink" href="#" target="_blank" style="display:none;color:#60a5fa;font-weight:600">Open Google Login Tab &rarr;</a>
    </div>
    <div class="flex-row">
      <input id="oauthCode" placeholder="2. Paste authorization code from Google here" style="flex:1;min-width:260px">
      <button class="btn-primary" onclick="finishOAuth()">3. Verify &amp; Save Account</button>
    </div>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="label">Accounts (Ready / Total)</div><div class="val" id="kpiAccounts">-</div></div>
    <div class="kpi"><div class="label">Total Compute Units</div><div class="val" id="kpiUnits">-</div></div>
    <div class="kpi"><div class="label">Hourly Burn Rate</div><div class="val" id="kpiRate">-</div></div>
    <div class="kpi"><div class="label">Active VM Sessions</div><div class="val" id="kpiSessions">-</div></div>
    <div class="kpi"><div class="label">Available GPU Pool</div><div class="val" id="kpiGpus">-</div></div>
  </div>

  <div class="card">
    <div class="card-head">
      <h2 class="card-title">Google Colab Accounts</h2>
    </div>
    <table>
      <thead>
        <tr>
          <th>Account</th>
          <th>Email</th>
          <th>Compute Units</th>
          <th>Rate/hr</th>
          <th>Eligible GPUs</th>
          <th>Eligible TPUs</th>
          <th>Sessions</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="accountsBody"></tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-head">
      <h2 class="card-title">Active VM Sessions Across Accounts</h2>
      <div class="flex-row" style="margin:0">
        <select id="newGpu">
          <option value="T4">GPU: T4</option>
          <option value="L4">GPU: L4</option>
          <option value="A100">GPU: A100</option>
          <option value="H100">GPU: H100</option>
          <option value="CPU">CPU Only</option>
        </select>
        <input id="newSessionName" placeholder="Session name (optional)" style="width:170px">
        <button class="btn-primary" onclick="createSession()">+ New VM (Auto Failover)</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Account</th>
          <th>Session Name</th>
          <th>Accelerator</th>
          <th>Shape</th>
          <th>Endpoint</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="sessionsBody"></tbody>
    </table>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
let currentOAuthAccount = "";

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 3500);
}

async function api(path, body) {
  const opts = body ? {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  } : {};
  const res = await fetch(path, opts);
  return await res.json();
}

function esc(v) {
  return String(v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function badges(items, cls) {
  if (!items || !items.length) return '<span class="sub">-</span>';
  return items.map(x => `<span class="badge ${cls}">${esc(x)}</span>`).join("");
}

async function loadOverview() {
  const data = await api("/api/overview");
  document.getElementById("updatedAt").textContent = "Updated " + new Date(data.updated_at).toLocaleTimeString();
  document.getElementById("btnModeB").classList.toggle("active", data.mode === "round-robin");
  document.getElementById("btnModeC").classList.toggle("active", data.mode === "fill-first");

  const t = data.totals;
  document.getElementById("kpiAccounts").textContent = `${t.ready} / ${t.accounts}`;
  document.getElementById("kpiUnits").textContent = t.units.toFixed(2);
  document.getElementById("kpiRate").textContent = t.rate.toFixed(2) + "/hr";
  document.getElementById("kpiSessions").textContent = t.sessions;
  document.getElementById("kpiGpus").textContent = t.gpus.length ? t.gpus.join(", ") : "None";

  const tbody = document.getElementById("accountsBody");
  if (!data.accounts.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="sub">No accounts yet. Click "+ Add Account" or run 9colab add.</td></tr>';
  } else {
    tbody.innerHTML = data.accounts.map(r => {
      let st = '<span class="badge ready">READY</span>';
      if (r.error) st = `<span class="badge err" title="${esc(r.error)}">ERROR</span>`;
      else if (r.cooldown_s > 0) st = `<span class="badge cd">COOLDOWN (${Math.ceil(r.cooldown_s/60)}m)</span>`;
      return `<tr>
        <td><strong>${esc(r.account)}</strong></td>
        <td>${esc(r.email)}</td>
        <td>${r.balance.toFixed(2)}</td>
        <td>${r.rate.toFixed(2)}</td>
        <td>${badges(r.gpus, "gpu")}</td>
        <td>${badges(r.tpus, "tpu")}</td>
        <td>${r.assignments}</td>
        <td>${st}</td>
        <td class="row-actions">
          ${r.cooldown_s > 0 ? `<button data-acct="${esc(r.account)}" onclick="clearCooldown(this.dataset.acct)">Unblock</button>` : ""}
          <button class="btn-danger" data-acct="${esc(r.account)}" onclick="removeAccount(this.dataset.acct)">Remove</button>
        </td>
      </tr>`;
    }).join("");
  }

  const sbody = document.getElementById("sessionsBody");
  if (!data.sessions.length) {
    sbody.innerHTML = '<tr><td colspan="6" class="sub">No active sessions across any account.</td></tr>';
  } else {
    sbody.innerHTML = data.sessions.map(s => `<tr>
      <td><strong>${esc(s.account)}</strong></td>
      <td>${esc(s.name)}</td>
      <td><span class="badge gpu">${esc(s.accelerator)}</span></td>
      <td>${esc(s.machine_shape)}</td>
      <td class="sub">${esc(s.endpoint)}</td>
      <td><button class="btn-danger" data-acct="${esc(s.account)}" data-sess="${esc(s.name)}" onclick="stopSession(this.dataset.acct,this.dataset.sess)">Stop VM</button></td>
    </tr>`).join("");
  }
}

async function setMode(m) {
  await api("/api/mode", {mode: m});
  showToast("Selection mode updated to " + m);
  loadOverview();
}

async function clearCooldown(acct) {
  await api("/api/clear-cooldown", {account: acct || null});
  showToast("Cooldown cleared");
  loadOverview();
}

async function removeAccount(acct) {
  if (!confirm(`Remove account profile '${acct}'?`)) return;
  await api("/api/remove-account", {account: acct});
  showToast(`Removed ${acct}`);
  loadOverview();
}

async function stopSession(acct, session) {
  showToast(`Stopping session ${session} on ${acct}...`);
  await api("/api/stop-session", {account: acct, session});
  loadOverview();
}

async function createSession() {
  const gpu = document.getElementById("newGpu").value;
  const name = document.getElementById("newSessionName").value;
  showToast(`Allocating ${gpu} VM via 9colab failover...`);
  const res = await api("/api/new-session", {gpu, name});
  showToast(res.ok ? `Created on ${res.account}` : (res.error || "Allocation failed"));
  loadOverview();
}

function toggleOAuth() {
  document.getElementById("oauthBox").classList.toggle("open");
}

async function startOAuth() {
  const name = document.getElementById("oauthName").value.trim();
  const res = await api("/api/oauth/start", {name: name || null});
  currentOAuthAccount = res.account;
  document.getElementById("oauthName").value = res.account;
  const link = document.getElementById("oauthLink");
  link.href = res.auth_url;
  link.style.display = "inline";
  window.open(res.auth_url, "_blank");
  showToast(`Opened Google login for ${res.account}`);
}

async function finishOAuth() {
  const acct = document.getElementById("oauthName").value.trim() || currentOAuthAccount;
  const code = document.getElementById("oauthCode").value.trim();
  if (!code) return showToast("Paste the Google authorization code first");
  const res = await api("/api/oauth/finish", {account: acct, code});
  if (res.ok) {
    showToast(`Added ${res.account} (${res.email})`);
    document.getElementById("oauthCode").value = "";
    document.getElementById("oauthBox").classList.remove("open");
    loadOverview();
  } else {
    showToast(res.error || "OAuth failed");
  }
}

loadOverview();
setInterval(loadOverview, 10000);
</script>
</body>
</html>
"""


def make_handler(service: DashboardService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _allowed(self, need_json: bool = False) -> bool:
            port = self.server.server_address[1]
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self._send_json({"error": "Forbidden host"}, status=403)
                return False
            ctype = self.headers.get("Content-Type", "")
            if need_json and not ctype.startswith("application/json"):
                self._send_json({"error": "Forbidden content type"}, status=403)
                return False
            return True

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:
            if not self._allowed():
                return
            if self.path in ("/", "/index.html"):
                raw = DASHBOARD_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path.startswith("/api/overview"):
                self._send_json(service.get_overview())
                return
            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            if not self._allowed(need_json=True):
                return
            try:
                body = self._read_json()
                if self.path == "/api/mode":
                    self._send_json(service.set_mode(str(body.get("mode", "B"))))
                elif self.path == "/api/clear-cooldown":
                    self._send_json(service.clear_cooldown(body.get("account")))
                elif self.path == "/api/remove-account":
                    self._send_json(service.remove_account(str(body["account"])))
                elif self.path == "/api/stop-session":
                    self._send_json(
                        service.stop_session(
                            str(body["account"]), str(body["session"])
                        )
                    )
                elif self.path == "/api/new-session":
                    self._send_json(
                        service.create_session(
                            gpu=str(body.get("gpu", "T4")),
                            session_name=body.get("name") or None,
                            account=body.get("account") or None,
                        )
                    )
                elif self.path == "/api/oauth/start":
                    self._send_json(service.start_oauth(body.get("name") or None))
                elif self.path == "/api/oauth/finish":
                    self._send_json(
                        service.finish_oauth(
                            str(body["account"]), str(body["code"])
                        )
                    )
                else:
                    self._send_json({"error": "Not found"}, status=404)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)

    return Handler


def run_ui_server(
    store: AccountStore,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    service = DashboardService(store)
    server = None
    for candidate in range(port, port + PORT_SEARCH_RANGE):
        try:
            server = ThreadingHTTPServer((host, candidate), make_handler(service))
            break
        except OSError:
            print(f"[9colab] Port {candidate} is busy, trying {candidate + 1}...")
    if server is None:
        print(f"[9colab] No free port in {port}-{port + PORT_SEARCH_RANGE - 1}.")
        return 1
    url = f"http://{host}:{server.server_address[1]}"
    print(f"[9colab] Dashboard running at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[9colab] Dashboard stopped.")
    finally:
        server.server_close()
    return 0
