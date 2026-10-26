"""Offline wrapper tests: Git reads and product fetching are always mocked."""

from contextlib import ExitStack, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import yaml

import new40_fetch as wrapper
from new40_history import HISTORY_GIT_PATH, empty_history
from sale_campaign import ACTUAL_SALE_PRICE_POLICY
import scraper


COMMIT = "a" * 40
DAY = "2026-10-26"
REGISTERED = "B000000001"


def history_fixture():
    history = empty_history()
    row = {
        "destination_account": 201, "source_account": 1, "source_position": 1,
        "planning_slot": "N01", "asin": REGISTERED,
        "source_commit": "b" * 40, "manifest_sha256": "c" * 64,
        "source_products_sha256": "d" * 64, "allocation_date": "2026-10-25",
        "note_key": "noffline", "note_url": "https://note.com/offline_fixture/n/noffline",
        "status": "published", "published_at": "2026-10-25T09:00:00+09:00",
    }
    row["verification"] = {
        "schema": "note40_note_readback.v1", "status": "VERIFIED_READBACK",
        "management_list_confirmed": True, "identity_verified": True,
        "destination_account": row["destination_account"], "note_key": row["note_key"],
        "note_url": row["note_url"], "publication_status": "published",
        "checked_at": "2026-10-25T09:01:00+09:00",
    }
    history["posted"].append(row)
    return history


def product(asin="B000000003", price=9000, category="fixture shelf"):
    return scraper.Product(
        asin=asin, title="Offline product", price=f"￥{price:,}", price_int=price,
        original_price=f"￥{price + 1000:,}", discount_rate="10%OFF",
        image_url="https://m.media-amazon.com/images/I/offline.jpg",
        affiliate_url=f"https://www.amazon.co.jp/dp/{asin}", category=category,
        rating="4.5", review_count="50", description="Offline description", specs="Offline specs",
    )


