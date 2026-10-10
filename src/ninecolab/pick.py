"""Pure account selection and failover logic for 9colab."""

from dataclasses import dataclass, field
from typing import Callable

MODE_ROUND_ROBIN = "round-robin"
MODE_FILL_FIRST = "fill-first"
VALID_MODES = (MODE_ROUND_ROBIN, MODE_FILL_FIRST)

REFUSAL_MARKERS = (
    "Allocation refused (precondition failed)",
    "Backend rejected accelerator",
    "TooManyAssignmentsError",
)


class NoAvailableAccountError(RuntimeError):
    """Raised when no account can allocate a Colab runtime."""


@dataclass
class SelectorState:
    mode: str = MODE_ROUND_ROBIN
    rr_index: int = 0
    fill_current: str | None = None
    cooldowns: dict[str, float] = field(default_factory=dict)
    cooldown_seconds: int = 3600


def normalize_mode(raw: str) -> str:
    val = raw.strip().lower()
    if val in ("b", "rr", MODE_ROUND_ROBIN):
        return MODE_ROUND_ROBIN
    if val in ("c", "ff", "sequential", MODE_FILL_FIRST):
        return MODE_FILL_FIRST
    raise ValueError(f"Unknown mode '{raw}'. Use 'round-robin' (B) or 'fill-first' (C).")


def active_cooldowns(cooldowns: dict[str, float], now: float) -> dict[str, float]:
    return {acct: until for acct, until in cooldowns.items() if until > now}


def order_candidates(
    accounts: list[str], state: SelectorState, now: float
) -> list[str]:
    if not accounts:
        return []
    live_cd = active_cooldowns(state.cooldowns, now)
    eligible = [a for a in accounts if a not in live_cd]
    if not eligible:
        return []

    n = len(accounts)
    if state.mode == MODE_FILL_FIRST and state.fill_current in accounts:
        start = accounts.index(state.fill_current)
    else:
        start = state.rr_index % n

    rotated = [accounts[(start + i) % n] for i in range(n)]
    return [a for a in rotated if a not in live_cd]


def is_allocation_refused(output: str) -> bool:
    return any(marker in output for marker in REFUSAL_MARKERS)


def _advance_state_on_success(
    accounts: list[str], state: SelectorState, picked: str, cooldowns: dict[str, float]
) -> SelectorState:
    idx = accounts.index(picked)
    next_rr = (idx + 1) % len(accounts)
    return SelectorState(
        mode=state.mode,
        rr_index=next_rr,
        fill_current=picked,
        cooldowns=cooldowns,
        cooldown_seconds=state.cooldown_seconds,
    )


def run_with_failover(
    accounts: list[str],
    state: SelectorState,
    now: float,
    runner: Callable[[str], tuple[int, str]],
) -> tuple[str, int, str, SelectorState]:
    if not accounts:
        raise NoAvailableAccountError("No accounts registered. Run '9colab add <name>'.")

    cooldowns = active_cooldowns(state.cooldowns, now)
    candidates = order_candidates(accounts, state, now)
    if not candidates:
        reasons = [
            f"{a}: in cooldown for {int(cooldowns[a] - now)}s"
            for a in accounts
            if a in cooldowns
        ]
        raise NoAvailableAccountError(
            "All accounts are in cooldown:\n  " + "\n  ".join(reasons)
        )

    failures: list[str] = []
    for acct in candidates:
        code, output = runner(acct)
        if code == 0:
            next_state = _advance_state_on_success(accounts, state, acct, cooldowns)
            return acct, code, output, next_state

        if not is_allocation_refused(output):
            next_state = _advance_state_on_success(accounts, state, acct, cooldowns)
            return acct, code, output, next_state

        cooldowns = {**cooldowns, acct: now + state.cooldown_seconds}
        first_line = next(
            (ln.strip() for ln in output.splitlines() if ln.strip()), "refused"
        )
        failures.append(f"{acct}: {first_line}")

    raise NoAvailableAccountError(
        "All candidate accounts refused VM allocation:\n  " + "\n  ".join(failures)
    )
