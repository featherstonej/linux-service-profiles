import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import service_profiles as sp


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = os.path.join(tempfile.mkdtemp(), "p.json")

    def test_create_and_delete(self):
        self.assertEqual(sp.main(["--config", self.cfg, "create", "game",
                                  "--stop", "cups"]), 0)
        self.assertEqual(sp.load_profiles(self.cfg)["game"],
                         {"start": [], "stop": ["cups"]})
        self.assertEqual(sp.main(["--config", self.cfg, "delete", "game"]), 0)
        self.assertEqual(sp.load_profiles(self.cfg), {})

    def test_overlap_rejected(self):
        self.assertEqual(sp.main(["--config", self.cfg, "create", "x",
                                  "--start", "a", "--stop", "a"]), 2)

    def test_missing_profile(self):
        self.assertEqual(sp.main(["--config", self.cfg, "show", "nope"]), 1)

    def test_apply_order_and_failures(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=1 if "b" in cmd else 0)

        failures = sp.apply_profile({"start": ["a"], "stop": ["b"]},
                                    user=True, runner=runner)
        self.assertEqual(calls[0], ["systemctl", "--user", "stop", "--", "b"])
        self.assertEqual(calls[1], ["systemctl", "--user", "start", "--", "a"])
        self.assertEqual(len(failures), 1)

    def test_dry_run_runs_nothing(self):
        def runner(cmd):
            raise AssertionError("should not run")
        self.assertEqual(sp.apply_profile({"stop": ["a"]}, dry_run=True,
                                          runner=runner), [])


if __name__ == "__main__":
    unittest.main()
