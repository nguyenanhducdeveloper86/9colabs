"""Account profile management, OAuth token refresh, CCU status, and session lookup."""

import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ninecolab.pick import MODE_ROUND_ROBIN, SelectorState

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CCU_INFO_URL = "https://colab.research.google.com/tun/m/ccu-info?authuser=0"
ASSIGNMENTS_URL = "https://colab.research.google.com/tun/m/assignments?authuser=0"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class SessionLookupError(RuntimeError):
    """Raised when session ownership is missing or ambiguous across accounts."""


def natural_key(name: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def validate_account_name(name: str) -> str:
    cleaned = name.strip()
    if not NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid account name '{name}'. Use letters, digits, '-' or '_'."
        )
    return cleaned

def parse_xssi_json(raw: str) -> dict[str, Any]:
    body = raw.split("\n", 1)[1] if raw.startswith(")]}'") else raw
    return json.loads(body)


class AccountStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        env_dir = os.environ.get("NINECOLAB_HOME")
        default_dir = Path(env_dir) if env_dir else Path.home() / ".9colab"
        self.base_dir = Path(base_dir) if base_dir else default_dir
        self.accounts_dir = self.base_dir / "accounts"
        self.state_path = self.base_dir / "state.json"

    def ensure_base(self) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)
        os.chmod(self.accounts_dir, 0o700)

    def profile_home(self, name: str) -> Path:
        valid = validate_account_name(name)
        return self.accounts_dir / valid

    def token_path(self, name: str) -> Path:
        return self.profile_home(name) / ".config" / "colab-cli" / "token.json"

    def sessions_path(self, name: str) -> Path:
        return self.profile_home(name) / ".config" / "colab-cli" / "sessions.json"

    def meta_path(self, name: str) -> Path:
        return self.profile_home(name) / "meta.json"

    def ensure_profile(self, name: str) -> Path:
        self.ensure_base()
        home = self.profile_home(name)
        cfg = home / ".config" / "colab-cli"
        cfg.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        return home

    def list_accounts(self) -> list[str]:
        if not self.accounts_dir.is_dir():
            return []
        names = [
            p.name
            for p in self.accounts_dir.iterdir()
            if p.is_dir() and (p / ".config" / "colab-cli" / "token.json").is_file()
        ]
        return sorted(names, key=natural_key)

    def next_account_name(self, prefix: str = "acc") -> str:
        existing = set(self.list_accounts())
        idx = 1
        while f"{prefix}{idx}" in existing:
            idx += 1
        return f"{prefix}{idx}"

    def import_from_home(self, name: str, src_home: Path | None = None) -> Path:
        source_root = Path(src_home) if src_home else Path.home()
        src_cfg = source_root / ".config" / "colab-cli"
        src_token = src_cfg / "token.json"
        if not src_token.is_file():
            raise FileNotFoundError(f"No token.json found at {src_token}")

        dest_home = self.ensure_profile(name)
        dest_cfg = dest_home / ".config" / "colab-cli"
        shutil.copy2(src_token, dest_cfg / "token.json")
        os.chmod(dest_cfg / "token.json", 0o600)

        src_sessions = src_cfg / "sessions.json"
        if src_sessions.is_file():
            shutil.copy2(src_sessions, dest_cfg / "sessions.json")
        return dest_home

    def remove_account(self, name: str) -> None:
        home = self.profile_home(name)
        if not home.exists():
            raise FileNotFoundError(f"Account '{name}' does not exist.")
        shutil.rmtree(home)

    def save_meta(self, name: str, meta: dict[str, Any]) -> None:
        self.meta_path(name).write_text(json.dumps(meta, indent=2))

    def load_meta(self, name: str) -> dict[str, Any]:
        path = self.meta_path(name)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def load_state(self) -> SelectorState:
        if not self.state_path.is_file():
            return SelectorState()
        try:
            raw = json.loads(self.state_path.read_text())
            return SelectorState(
                mode=raw.get("mode", MODE_ROUND_ROBIN),
                rr_index=int(raw.get("rr_index", 0)),
                fill_current=raw.get("fill_current"),
                cooldowns={
                    k: float(v) for k, v in raw.get("cooldowns", {}).items()
                },
                cooldown_seconds=int(raw.get("cooldown_seconds", 3600)),
            )
        except Exception:
            return SelectorState()

    def save_state(self, state: SelectorState) -> None:
        self.ensure_base()
        payload = {
            "mode": state.mode,
            "rr_index": state.rr_index,
            "fill_current": state.fill_current,
            "cooldowns": state.cooldowns,
            "cooldown_seconds": state.cooldown_seconds,
        }
        self.state_path.write_text(json.dumps(payload, indent=2))

    def read_local_sessions(self, name: str) -> dict[str, dict[str, Any]]:
        path = self.sessions_path(name)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def resolve_session_owner(
        self, session_name: str | None, explicit_account: str | None = None
    ) -> tuple[str, str | None]:
        accounts = self.list_accounts()
        if explicit_account:
            valid = validate_account_name(explicit_account)
            if valid not in accounts:
                raise SessionLookupError(f"Account '{valid}' is not registered.")
            return valid, session_name

        if not accounts:
            raise SessionLookupError("No accounts registered. Run '9colab add <name>'.")

        matches: list[tuple[str, str]] = []
        for acct in accounts:
            sessions = self.read_local_sessions(acct)
            if session_name:
                if session_name in sessions:
                    matches.append((acct, session_name))
            else:
                for s_name in sessions:
                    matches.append((acct, s_name))

        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            if session_name:
                raise SessionLookupError(
                    f"Session '{session_name}' not found in any account. "
                    "Pass '-a <account>' or run '9colab sessions'."
                )
            if len(accounts) == 1:
                return accounts[0], None
            raise SessionLookupError(
                "No active session found. Pass '-s <session>' or '-a <account>'."
            )

        owners = ", ".join(f"{a} ({s})" for a, s in matches)
        raise SessionLookupError(
            f"Multiple matching sessions found: {owners}. "
            "Pass '-a <account>' and '-s <session>' to disambiguate."
        )


