import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ninecolab.accounts import (
    AccountStore,
    SessionLookupError,
    parse_xssi_json,
    validate_account_name,
)
from ninecolab.pick import MODE_FILL_FIRST


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / ".9colab"
        self.store = AccountStore(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_account_name(self):
        self.assertEqual(validate_account_name("pro-1_a"), "pro-1_a")
        for bad in ["", "../x", "a/b", "a b", ".hidden"]:
            with self.assertRaises(ValueError):
                validate_account_name(bad)

    def test_import_from_home_creates_0700_profile(self):
        fake_home = Path(self.tmp.name) / "src_home"
        cfg = fake_home / ".config" / "colab-cli"
        cfg.mkdir(parents=True)
        (cfg / "token.json").write_text(json.dumps({"token": "t1", "refresh_token": "r1"}))

        dest = self.store.import_from_home("acc1", fake_home)
        self.assertTrue((dest / ".config" / "colab-cli" / "token.json").is_file())
        mode = stat.S_IMODE(dest.stat().st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(self.store.list_accounts(), ["acc1"])

    def test_state_persistence(self):
        state = self.store.load_state()
        state.mode = MODE_FILL_FIRST
        state.fill_current = "acc1"
        state.cooldowns["acc2"] = 999.0
        self.store.save_state(state)

        reloaded = self.store.load_state()
        self.assertEqual(reloaded.mode, MODE_FILL_FIRST)
        self.assertEqual(reloaded.fill_current, "acc1")
        self.assertEqual(reloaded.cooldowns, {"acc2": 999.0})

    def test_parse_xssi_json(self):
        raw = ")]}'\n{\"currentBalance\": 12.5, \"eligibleGpus\": [\"T4\", \"L4\"]}"
        parsed = parse_xssi_json(raw)
        self.assertEqual(parsed["currentBalance"], 12.5)
        self.assertEqual(parsed["eligibleGpus"], ["T4", "L4"])

    def test_resolve_session_owner_auto_and_conflict(self):
        for acc in ["acc1", "acc2"]:
            p = self.store.sessions_path(acc)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
            self.store.token_path(acc).write_text("{}")
        # Single active session across all accounts -> auto-resolved when -s omitted
        self.store.sessions_path("acc2").write_text(
            json.dumps({"s-alpha": {"name": "s-alpha", "endpoint": "ep1"}})
        )
        owner, resolved_session = self.store.resolve_session_owner(None)
        self.assertEqual((owner, resolved_session), ("acc2", "s-alpha"))

        # Explicit session name lookup
        owner2, resolved2 = self.store.resolve_session_owner("s-alpha")
        self.assertEqual((owner2, resolved2), ("acc2", "s-alpha"))

        # Duplicate session name across two accounts -> error unless -a given
        self.store.sessions_path("acc1").write_text(
            json.dumps({"s-alpha": {"name": "s-alpha", "endpoint": "ep0"}})
        )
        with self.assertRaises(SessionLookupError):
            self.store.resolve_session_owner("s-alpha")

        owner3, _ = self.store.resolve_session_owner("s-alpha", explicit_account="acc1")
        self.assertEqual(owner3, "acc1")

    def test_next_account_name_auto_increments(self):
        self.assertEqual(self.store.next_account_name(), "acc1")
        p = self.store.token_path("acc1")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        self.assertEqual(self.store.next_account_name(), "acc2")


if __name__ == "__main__":
    unittest.main()
