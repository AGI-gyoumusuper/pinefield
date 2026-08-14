import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scraper import (
    Product,
    load_posted_asins,
    load_product_exclusion_registry,
    save_scraped_asins_to_history,
)
from scripts.prune_asin_history import prune_history_data
from scripts.sync_asin_history import SyncError, sync


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class VerifiedAsinSyncTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        (root / "data" / "account1").mkdir(parents=True)
        (root / "categories1.yaml").write_text(
            "categories:\n"
            "  - name: C1\n"
            "    url: https://www.amazon.co.jp/s?rh=n%3A1001\n"
            "  - name: C2\n"
            "    url: https://www.amazon.co.jp/s?rh=n%3A1002\n",
            encoding="utf-8",
        )
        write_json(
            root / "data" / "account1" / "asin_history.json",
            {"schema": "note-amazon-asin-history-v1", "updated_at": "", "posted": []},
        )

    def test_only_confirmed_reservation_is_recorded_and_rotated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            source = root / "results.json"
            products = root / "products.json"
            write_json(
                source,
                [
                    {
                        "asin": "B000000001",
                        "status": "reserved",
                        "reserved_at": "2026-08-01T07:00:00+09:00",
                        "reserved_list_confirmed": True,
                    },
                    {
                        "asin": "B000000002",
                        "status": "skipped_past_slot",
                        "publish_at": "2026-08-01T07:30:00+09:00",
                    },
                ],
            )
            write_json(
                products,
                [
                    {
                        "asin": "B000000001",
                        "category": "C1#1",
                        "specs": "ブランド名 Sony メーカー型番 WH-1000XM5 UPC 077924051524 Amazon 売れ筋ランキング 1位",
                    },
                    {"asin": "B000000002", "category": "C2#1"},
                ],
            )
            result = sync(root, "account1", "account1", [source], [products], True, False)
            history = json.loads((root / "data" / "account1" / "asin_history.json").read_text(encoding="utf-8"))
            rotation = json.loads((root / "data" / "account1" / "category_rotation.json").read_text(encoding="utf-8"))
            self.assertEqual(["B000000001"], [row["asin"] for row in history["posted"]])
            self.assertEqual("reserved", history["posted"][0]["status"])
            self.assertEqual("C1", history["posted"][0]["category"])
            self.assertEqual(
                ["SONY::WH1000XM5"],
                history["posted"][0]["product_identity"]["brand_model_keys"],
            )
            self.assertEqual(
                ["00077924051524"],
                history["posted"][0]["product_identity"]["global_trade_numbers"],
            )
            self.assertEqual("B000000001", rotation["last_asin"])
            self.assertEqual(2, rotation["next_category_position"])
            self.assertEqual(1, result["accepted_count"])

    def test_unconfirmed_reservation_is_fatal_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            history_path = root / "data" / "account1" / "asin_history.json"
            before = history_path.read_bytes()
            source = root / "results.json"
            write_json(
                source,
                [{"asin": "B000000001", "status": "reserved", "reserved_at": "2026-08-01T07:00:00+09:00"}],
            )
            with self.assertRaises(SyncError):
                sync(root, "account1", "account1", [source], [], False, False)
            self.assertEqual(before, history_path.read_bytes())
            self.assertFalse((root / "data" / "account1" / "category_rotation.json").exists())

    def test_unknown_status_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            source = root / "results.json"
            write_json(source, [{"asin": "B000000001", "status": "maybe_posted"}])
            with self.assertRaises(SyncError):
                sync(root, "account1", "account1", [source], [], False, False)

    def test_posted_status_requires_management_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            source = root / "results.json"
            write_json(
                source,
                [{"asin": "B000000001", "status": "posted", "posted_at": "2026-08-01T07:00:00+09:00"}],
            )
            with self.assertRaises(SyncError):
                sync(root, "account1", "account1", [source], [], False, False)

    def test_rotation_uses_event_time_when_source_rows_are_out_of_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            source = root / "results.json"
            products = root / "products.json"
            write_json(
                source,
                [
                    {"asin": "B000000002", "status": "reserved", "reserved_at": "2026-08-01T08:00:00+09:00", "reserved_list_confirmed": True},
                    {"asin": "B000000001", "status": "reserved", "reserved_at": "2026-08-01T07:00:00+09:00", "reserved_list_confirmed": True},
                ],
            )
            write_json(
                products,
                [
                    {"asin": "B000000001", "category": "C1#1"},
                    {"asin": "B000000002", "category": "C2#1"},
                ],
            )
            sync(root, "account1", "account1", [source], [products], True, False)
            rotation = json.loads((root / "data" / "account1" / "category_rotation.json").read_text(encoding="utf-8"))
            self.assertEqual("B000000002", rotation["last_asin"])
            self.assertEqual(2, rotation["last_category_position"])
            self.assertEqual(1, rotation["next_category_position"])

    def test_category_quota_account_updates_history_without_rotation_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            (root / "categories1.yaml").write_text(
                "categories:\n"
                "  - name: C1\n"
                "    url: https://www.amazon.co.jp/s?rh=n%3A1001\n"
                "  - name: C2\n"
                "    url: https://www.amazon.co.jp/s?rh=n%3A1002\n"
                "filters:\n"
                "  selection_mode: category_quota\n",
                encoding="utf-8",
            )
            source = root / "results.json"
            products = root / "products.json"
            write_json(
                source,
                [{
                    "asin": "B000000001",
                    "status": "reserved",
                    "reserved_at": "2026-08-01T07:00:00+09:00",
                    "reserved_list_confirmed": True,
                }],
            )
            write_json(products, [{"asin": "B000000001", "category": "C1#1"}])
            result = sync(root, "account1", "account1", [source], [products], True, False)
            history = json.loads((root / "data" / "account1" / "asin_history.json").read_text(encoding="utf-8"))
            self.assertEqual(["B000000001"], [row["asin"] for row in history["posted"]])
            self.assertFalse((root / "data" / "account1" / "category_rotation.json").exists())
            self.assertFalse(result["rotation_changed"])

    def test_unknown_selection_mode_is_fatal_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            (root / "categories1.yaml").write_text(
                "categories:\n"
                "  - name: C1\n"
                "    url: https://www.amazon.co.jp/s?rh=n%3A1001\n"
                "filters:\n"
                "  selection_mode: typo_mode\n",
                encoding="utf-8",
            )
            history_path = root / "data" / "account1" / "asin_history.json"
            before = history_path.read_bytes()
            source = root / "results.json"
            write_json(
                source,
                [{
                    "asin": "B000000001",
                    "status": "reserved",
                    "reserved_at": "2026-08-01T07:00:00+09:00",
                    "reserved_list_confirmed": True,
                }],
            )
            with self.assertRaisesRegex(SyncError, "unsupported selection_mode"):
                sync(root, "account1", "account1", [source], [], False, False)
            self.assertEqual(before, history_path.read_bytes())

    def test_existing_duplicate_event_key_is_fatal_instead_of_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            duplicate = {"asin": "B000000009", "status": "reserved", "reserved_at": "2026-07-31", "account_id": "account1"}
            write_json(
                root / "data" / "account1" / "asin_history.json",
                {"posted": [duplicate, duplicate]},
            )
            source = root / "results.json"
            write_json(
                source,
                [{"asin": "B000000001", "status": "reserved", "reserved_at": "2026-08-01T07:00:00+09:00", "reserved_list_confirmed": True}],
            )
            with self.assertRaises(SyncError):
                sync(root, "account1", "account1", [source], [], False, False)

    def test_required_category_missing_is_fatal_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_repo(root)
            history_path = root / "data" / "account1" / "asin_history.json"
            before = history_path.read_bytes()
            source = root / "results.json"
            write_json(
                source,
                [{
                    "asin": "B000000001",
                    "status": "reserved",
                    "reserved_at": "2026-08-01T07:00:00+09:00",
                    "reserved_list_confirmed": True,
                }],
            )
            with self.assertRaises(SyncError):
                sync(root, "account1", "account1", [source], [], True, False)
            self.assertEqual(before, history_path.read_bytes())


