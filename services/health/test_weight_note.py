#!/usr/bin/env python3
"""weight_note.py: every row carries an offset-aware ts; refusals write nothing."""
import json, os, subprocess, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path

CLI = Path(__file__).resolve().parent / "weight_note.py"


class T(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = Path(self.dir.name) / "w.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, WEIGHT_LOG=str(self.log), AGENT_ID="coach")
        return subprocess.run([sys.executable, str(CLI), *args], env=env,
                              capture_output=True, text=True)

    def rows(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_row_has_offset_aware_ts_first(self):
        p = self.run_cli("--kg", "73.4", "--time", "morning")
        self.assertEqual(p.returncode, 0, p.stderr)
        (r,) = self.rows()
        self.assertEqual(next(iter(r)), "ts")
        ts = datetime.fromisoformat(r["ts"])
        self.assertIsNotNone(ts.utcoffset())
        self.assertLess(abs((datetime.now().astimezone() - ts).total_seconds()), 60)
        self.assertEqual((r["kg"], r["time"], r["scale"], r["source"], r["agent"], r["approx"]),
                         (73.4, "morning", "salter", "operator", "coach", False))
        self.assertEqual(r["date"], ts.date().isoformat())
        self.assertNotIn("reference", r)
        self.assertEqual(json.loads(p.stdout), r)

    def test_tanita_is_reference(self):
        self.run_cli("--kg", "75.4", "--time", "evening", "--scale", "tanita", "--note", "pair")
        (r,) = self.rows()
        self.assertIs(r["reference"], True)
        self.assertEqual((r["scale"], r["note"]), ("tanita", "pair"))

    def test_unknown_time_is_null_and_date_is_kept(self):
        self.run_cli("--kg", "71", "--time", "unknown", "--date", "2026-08-23", "--approx")
        (r,) = self.rows()
        self.assertIsNone(r["time"])
        self.assertEqual((r["date"], r["approx"]), ("2026-08-23", True))

    def test_appends(self):
        self.run_cli("--kg", "73.4", "--time", "morning")
        self.run_cli("--kg", "74.4", "--time", "evening")
        self.assertEqual([r["kg"] for r in self.rows()], [73.4, 74.4])

    def test_refusals_write_nothing(self):
        for args in (["--kg", "73.4"],                                        # no --time
                     ["--kg", "73.4", "--time", "noon"],
                     ["--kg", "73.4", "--time", "morning", "--date", "25 Nov"],
                     ["--kg", "734", "--time", "morning"],
                     ["--kg", "nan", "--time", "morning"],
                     ["--kg", "heavy", "--time", "morning"],
                     ["--kg", "73.4", "--time", "morning", "--ts", "2026-01-01T00:00:00+00:00"],
                     ["--time", "morning"]):
            p = self.run_cli(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertFalse(self.log.exists(), args)


if __name__ == "__main__":
    unittest.main()