def _is_token_expired(tok: dict[str, Any]) -> bool:
    expiry = tok.get("expiry")
    if not expiry:
        return False
    try:
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def get_valid_access_token(store: AccountStore, name: str) -> str:
    tpath = store.token_path(name)
    tok = json.loads(tpath.read_text())
    access = tok.get("token", "")
    if access and not _is_token_expired(tok):
        return access

    refresh = tok.get("refresh_token")
    if not refresh:
        return access

    form = urllib.parse.urlencode(
        {
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        tok.get("token_uri", DEFAULT_TOKEN_URI),
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        refreshed = json.loads(resp.read().decode())
    tok["token"] = refreshed["access_token"]
    tpath.write_text(json.dumps(tok))
    return tok["token"]


def fetch_email(access_token: str) -> str:
    req = urllib.request.Request(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    return str(data.get("email", "-"))


def fetch_ccu_info(access_token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        CCU_INFO_URL,
        headers={
            "Accept": "application/json",
            "X-Colab-Client-Agent": "colab-cli",
            "x-goog-api-client": "cred-type/u",
            "Authorization": f"Bearer {access_token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode()
    return parse_xssi_json(raw)


def fetch_account_status(store: AccountStore, name: str, now: float) -> dict[str, Any]:
    meta = store.load_meta(name)
    email = meta.get("email", "")
    state = store.load_state()
    cd_until = state.cooldowns.get(name, 0.0)
    cooldown_rem = max(0, int(cd_until - now))
    try:
        token = get_valid_access_token(store, name)
        if not email or email == "-":
            email = fetch_email(token)
            store.save_meta(name, {"email": email})
        ccu = fetch_ccu_info(token)
        return {
            "account": name,
            "email": email,
            "balance": float(ccu.get("currentBalance", 0.0)),
            "rate": float(ccu.get("consumptionRateHourly", 0.0)),
            "assignments": int(ccu.get("assignmentsCount", 0)),
            "gpus": ccu.get("eligibleGpus", []),
            "tpus": ccu.get("eligibleTpus", []),
            "cooldown_s": cooldown_rem,
            "error": None,
        }
    except Exception as exc:
        return {
            "account": name,
            "email": email or "-",
            "balance": 0.0,
            "rate": 0.0,
            "assignments": len(store.read_local_sessions(name)),
            "gpus": [],
            "tpus": [],
            "cooldown_s": cooldown_rem,
            "error": str(exc),
        }
