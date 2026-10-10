import unittest

from ninecolab.cli import extract_routing_flags


class TestCliRouting(unittest.TestCase):
    def test_extract_routing_flags_strips_9colab_options_and_keeps_colab_args(self):
        parsed = extract_routing_flags(
            ["run", "train.py", "--gpu", "T4", "-a", "acc2", "--", "--epochs", "5"]
        )
        self.assertEqual(parsed.command, "run")
        self.assertEqual(parsed.account, "acc2")
        self.assertIsNone(parsed.session)
        self.assertEqual(
            parsed.colab_args, ["run", "train.py", "--gpu", "T4", "--", "--epochs", "5"]
        )

    def test_extract_routing_flags_reads_session_without_stripping_it(self):
        parsed = extract_routing_flags(["ssh", "-s", "dev-vm", "-a", "acc1"])
        self.assertEqual(parsed.command, "ssh")
        self.assertEqual(parsed.account, "acc1")
        self.assertEqual(parsed.session, "dev-vm")
        self.assertEqual(parsed.colab_args, ["ssh", "-s", "dev-vm"])

    def test_extract_routing_flags_supports_all_and_on(self):
        parsed = extract_routing_flags(["run", "job.py", "--all", "--gpu", "T4"])
        self.assertTrue(parsed.all_accounts)
        self.assertEqual(parsed.colab_args, ["run", "job.py", "--gpu", "T4"])

        parsed2 = extract_routing_flags(["exec", "nvidia-smi", "--on", "a1,a2"])
        self.assertEqual(parsed2.on_accounts, ["a1", "a2"])
        self.assertEqual(parsed2.colab_args, ["exec", "nvidia-smi"])


if __name__ == "__main__":
    unittest.main()
