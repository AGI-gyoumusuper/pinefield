import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import ensure_daily_scrape as daily  # noqa: E402


TEST_DATE = "2026-08-24"


def make_product(account: str, index: int) -> dict:
    asin = f"B{index:09d}"
    number = account.removeprefix("account")
    return {
        "asin": asin,
        "title": f"Product {index}",
        "price": "1,000円",
        "price_int": 1000,
        "original_price": "",
        "discount_rate": "",
        "image_url": "https://example.com/image.jpg",
        "affiliate_url": f"https://www.amazon.co.jp/dp/{asin}?tag=noteamazon{number}-22",
        "category": "test#1",
        "rating": "",
        "review_count": "",
        "description": "",
        "specs": "",
    }


def write_valid_output(root: Path, account: str, count: int = 5) -> None:
    account_root = root / "data" / account
    account_root.mkdir(parents=True, exist_ok=True)
    products = [make_product(account, index) for index in range(1, count + 1)]
    (account_root / f"products_{TEST_DATE}.json").write_text(
        json.dumps(products, ensure_ascii=False),
        encoding="utf-8",
    )
    (account_root / f"scrape_summary_{TEST_DATE}.json").write_text(
        json.dumps(
            {"date": TEST_DATE, "total_taken": count, "categories": {"test": {}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    history_path = account_root / "asin_history.json"
    if not history_path.exists():
        history_path.write_text(
            json.dumps(
                {
                    "schema": "note-amazon-asin-history-v1",
                    "updated_at": "",
                    "description": "test history",
                    "posted": [],
                }
            ),
            encoding="utf-8",
        )


class DailyScrapeValidationTests(unittest.TestCase):
    def test_common_floor_and_account20_floor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_valid_output(root, "account1", 5)
            write_valid_output(root, "account20", 9)

            self.assertTrue(daily.validate("account1", root, TEST_DATE)[0])
            ok, message = daily.validate("account20", root, TEST_DATE)
            self.assertFalse(ok)
            self.assertIn("9 < 10", message)

    def test_required_fields_affiliate_tag_and_duplicate_asin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_valid_output(root, "account2", 5)
            products_path = root / "data" / "account2" / f"products_{TEST_DATE}.json"
            products = json.loads(products_path.read_text(encoding="utf-8"))

            del products[0]["specs"]
            products_path.write_text(json.dumps(products), encoding="utf-8")
            self.assertIn("missing fields", daily.validate("account2", root, TEST_DATE)[1])

            products[0] = make_product("account2", 1)
            products[0]["affiliate_url"] = products[0]["affiliate_url"].replace(
                "noteamazon2-22", "noteamazon1-22"
            )
            products_path.write_text(json.dumps(products), encoding="utf-8")
            self.assertIn("affiliate URL mismatch", daily.validate("account2", root, TEST_DATE)[1])

            products[0] = make_product("account2", 2)
            products_path.write_text(json.dumps(products), encoding="utf-8")
            self.assertIn("duplicate ASINs", daily.validate("account2", root, TEST_DATE)[1])

    def test_usable_product_values_and_history_schema_are_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_valid_output(root, "account2", 5)
            products_path = root / "data" / "account2" / f"products_{TEST_DATE}.json"
            products = json.loads(products_path.read_text(encoding="utf-8"))

            products[0]["price_int"] = 0
            products_path.write_text(json.dumps(products), encoding="utf-8")
            self.assertIn("invalid price", daily.validate("account2", root, TEST_DATE)[1])

            products[0] = make_product("account2", 1)
            products[0]["image_url"] = ""
            products_path.write_text(json.dumps(products), encoding="utf-8")
            self.assertIn("invalid image URL", daily.validate("account2", root, TEST_DATE)[1])

            products[0] = make_product("account2", 1)
            products_path.write_text(json.dumps(products), encoding="utf-8")
            history_path = root / "data" / "account2" / "asin_history.json"
            history_path.write_text(json.dumps({"schema": "wrong", "posted": []}), encoding="utf-8")
            self.assertIn("history schema mismatch", daily.validate("account2", root, TEST_DATE)[1])

    def test_summary_date_and_count_must_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_valid_output(root, "account3", 5)
            summary_path = root / "data" / "account3" / f"scrape_summary_{TEST_DATE}.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

            summary["date"] = "2026-08-23"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            self.assertIn("summary date mismatch", daily.validate("account3", root, TEST_DATE)[1])

            summary["date"] = TEST_DATE
            summary["total_taken"] = 4
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            self.assertIn("summary count mismatch", daily.validate("account3", root, TEST_DATE)[1])


class DailyScrapeRepairTests(unittest.TestCase):
    def test_failed_repair_restores_products_summary_and_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account_root = root / "data" / "account4"
            account_root.mkdir(parents=True)
            paths = daily.account_artifact_paths("account4", root, TEST_DATE)
            original = {
                paths[0]: b"original invalid products",
                paths[1]: b"original summary",
                paths[2]: b"original history",
            }
            for path, content in original.items():
                path.write_bytes(content)

            calls = []

            def failing_scrape(account: str, scrape_root: Path, target_date: str) -> None:
                self.assertEqual(TEST_DATE, target_date)
                calls.append(account)
                for path in paths:
                    path.write_bytes(f"changed-{len(calls)}".encode())
                raise subprocess.CalledProcessError(1, ["scrape"])

            with patch.object(daily, "scrape", side_effect=failing_scrape), patch.object(
                daily.time, "sleep", return_value=None
            ):
                with self.assertRaisesRegex(RuntimeError, "failed to create valid output"):
                    daily.ensure("account4", root, TEST_DATE)

            self.assertEqual(calls, ["account4", "account4", "account4"])
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)

    def test_nonzero_exit_cannot_reuse_a_stale_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account_root = root / "data" / "account6"
            account_root.mkdir(parents=True)
            product_path, summary_path, history_path = daily.account_artifact_paths(
                "account6", root, TEST_DATE
            )
            product_path.write_text("[]", encoding="utf-8")
            summary_path.write_text(
                json.dumps(
                    {
                        "date": TEST_DATE,
                        "total_taken": 5,
                        "categories": {"stale": {}},
                    }
                ),
                encoding="utf-8",
            )
            history_path.write_bytes(b"original-history")
            original = {path: path.read_bytes() for path in (product_path, summary_path, history_path)}
            calls = []

            def partial_scrape(account: str, scrape_root: Path, target_date: str) -> None:
                self.assertEqual(TEST_DATE, target_date)
                calls.append(account)
                products = [make_product(account, index) for index in range(1, 6)]
                product_path.write_text(json.dumps(products), encoding="utf-8")
                raise subprocess.CalledProcessError(1, ["scrape"])

            with patch.object(daily, "scrape", side_effect=partial_scrape), patch.object(
                daily.time, "sleep", return_value=None
            ):
                with self.assertRaisesRegex(RuntimeError, "failed to create valid output"):
                    daily.ensure("account6", root, TEST_DATE)

            self.assertEqual(calls, ["account6", "account6", "account6"])
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)

    def test_second_attempt_succeeds_without_first_attempt_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_path = root / "data" / "account5" / "asin_history.json"
            history_path.parent.mkdir(parents=True)
            original_history = json.dumps(
                {
                    "schema": "note-amazon-asin-history-v1",
                    "updated_at": "",
                    "description": "original history",
                    "posted": [],
                }
            ).encode()
            history_path.write_bytes(original_history)
            calls = []

            def retrying_scrape(account: str, scrape_root: Path, target_date: str) -> None:
                self.assertEqual(TEST_DATE, target_date)
                calls.append(account)
                if len(calls) == 1:
                    product_path, summary_path, current_history = daily.account_artifact_paths(
                        account, scrape_root, TEST_DATE
                    )
                    product_path.write_text("[]", encoding="utf-8")
                    summary_path.write_bytes(b"first-attempt-summary")
                    current_history.write_bytes(b"first-attempt-history")
                    return
                self.assertEqual(history_path.read_bytes(), original_history)
                write_valid_output(scrape_root, account, 5)
                successful_history = json.dumps(
                    {
                        "schema": "note-amazon-asin-history-v1",
                        "updated_at": "2026-08-24T00:00:00+09:00",
                        "description": "successful history",
                        "posted": [],
                    }
                ).encode()
                history_path.write_bytes(successful_history)

            with patch.object(daily, "scrape", side_effect=retrying_scrape), patch.object(
                daily.time, "sleep", return_value=None
            ):
                self.assertTrue(daily.ensure("account5", root, TEST_DATE))

            self.assertEqual(calls, ["account5", "account5"])
            self.assertIn(b"successful history", history_path.read_bytes())

    def test_one_failure_does_not_stop_later_accounts(self):
        calls = []

        def fake_ensure(account: str, root: Path, today: str) -> bool:
            calls.append(account)
            if account == "account1":
                raise RuntimeError("blocked")
            return account == "account2"

        with patch.object(daily, "ensure", side_effect=fake_ensure):
            report = daily.process_accounts(
                ("account1", "account2", "account3"),
                Path("."),
                TEST_DATE,
                validate_only=False,
            )

        self.assertEqual(calls, ["account1", "account2", "account3"])
        self.assertEqual(report["valid_accounts"], ["account2", "account3"])
        self.assertEqual(report["repaired_accounts"], ["account2"])
        self.assertEqual(report["failed_accounts"][0]["account"], "account1")

    def test_report_preserves_completed_repairs_before_process_interruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "repair-report.json"

            def interrupted_ensure(account: str, scrape_root: Path, today: str) -> bool:
                if account == "account1":
                    return True
                raise KeyboardInterrupt

            with patch.object(daily, "ensure", side_effect=interrupted_ensure):
                with self.assertRaises(KeyboardInterrupt):
                    daily.process_accounts(
                        ("account1", "account2"),
                        root,
                        TEST_DATE,
                        validate_only=False,
                        report_path=report_path,
                    )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["repaired_accounts"], ["account1"])
            self.assertEqual(report["valid_accounts"], ["account1"])
            self.assertFalse(report_path.with_name(".repair-report.json.tmp").exists())

    def test_validate_only_report_controls_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "report.json"
            write_valid_output(root, "account1", 5)

            exit_code = daily.main(
                [
                    "--validate-only",
                    "--account",
                    "account1",
                    "--root",
                    str(root),
                    "--date",
                    TEST_DATE,
                    "--report",
                    str(report_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["valid_accounts"],
                ["account1"],
            )

            exit_code = daily.main(
                [
                    "--validate-only",
                    "--account",
                    "account2",
                    "--root",
                    str(root),
                    "--date",
                    TEST_DATE,
                    "--report",
                    str(report_path),
                ]
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["failed_accounts"][0][
                    "account"
                ],
                "account2",
            )


if __name__ == "__main__":
    unittest.main()
