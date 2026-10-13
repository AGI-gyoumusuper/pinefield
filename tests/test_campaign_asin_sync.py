"""Campaign synchronization validates frozen evidence before ledger writes."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sale_campaign import apply_campaign_config
from scripts.sync_asin_history import SyncError, sync


SOURCE_DATE = "2026-10-15"
ACCOUNT = 8
ACCOUNT_ID = "account8"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CampaignAsinSyncTests(unittest.TestCase):
    def make_repo(self, root):
        (root / "categories8.yaml").write_text(
            "categories:\n  - name: DIY\n    url: https://www.amazon.co.jp/s?rh=n%3A2031744051\n"
            "filters:\n  selection_mode: category_round_robin\n  min_price: 1000\n",
            encoding="utf-8",
        )
        self.history = root / "data/account8/asin_history.json"
        self.cursor = root / "data/account8/category_rotation.json"
        write_json(self.history, {"posted": []})
        write_json(self.cursor, {"last_asin": "B000000099", "last_category_position": 1})
        self.initial_history = self.history.read_bytes()
        self.initial_cursor = self.cursor.read_bytes()
        self.results = root / "results.json"

    def campaign_source(self, root, *, folder=None, manifest=True):
        self.source = folder or (root / "frozen/source")
        marker = apply_campaign_config({}, "categories8.yaml", SOURCE_DATE)["_sale_campaign"]
        self.raw_product = {
            "asin": "B000000001", "title": "Sony WH-1000XM5", "price": "￥8,000", "price_int": 8000,
            "original_price": "￥10,000", "discount_rate": "20%OFF",
            "image_url": "https://m.media-amazon.com/images/I/sample.jpg",
            "affiliate_url": "https://www.amazon.co.jp/dp/B000000001?tag=noteamazon8-22",
            "category": marker["categories"][0]["name"] + "#1", "rating": "4.5", "review_count": "100",
            "description": "固定取得本文", "specs": "ブランド名 Sony メーカー型番 WH-1000XM5",
        }
        write_json(self.source / "products.json", [self.raw_product])
        write_json(self.source / "scrape_summary.json", {
            "date": SOURCE_DATE, "total_taken": 1, "campaign": marker,
            "selection_policy": {"selection_mode": "global_ranked", "sort_order": "sale_first",
                                 "max_total_items": 10, "max_per_category": 2, "min_price": 8000},
        })
        derived = dict(self.raw_product, account=8, source_position=1, input_policy_mode="git_source_only_v1")
        self.products = self.source / "products_article_input.json"
        write_json(self.products, [derived])
        if manifest:
            self.refresh_manifest()
        write_json(self.results, [{
            "asin": self.raw_product["asin"], "status": "reserved",
            "reserved_at": "2026-10-19T07:00:00+09:00", "reserved_list_confirmed": True,
            "account": ACCOUNT, "category": self.raw_product["category"],
        }])

    def refresh_manifest(self):
        write_json(self.source / "source_manifest.json", {
            "schema": 1, "account": 8, "date": SOURCE_DATE,
            "products_sha256": hashlib.sha256((self.source / "products.json").read_bytes()).hexdigest(),
            "summary_sha256": hashlib.sha256((self.source / "scrape_summary.json").read_bytes()).hexdigest(),
            "product_count": len(read_json(self.source / "products.json")),
            "source_order_preserved": True, "asin_refiltered": False,
        })

    def assert_unchanged(self):
        self.assertEqual(self.initial_history, self.history.read_bytes())
        self.assertEqual(self.initial_cursor, self.cursor.read_bytes())

    def run_sync(self, root, *, paths=None, dry_run=False):
        return sync(root, ACCOUNT_ID, "DIY", [self.results], [self.products] if paths is None else paths,
                    True, dry_run)

    def test_after_campaign_sync_restores_frozen_categories_and_keeps_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            with patch("sale_campaign._today_jst", return_value=date(2026, 10, 21)):
                result = self.run_sync(root)
            self.assertEqual(["B000000001"], result["campaign_source_asins"])
            self.assertFalse(result["rotation_applicable"])
            self.assertFalse(result["rotation_changed"])
            self.assertEqual(self.initial_cursor, self.cursor.read_bytes())
            row = read_json(self.history)["posted"][0]
            self.assertEqual(self.raw_product["category"].removesuffix("#1"), row["category"])
            self.assertEqual(["SONY::WH1000XM5"], row["product_identity"]["brand_model_keys"])
            self.assertEqual(0, self.run_sync(root)["added"])

    def test_dry_run_has_no_ledger_or_cursor_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            self.assertEqual(1, self.run_sync(root, dry_run=True)["added"])
            self.assert_unchanged()

    def test_raw_dated_git_products_restore_only_same_day_summary(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                self.campaign_source(root)
                product_path = root / f"data/account8/products_{SOURCE_DATE}.json"
                summary_path = root / f"data/account8/scrape_summary_{SOURCE_DATE}.json"
                shutil.copyfile(self.source / "products.json", product_path)
                shutil.copyfile(self.source / "scrape_summary.json", summary_path)
                result = self.run_sync(root, paths=[product_path] if explicit else [], dry_run=True)
                self.assertEqual(["B000000001"], result["campaign_source_asins"])
                self.assert_unchanged()

    def test_part_input_resolves_same_factory_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            factory = root / "★全体的なワークフロー_1_8"
            folder = factory / "②記事制作/実行結果/account8" / SOURCE_DATE / "source"
            self.campaign_source(root, folder=folder)
            product_path = factory / "①商品取得/実行結果/account8" / SOURCE_DATE / f"products_account8_{SOURCE_DATE}_p12.json"
            write_json(product_path, read_json(self.products))
            self.assertEqual(["B000000001"], self.run_sync(root, paths=[product_path], dry_run=True)["campaign_source_asins"])
            self.assert_unchanged()

    def test_reservation_subset_input_resolves_only_fixed_factory_path(self):
        for account_path in ("account8", "account9"):
            with self.subTest(account_path=account_path), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                factory = root / "★全体的なワークフロー_1_8"
                folder = factory / "②記事制作/実行結果/account8" / SOURCE_DATE / "source"
                self.campaign_source(root, folder=folder)
                second = dict(self.raw_product, asin="B000000002", category=self.raw_product["category"].removesuffix("#1") + "#2",
                              affiliate_url="https://www.amazon.co.jp/dp/B000000002?tag=noteamazon8-22")
                write_json(self.source / "products.json", [self.raw_product, second])
                summary = read_json(self.source / "scrape_summary.json")
                summary["total_taken"] = 2
                write_json(self.source / "scrape_summary.json", summary)
                self.refresh_manifest()
                product_path = factory / "④note予約/実行結果" / account_path / SOURCE_DATE / "p12/reservation_inputs/products.json"
                write_json(product_path, [dict(second, account=8, source_position=2)])
                results = read_json(self.results)
                results[0].update(asin=second["asin"], category=second["category"])
                write_json(self.results, results)
                if account_path == "account8":
                    result = self.run_sync(root, paths=[product_path], dry_run=True)
                    self.assertEqual(["B000000002"], result["campaign_source_asins"])
                else:
                    with self.assertRaisesRegex(SyncError, "account mismatch"):
                        self.run_sync(root, paths=[product_path])
                self.assert_unchanged()

    def test_modified_raw_or_summary_fails_hash_before_writes(self):
        for filename in ("products.json", "scrape_summary.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                self.campaign_source(root)
                path = self.source / filename
                path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                with self.assertRaisesRegex(SyncError, "SHA mismatch"):
                    self.run_sync(root)
                self.assert_unchanged()

    def test_derived_original_fields_and_positions_are_immutable(self):
        for field, value in (("affiliate_url", "https://example.com/wrong"), ("title", "別商品"),
                             ("price_int", 9000), ("source_position", 2), ("account", 9)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                self.campaign_source(root)
                derived = read_json(self.products)
                derived[0][field] = value
                write_json(self.products, derived)
                with self.assertRaisesRegex(SyncError, "derived product"):
                    self.run_sync(root)
                self.assert_unchanged()

    def test_invalid_campaign_marker_or_under_floor_fails_before_writes(self):
        for mutation in ("account", "date", "config_sha256", "minimum"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                self.campaign_source(root)
                summary = read_json(self.source / "scrape_summary.json")
                if mutation == "minimum":
                    raw = read_json(self.source / "products.json")
                    raw[0]["price_int"] = 7999
                    write_json(self.source / "products.json", raw)
                else:
                    summary["campaign"][mutation] = {"account": 9, "date": "2026-10-20", "config_sha256": "0" * 64}[mutation]
                    write_json(self.source / "scrape_summary.json", summary)
                self.refresh_manifest()
                with self.assertRaisesRegex(SyncError, "invalid campaign source"):
                    self.run_sync(root)
                self.assert_unchanged()

    def test_markerless_source_keeps_normal_account_categories_even_during_sale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            summary = read_json(self.source / "scrape_summary.json")
            del summary["campaign"]
            write_json(self.source / "scrape_summary.json", summary)
            self.refresh_manifest()
            with self.assertRaisesRegex(SyncError, "no active category mapping"):
                self.run_sync(root)
            self.assert_unchanged()

    def test_markerless_normal_source_retains_its_price_floor_and_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            summary = read_json(self.source / "scrape_summary.json")
            del summary["campaign"]
            write_json(self.source / "scrape_summary.json", summary)
            raw = read_json(self.source / "products.json")
            raw[0].update(category="DIY#1", price_int=1000)
            write_json(self.source / "products.json", raw)
            write_json(self.products, [dict(raw[0], account=8, source_position=1)])
            results = read_json(self.results)
            results[0]["category"] = "DIY#1"
            write_json(self.results, results)
            self.refresh_manifest()
            result = self.run_sync(root)
            self.assertNotIn("campaign_source_asins", result)
            self.assertTrue(result["rotation_applicable"])
            self.assertEqual(["B000000001"], result["rotation_matched_asins"])

    def test_unconfirmed_or_mismatched_result_cannot_reach_ledger(self):
        for mutation in ("unconfirmed", "category", "account", "absent_asin"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_repo(root)
                self.campaign_source(root)
                results = read_json(self.results)
                if mutation == "unconfirmed":
                    results[0]["reserved_list_confirmed"] = False
                else:
                    field = "asin" if mutation == "absent_asin" else mutation
                    results[0][field] = {"category": "DIY#1", "account": 9, "absent_asin": "B000000009"}[mutation]
                write_json(self.results, results)
                with self.assertRaises(SyncError):
                    self.run_sync(root)
                self.assert_unchanged()

    def test_mixed_inputs_rotate_only_normal_category(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            normal_path = root / "normal_products.json"
            write_json(normal_path, [{"asin": "B000000002", "category": "DIY#1"}])
            results = read_json(self.results)
            results.append({"asin": "B000000002", "status": "reserved", "reserved_at": "2026-10-19T08:00:00+09:00",
                            "reserved_list_confirmed": True, "category": "DIY#1"})
            write_json(self.results, results)
            result = self.run_sync(root, paths=[self.products, normal_path])
            self.assertTrue(result["rotation_applicable"])
            self.assertEqual(["B000000002"], result["rotation_matched_asins"])
            self.assertEqual("B000000002", read_json(self.cursor)["last_asin"])
            self.assertEqual(2, len(read_json(self.history)["posted"]))

    def test_isolated_core_accepts_absolute_original_product_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            self.campaign_source(root)
            isolated = root / "isolated"
            isolated.mkdir()
            self.make_repo(isolated)
            original_repo = Path(__file__).resolve().parents[1]
            (isolated / "scripts").mkdir()
            for filename in ("sync_asin_history.py", "update_category_rotation.py"):
                shutil.copyfile(original_repo / "scripts" / filename, isolated / "scripts" / filename)
            for filename in ("sale_campaign.py", "sale_campaign.yaml", "product_identity.py"):
                shutil.copyfile(original_repo / filename, isolated / filename)
            completed = subprocess.run([
                sys.executable, str(isolated / "scripts/sync_asin_history.py"), "--repo-dir", str(isolated),
                "--account", ACCOUNT_ID, "--source-json", str(root / "results.json"),
                "--product-json", str(root / "frozen/source/products_article_input.json"),
                "--require-category", "--dry-run",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout.split("ASIN_SYNC_RESULT=", 1)[1])
            self.assertEqual(["B000000001"], result["campaign_source_asins"])
            self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
