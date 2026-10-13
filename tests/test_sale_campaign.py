import copy
import hashlib
import json
import unittest
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml

from sale_campaign import (
    CAMPAIGN_CONFIG_PATH, CampaignConfigError, apply_campaign_config, source_campaign_config,
)


ROOT = Path(__file__).resolve().parents[1]


def base_config(account=8):
    return yaml.safe_load((ROOT / f"categories{account}.yaml").read_text(encoding="utf-8-sig"))


def frozen_source(account=8, day="2026-10-19", price=8000):
    base = base_config(account)
    effective = apply_campaign_config(base, f"categories{account}.yaml", day)
    marker = effective["_sale_campaign"]
    products = [{
        "asin": "B000000001", "price_int": price, "category": marker["categories"][0]["name"] + "#1",
        "source_position": 3, "article_title": "派生データは保持する",
    }]
    summary = {
        "date": day, "total_taken": len(products), "campaign": marker,
        "selection_policy": {
            "selection_mode": "global_ranked", "sort_order": "sale_first",
            "max_total_items": 10, "max_per_category": 2,
        },
    }
    return base, summary, products


class SaleCampaignTests(unittest.TestCase):
    def test_period_boundaries_and_deep_copy_preserve_normal_config(self):
        base = base_config()
        original = copy.deepcopy(base)
        for day, active in [("2026-10-12", False), ("2026-10-13", True), ("2026-10-19", True), ("2026-10-20", False)]:
            with self.subTest(day=day):
                effective = apply_campaign_config(base, "categories8.yaml", day)
                self.assertEqual(active, "_sale_campaign" in effective)
                if not active:
                    self.assertEqual(base, effective)
                effective["exclusion"]["exclude_within_days"] = 999
                self.assertEqual(original, base)

    def test_all_20_accounts_have_four_different_balanced_allocations(self):
        campaign = yaml.safe_load(CAMPAIGN_CONFIG_PATH.read_text(encoding="utf-8"))
        families = {node["name"]: node["family"] for node in campaign["nodes"]}
        for day in ["2026-10-13", "2026-10-19"]:
            allocations, node_visits = set(), Counter()
            for account in range(1, 21):
                base = base_config(account)
                effective = apply_campaign_config(base, f"categories{account}.yaml", day)
                marker = effective["_sale_campaign"]
                self.assertEqual(account, marker["account"])
                self.assertEqual(day, marker["date"])
                categories = effective["categories"]
                names = tuple(sorted(category["name"] for category in categories))
                allocations.add(names)
                node_visits.update(names)
                self.assertEqual(Counter(pc=2, appliance=2), Counter(families[name] for name in names))
                self.assertEqual(4, len(categories))
                self.assertEqual(base["exclusion"], effective["exclusion"])
                for category in categories:
                    self.assertIs(True, category["is_search"])
                    self.assertEqual(20, category["max_items"])
                    self.assertEqual(8000, category["min_price"])
                    query = parse_qs(urlparse(category["url"]).query)["rh"][0]
                    self.assertIn("p_36:800000-", query)
                    self.assertIn("p_n_deal_type:10343616051", query)
                filters = effective["filters"]
                for key, expected in {
                    "selection_mode": "global_ranked", "sort_order": "sale_first",
                    "min_price": 8000, "max_total_items": 10, "max_per_category": 2,
                }.items():
                    self.assertEqual(expected, filters[key])
                for key in ["discount_contract", "offer_scope", "verify_detail_offer"]:
                    self.assertEqual(base["filters"][key], filters[key])
            self.assertEqual(20, len(allocations))
            self.assertEqual({4}, set(node_visits.values()))
            self.assertEqual(20, len(node_visits))

    def test_daily_rotation_is_deterministic_and_keeps_source_order(self):
        base = base_config()
        first = apply_campaign_config(base, "categories8.yaml", "2026-10-13")
        repeated = apply_campaign_config(base, "categories8.yaml", "2026-10-13")
        following = apply_campaign_config(base, "categories8.yaml", "2026-10-14")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first["categories"], following["categories"])
        base, summary, products = frozen_source()
        before = copy.deepcopy((base, summary, products))
        restored = source_campaign_config(base, summary, products, "account8", "2026-10-19")
        self.assertEqual(summary["campaign"], restored["_sale_campaign"])
        self.assertEqual(before, (base, summary, products))
        self.assertEqual(3, products[0]["source_position"])

    def test_unknown_account_and_profile100_are_untouched(self):
        base = base_config()
        for filename in ["categories0.yaml", "categories21.yaml", "categories100.yaml", "Profile100", "categories.yaml", "categories01.yaml"]:
            self.assertEqual(base, apply_campaign_config(base, filename, "2026-10-13"))
        windows = apply_campaign_config(base, r"D:\repo\categories8.yaml", "2026-10-13")
        self.assertEqual(8, windows["_sale_campaign"]["account"])

    def test_aware_timestamp_uses_jst_day(self):
        before = datetime(2026, 10, 12, 14, 59, tzinfo=timezone.utc)
        start = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
        self.assertNotIn("_sale_campaign", apply_campaign_config(base_config(), "categories8.yaml", before))
        self.assertEqual("2026-10-13", apply_campaign_config(base_config(), "categories8.yaml", start)["_sale_campaign"]["date"])

    def test_metadata_hash_uses_canonical_parsed_json(self):
        parsed = yaml.safe_load(CAMPAIGN_CONFIG_PATH.read_text(encoding="utf-8"))
        encoded = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        marker = apply_campaign_config(base_config(), "categories8.yaml", "2026-10-13")["_sale_campaign"]
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), marker["config_sha256"])

    def test_price_8000_passes_and_7999_is_rejected(self):
        base, summary, products = frozen_source(price=8000)
        self.assertEqual(8000, source_campaign_config(base, summary, products, 8, "2026-10-19")["filters"]["min_price"])
        products[0]["price_int"] = 7999
        with self.assertRaises(CampaignConfigError):
            source_campaign_config(base, summary, products, 8, "2026-10-19")
        for invalid in ["8000", 8000.0, True, None]:
            products[0]["price_int"] = invalid
            with self.subTest(price=invalid), self.assertRaises(CampaignConfigError):
                source_campaign_config(base, summary, products, 8, "2026-10-19")

    def test_fixed_source_replays_after_event_end(self):
        base, summary, products = frozen_source()
        with patch("sale_campaign._today_jst", return_value=date(2026, 10, 23)):
            self.assertNotIn("_sale_campaign", apply_campaign_config(base, "categories8.yaml"))
            restored = source_campaign_config(base, summary, products, 8, "2026-10-19")
            self.assertEqual(summary["campaign"], restored["_sale_campaign"])

    def test_markerless_legacy_source_keeps_normal_account20_selection(self):
        base = base_config(20)
        for summary in [{"date": "2026-10-13"}, None]:
            result = source_campaign_config(base, summary, [], 20, "2026-10-13")
            self.assertEqual(base, result)
            self.assertEqual("category_quota", result["filters"]["selection_mode"])

    def test_empty_base_can_validate_campaign_for_recovery_readiness(self):
        _, summary, products = frozen_source()
        restored = source_campaign_config({}, summary, products, 8, "2026-10-19")
        self.assertEqual(summary["campaign"], restored["_sale_campaign"])
        self.assertEqual(8000, restored["filters"]["min_price"])
        self.assertEqual({}, source_campaign_config({}, {}, [], 8, "2026-10-19"))

    def test_marker_tampering_and_account_date_mismatch_are_rejected(self):
        base, summary, products = frozen_source()
        for key, value in {
            "schema": True, "id": "other", "policy": "other", "account": "8",
            "date": "2026-10-18", "starts_at": "2026-10-12T00:00:00+09:00",
            "ends_at": "2026-10-21T00:00:00+09:00", "min_price": 7999,
            "selection_mode": "category_quota", "config_sha256": "0" * 64,
            "allocation_method": "other", "official_source": "https://example.com",
        }.items():
            altered = copy.deepcopy(summary)
            altered["campaign"][key] = value
            with self.subTest(key=key), self.assertRaises(CampaignConfigError):
                source_campaign_config(base, altered, products, 8, "2026-10-19")
        for account, day in [(7, "2026-10-19"), (True, "2026-10-19"), (100, "2026-10-19"), (8, "2026-10-20"), (8, None)]:
            with self.subTest(account=account, day=day), self.assertRaises(CampaignConfigError):
                source_campaign_config(base, summary, products, account, day)
        altered = copy.deepcopy(summary)
        altered["campaign"]["categories"].reverse()
        with self.assertRaises(CampaignConfigError):
            source_campaign_config(base, altered, products, 8, "2026-10-19")

    def test_invalid_product_category_policy_and_count_are_rejected(self):
        base, summary, products = frozen_source()
        for category in ["農具#1", products[0]["category"] + " ", products[0]["category"].replace("#1", "#0"), None]:
            altered = copy.deepcopy(products)
            altered[0]["category"] = category
            with self.subTest(category=category), self.assertRaises(CampaignConfigError):
                source_campaign_config(base, summary, altered, 8, "2026-10-19")
        altered = copy.deepcopy(summary)
        altered["selection_policy"]["selection_mode"] = "category_quota"
        with self.assertRaises(CampaignConfigError):
            source_campaign_config(base, altered, products, 8, "2026-10-19")
        for bad in [[], [None], "products", products * 2]:
            with self.subTest(products=bad), self.assertRaises(CampaignConfigError):
                source_campaign_config(base, summary, bad, 8, "2026-10-19")


if __name__ == "__main__":
    unittest.main()