class New40FetchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.repo = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="new40-fetch-offline-"))).resolve()
        self.config = {
            "categories": [{"name": "fixture shelf", "url": "https://www.amazon.co.jp/s?k=offline", "max_items": 10, "is_search": True}],
            "filters": {"min_price": 8000, "max_total_items": 10, "max_per_category": 0,
                        "sort_order": "price_desc", "selection_mode": "global_ranked",
                        "discount_contract": "amazon_direct_discounts_v1"},
            "exclusion": {"posted_asins_file": "data/account1/asin_history.json",
                          "exclude_within_days": 20, "exclude_product_identifiers": True},
        }
        self.blobs = {HISTORY_GIT_PATH: json.dumps(history_fixture()).encode()}
        self.set_config()
        self.legacy = self.repo / "data" / "account1" / "asin_history.json"
        self.legacy.parent.mkdir(parents=True)
        self.legacy_bytes = b'{"offline_sentinel":"never read or write"}\n'
        self.legacy.write_bytes(self.legacy_bytes)
        self.products = [product(), product("B000000002", 10000)]
        self.coupons = {}
        self.git = self.stack.enter_context(patch("new40_fetch.subprocess.run", side_effect=self.git_show))
        self.fetch = self.stack.enter_context(patch("scraper.fetch_products", new_callable=AsyncMock, side_effect=self.mock_fetch))
        self.legacy_reader = self.stack.enter_context(patch("scraper.load_product_exclusion_registry", side_effect=AssertionError("legacy history read")))
        self.legacy_writer = self.stack.enter_context(patch("scraper.save_scraped_asins_to_history", side_effect=AssertionError("legacy history write")))
        self.legacy_route = self.stack.enter_context(patch("scraper.fetch_and_save", side_effect=AssertionError("legacy fetch route")))

    def tearDown(self):
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        self.legacy_reader.assert_not_called()
        self.legacy_writer.assert_not_called()
        self.legacy_route.assert_not_called()

    def set_config(self):
        blob = yaml.safe_dump(self.config, allow_unicode=True).encode()
        self.blobs["categories1.yaml"] = blob
        # Exercise the real Windows Git LF/checkout CRLF correspondence.
        (self.repo / "categories1.yaml").write_bytes(blob.replace(b"\n", b"\r\n"))

    def git_show(self, command, **kwargs):
        self.assertEqual(command[:4], ["git", "-C", str(self.repo), "show"])
        self.assertTrue(command[4].startswith(COMMIT + ":"))
        path = command[4][len(COMMIT) + 1:]
        self.assertIn(path, {"categories1.yaml", HISTORY_GIT_PATH})
        value = self.blobs.get(path)
        return subprocess.CompletedProcess(command, 0 if value is not None else 128,
                                           stdout=value or b"", stderr=b"")

    async def mock_fetch(self, config_path, *, associate_tag, reference_date, new40_selection_context):
        self.assertEqual(config_path, str(self.repo / "categories1.yaml"))
        self.assertEqual(associate_tag, "")
        self.assertEqual(reference_date, DAY)
        context = new40_selection_context
        # The registered ASIN is supplied before the mocked search can run.
        self.assertIn(REGISTERED, context.registry_for_selection(
            source_account=1, reference_date=DAY, within_days=20,
            include_product_identities=True).asins)
        self.last_context = context
        stats = {
            "_new40_selection": context.public_evidence(),
            "_selection_policy": {"actual_sale_price_policy": ACTUAL_SALE_PRICE_POLICY,
                                  "sort_order": "price_desc", "max_total_items": 10},
            "_excluded_loaded": 1, "_excluded_gtins_loaded": 0,
            "_excluded_brand_models_loaded": 0, "_skipped_product_identity": 1,
            "_skipped_product_identity_reasons": {"asin": 1},
            "_detail_offer_verification": {"verified": len(self.products)},
            "_detail_retry": {"attempts": 1}, "_supply_completion": {"reason": "fixture"},
            "_coupon_search": {"verified": 0},
            "_discount_contract": {"schema_version": 1, "policy": "amazon_direct_discounts_v1",
                                   "account": 1, "date": DAY, "products": [
                {"asin": p.asin, "source_position": position,
                 "coupon": self.coupons.get(p.asin, {"status": "none"}),
                 "prime_event": {"verified": False}, "evidence_sha256": str(position) * 64}
                for position, p in enumerate(self.products, 1)]},
            "fixture shelf": {"fetched": len(self.products), "taken": len(self.products)},
        }
        self.returned_stats = stats
        self.stats_before = copy.deepcopy(stats)
        return self.products, stats

    def run_fetch(self, execute=True):
        return wrapper.run_new40_fetch(self.repo, COMMIT, 1, DAY, execute_fetch=execute)

    def paths(self):
        folder = self.repo / "data" / "new40" / "sources" / "account1"
        return folder / f"products_{DAY}.json", folder / f"scrape_summary_{DAY}.json"

    def test_default_dry_run_has_no_fetch_and_creates_no_output(self):
        result = wrapper.run_new40_fetch(self.repo, COMMIT, 1, DAY)
        self.assertEqual(result["status"], "DRY_RUN_READY")
        self.assertFalse(result["execute_fetch"])
        self.fetch.assert_not_called()
        self.assertFalse((self.repo / "data" / "new40").exists())
        self.assertEqual(self.git.call_count, 2)
        self.assertEqual(result["new40_selection"]["excluded_count"], 1)

    def test_cli_defaults_to_offline_preflight(self):
        with redirect_stdout(io.StringIO()) as output:
            code = wrapper.main(["--repo", str(self.repo), "--commit", COMMIT,
                                 "--source-account", "1", "--date", DAY])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "DRY_RUN_READY")
        self.fetch.assert_not_called()

    def test_history_context_order_null_affiliate_and_summary_are_preserved(self):
        result = self.run_fetch()
        self.fetch.assert_awaited_once()
        self.assertEqual(result["status"], "SOURCE_SAVED")
        product_path, summary_path = self.paths()
        saved = json.loads(product_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual([row["asin"] for row in saved], [p.asin for p in self.products])
        self.assertEqual([row["price_int"] for row in saved], [9000, 10000])
        self.assertTrue(all(row["affiliate_url"] is None for row in saved))
        self.assertTrue(all(set(row) == set(wrapper.PRODUCT_FIELDS) for row in saved))
        self.assertTrue(all(p.affiliate_url is not None for p in self.products))
        self.assertEqual(self.returned_stats, self.stats_before)
        self.assertEqual(summary["new40_selection"], self.last_context.public_evidence())
        self.assertEqual(summary["source_config"], {
            "git_path": "categories1.yaml", "git_commit": COMMIT,
            "sha256": hashlib.sha256(self.blobs["categories1.yaml"]).hexdigest()})
        for original, key in (("_detail_offer_verification", "detail_offer_verification"),
                              ("_detail_retry", "detail_retry"), ("_supply_completion", "supply_completion"),
                              ("_discount_contract", "discount_contract"), ("_coupon_search", "coupon_search")):
            self.assertEqual(summary[key], self.stats_before[original])
        self.assertEqual(summary["categories"], {"fixture shelf": self.stats_before["fixture shelf"]})
        self.assertNotIn(REGISTERED, json.dumps(summary))  # public evidence is not a ledger copy

    def test_missing_git_config_stops_before_fetch(self):
        del self.blobs["categories1.yaml"]
        with self.assertRaisesRegex(ValueError, "Git input is unavailable"):
            self.run_fetch()
        self.fetch.assert_not_called()

    def test_missing_worktree_config_stops_before_fetch(self):
        (self.repo / "categories1.yaml").unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_fetch()
        self.fetch.assert_not_called()

    def test_modified_config_stops_before_fetch(self):
        with (self.repo / "categories1.yaml").open("ab") as stream:
            stream.write(b"# changed content\n")
        with self.assertRaisesRegex(ValueError, "differs from its pinned"):
            self.run_fetch()
        self.fetch.assert_not_called()

    def test_missing_history_stops_before_fetch_without_legacy_fallback(self):
        del self.blobs[HISTORY_GIT_PATH]
        with self.assertRaisesRegex(ValueError, "history missing"):
            self.run_fetch()
        self.fetch.assert_not_called()
        self.assertFalse((self.repo / "data" / "new40").exists())

    def test_invalid_history_stops_before_fetch(self):
        self.blobs[HISTORY_GIT_PATH] = b'{"posted":[]}'
        with self.assertRaisesRegex(ValueError, "history schema"):
            self.run_fetch()
        self.fetch.assert_not_called()

    def test_existing_products_stops_before_fetch_and_does_not_fill_summary(self):
        products, summary = self.paths()
        products.parent.mkdir(parents=True)
        products.write_bytes(b"[]\n")
        with self.assertRaises(FileExistsError):
            self.run_fetch()
        self.fetch.assert_not_called()
        self.assertEqual(products.read_bytes(), b"[]\n")
        self.assertFalse(summary.exists())

    def test_existing_summary_stops_before_fetch_and_does_not_fill_products(self):
        products, summary = self.paths()
        summary.parent.mkdir(parents=True)
        summary.write_bytes(b"{}\n")
        with self.assertRaises(FileExistsError):
            self.run_fetch()
        self.fetch.assert_not_called()
        self.assertEqual(summary.read_bytes(), b"{}\n")
        self.assertFalse(products.exists())

    def test_both_files_are_reserved_before_fetch_and_failure_is_not_refilled(self):
        async def fail(*args, **kwargs):
            self.assertTrue(all(path.exists() for path in self.paths()))
            raise RuntimeError("offline intentional fetch failure")
        self.fetch.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "intentional fetch failure"):
            self.run_fetch()
        self.assertTrue(all(path.read_bytes() == b"" for path in self.paths()))
        with self.assertRaises(FileExistsError):
            self.run_fetch()
        self.assertEqual(self.fetch.await_count, 1)

    def test_zero_products_is_saved_without_filling_or_fallback(self):
        self.products = []
        result = self.run_fetch()
        self.assertEqual(result["total_taken"], 0)
        self.assertEqual(json.loads(self.paths()[0].read_text(encoding="utf-8")), [])

    def test_more_than_ten_products_is_rejected_without_truncation(self):
        self.products = [product(f"B{i:09d}") for i in range(10, 21)]
        with self.assertRaisesRegex(ValueError, "zero through ten"):
            self.run_fetch()
        self.assertTrue(all(path.read_bytes() == b"" for path in self.paths()))

    def test_8000_current_price_is_rejected_before_save(self):
        self.products = [product(price=8000)]
        with self.assertRaisesRegex(ValueError, "actual_sale_price_at_or_below_8000"):
            self.run_fetch()
        self.assertTrue(all(path.read_bytes() == b"" for path in self.paths()))

    def test_8001_and_8800_prices_pass_without_rounding(self):
        self.products = [product(price=8001), product("B000000002", 8800)]
        self.run_fetch()
        self.assertEqual([p["price_int"] for p in json.loads(self.paths()[0].read_text(encoding="utf-8"))], [8001, 8800])

    def test_unknown_verified_coupon_is_rejected_before_save(self):
        self.coupons[self.products[0].asin] = {"status": "verified", "final_price_yen": None}
        with self.assertRaisesRegex(ValueError, "actual_coupon_price_unknown"):
            self.run_fetch()

    def test_low_verified_coupon_is_rejected_before_save(self):
        self.coupons[self.products[0].asin] = {"status": "verified", "final_price_yen": 8000}
        with self.assertRaisesRegex(ValueError, "actual_coupon_price_at_or_below_8000"):
            self.run_fetch()

    def test_internal_verified_coupon_is_not_discarded_for_unverified_summary_coupon(self):
        self.products[0]._actual_sale_coupon = {"status": "verified", "final_price_yen": 8000}
        with self.assertRaisesRegex(ValueError, "actual_coupon_price_at_or_below_8000"):
            self.run_fetch()

    def test_existing_stricter_category_floor_is_applied_to_coupon_payable(self):
        self.config["categories"][0]["min_price"] = 20000
        self.set_config()
        self.products = [product(price=22000)]
        self.coupons[self.products[0].asin] = {"status": "verified", "final_price_yen": 19999}
        with self.assertRaisesRegex(ValueError, "existing category floor"):
            self.run_fetch()

    def test_fetch_without_new40_marker_is_rejected(self):
        async def missing_marker(*args, **kwargs):
            products, stats = await self.mock_fetch(*args, **kwargs)
            del stats["_new40_selection"]
            return products, stats
        self.fetch.side_effect = missing_marker
        with self.assertRaisesRegex(ValueError, "pinned new40 selection context"):
            self.run_fetch()

    def test_config_changed_during_fetch_is_not_saved(self):
        async def changed_config(*args, **kwargs):
            fetched = await self.mock_fetch(*args, **kwargs)
            (self.repo / "categories1.yaml").write_bytes(b"changed")
            return fetched
        self.fetch.side_effect = changed_config
        with self.assertRaisesRegex(ValueError, "changed during fetch"):
            self.run_fetch()
        self.assertTrue(all(path.read_bytes() == b"" for path in self.paths()))

    def test_unallocated_source_accounts_are_rejected_before_git_or_fetch(self):
        for account in (0, 10, 13, 14, 21, True):
            with self.subTest(account=account), self.assertRaisesRegex(ValueError, "source account"):
                wrapper.run_new40_fetch(self.repo, COMMIT, account, DAY, execute_fetch=True)
        self.git.assert_not_called()
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
