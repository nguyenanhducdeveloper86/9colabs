import unittest

from ninecolab.pick import (
    MODE_FILL_FIRST,
    MODE_ROUND_ROBIN,
    NoAvailableAccountError,
    SelectorState,
    is_allocation_refused,
    order_candidates,
    run_with_failover,
)


class TestPick(unittest.TestCase):
    def test_round_robin_advances_pointer(self):
        state = SelectorState(mode=MODE_ROUND_ROBIN, rr_index=0)
        acct1, code1, _, state = run_with_failover(
            ["a1", "a2", "a3"], state, now=100.0, runner=lambda a: (0, f"ok:{a}")
        )
        self.assertEqual(acct1, "a1")
        self.assertEqual(code1, 0)
        self.assertEqual(state.rr_index, 1)

        acct2, _, _, state = run_with_failover(
            ["a1", "a2", "a3"], state, now=101.0, runner=lambda a: (0, f"ok:{a}")
        )
        self.assertEqual(acct2, "a2")
        self.assertEqual(state.rr_index, 2)

    def test_fill_first_sticks_until_refused(self):
        state = SelectorState(mode=MODE_FILL_FIRST, fill_current="a1")
        acct1, _, _, state = run_with_failover(
            ["a1", "a2"], state, now=100.0, runner=lambda a: (0, "ok")
        )
        self.assertEqual(acct1, "a1")
        self.assertEqual(state.fill_current, "a1")

        acct2, _, _, state = run_with_failover(
            ["a1", "a2"], state, now=105.0, runner=lambda a: (0, "ok")
        )
        self.assertEqual(acct2, "a1")

    def test_failover_on_http_412_sets_cooldown_and_picks_next(self):
        state = SelectorState(mode=MODE_FILL_FIRST, cooldown_seconds=3600)

        def runner(acct: str) -> tuple[int, str]:
            if acct == "a1":
                return (
                    1,
                    "[colab] Allocation refused (precondition failed). Run colab stop",
                )
            return (0, "created")

        picked, code, out, next_state = run_with_failover(
            ["a1", "a2"], state, now=1000.0, runner=runner
        )
        self.assertEqual(picked, "a2")
        self.assertEqual(code, 0)
        self.assertEqual(out, "created")
        self.assertEqual(next_state.fill_current, "a2")
        self.assertEqual(next_state.cooldowns.get("a1"), 4600.0)

    def test_expired_cooldown_becomes_eligible_again(self):
        state = SelectorState(
            mode=MODE_ROUND_ROBIN,
            rr_index=0,
            cooldowns={"a1": 500.0, "a2": 2000.0},
        )
        active = order_candidates(["a1", "a2", "a3"], state, now=1000.0)
        self.assertEqual(active, ["a1", "a3"])

    def test_user_script_error_does_not_failover(self):
        state = SelectorState(mode=MODE_ROUND_ROBIN, rr_index=0)
        calls: list[str] = []

        def runner(acct: str) -> tuple[int, str]:
            calls.append(acct)
            return (1, "Traceback (most recent call last):\nZeroDivisionError")

        picked, code, _, next_state = run_with_failover(
            ["a1", "a2"], state, now=100.0, runner=runner
        )
        self.assertEqual(calls, ["a1"])
        self.assertEqual(picked, "a1")
        self.assertEqual(code, 1)
        self.assertNotIn("a1", next_state.cooldowns)

    def test_all_refused_raises_with_reasons(self):
        state = SelectorState(mode=MODE_ROUND_ROBIN)

        def runner(acct: str) -> tuple[int, str]:
            return (1, f"[colab] Backend rejected accelerator 'L4' on {acct}")

        with self.assertRaises(NoAvailableAccountError) as ctx:
            run_with_failover(["a1", "a2"], state, now=50.0, runner=runner)
        self.assertIn("a1", str(ctx.exception))
        self.assertIn("a2", str(ctx.exception))

    def test_is_allocation_refused_matches_known_colab_errors(self):
        self.assertTrue(
            is_allocation_refused("[colab] Allocation refused (precondition failed).")
        )
        self.assertTrue(
            is_allocation_refused("[colab] Backend rejected accelerator 'A100'.")
        )
        self.assertFalse(is_allocation_refused("SyntaxError: invalid syntax"))


if __name__ == "__main__":
    unittest.main()
