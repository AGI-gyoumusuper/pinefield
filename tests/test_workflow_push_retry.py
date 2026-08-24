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


class WorkflowPushRetryTests(unittest.TestCase):
    def assert_fails_after_third_push(self, script: str, label: str) -> None:
        required_in_order = (
            "push_succeeded=false",
            "for i in 1 2 3; do",
            "if git push; then",
            "push_succeeded=true",
            'if [ "$i" -lt 3 ]; then',
            'if [ "$push_succeeded" != "true" ]; then',
            'echo "git push failed after 3 attempts"',
            "exit 1",
        )
        position = -1
        for marker in required_in_order:
            next_position = script.find(marker, position + 1)
            self.assertNotEqual(-1, next_position, f"{label}: missing {marker}")
            position = next_position
        self.assertNotIn("git push && break", script, label)

    def test_shared_account1_through_account5_jobs_fail_closed(self):
        text = (WORKFLOWS / "scrape.yml").read_text(encoding="utf-8")
        for account in range(1, 6):
            self.assert_fails_after_third_push(
                shared_job(text, account),
                f"scrape.yml account{account}",
            )

    def test_dedicated_account6_through_account20_workflows_fail_closed(self):
        for account in range(6, 21):
            path = WORKFLOWS / f"scrape{account}.yml"
            self.assert_fails_after_third_push(
                path.read_text(encoding="utf-8"),
                path.name,
            )

    def test_repair_workflows_fail_closed(self):
        for name in ("ensure-scrape.yml", "late-repair-scrape.yml"):
            path = WORKFLOWS / name
            self.assert_fails_after_third_push(
                path.read_text(encoding="utf-8"),
                name,
            )

    def test_late_repair_isolates_failures_and_audits_origin_main(self):
        text = (WORKFLOWS / "late-repair-scrape.yml").read_text(encoding="utf-8")
        required_in_order = (
            "continue-on-error: true",
            '--date "${{ steps.target.outputs.date }}"',
            '--report "$REPAIR_REPORT"',
            "Commit successful repairs once",
            'for ACCOUNT in "${REPAIRED_ACCOUNTS[@]}"; do',
            "--validate-only",
            'VALID_REPAIRED_ACCOUNTS+=("$ACCOUNT")',
            'for ACCOUNT in "${VALID_REPAIRED_ACCOUNTS[@]}"; do',
            "git diff --cached --quiet --diff-filter=D",
            'git commit -m "Auto-late-repair daily scrape outputs ${DATE}"',
            "git stash push --include-untracked",
            "Audit all origin main outputs",
            "git fetch origin main",
            'git worktree add --detach "$AUDIT_ROOT" origin/main',
            "--validate-only",
            'exit "$AUDIT_RC"',
        )
        position = -1
        for marker in required_in_order:
            next_position = text.find(marker, position + 1)
            self.assertNotEqual(-1, next_position, f"missing {marker}")
            position = next_position

        self.assertEqual(
            text.count('git commit -m "Auto-late-repair daily scrape outputs ${DATE}"'),
            1,
        )
        self.assertGreaterEqual(text.count("!cancelled()"), 2)
        self.assertNotIn(
            "for ACCOUNT in account1 account2 account3 account4",
            text,
        )


if __name__ == "__main__":
    unittest.main()
