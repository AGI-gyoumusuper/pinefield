import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ensure_daily_scrape


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATE = "2026-08-06"


def write_products(root: Path, account: str, count: int) -> Path:
    output_path = root / "data" / account / f"products_{TEST_DATE}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([{"asin": f"B{index:09d}"} for index in range(count)]),
        encoding="utf-8",
    )
    return output_path


class OutputValidationTests(unittest.TestCase):
    def validate_at(self, root: Path, account: str) -> tuple[bool, str]:
        with (
            mock.patch.object(ensure_daily_scrape, "ROOT", root),
            mock.patch.object(ensure_daily_scrape, "TODAY", TEST_DATE),
        ):
            return ensure_daily_scrape.validate(account)

    def test_account7_requires_ten_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_products(root, "account7", 10)
            ok, message = self.validate_at(root, "account7")
            self.assertTrue(ok, message)

            write_products(root, "account7", 9)
            ok, message = self.validate_at(root, "account7")
            self.assertFalse(ok)
            self.assertIn("9 < 10", message)

    def test_other_accounts_keep_the_five_item_minimum(self):
        for account_number in (*range(1, 7), *range(8, 11)):
            account = f"account{account_number}"
            with self.subTest(account=account), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_products(root, account, 5)
                ok, message = self.validate_at(root, account)
                self.assertTrue(ok, message)

                write_products(root, account, 4)
                ok, message = self.validate_at(root, account)
                self.assertFalse(ok)
                self.assertIn("4 < 5", message)

    def test_missing_invalid_and_non_list_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "products.json"

            ok, message = ensure_daily_scrape.valid_product_list(output_path)
            self.assertFalse(ok)
            self.assertIn("missing:", message)

            output_path.write_text("{", encoding="utf-8")
            ok, message = ensure_daily_scrape.valid_product_list(output_path)
            self.assertFalse(ok)
            self.assertIn("invalid json:", message)

            output_path.write_text("{}", encoding="utf-8")
            ok, message = ensure_daily_scrape.valid_product_list(output_path)
            self.assertFalse(ok)
            self.assertIn("not list:", message)

    def test_validate_only_checks_account7_without_repairing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv = [
                "ensure_daily_scrape.py",
                "--validate-only",
                "--account",
                "account7",
            ]
            write_products(root, "account7", 10)
            with (
                mock.patch.object(ensure_daily_scrape, "ROOT", root),
                mock.patch.object(ensure_daily_scrape, "TODAY", TEST_DATE),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(ensure_daily_scrape, "ensure") as repair,
            ):
                self.assertEqual(0, ensure_daily_scrape.main())
                repair.assert_not_called()

            write_products(root, "account7", 9)
            with (
                mock.patch.object(ensure_daily_scrape, "ROOT", root),
                mock.patch.object(ensure_daily_scrape, "TODAY", TEST_DATE),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(ensure_daily_scrape, "ensure") as repair,
            ):
                self.assertEqual(1, ensure_daily_scrape.main())
                repair.assert_not_called()


class WorkflowValidationTests(unittest.TestCase):
    def test_only_account7_uses_validate_only(self):
        account7_workflow = (
            REPO_ROOT / ".github" / "workflows" / "scrape7.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "python ensure_daily_scrape.py --validate-only --account account7",
            account7_workflow,
        )
        self.assertNotIn("count >= 5", account7_workflow)

        shared_workflow = (REPO_ROOT / ".github" / "workflows" / "scrape.yml").read_text(
            encoding="utf-8"
        )
        for account_number in range(1, 6):
            with self.subTest(account=account_number):
                job = re.search(
                    rf"(?ms)^  scrape_account{account_number}:.*?(?=^  scrape_account\d+:|\Z)",
                    shared_workflow,
                )
                self.assertIsNotNone(job)
                self.assertIn("count >= 5", job.group(0))
                self.assertNotIn("--validate-only", job.group(0))

        for account_number in (6, 8, 9, 10):
            with self.subTest(account=account_number):
                workflow = (
                    REPO_ROOT / ".github" / "workflows" / f"scrape{account_number}.yml"
                ).read_text(encoding="utf-8")
                self.assertIn("count >= 5", workflow)
                self.assertNotIn("--validate-only", workflow)


if __name__ == "__main__":
    unittest.main()
