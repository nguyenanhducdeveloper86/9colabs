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
    natural_key,
    validate_account_name,
)
from ninecolab.cli import build_account_env, find_colab_bin, run_colab_stream
from ninecolab.pick import normalize_mode, order_candidates, run_with_failover

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
        candidates = order_candidates(accounts, state, ts)
        next_pick = candidates[0] if candidates else None

        rows: list[dict[str, Any]] = []
        if accounts:
            with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
                futs = {
                    pool.submit(fetch_account_status, self.store, acct, ts): acct
                    for acct in accounts
                }
                for fut in as_completed(futs):
                    rows.append(fut.result())
        rows.sort(key=lambda r: natural_key(r["account"]))

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
        pro_count = sum(1 for r in rows if r["balance"] > 0)
        cooldown_count = sum(1 for r in rows if r["cooldown_s"] > 0)
        gpus_set = sorted({g for r in rows for g in r.get("gpus", [])})
        tpus_set = sorted({t for r in rows for t in r.get("tpus", [])})

        gpu_Order = ["A100", "L4", "G4", "T4", "H100"]
        tpu_order = ["V6E1", "V5E1"]
        accel_counts: dict[str, int] = {}
        for g in gpu_Order:
            accel_counts[g] = sum(1 for r in rows if g in r.get("gpus", []))
        for t in tpu_order:
            accel_counts[f"TPU {t}"] = sum(
                1 for r in rows if t in r.get("tpus", [])
            )

        return {
            "mode": state.mode,
            "next_account": next_pick,
            "accounts": rows,
            "sessions": sessions,
            "totals": {
                "accounts": len(rows),
                "ready": ready_count,
                "pro": pro_count,
                "free": max(0, len(rows) - pro_count),
                "cooldown": cooldown_count,
                "units": total_units,
                "rate": total_rate,
                "sessions": total_sessions,
                "gpus": gpus_set,
                "tpus": tpus_set,
                "accelerator_counts": accel_counts,
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
<title>9colab — Multi-Account GPU Pool & Failover Telemetry</title>
<style>
  :root {
    --bg: #070a10;
    --bg-elev: #0d121c;
    --panel: #111826;
    --panel-2: #172033;
    --panel-hover: #1c273d;
    --border: #1f2b42;
    --border-bright: #2e4063;
    --text: #edf2fb;
    --muted: #8b9bb8;
    --dim: #586885;
    --accent: #3b82f6;
    --accent-2: #06b6d4;
    --emerald: #10b981;
    --amber: #f59e0b;
    --violet: #a855f7;
    --rose: #f43f5e;
    --mono: "JetBrains Mono", "SFMono-Regular", Consolas, ui-monospace, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: var(--sans);
    background: radial-gradient(circle at 15% 0%, #111c33 0%, var(--bg) 42%);
    color: var(--text);
    line-height: 1.45;
    min-height: 100vh;
  }
  .wrap { max-width: 1360px; margin: 0 auto; padding: 22px 26px 48px; }
  /* Topbar */
  .topbar {
    display: flex; flex-wrap: wrap; gap: 14px;
    justify-content: space-between; align-items: center;
    padding: 14px 18px;
    background: rgba(17, 24, 38, 0.85);
    backdrop-filter: blur(10px);
    border: 1px solid var(--border);
    border-radius: 14px;
    margin-bottom: 18px;
  }
  .brand { display: flex; align-items: center; gap: 13px; }
  .logo {
    width: 40px; height: 40px; border-radius: 10px;
    background: linear-gradient(135deg, #f97316 0%, #3b82f6 100%);
    display: grid; place-items: center;
    font-family: var(--mono); font-weight: 800; font-size: 17px; color: #fff;
    box-shadow: 0 4px 16px rgba(59, 130, 246, 0.3);
  }
  .brand h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.02em; display: flex; align-items: center; gap: 8px; }
  .version-pill {
    font-family: var(--mono); font-size: 11px; font-weight: 600;
    padding: 2px 7px; border-radius: 6px;
    background: var(--panel-2); border: 1px solid var(--border-bright); color: #93c5fd;
  }
  .sub { color: var(--muted); font-size: 12.5px; display: flex; align-items: center; gap: 8px; margin-top: 2px; }
  .pulse {
    width: 8px; height: 8px; border-radius: 50%; background: var(--emerald);
    box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.18);
  }
  .toolbar { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
  button, select, input {
    font-family: var(--sans); font-size: 12.5px; color: var(--text);
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; outline: none;
  }
  input:focus, select:focus { border-color: var(--accent); }
  button { cursor: pointer; font-weight: 600; transition: all 0.15s ease; display: inline-flex; align-items: center; gap: 6px; }
  button:hover { border-color: var(--border-bright); background: var(--panel-hover); }
  .btn-primary { background: var(--accent); border-color: #2563eb; color: #fff; }
  .btn-primary:hover { background: #2563eb; border-color: #60a5fa; }
  .btn-ghost { background: transparent; border-color: var(--border); color: var(--muted); }
  .btn-ghost.active { color: #fde68a; border-color: #b45309; background: rgba(245, 158, 11, 0.12); }
  .btn-danger { color: #fda4af; border-color: #881337; background: rgba(244, 63, 94, 0.1); }
  .btn-danger:hover { background: rgba(244, 63, 94, 0.22); border-color: var(--rose); }
  .btn-sm { padding: 4px 9px; font-size: 11.5px; border-radius: 6px; }
  .mode-switch {
    display: inline-flex; background: var(--bg-elev);
    border: 1px solid var(--border); border-radius: 9px; padding: 3px; gap: 3px;
  }
  .mode-switch button { border: 0; border-radius: 6px; background: transparent; color: var(--muted); padding: 5px 11px; font-size: 12px; }
  .mode-switch button.active { background: var(--accent); color: #fff; box-shadow: 0 2px 8px rgba(59, 130, 246, 0.35); }

  /* KPI Row */
  .kpis {
    display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 13px; margin-bottom: 16px;
  }
  @media (max-width: 1100px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
  .kpi {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 15px 16px; position: relative; overflow: hidden;
  }
  .kpi .k-label { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; display: flex; justify-content: space-between; }
  .kpi .k-val { font-family: var(--mono); font-size: 25px; font-weight: 700; margin-top: 6px; font-variant-numeric: tabular-nums; }
  .kpi .k-foot { color: var(--muted); font-size: 12px; margin-top: 8px; display: flex; justify-content: space-between; align-items: center; }
  .mini-bar { height: 5px; background: var(--bg-elev); border-radius: 999px; overflow: hidden; margin-top: 8px; display: flex; }
  .mini-bar > div { height: 100%; transition: width 0.3s ease; }

  /* Visual Analytics Grid (Option B) */
  .charts-grid {
    display: grid; grid-template-columns: 1.25fr 1.1fr 0.95fr;
    gap: 14px; margin-bottom: 18px;
  }
  @media (max-width: 1100px) { .charts-grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 13px; padding: 16px 18px; margin-bottom: 16px;
  }
  .charts-grid .card { margin-bottom: 0; }
  .card-head {
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; margin-bottom: 13px; flex-wrap: wrap;
  }
  .card-title { margin: 0; font-size: 14.5px; font-weight: 700; letter-spacing: -0.01em; display: flex; align-items: center; gap: 8px; }
  .card-sub { color: var(--muted); font-size: 12px; }

  /* Bar Chart Rows */
  .bar-list { display: flex; flex-direction: column; gap: 8px; max-height: 235px; overflow-y: auto; padding-right: 4px; }
  .bar-row { display: grid; grid-template-columns: 74px 1fr 96px; align-items: center; gap: 10px; font-size: 12.5px; }
  .bar-label { font-family: var(--mono); font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-track { height: 9px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 999px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 999px; transition: width 0.35s ease; }
  .bar-fill.pro { background: linear-gradient(90deg, #3b82f6, #10b981); }
  .bar-fill.free { background: #334155; }
  .bar-fill.gpu { background: linear-gradient(90deg, #2563eb, #06b6d4); }
  .bar-fill.tpu { background: linear-gradient(90deg, #7c3aed, #ec4899); }
  .bar-val { font-family: var(--mono); font-size: 12px; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }

  /* Router & Pool Breakdown Box */
  .router-box {
    background: var(--bg-elev); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 12px;
  }
  .router-next {
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 6px;
  }
  .router-acct { font-family: var(--mono); font-size: 18px; font-weight: 700; color: #60a5fa; }
  .legend-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
  .legend-item {
    background: var(--bg-elev); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; display: flex; justify-content: space-between; align-items: center; font-size: 12px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }

  /* Filter & Table */
  .filter-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .tabs { display: flex; flex-wrap: wrap; gap: 5px; }
  .tab {
    padding: 5px 11px; border-radius: 7px; font-size: 12px; font-weight: 600;
    background: var(--bg-elev); border: 1px solid var(--border); color: var(--muted); cursor: pointer;
  }
  .tab.active { background: rgba(59, 130, 246, 0.16); color: #93c5fd; border-color: var(--accent); }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 10px 11px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tbody tr:hover { background: rgba(255, 255, 255, 0.02); }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .badge {
    display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 6px;
    font-family: var(--mono); font-size: 11px; font-weight: 600; margin-right: 4px; margin-bottom: 2px;
    border: 1px solid var(--border);
  }
  .badge.gpu-hi { color: #34d399; border-color: #065f46; background: rgba(16, 185, 129, 0.12); }
  .badge.gpu-std { color: #60a5fa; border-color: #1e40af; background: rgba(59, 130, 246, 0.12); }
  .badge.tpu { color: #c084fc; border-color: #581c87; background: rgba(168, 85, 247, 0.12); }
  .badge.pro { color: #fde68a; border-color: #92400e; background: rgba(245, 158, 11, 0.15); }
  .badge.free { color: var(--muted); border-color: var(--border); background: var(--bg-elev); }
  .badge.next { color: #38bdf8; border-color: #0284c7; background: rgba(14, 165, 233, 0.15); }
  .badge.ready { color: #34d399; border-color: #065f46; background: rgba(16, 185, 129, 0.12); }
  .badge.cd { color: #fbbf24; border-color: #92400e; background: rgba(245, 158, 11, 0.14); }
  .badge.err { color: #f87171; border-color: #991b1b; background: rgba(239, 68, 68, 0.14); }

  .ccu-cell { min-width: 135px; }
  .ccu-top { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 12.5px; font-weight: 600; }
  .ccu-bar { height: 5px; background: var(--bg-elev); border-radius: 999px; overflow: hidden; margin-top: 4px; }
  .ccu-bar > div { height: 100%; border-radius: 999px; }

  .row-actions { display: flex; gap: 5px; flex-wrap: nowrap; }
  .oauth-box {
    display: none; background: var(--panel); border: 1px solid var(--accent);
    border-radius: 12px; padding: 16px 18px; margin-bottom: 16px;
  }
  .oauth-box.open { display: block; }
  .flex-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
  .toast {
    position: fixed; right: 22px; bottom: 22px; background: #0f172a;
    border: 1px solid var(--accent); padding: 10px 15px; border-radius: 9px;
    font-size: 12.5px; font-weight: 600; display: none; box-shadow: 0 10px 30px rgba(0,0,0,0.5); z-index: 99;
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <div class="logo">9C</div>
      <div>
        <h1>9colab Cluster Console <span class="version-pill">v0.1.0</span></h1>
        <div class="sub">
          <span class="pulse"></span>
          <span id="updatedAt">Syncing pool telemetry...</span>
          <span>&bull;</span>
          <span>Auto-refresh: 10s</span>
        </div>
      </div>
    </div>
    <div class="toolbar">
      <div class="mode-switch" title="Switch account selection algorithm">
        <button id="btnModeB" onclick="setMode('round-robin')">Mode B &middot; Round-Robin</button>
        <button id="btnModeC" onclick="setMode('fill-first')">Mode C &middot; Fill-First</button>
      </div>
      <button id="btnPrivacy" class="btn-ghost" onclick="togglePrivacy()">Hide Emails</button>
      <button onclick="clearCooldown('')">Reset Cooldowns</button>
      <button onclick="toggleOAuth()">+ Add Account</button>
      <button class="btn-primary" onclick="loadOverview()">Refresh</button>
    </div>
  </div>

  <div id="oauthBox" class="oauth-box">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <strong>Connect New Google Colab / AI Pro Account (PKCE OAuth2)</strong>
      <button class="btn-sm" onclick="toggleOAuth()">Close</button>
    </div>
    <div class="flex-row">
      <input id="oauthName" placeholder="Account alias (optional: auto acc12, acc13...)" style="width:270px">
      <button class="btn-primary" onclick="startOAuth()">1. Generate Google Auth Link</button>
      <a id="oauthLink" href="#" target="_blank" style="display:none;color:#60a5fa;font-weight:600;font-size:13px">Open Authorization Page &rarr;</a>
    </div>
    <div class="flex-row">
      <input id="oauthCode" placeholder="2. Paste authorization code from Google page here" style="flex:1;min-width:260px">
      <button class="btn-primary" onclick="finishOAuth()">3. Verify &amp; Save Profile</button>
    </div>
  </div>

  <!-- 5 KPI Cards -->
  <div class="kpis">
    <div class="kpi">
      <div class="k-label"><span>Account Pool</span><span id="kpiPoolPct">100% Ready</span></div>
      <div class="k-val" id="kpiAccounts">-</div>
      <div class="mini-bar">
        <div id="barPro" style="width:0%;background:var(--amber)"></div>
        <div id="barFree" style="width:0%;background:var(--accent)"></div>
      </div>
      <div class="k-foot"><span id="kpiTierText">0 Pro &middot; 0 Free</span><span id="kpiCdText">0 Cooldown</span></div>
    </div>
    <div class="kpi">
      <div class="k-label"><span>Total Compute Units</span><span>CCU Balance</span></div>
      <div class="k-val" id="kpiUnits">-</div>
      <div class="mini-bar"><div id="barUnits" style="width:0%;background:var(--emerald)"></div></div>
      <div class="k-foot"><span id="kpiAvgUnits">0.00 CCU avg/pro</span><span>~200 CCU/grant</span></div>
    </div>
    <div class="kpi">
      <div class="k-label"><span>Hourly Burn Rate</span><span>Active Drain</span></div>
      <div class="k-val" id="kpiRate">-</div>
      <div class="mini-bar"><div id="barRate" style="width:0%;background:var(--rose)"></div></div>
      <div class="k-foot"><span id="kpiEta">Idle (No burn)</span><span>Live tunnel</span></div>
    </div>
    <div class="kpi">
      <div class="k-label"><span>Active VM Runtimes</span><span>Assignments</span></div>
      <div class="k-val" id="kpiSessions">-</div>
      <div class="mini-bar"><div id="barSessions" style="width:0%;background:var(--violet)"></div></div>
      <div class="k-foot"><span id="kpiSessionCap">Capacity available</span><span>HTTP 412 guard</span></div>
    </div>
    <div class="kpi">
      <div class="k-label"><span>Hardware Matrix</span><span>Pool Entitlements</span></div>
      <div class="k-val" id="kpiGpus" style="font-size:20px;line-height:36px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">-</div>
      <div class="mini-bar"><div style="width:100%;background:linear-gradient(90deg,#3b82f6,#a855f7)"></div></div>
      <div class="k-foot"><span id="kpiTpus">TPUs: -</span><span>Auto failover</span></div>
    </div>
  </div>

  <!-- 3 Visual Telemetry Charts Row (Option B) -->
  <div class="charts-grid">
    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">Compute Units (CCU) by Account</h2>
          <div class="card-sub">Paid CCU balance vs 200 CCU monthly reference</div>
        </div>
      </div>
      <div id="ccuChart" class="bar-list"></div>
    </div>

    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">Accelerator Availability across Pool</h2>
          <div class="card-sub">Number of accounts entitled to each GPU / TPU</div>
        </div>
      </div>
      <div id="accelChart" class="bar-list"></div>
    </div>

    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">Router &amp; Pool Composition</h2>
          <div class="card-sub">Live failover pointer &amp; tier distribution</div>
        </div>
      </div>
      <div class="router-box">
        <div class="card-sub">NEXT ACCOUNT FOR <code style="color:#93c5fd">9colab run / new</code></div>
        <div class="router-next">
          <span class="router-acct" id="nextAccountLabel">-</span>
          <span class="badge next" id="nextModeBadge">MODE B</span>
        </div>
      </div>
      <div class="legend-grid">
        <div class="legend-item"><span><i class="dot" style="background:var(--amber)"></i>Pro (CCU &gt; 0)</span><strong class="mono" id="legPro">0</strong></div>
        <div class="legend-item"><span><i class="dot" style="background:var(--accent)"></i>Free Quota (T4)</span><strong class="mono" id="legFree">0</strong></div>
        <div class="legend-item"><span><i class="dot" style="background:var(--emerald)"></i>Ready Accounts</span><strong class="mono" id="legReady">0</strong></div>
        <div class="legend-item"><span><i class="dot" style="background:var(--rose)"></i>412 Cooldown</span><strong class="mono" id="legCd">0</strong></div>
      </div>
    </div>
  </div>

  <!-- Accounts Table Card -->
  <div class="card">
    <div class="filter-bar">
      <div class="tabs">
        <button class="tab active" data-filter="all" onclick="setFilter('all', this)">All Accounts</button>
        <button class="tab" data-filter="pro" onclick="setFilter('pro', this)">Pro CCU (&gt;0)</button>
        <button class="tab" data-filter="free" onclick="setFilter('free', this)">Free Tier (0 CCU)</button>
        <button class="tab" data-filter="hipower" onclick="setFilter('hipower', this)">A100 / L4 / G4</button>
        <button class="tab" data-filter="cooldown" onclick="setFilter('cooldown', this)">In Cooldown</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input id="searchInput" placeholder="Search account or email..." oninput="renderTable()" style="width:210px">
        <select id="sortSelect" onchange="renderTable()">
          <option value="natural">Sort: Account (1 &rarr; 99)</option>
          <option value="ccu_desc">Sort: Compute Units (High &rarr; Low)</option>
          <option value="sessions_desc">Sort: Active Sessions</option>
        </select>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Account</th>
            <th>Tier</th>
            <th>Google Email</th>
            <th>Compute Units (CCU)</th>
            <th>Burn/hr</th>
            <th>Eligible GPUs</th>
            <th>Eligible TPUs</th>
            <th>VMs</th>
            <th>Status</th>
            <th>Quick Actions</th>
          </tr>
        </thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Active Sessions Card -->
  <div class="card">
    <div class="card-head">
      <div>
        <h2 class="card-title">Active Colab VM Sessions</h2>
        <div class="card-sub">Live runtimes across all isolated account profiles</div>
      </div>
      <div class="flex-row" style="margin:0">
        <select id="newAccountSelect">
          <option value="">Account: Auto Router (B/C)</option>
        </select>
        <select id="newGpu">
          <option value="T4">GPU: T4</option>
          <option value="L4">GPU: L4</option>
          <option value="A100">GPU: A100</option>
          <option value="G4">GPU: G4</option>
          <option value="H100">GPU: H100</option>
          <option value="CPU">CPU Runtime</option>
        </select>
        <input id="newSessionName" placeholder="Session name (optional)" style="width:165px">
        <button class="btn-primary" onclick="createSession()">+ Launch VM</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Account</th>
            <th>Session Name</th>
            <th>Accelerator</th>
            <th>Machine Shape</th>
            <th>Runtime Endpoint</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="sessionsBody"></tbody>
      </table>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
let stateData = null;
let currentFilter = "all";
let currentOAuthAccount = "";
let maskEmails = new URLSearchParams(window.location.search).get("mask") === "1";

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function formatEmail(email, idx) {
  if (!email || email === "-") return "-";
  if (!maskEmails) return esc(email);
  const parts = String(email).split("@");
  if (parts.length !== 2) return "hidden@example.com";
  const prefix = parts[0].slice(0, 3);
  return esc(`${prefix}•••@${parts[1]}`);
}

function togglePrivacy() {
  maskEmails = !maskEmails;
  const btn = document.getElementById("btnPrivacy");
  btn.classList.toggle("active", maskEmails);
  btn.textContent = maskEmails ? "Emails Hidden" : "Hide Emails";
  if (stateData) renderAll(stateData);
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 3400);
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

function gpuBadges(items) {
  if (!items || !items.length) return '<span class="card-sub">-</span>';
  return items.map(x => {
    const cls = (x === "A100" || x === "H100" || x === "L4" || x === "G4") ? "gpu-hi" : "gpu-std";
    return `<span class="badge ${cls}">${esc(x)}</span>`;
  }).join("");
}

function tpuBadges(items) {
  if (!items || !items.length) return '<span class="card-sub">-</span>';
  return items.map(x => `<span class="badge tpu">${esc(x)}</span>`).join("");
}

async function loadOverview() {
  const data = await api("/api/overview");
  stateData = data;
  renderAll(data);
}

function renderAll(data) {
  document.getElementById("btnPrivacy").classList.toggle("active", maskEmails);
  document.getElementById("btnPrivacy").textContent = maskEmails ? "Emails Hidden" : "Hide Emails";

  document.getElementById("updatedAt").textContent = "Synced " + new Date(data.updated_at).toLocaleTimeString();
  document.getElementById("btnModeB").classList.toggle("active", data.mode === "round-robin");
  document.getElementById("btnModeC").classList.toggle("active", data.mode === "fill-first");

  const t = data.totals;
  const totalAcc = Math.max(1, t.accounts);
  document.getElementById("kpiAccounts").textContent = `${t.ready} / ${t.accounts}`;
  document.getElementById("kpiPoolPct").textContent = `${Math.round((t.ready / totalAcc) * 100)}% Ready`;
  document.getElementById("barPro").style.width = `${(t.pro / totalAcc) * 100}%`;
  document.getElementById("barFree").style.width = `${(t.free / totalAcc) * 100}%`;
  document.getElementById("kpiTierText").textContent = `${t.pro} Pro · ${t.free} Free`;
  document.getElementById("kpiCdText").textContent = `${t.cooldown} Cooldown`;

  document.getElementById("kpiUnits").textContent = t.units.toFixed(2);
  const cap = Math.max(200, t.pro * 200);
  document.getElementById("barUnits").style.width = `${Math.min(100, (t.units / cap) * 100)}%`;
  document.getElementById("kpiAvgUnits").textContent = t.pro ? `${(t.units / t.pro).toFixed(1)} CCU/pro` : "0 Pro grants";

  document.getElementById("kpiRate").textContent = t.rate.toFixed(2) + "/hr";
  document.getElementById("barRate").style.width = `${Math.min(100, t.rate * 10)}%`;
  document.getElementById("kpiEta").textContent = t.rate > 0 ? `~${(t.units / t.rate).toFixed(1)}h left` : "Idle (0.00/hr)";

  document.getElementById("kpiSessions").textContent = t.sessions;
  document.getElementById("barSessions").style.width = `${Math.min(100, (t.sessions / totalAcc) * 100)}%`;
  document.getElementById("kpiSessionCap").textContent = `${t.ready} accounts available`;

  document.getElementById("kpiGpus").textContent = t.gpus.length ? t.gpus.join(" · ") : "None";
  document.getElementById("kpiTpus").textContent = "TPUs: " + (t.tpus && t.tpus.length ? t.tpus.join(", ") : "-");

  // Router & Legend
  document.getElementById("nextAccountLabel").textContent = data.next_account || "None";
  document.getElementById("nextModeBadge").textContent = data.mode === "round-robin" ? "MODE B · RR" : "MODE C · FILL";
  document.getElementById("legPro").textContent = t.pro;
  document.getElementById("legFree").textContent = t.free;
  document.getElementById("legReady").textContent = t.ready;
  document.getElementById("legCd").textContent = t.cooldown;

  // Chart 1: CCU Balance per Account
  const ccuSorted = [...data.accounts].sort((a, b) => b.balance - a.balance);
  const maxCcu = Math.max(200, ...ccuSorted.map(x => x.balance));
  document.getElementById("ccuChart").innerHTML = ccuSorted.map(r => {
    const pct = Math.max(3, Math.min(100, (r.balance / maxCcu) * 100));
    const cls = r.balance > 0 ? "pro" : "free";
    return `<div class="bar-row">
      <span class="bar-label">${esc(r.account)}</span>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div>
      <span class="bar-val">${r.balance.toFixed(2)} CCU</span>
    </div>`;
  }).join("");

  // Chart 2: Accelerator Availability Matrix
  const ac = t.accelerator_counts || {};
  document.getElementById("accelChart").innerHTML = Object.entries(ac).map(([name, cnt]) => {
    const pct = t.accounts ? Math.round((cnt / t.accounts) * 100) : 0;
    const cls = name.startsWith("TPU") ? "tpu" : "gpu";
    return `<div class="bar-row">
      <span class="bar-label">${esc(name)}</span>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div>
      <span class="bar-val">${cnt}/${t.accounts} (${pct}%)</span>
    </div>`;
  }).join("");

  // Populate account selector for New VM
  const sel = document.getElementById("newAccountSelect");
  const prev = sel.value;
  sel.innerHTML = '<option value="">Account: Auto Router (B/C)</option>' +
    data.accounts.map(r => `<option value="${esc(r.account)}">${esc(r.account)} (${r.balance.toFixed(0)} CCU)</option>`).join("");
  sel.value = prev;

  renderTable();
  renderSessions(data.sessions);
}

function setFilter(f, btn) {
  currentFilter = f;
  document.querySelectorAll(".tab").forEach(el => el.classList.remove("active"));
  btn.classList.add("active");
  renderTable();
}

function renderTable() {
  if (!stateData) return;
  const q = document.getElementById("searchInput").value.trim().toLowerCase();
  const sortMode = document.getElementById("sortSelect").value;

  let rows = stateData.accounts.filter(r => {
    if (currentFilter === "pro" && !(r.balance > 0)) return false;
    if (currentFilter === "free" && !(r.balance === 0)) return false;
    if (currentFilter === "hipower" && !r.gpus.some(g => ["A100", "L4", "G4", "H100"].includes(g))) return false;
    if (currentFilter === "cooldown" && !(r.cooldown_s > 0)) return false;
    if (q && !r.account.toLowerCase().includes(q) && !String(r.email).toLowerCase().includes(q)) return false;
    return true;
  });

  if (sortMode === "ccu_desc") rows.sort((a, b) => b.balance - a.balance);
  else if (sortMode === "sessions_desc") rows.sort((a, b) => b.assignments - a.assignments);

  const tbody = document.getElementById("accountsBody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="card-sub">No matching accounts for this filter.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r, i) => {
    const isNext = stateData.next_account === r.account;
    const tierBadge = r.balance > 0
      ? '<span class="badge pro">PRO CCU</span>'
      : '<span class="badge free">FREE TIER</span>';
    let st = '<span class="badge ready">READY</span>';
    if (r.error) st = `<span class="badge err" title="${esc(r.error)}">ERROR</span>`;
    else if (r.cooldown_s > 0) st = `<span class="badge cd">COOLDOWN (${Math.ceil(r.cooldown_s/60)}m)</span>`;

    const ccuPct = Math.min(100, (r.balance / 200) * 100);
    const ccuColor = r.balance > 0 ? "var(--emerald)" : "#334155";
    const bestGpu = r.gpus.includes("A100") ? "A100" : (r.gpus.includes("L4") ? "L4" : "T4");

    return `<tr>
      <td class="mono">
        <strong>${esc(r.account)}</strong>
        ${isNext ? '<span class="badge next" title="Next account selected by router">NEXT</span>' : ''}
      </td>
      <td>${tierBadge}</td>
      <td class="mono">${formatEmail(r.email, i)}</td>
      <td class="ccu-cell">
        <div class="ccu-top"><span>${r.balance.toFixed(2)}</span><span class="card-sub">/ 200</span></div>
        <div class="ccu-bar"><div style="width:${ccuPct}%;background:${ccuColor}"></div></div>
      </td>
      <td class="mono">${r.rate.toFixed(2)}</td>
      <td>${gpuBadges(r.gpus)}</td>
      <td>${tpuBadges(r.tpus)}</td>
      <td class="mono">${r.assignments}</td>
      <td>${st}</td>
      <td class="row-actions">
        <button class="btn-sm" data-acct="${esc(r.account)}" data-gpu="${esc(bestGpu)}" onclick="quickCopyCli(this.dataset.acct, this.dataset.gpu)" title="Copy 9colab CLI command">Copy CLI</button>
        ${r.cooldown_s > 0 ? `<button class="btn-sm" data-acct="${esc(r.account)}" onclick="clearCooldown(this.dataset.acct)">Unblock</button>` : ""}
        <button class="btn-sm btn-danger" data-acct="${esc(r.account)}" onclick="removeAccount(this.dataset.acct)">Remove</button>
      </td>
    </tr>`;
  }).join("");
}

function renderSessions(sessions) {
  const sbody = document.getElementById("sessionsBody");
  if (!sessions || !sessions.length) {
    sbody.innerHTML = '<tr><td colspan="6" class="card-sub">No active VM sessions across any account. Use "+ Launch VM" above or run <code>9colab run script.py --gpu T4</code>.</td></tr>';
    return;
  }
  sbody.innerHTML = sessions.map(s => `<tr>
    <td class="mono"><strong>${esc(s.account)}</strong></td>
    <td class="mono">${esc(s.name)}</td>
    <td><span class="badge gpu-hi">${esc(s.accelerator)}</span></td>
    <td>${esc(s.machine_shape)}</td>
    <td class="mono card-sub">${esc(s.endpoint)}</td>
    <td class="row-actions">
      <button class="btn-sm" data-sess="${esc(s.name)}" onclick="copySessionSsh(this.dataset.sess)">Copy SSH</button>
      <button class="btn-sm btn-danger" data-acct="${esc(s.account)}" data-sess="${esc(s.name)}" onclick="stopSession(this.dataset.acct,this.dataset.sess)">Stop VM</button>
    </td>
  </tr>`).join("");
}

function quickCopyCli(acct, gpu) {
  const cmd = `9colab run train.py -a ${acct} --gpu ${gpu}`;
  navigator.clipboard.writeText(cmd);
  showToast(`Copied: ${cmd}`);
}

function copySessionSsh(sess) {
  const cmd = `9colab ssh -s ${sess}`;
  navigator.clipboard.writeText(cmd);
  showToast(`Copied: ${cmd}`);
}

async function setMode(m) {
  await api("/api/mode", {mode: m});
  showToast("Router mode switched to " + m);
  loadOverview();
}

async function clearCooldown(acct) {
  await api("/api/clear-cooldown", {account: acct || null});
  showToast("Cooldown timer cleared");
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
  const account = document.getElementById("newAccountSelect").value;
  showToast(`Allocating ${gpu} VM...`);
  const res = await api("/api/new-session", {gpu, name, account: account || null});
  showToast(res.ok ? `VM ready on ${res.account}` : (res.error || "Allocation failed"));
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
    showToast(`Connected ${res.account} (${res.email})`);
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
            if self.path == "/" or self.path.startswith("/?") or self.path.startswith("/index.html"):
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
