import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ninecolab.accounts import AccountStore
from ninecolab.pick import MODE_FILL_FIRST, MODE_ROUND_ROBIN
from ninecolab.ui import DashboardService, make_pkce_pair


class TestDashboardUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / ".9colab"
        self.store = AccountStore(self.base)
        self.svc = DashboardService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_make_pkce_pair_s256(self):
        verifier, challenge = make_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotEqual(verifier, challenge)
        self.assertNotIn("=", challenge)

    def test_overview_and_mode_toggle(self):
        p = self.store.token_path("acc1")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"token": "t", "refresh_token": "r"}))
        self.store.sessions_path("acc1").write_text(
            json.dumps(
                {
                    "dev1": {
                        "name": "dev1",
                        "endpoint": "gpu-t4-s-123",
                        "variant": "GPU",
                        "accelerator": "T4",
                        "machine_shape": "STANDARD",
                    }
                }
            )
        )

        fake_status = {
            "account": "acc1",
            "email": "u1@example.com",
            "balance": 15.5,
            "rate": 1.2,
            "assignments": 1,
            "gpus": ["T4", "L4"],
            "tpus": ["V5E1"],
            "cooldown_s": 0,
            "error": None,
        }
        with patch("ninecolab.ui.fetch_account_status", return_value=fake_status):
            data = self.svc.get_overview(now=1000.0)

        self.assertEqual(data["mode"], MODE_ROUND_ROBIN)
        self.assertEqual(data["totals"]["accounts"], 1)
        self.assertEqual(data["totals"]["units"], 15.5)
        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["name"], "dev1")
        self.assertEqual(data["sessions"][0]["account"], "acc1")

        # Switch mode to C (fill-first)
        res = self.svc.set_mode("C")
        self.assertEqual(res["mode"], MODE_FILL_FIRST)
        self.assertEqual(self.store.load_state().mode, MODE_FILL_FIRST)

    def test_start_oauth_returns_google_auth_url_and_next_account(self):
        res = self.svc.start_oauth(name=None)
        self.assertEqual(res["account"], "acc1")
        self.assertIn("accounts.google.com/o/oauth2/auth", res["auth_url"])
        self.assertIn("code_challenge=", res["auth_url"])


if __name__ == "__main__":
    unittest.main()