class AsinExclusionTests(unittest.TestCase):
    def test_popular_mode_excludes_only_success_statuses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            write_json(
                path,
                {
                    "posted": [
                        {"asin": "B000000001", "status": "reserved", "reserved_at": "2099-01-01"},
                        {"asin": "B000000002", "status": "scraped", "posted_at": "2099-01-01"},
                        {"asin": "B000000003", "status": "rejected", "posted_at": "2099-01-01"},
                    ]
                },
            )
            self.assertEqual({"B000000001"}, load_posted_asins(str(path), 20, include_scraped=False))

    def test_product_identities_use_the_same_success_status_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            write_json(
                path,
                {
                    "posted": [
                        {
                            "asin": "B000000001",
                            "status": "reserved",
                            "reserved_at": "2099-01-01",
                            "product_identity": {
                                "brand_model_keys": ["SONY::WH1000XM5"],
                                "global_trade_numbers": ["00077924051524"],
                            },
                        },
                        {
                            "asin": "B000000002",
                            "status": "scraped",
                            "posted_at": "2099-01-01",
                            "product_identity": {
                                "brand_model_keys": ["ACME::BAD1000"],
                                "global_trade_numbers": ["00074451126220"],
                            },
                        },
                    ]
                },
            )
            registry = load_product_exclusion_registry(
                str(path), 20, include_scraped=False, include_product_identities=True
            )
            self.assertEqual({"B000000001"}, registry.asins)
            self.assertEqual({"SONY::WH1000XM5"}, registry.brand_model_keys)
            self.assertEqual({"00077924051524"}, registry.global_trade_numbers)

    def test_product_identity_outside_twenty_day_window_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            old_date = (
                datetime.now(timezone(timedelta(hours=9))).date() - timedelta(days=20)
            ).isoformat()
            write_json(
                path,
                {
                    "posted": [
                        {
                            "asin": "B000000001",
                            "status": "reserved",
                            "reserved_at": old_date,
                            "product_identity": {
                                "brand_model_keys": ["SONY::WH1000XM5"],
                            },
                        }
                    ]
                },
            )
            registry = load_product_exclusion_registry(
                str(path), 20, include_scraped=False, include_product_identities=True
            )
            self.assertEqual(set(), registry.asins)
            self.assertEqual(set(), registry.brand_model_keys)

    def test_popular_mode_does_not_append_scraped_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history = root / "data" / "account1" / "asin_history.json"
            config = root / "categories1.yaml"
            write_json(history, {"posted": []})
            config.write_text(
                "exclusion:\n"
                "  posted_asins_file: data/account1/asin_history.json\n"
                "  exclude_scraped_candidates: false\n",
                encoding="utf-8",
            )
            item = Product(
                asin="B000000001", title="x", price="￥3,000", price_int=3000,
                original_price="", discount_rate="", image_url="", affiliate_url="", category="C1#1",
                rating="4.5", review_count="100",
            )
            save_scraped_asins_to_history([item], str(config), "2026-08-01", str(root / "data" / "account1" / "products.json"))
            self.assertEqual([], json.loads(history.read_text(encoding="utf-8"))["posted"])

    def test_prune_uses_newer_of_posted_and_reserved_and_preserves_fields(self):
        value = {
            "posted": [
                {"asin": "B000000001", "status": "reserved", "posted_at": None, "reserved_at": "2026-07-31", "category": "C1"},
                {"asin": "B000000002", "status": "posted", "posted_at": "2026-07-11", "reserved_at": None},
                {"asin": "B000000003", "status": "reserved", "posted_at": None, "reserved_at": "2026-08-05"},
                {"asin": "B000000004", "status": "posted", "posted_at": "unknown", "note": "keep safely"},
            ]
        }
        output, removed = prune_history_data(value, date(2026, 7, 31), 20)
        self.assertEqual(1, removed)
        self.assertEqual(
            ["B000000001", "B000000003", "B000000004"],
            [row["asin"] for row in output["posted"]],
        )
        self.assertEqual("C1", output["posted"][0]["category"])


if __name__ == "__main__":
    unittest.main()
