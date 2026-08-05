import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import ensure_daily_scrape


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATE = "2026-08-06"


def write_config(
    root: Path,
    account_number: int,
    category_count: int,
    *,
    max_total_items: int = 10,
    max_per_category: int = 1,
    inactive_categories: int = 0,
) -> None:
    categories = [
        {
            "name": f"category-{index}",
            "url": f"https://www.amazon.co.jp/s?i={index}",
        }
        for index in range(1, category_count + 1)
    ]
    categories.extend(
        {"name": f"inactive-{index}", "url": ""}
        for index in range(1, inactive_categories + 1)
    )
    config = {
        "categories": categories,
        "filters": {
            "max_total_items": max_total_items,
            "max_per_category": max_per_category,
        },
    }
    (root / f"categories{account_number}.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_products(root: Path, account_number: int, count: int) -> Path:
    account = f"account{account_number}"
    output_path = root / "data" / account / f"products_{TEST_DATE}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([{"asin": f"B{index:09d}"} for index in range(count)]),
        encoding="utf-8",
    )
    return output_path


class ExpectedItemCountTests(unittest.TestCase):
    def test_live_account_expectations_come_from_category_capacity(self):
        expected = {
            1: 10,
            2: 10,
            3: 10,
            4: 10,
            5: 10,
            6: 7,
            7: 10,
            8: 10,
            9: 10,
            10: 10,
        }
        for account_number, expected_count in expected.items():
            with self.subTest(account=account_number):
                self.assertEqual(
                    expected_count,
                    ensure_daily_scrape.expected_item_count(
                        REPO_ROOT / f"categories{account_number}.yaml"
                    ),
                )

    def test_capacity_uses_only_active_categories_and_both_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_config(
                root,
                1,
                3,
                max_total_items=10,
                max_per_category=2,
                inactive_categories=2,
            )
            self.assertEqual(
                6,
                ensure_daily_scrape.expected_item_count(root / "categories1.yaml"),
            )

            write_config(root, 1, 3, max_total_items=4, max_per_category=2)
            self.assertEqual(
                4,
                ensure_daily_scrape.expected_item_count(root / "categories1.yaml"),
            )

    def test_nonpositive_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_config(root, 1, 3, max_total_items=0, max_per_category=1)
            with self.assertRaisesRegex(ValueError, "max_total_items must be positive"):
                ensure_daily_scrape.expected_item_count(root / "categories1.yaml")

            write_config(root, 1, 3, max_total_items=10, max_per_category=0)
            with self.assertRaisesRegex(ValueError, "max_per_category must be positive"):
                ensure_daily_scrape.expected_item_count(root / "categories1.yaml")


class OutputValidationTests(unittest.TestCase):
    def assert_gate(self, account_number: int, expected_count: int) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_config(root, account_number, expected_count)
            write_products(root, account_number, expected_count)
            ok, message = ensure_daily_scrape.validate(
                f"account{account_number}", root=root, today=TEST_DATE
            )
            self.assertTrue(ok, message)

            write_products(root, account_number, expected_count - 1)
            ok, message = ensure_daily_scrape.validate(
                f"account{account_number}", root=root, today=TEST_DATE
            )
            self.assertFalse(ok)
            self.assertIn(f"{expected_count - 1} < {expected_count}", message)

    def test_account6_requires_seven_items(self):
        self.assert_gate(6, 7)

    def test_account7_requires_ten_items(self):
        self.assert_gate(7, 10)

    def test_missing_invalid_and_non_list_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "products.json"

            ok, message = ensure_daily_scrape.valid_product_list(output_path, 10)
            self.assertFalse(ok)
            self.assertIn("missing:", message)

            output_path.write_text("{", encoding="utf-8")
            ok, message = ensure_daily_scrape.valid_product_list(output_path, 10)
            self.assertFalse(ok)
            self.assertIn("invalid json:", message)

            output_path.write_text("{}", encoding="utf-8")
            ok, message = ensure_daily_scrape.valid_product_list(output_path, 10)
            self.assertFalse(ok)
            self.assertIn("not list:", message)

    def test_validate_only_never_calls_repair_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_config(root, 7, 17)
            write_products(root, 7, 10)
            argv = [
                "ensure_daily_scrape.py",
                "--validate-only",
                "--account",
                "account7",
            ]
            with (
                mock.patch.object(ensure_daily_scrape, "ROOT", root),
                mock.patch.object(ensure_daily_scrape, "TODAY", TEST_DATE),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(ensure_daily_scrape, "ensure") as repair,
            ):
                self.assertEqual(0, ensure_daily_scrape.main())
                repair.assert_not_called()

            write_products(root, 7, 9)
            with (
                mock.patch.object(ensure_daily_scrape, "ROOT", root),
                mock.patch.object(ensure_daily_scrape, "TODAY", TEST_DATE),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(ensure_daily_scrape, "ensure") as repair,
            ):
                self.assertEqual(1, ensure_daily_scrape.main())
                repair.assert_not_called()


class WorkflowValidationTests(unittest.TestCase):
    def test_active_regular_workflows_call_shared_validator(self):
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
                self.assertIn(
                    f"python ensure_daily_scrape.py --validate-only --account account{account_number}",
                    job.group(0),
                )
                self.assertNotIn("count >= 5", job.group(0))

        for account_number in range(6, 11):
            with self.subTest(account=account_number):
                workflow = (
                    REPO_ROOT / ".github" / "workflows" / f"scrape{account_number}.yml"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    f"python ensure_daily_scrape.py --validate-only --account account{account_number}",
                    workflow,
                )
                self.assertNotIn("count >= 5", workflow)


if __name__ == "__main__":
    unittest.main()
