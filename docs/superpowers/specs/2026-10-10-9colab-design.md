# 9colab design

Date: 2026-10-10
Status: draft, waits for user review

## Goal

9colab is a CLI for one user. It controls many Google Colab accounts from one place. It logs in many accounts. It picks an account for each new job. It runs jobs on one or many accounts at the same time. It manages live sessions across accounts.

## Constraints

- Wrap the installed `colab` CLI (google-colab-cli 0.7.4). Do not patch it.
- Use the Python standard library only. Use `argparse` and `concurrent.futures`.
- The `colab` CLI hardcodes the token path `~/.config/colab-cli/token.json` with `expanduser("~")`. The `HOME` variable changes this path.
- Credentials never enter the repo.

## Account isolation

- Each account has a profile directory: `~/.9colab/accounts/<name>/`. Mode `0700`.
- 9colab calls `colab` as a subprocess with `HOME=<profile dir>`.
- Token, `sessions.json`, history, settings, and log of each account stay separate.
- `9colab add <name>` runs `colab --auth oauth2 usage` with the profile `HOME`. The user pastes the code from the printed URL. The `account` field in `token.json` is empty, so 9colab gets the email from `https://openidconnect.googleapis.com/v1/userinfo` with the stored access token (refresh it first if it is expired). The test returned HTTP 200 with an `email` field.
- Each `add` makes a new URL. The user must paste the code of the latest URL only.

## Commands

9colab keeps all commands and flags of `google-colab-cli`. It groups them into 5 rules:

| Group | Commands | Action |
|---|---|---|
| Account control | `account add\|rm\|list\|mode` (alias: `add`, `list`, `mode`) | Manage profiles and selection mode. `rm` stays under `account rm` so `9colab rm` still deletes a remote file on a VM. |
| Create a VM | `new`, `run` | Pick an account (or use `-a <account>`), create the VM, and fail over on HTTP 412. `run` and `exec` also accept `--all` and `--on a,b` to run in parallel across accounts. |
| Multi-account view | `sessions`, `usage`, `whoami` | Show a combined table across all accounts. With `-a <account>`, run `colab` for that account only. |
| Session commands | `auth`, `console`, `download`, `drivemount`, `edit`, `exec`, `install`, `log`, `ls`, `repl`, `restart-kernel`, `rm`, `ssh`, `status`, `stop`, `upload`, `url` | Find the account that owns `-s <session>` (or the only active session across all accounts) and replace the process (`os.execvp`) with `colab` so TTY commands (`ssh`, `repl`, `console`, `edit`) keep full terminal control. |
| Global commands | `pay`, `readme`, `skill`, `update`, `version`, `help`, and any new future `colab` command | Forward all arguments to `colab`. |

Session lookup scans each profile `sessions.json`. 9colab keeps no copy of session state. If two accounts have the same session name, or if `-s` is omitted when more than one session is active, the command fails and asks for `-s` or `-a`.

## Account selection

State file: `~/.9colab/state.json`. It holds the mode, the round-robin pointer, the fill-first current account, and the cooldown times.

- `round-robin` (default): take the next account after the pointer. Skip accounts in cooldown.
- `fill-first`: stay on the current account until it is refused. Then move to the next account.

Both modes use the same failover loop:

1. Pick a candidate account.
2. Try to create the VM.
3. On success, return.
4. On refusal, put the account in cooldown and go to step 1 with the next candidate.
5. If no candidate is left, fail with one reason line for each account.

A refusal is the `colab` message `Allocation refused (precondition failed)` (HTTP 412). Default cooldown is 60 minutes. The user can change it in `state.json`.

`currentBalance` is not a selection filter. In `colab.log`, the user account had `currentBalance: 0` and still got a T4 VM. The `ccu-info` response also has `eligibleGpus`. `colab usage` does not show it. `9colab list` shows `eligibleGpus` to help the user.

## Layout

```
9colab/
  pyproject.toml        entry point: 9colab
  README.md
  .gitignore
  src/ninecolab/
    accounts.py         profiles, login, run colab with HOME
    pick.py             selection modes and failover (pure functions)
    cli.py              argparse and command wiring
  tests/test_pick.py    no network
  docs/superpowers/specs/
```

Install: `uv tool install .`

## Testing

- `tests/test_pick.py`: round-robin order, fill-first stickiness, cooldown skip, all-refused error. The test uses a fake `try_create` function. No network.
- Smoke test with real accounts: `add` two accounts, run `list`, run `run` with a small script, run `sessions`, run `stop`. Use only accounts the user logs in.

## Out of scope

- A web UI.
- A daemon or scheduler.
- Billing or buying units.
- Automatic Google login without the user (OAuth needs the user to paste a code).

## Risks

- A future `colab` version can change the token path. Then `HOME` isolation fails. Mitigation: `add` checks that `token.json` exists in the profile after login, and fails if not.
- Free or Pro quota rules are set by Google. Cooldown is a guess. The user can tune it.
