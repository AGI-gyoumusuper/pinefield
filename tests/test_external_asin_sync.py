import json
import tempfile
import unittest
from pathlib import Path

from scripts.sync_asin_history import SyncError, sync


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ExternalSelectionSyncTests(unittest.TestCase):
    def make_fixture(self, root):
        account = root / "data" / "account1"
        account.mkdir(parents=True)
        (root / "categories1.yaml").write_text(
            'categories:\n  - name: "DIY・工具・ガーデン"\n    url: https://example.invalid/category\n',
            encoding="utf-8",
        )
        write_json(account / "asin_history.json", {"posted": []})
        # Noncanonical whitespace is deliberate: preservation is byte-for-byte.
        (account / "category_rotation.json").write_bytes(
            b'{ "next_category_position": 7, "last_event_at": "2026-09-06T20:00:00+09:00" }\r\n'
        )
        rows = [
            {"asin": "B000000001", "status": "reserved", "reserved_list_confirmed": True,
             "reserved_at": "2026-09-07T07:00:00+09:00", "category": "DIY・工具・ガーデン"},
            {"asin": "B000000002", "status": "reserved", "reserved_list_confirmed": True,
             "reserved_at": "2026-09-07T11:30:00+09:00", "category": "書斎・文具"},
            {"asin": "B000000003", "status": "skipped_past_slot"},
        ]
        products = [
            {"asin": "B000000001", "category": "DIY・工具・ガーデン", "account": 1},
            {"asin": "B000000002", "category": "書斎・文具", "account_id": "account1",
             "specs": "ブランド名 Sony メーカー型番 WH-1000XM5"},
            {"asin": "B000000003", "category": "書斎・文具", "assigned_account": 1},
        ]
        write_json(root / "results.json", rows)
        write_json(root / "products.json", products)
        return rows, products

    def run_sync(self, root, dry_run=False, require_category=False):
        return sync(root, "account1", "skkoto", [root / "results.json"],
                    [root / "products.json"], require_category, dry_run, external_selection=True)

    def protected_bytes(self, root):
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in [root / "categories1.yaml", root / "data/account1/category_rotation.json",
                      root / "data/account1/asin_history.json"]
            if p.exists()
        }

    def test_active_and_external_categories_recorded_without_cursor_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_fixture(root)
            before = self.protected_bytes(root)
            summary = self.run_sync(root)
            history = json.loads((root / "data/account1/asin_history.json").read_text(encoding="utf-8"))
            self.assertEqual(["B000000001", "B000000002"], [r["asin"] for r in history["posted"]])
            self.assertEqual(["DIY・工具・ガーデン", "書斎・文具"], [r["category"] for r in history["posted"]])
            self.assertEqual(["SONY::WH1000XM5"], history["posted"][1]["product_identity"]["brand_model_keys"])
            self.assertTrue(all(r["account_id"] == "account1" for r in history["posted"]))
            self.assertEqual(2, summary["accepted_count"])
            self.assertEqual(1, summary["skipped"])
            self.assertFalse(summary["rotation_applicable"])
            self.assertFalse(summary["rotation_changed"])
            self.assertEqual([], summary["rotation_matched_asins"])
            self.assertIsNone(summary["rotation_warning"])
            self.assertEqual("external_selection", summary["rotation_skip_reason"])
            for name in ("categories1.yaml", "data/account1/category_rotation.json"):
                self.assertEqual(before[name], (root / name).read_bytes())
            first = (root / "data/account1/asin_history.json").read_bytes()
            second = self.run_sync(root)
            self.assertFalse(second["history_changed"])
            self.assertEqual(first, (root / "data/account1/asin_history.json").read_bytes())

    def test_invalid_confirmation_and_mapping_are_rejected_before_any_write(self):
        cases = ["unconfirmed", "wrong_product_account", "wrong_result_account", "wrong_category",
                 "empty_category", "missing_product", "duplicate_product_category", "invalid_asin"]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                rows, products = self.make_fixture(root)
                if case == "unconfirmed": rows[0]["reserved_list_confirmed"] = False
                if case == "wrong_product_account": products[0]["account"] = 2
                if case == "wrong_result_account": rows[0]["account_id"] = "account2"
                if case == "wrong_category": rows[0]["category"] = "wrong category"
                if case == "empty_category": products[0]["category"] = " "
                if case == "missing_product": products.pop(0)
                if case == "duplicate_product_category": products.append({**products[0], "category": "wrong"})
                if case == "invalid_asin": products[0]["asin"] = "invalid"
                write_json(root / "results.json", rows)
                write_json(root / "products.json", products)
                before = self.protected_bytes(root)
                with self.assertRaises(SyncError):
                    self.run_sync(root)
                self.assertEqual(before, self.protected_bytes(root))

    def test_explicit_products_required_and_cannot_mix_category_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_fixture(root)
            before = self.protected_bytes(root)
            with self.assertRaisesRegex(SyncError, "requires explicit product"):
                sync(root, "account1", "skkoto", [root / "results.json"], [], False, False, True)
            with self.assertRaisesRegex(SyncError, "cannot be combined"):
                self.run_sync(root, require_category=True)
            self.assertEqual(before, self.protected_bytes(root))

    def test_dry_run_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_fixture(root)
            before = self.protected_bytes(root)
            summary = self.run_sync(root, dry_run=True)
            self.assertEqual(2, summary["accepted_count"])
            self.assertTrue(summary["dry_run"])
            self.assertEqual(before, self.protected_bytes(root))

    def test_external_mode_does_not_require_or_create_category_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_fixture(root)
            (root / "categories1.yaml").unlink()
            (root / "data/account1/category_rotation.json").unlink()
            self.assertEqual(2, self.run_sync(root)["accepted_count"])
            self.assertFalse((root / "categories1.yaml").exists())
            self.assertFalse((root / "data/account1/category_rotation.json").exists())

    def test_empty_success_set_still_reports_external_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_fixture(root)
            write_json(root / "results.json", [{"asin": "B000000001", "status": "draft"}])
            before = self.protected_bytes(root)
            summary = self.run_sync(root)
            self.assertEqual(0, summary["accepted_count"])
            self.assertTrue(summary["external_selection"])
            self.assertFalse(summary["rotation_applicable"])
            self.assertEqual("external_selection", summary["rotation_skip_reason"])
            self.assertEqual(before, self.protected_bytes(root))


if __name__ == "__main__":
    unittest.main()
