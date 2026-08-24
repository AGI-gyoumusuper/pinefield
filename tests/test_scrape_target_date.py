import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ensure_daily_scrape as daily  # noqa: E402
from scrape_target_date import resolve_target_date  # noqa: E402


NOW = datetime(2026, 8, 24, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


class ScrapeTargetDateTests(unittest.TestCase):
    def test_local_and_manual_runs_default_to_jst_today(self):
        self.assertEqual(
            "2026-08-24",
            resolve_target_date("account1", environment={}, now=NOW),
        )
        self.assertEqual(
            "2026-08-24",
            resolve_target_date(
                "account1",
                environment={"GITHUB_EVENT_NAME": "workflow_dispatch"},
                now=NOW,
            ),
        )

    def test_explicit_target_date_has_priority(self):
        self.assertEqual(
            "2026-08-25",
            resolve_target_date(
                "account2",
                environment={
                    "GITHUB_EVENT_NAME": "push",
                    "PINEFIELD_TARGET_DATE": "2026-08-25",
                },
                now=NOW,
            ),
        )

    def test_push_reads_matching_today_or_tomorrow_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trigger_path = root / ".github" / "triggers" / "account3.json"
            trigger_path.parent.mkdir(parents=True)
            trigger_path.write_text(
                json.dumps(
                    {
                        "account": "account3",
                        "target_date": "2026-08-25",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                "2026-08-25",
                resolve_target_date(
                    "account3",
                    root,
                    {"GITHUB_EVENT_NAME": "push"},
                    NOW,
                ),
            )

            trigger_path.write_text(
                json.dumps(
                    {
                        "account": "account3",
                        "target_date": "2026-08-26",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "today or tomorrow"):
                resolve_target_date(
                    "account3",
                    root,
                    {"GITHUB_EVENT_NAME": "push"},
                    NOW,
                )

    def test_push_rejects_missing_or_mismatched_trigger_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "could not read target date"):
                resolve_target_date(
                    "account4",
                    root,
                    {"GITHUB_EVENT_NAME": "push"},
                    NOW,
                )

            trigger_path = root / ".github" / "triggers" / "account4.json"
            trigger_path.parent.mkdir(parents=True)
            trigger_path.write_text(
                json.dumps(
                    {
                        "account": "account5",
                        "target_date": "2026-08-25",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "account mismatch"):
                resolve_target_date(
                    "account4",
                    root,
                    {"GITHUB_EVENT_NAME": "push"},
                    NOW,
                )

    def test_repair_passes_requested_date_to_scrape_entrypoint(self):
        captured: dict[str, object] = {}

        def fake_run(command, root, environment):
            captured["command"] = command
            captured["root"] = root
            captured["environment"] = environment

        with patch.object(daily, "run", side_effect=fake_run):
            daily.scrape("account5", Path("."), "2026-08-25")

        self.assertEqual(
            [sys.executable, "scrape_main5.py"],
            captured["command"],
        )
        self.assertEqual(
            "2026-08-25",
            captured["environment"]["PINEFIELD_TARGET_DATE"],
        )
        self.assertEqual(
            os.environ.get("GITHUB_EVENT_NAME"),
            captured["environment"].get("GITHUB_EVENT_NAME"),
        )


if __name__ == "__main__":
    unittest.main()
