"""CLI entry point and command router for 9colab."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from ninecolab.accounts import (
    AccountStore,
    SessionLookupError,
    fetch_account_status,
    fetch_email,
    get_valid_access_token,
    natural_key,
    validate_account_name,
)
from ninecolab.pick import (
    MODE_FILL_FIRST,
    MODE_ROUND_ROBIN,
    NoAvailableAccountError,
    is_allocation_refused,
    normalize_mode,
    run_with_failover,
)

SESSION_COMMANDS = {
    "auth",
    "console",
    "download",
    "drivemount",
    "edit",
    "exec",
    "install",
    "log",
    "ls",
    "repl",
    "restart-kernel",
    "rm",
    "ssh",
    "status",
    "stop",
    "upload",
    "url",
}

CREATE_COMMANDS = {"new", "run"}
AGGREGATE_COMMANDS = {"sessions", "usage", "whoami"}
GLOBAL_COMMANDS = {"pay", "readme", "skill", "update", "version", "help"}

HELP_BANNER = """9colab — Multi-account controller & failover router for google-colab-cli

Dashboard:
  9colab                                 Start the web dashboard at http://127.0.0.1:8765
  9colab ui [--port N] [--no-open]       Same, with custom port or without opening the browser

Multi-Account Commands:
  9colab add [name]                      Authorize a new Google Colab account (auto-names acc1, acc2, ...)
  9colab import [name]                   Import current ~/.config/colab-cli login as [name]
  9colab list                            Show balance, GPUs, TPUs, and status across all accounts
  9colab mode [B|C|round-robin|fill-first]
                                         Get or set account selection mode:
                                           B = round-robin (default, rotate across accounts)
                                           C = fill-first  (use one account until quota/412 limit)
  9colab account rm <name>               Remove a saved account profile
  9colab account clear-cooldown [name]   Clear 412 cooldown timer for one or all accounts

Multi-Account Flags (usable on colab commands):
  -a, --account <name>                   Run command on a specific account
  --all                                  Run command in parallel on all accounts (run, exec, stop, ...)
  --on <a1,a2,...>                       Run command in parallel on listed accounts
