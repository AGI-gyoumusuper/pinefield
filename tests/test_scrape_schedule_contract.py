import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def shared_job(text: str, account: int) -> str:
    match = re.search(
        rf"(?ms)^  scrape_account{account}:.*?(?=^  scrape_account\d+:|\Z)",
        text,
    )
    if not match:
        raise AssertionError(f"scrape_account{account} job not found")
    return match.group(0)


class ScrapeScheduleContractTests(unittest.TestCase):
    def test_regular_workflows_freeze_one_trigger_target_date_for_all_steps(self):
        shared = (WORKFLOWS / "scrape.yml").read_text(encoding="utf-8")
        for number in range(1, 6):
            job = shared_job(shared, number)
            command = f"python scrape_target_date.py --account account{number}"
            self.assertEqual(3, job.count(command), f"account{number}")
            self.assertIn(
                'echo "PINEFIELD_TARGET_DATE=$TARGET_DATE" >> "$GITHUB_ENV"',
                job,
            )

        for number in range(6, 21):
            text = (WORKFLOWS / f"scrape{number}.yml").read_text(encoding="utf-8")
            command = f"python scrape_target_date.py --account account{number}"
            self.assertEqual(3, text.count(command), f"account{number}")
            self.assertIn(
                'echo "PINEFIELD_TARGET_DATE=$TARGET_DATE" >> "$GITHUB_ENV"',
                text,
            )

        for number in range(1, 21):
            entrypoint = (ROOT / f"scrape_main{number}.py").read_text(encoding="utf-8")
            self.assertIn(
                f'resolve_target_date("account{number}", BASE_DIR)',
                entrypoint,
            )

    def test_insurance_is_2330_jst_and_targets_the_next_business_date(self):
        text = (WORKFLOWS / "ensure-scrape.yml").read_text(encoding="utf-8")
        self.assertEqual(21, text.count('"30 14 * * *"'))
        self.assertNotIn('"0 16 * * *"', text)
        self.assertIn("TARGET_DATE=$(date -u -d '+1 day' +%F)", text)
        self.assertIn("max-parallel: 1", text)
        self.assertIn("needs: prepare", text)
        self.assertIn("--validate-only", text)
        self.assertIn("steps.precheck.outputs.needs_repair == 'true'", text)
        self.assertIn('--date "${{ needs.prepare.outputs.target_date }}"', text)
        self.assertIn('DATE="${{ needs.prepare.outputs.target_date }}"', text)

    def test_late_repair_is_midnight_jst_and_keeps_one_target_date(self):
        text = (WORKFLOWS / "late-repair-scrape.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "0 15 * * *"', text)
        self.assertNotIn('cron: "0 17 * * *"', text)
        self.assertIn("TARGET_DATE=$(TZ=Asia/Tokyo date +%F)", text)
        self.assertIn('--date "${{ steps.target.outputs.date }}"', text)
        self.assertEqual(2, text.count('DATE="${{ steps.target.outputs.date }}"'))

    def test_insurance_wait_window_covers_the_full_evening_wave(self):
        text = (ROOT / "wait_for_regular_scrape.py").read_text(encoding="utf-8")
        self.assertIn("timedelta(hours=5)", text)


if __name__ == "__main__":
    unittest.main()