"""


@dataclass
class RoutedArgs:
    command: str | None
    colab_args: list[str] = field(default_factory=list)
    account: str | None = None
    session: str | None = None
    all_accounts: bool = False
    on_accounts: list[str] = field(default_factory=list)


def extract_routing_flags(argv: list[str]) -> RoutedArgs:
    command: str | None = None
    colab_args: list[str] = []
    account: str | None = None
    session: str | None = None
    all_accounts = False
    on_accounts: list[str] = []

    i = 0
    passthrough = False
    while i < len(argv):
        tok = argv[i]
        if passthrough:
            colab_args.append(tok)
            i += 1
            continue
        if tok == "--":
            passthrough = True
            colab_args.append(tok)
            i += 1
            continue
        if tok in ("-a", "--account") and i + 1 < len(argv):
            account = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--account="):
            account = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--all":
            all_accounts = True
            i += 1
            continue
        if tok == "--on" and i + 1 < len(argv):
            on_accounts = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            i += 2
            continue
        if tok.startswith("--on="):
            on_accounts = [
                x.strip() for x in tok.split("=", 1)[1].split(",") if x.strip()
            ]
            i += 1
            continue
        if tok in ("-s", "--session") and i + 1 < len(argv):
            session = argv[i + 1]
            colab_args.extend([tok, argv[i + 1]])
            i += 2
            continue
        if tok.startswith("--session="):
            session = tok.split("=", 1)[1]
            colab_args.append(tok)
            i += 1
            continue

        if command is None and not tok.startswith("-"):
            command = tok
        colab_args.append(tok)
        i += 1

    return RoutedArgs(
        command=command,
        colab_args=colab_args,
        account=account,
        session=session,
        all_accounts=all_accounts,
        on_accounts=on_accounts,
    )


def find_colab_bin() -> str:
    local_bin = Path.home() / ".local" / "bin" / "colab"
    found = shutil.which("colab")
    if found:
        return found
    if local_bin.is_file():
        return str(local_bin)
    raise FileNotFoundError(
        "Could not find 'colab' executable. Install google-colab-cli first."
    )


def build_account_env(store: AccountStore, account: str) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(store.ensure_profile(account))
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    return env


def run_colab_stream(
    store: AccountStore, account: str, args: list[str], prefix: str | None = None
) -> tuple[int, str]:
    colab_bin = find_colab_bin()
    env = build_account_env(store, account)
    proc = subprocess.Popen(
        [colab_bin, *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        if prefix:
            sys.stdout.write(f"[{prefix}] {line}")
        else:
            sys.stdout.write(line)
        sys.stdout.flush()
    proc.wait()
    return proc.returncode, "".join(captured)


def exec_colab_interactive(
    store: AccountStore, account: str, args: list[str]
) -> None:
    colab_bin = find_colab_bin()
    env = build_account_env(store, account)
    sys.stderr.write(f"[9colab] Using account '{account}'\n")
    sys.stderr.flush()
    os.execvpe(colab_bin, [colab_bin, *args], env)


def cmd_add(store: AccountStore, name: str | None = None) -> int:
    valid = validate_account_name(name) if name else store.next_account_name()
    store.ensure_profile(valid)
    colab_bin = find_colab_bin()
    env = build_account_env(store, valid)
    sys.stderr.write(
        f"[9colab] Logging in account '{valid}'. "
        "Use the authorization code from the URL printed below.\n"
    )
    res = subprocess.run([colab_bin, "--auth", "oauth2", "usage"], env=env)
    tpath = store.token_path(valid)
    if not tpath.is_file():
        shutil.rmtree(store.profile_home(valid), ignore_errors=True)
        sys.stderr.write(
            f"[9colab] Login failed: no token.json created at {tpath}\n"
        )
        return res.returncode or 1
    os.chmod(tpath, 0o600)
    try:
        token = get_valid_access_token(store, valid)
        email = fetch_email(token)
        store.save_meta(valid, {"email": email})
        sys.stderr.write(f"[9colab] Saved account '{valid}' ({email}).\n")
    except Exception:
        sys.stderr.write(f"[9colab] Saved account '{valid}'.\n")
    return 0


def cmd_import(
    store: AccountStore, name: str | None = None, src_home: str | None = None
) -> int:
    valid = validate_account_name(name) if name else store.next_account_name()
    src = Path(src_home).expanduser() if src_home else Path.home()
    store.import_from_home(valid, src)
    try:
        token = get_valid_access_token(store, valid)
        email = fetch_email(token)
        store.save_meta(valid, {"email": email})
    except Exception:
        email = "-"
    print(f"[9colab] Imported account '{valid}' ({email}) from {src}/.config/colab-cli")
    return 0


def cmd_list(store: AccountStore) -> int:
    accounts = store.list_accounts()
    state = store.load_state()
    mode_tag = "B (round-robin)" if state.mode == MODE_ROUND_ROBIN else "C (fill-first)"
    print(f"Selection mode: {mode_tag}")
    if not accounts:
        print("No accounts registered. Run '9colab add <name>' or '9colab import <name>'.")
        return 0

    now = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        futs = {
            pool.submit(fetch_account_status, store, acct, now): acct
            for acct in accounts
        }
        for fut in as_completed(futs):
            rows.append(fut.result())

    rows.sort(key=lambda r: natural_key(r["account"]))
    header = (
        f"{'ACCOUNT':<12} {'EMAIL':<34} {'UNITS':>8} {'RATE/HR':>8} "
        f"{'SESSIONS':>8} {'GPUS':<14} {'TPUS':<10} {'STATUS'}"
    )
    print(header)
    print("-" * len(header))
    total_units = 0.0
    total_sessions = 0
    for r in rows:
        total_units += r["balance"]
        total_sessions += r["assignments"]
        gpus = ",".join(r["gpus"]) if r["gpus"] else "-"
        tpus = ",".join(r["tpus"]) if r["tpus"] else "-"
        if r["error"]:
            status = f"ERR: {r['error'][:28]}"
        elif r["cooldown_s"] > 0:
            status = f"COOLDOWN ({r['cooldown_s'] // 60}m)"
        else:
            status = "READY"
        print(
            f"{r['account']:<12} {r['email'][:34]:<34} {r['balance']:>8.2f} "
            f"{r['rate']:>8.2f} {r['assignments']:>8d} {gpus[:14]:<14} "
            f"{tpus[:10]:<10} {status}"
        )
    print("-" * len(header))
    print(
        f"Total accounts: {len(rows)} | Total compute units: {total_units:.2f} | "
        f"Active sessions: {total_sessions}"
    )
    return 0


def cmd_mode(store: AccountStore, arg: str | None) -> int:
    state = store.load_state()
    if not arg:
        label = (
            "round-robin (B: rotate across accounts)"
            if state.mode == MODE_ROUND_ROBIN
            else "fill-first (C: use one account until refused)"
        )
        print(f"Current mode: {label}")
        return 0
    new_mode = normalize_mode(arg)
    state.mode = new_mode
    store.save_state(state)
    print(f"[9colab] Selection mode set to '{new_mode}'.")
    return 0


def cmd_sessions_all(store: AccountStore) -> int:
    accounts = store.list_accounts()
    if not accounts:
        print("[9colab] No accounts registered.")
        return 0

    def _run_one(acct: str) -> tuple[str, str]:
        colab_bin = find_colab_bin()
        env = build_account_env(store, acct)
        proc = subprocess.run(
            [colab_bin, "sessions"],
            env=env,
            capture_output=True,
            text=True,
        )
        return acct, (proc.stdout + proc.stderr).strip()

    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        results = list(pool.map(_run_one, accounts))

    any_active = False
    for acct, out in results:
        if "No active sessions" in out or not out:
            continue
        any_active = True
        for line in out.splitlines():
            print(f"[{acct}] {line}")
    if not any_active:
        print("[9colab] No active sessions across any account.")
    return 0


def cmd_whoami_all(store: AccountStore) -> int:
    accounts = store.list_accounts()
    if not accounts:
        print("[9colab] No accounts registered.")
        return 0
    for acct in accounts:
        meta = store.load_meta(acct)
        email = meta.get("email")
        if not email:
            try:
                email = fetch_email(get_valid_access_token(store, acct))
                store.save_meta(acct, {"email": email})
            except Exception:
                email = "-"
        print(f"{acct:<14} {email}")
    return 0


def run_parallel(
    store: AccountStore, target_accounts: list[str], colab_args: list[str]
) -> int:
    if not target_accounts:
        sys.stderr.write("[9colab] No target accounts found.\n")
        return 1
    sys.stderr.write(
        f"[9colab] Running in parallel across {len(target_accounts)} account(s): "
        f"{', '.join(target_accounts)}\n"
    )
    lock = threading.Lock()

    def _worker(acct: str) -> int:
        colab_bin = find_colab_bin()
        env = build_account_env(store, acct)
        proc = subprocess.Popen(
            [colab_bin, *colab_args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            with lock:
                sys.stdout.write(f"[{acct}] {line}")
                sys.stdout.flush()
        proc.wait()
        return proc.returncode

    with ThreadPoolExecutor(max_workers=len(target_accounts)) as pool:
        codes = list(pool.map(_worker, target_accounts))
    return 0 if all(c == 0 for c in codes) else 1


def handle_account_subcommand(store: AccountStore, args: list[str]) -> int:
    if not args or args[0] == "list":
        return cmd_list(store)
    sub = args[0]
    if sub == "add":
        return cmd_add(store, args[1] if len(args) >= 2 else None)
    if sub == "import":
        name = args[1] if len(args) >= 2 else None
        src = args[2] if len(args) >= 3 else None
        return cmd_import(store, name, src)
    if sub == "rm" and len(args) >= 2:
        store.remove_account(args[1])
        print(f"[9colab] Removed account '{args[1]}'.")
        return 0
    if sub == "mode":
        return cmd_mode(store, args[1] if len(args) >= 2 else None)
    if sub == "clear-cooldown":
        state = store.load_state()
        if len(args) >= 2:
            state.cooldowns.pop(args[1], None)
        else:
            state.cooldowns.clear()
        store.save_state(state)
        print("[9colab] Cooldowns cleared.")
        return 0
    sys.stderr.write(
        "Usage: 9colab account [add|import|rm|list|mode|clear-cooldown] ...\n"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    store = AccountStore()

    if raw_argv and raw_argv[0] in ("-h", "--help", "help") and len(raw_argv) == 1:
        print(HELP_BANNER)
        try:
            colab_bin = find_colab_bin()
            subprocess.run([colab_bin, "--help"])
        except Exception:
            pass
        return 0

    if not raw_argv or raw_argv[0] == "ui":
        from ninecolab.ui import run_ui_server

        ui_args = raw_argv[1:]
        port = 8765
        if "--port" in ui_args:
            port = int(ui_args[ui_args.index("--port") + 1])
        return run_ui_server(store, port=port, open_browser="--no-open" not in ui_args)

    top = raw_argv[0]
    if top == "account":
        return handle_account_subcommand(store, raw_argv[1:])
    if top == "add":
        return cmd_add(store, raw_argv[1] if len(raw_argv) >= 2 else None)
    if top == "import":
        name = raw_argv[1] if len(raw_argv) >= 2 else None
        src = raw_argv[2] if len(raw_argv) >= 3 else None
        return cmd_import(store, name, src)
    if top == "list" and len(raw_argv) == 1:
        return cmd_list(store)
    if top == "mode":
        return cmd_mode(store, raw_argv[1] if len(raw_argv) >= 2 else None)

    routed = extract_routing_flags(raw_argv)
    cmd = routed.command

    if routed.all_accounts or routed.on_accounts:
        targets = (
            store.list_accounts() if routed.all_accounts else routed.on_accounts
        )
        return run_parallel(store, targets, routed.colab_args)

    if cmd in AGGREGATE_COMMANDS and not routed.account:
        if cmd == "usage":
            return cmd_list(store)
        if cmd == "sessions":
            return cmd_sessions_all(store)
        if cmd == "whoami":
            return cmd_whoami_all(store)

    if cmd in CREATE_COMMANDS:
        if routed.account:
            code, _ = run_colab_stream(
                store, validate_account_name(routed.account), routed.colab_args
            )
            return code
        accounts = store.list_accounts()
        state = store.load_state()
        now = time.time()

        def _runner(acct: str) -> tuple[int, str]:
            sys.stderr.write(
                f"[9colab] Trying account '{acct}' (mode: {state.mode})...\n"
            )
            code, out = run_colab_stream(store, acct, routed.colab_args)
            if code != 0 and is_allocation_refused(out):
                sys.stderr.write(
                    f"[9colab] Account '{acct}' refused allocation; failing over...\n"
                )
            return code, out

        try:
            picked, code, _, next_state = run_with_failover(
                accounts, state, now, _runner
            )
            store.save_state(next_state)
            sys.stderr.write(f"[9colab] Completed on account '{picked}'.\n")
            return code
        except NoAvailableAccountError as exc:
            sys.stderr.write(f"[9colab] {exc}\n")
            return 1

    if cmd in SESSION_COMMANDS:
        try:
            owner, _ = store.resolve_session_owner(routed.session, routed.account)
        except SessionLookupError as exc:
            sys.stderr.write(f"[9colab] {exc}\n")
            return 1
        exec_colab_interactive(store, owner, routed.colab_args)
        return 0

    colab_bin = find_colab_bin()
    if routed.account:
        exec_colab_interactive(store, routed.account, routed.colab_args)
        return 0
    return subprocess.call([colab_bin, *routed.colab_args])


if __name__ == "__main__":
    raise SystemExit(main())
