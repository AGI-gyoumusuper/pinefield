"""Offline integration checks for campaign source creation and reuse."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ensure_daily_scrape as daily
import scraper
from sale_campaign import apply_campaign_config
from scripts import recover_local_daily_source as recovery

DAY = "2026-10-14"


def campaign_source(account=20):
    base = yaml.safe_load((ROOT / f"categories{account}.yaml").read_text(encoding="utf-8-sig"))
    effective = apply_campaign_config(base, f"categories{account}.yaml", DAY)
    marker = effective["_sale_campaign"]
    # This regression fixture represents the already sealed v1 source contract.
    marker["policy"] = "prime_appreciation_2026_electronics_v1"
    marker["config_sha256"] = "6161f0c6ae9828bd7de6f6b8f1c06ffacb0245afff2744e4ad9392d7c37b1e6c"
    products = [asdict(scraper.Product(
        asin=f"B{index:09d}", title=f"Product {index}", price="8,000円", price_int=8000,
        original_price="10,000円", discount_rate="20%", image_url="https://example.com/image.jpg",
        affiliate_url=f"https://www.amazon.co.jp/dp/B{index:09d}?tag=noteamazon{account}-22",
        category=category["name"] + "#1", rating="4.5", review_count="100",
    )) for index, category in enumerate(marker["categories"], 1)]
    summary = {"date": DAY, "campaign": marker, "total_taken": len(products),
               "categories": {category["name"]: {} for category in marker["categories"]},
               "selection_policy": {"selection_mode": "global_ranked", "sort_order": "sale_first",
                                    "require_sale_info": True, "max_per_category": 2, "max_total_items": 10}}
    return base, effective, products, summary


def write_source(root, account, base, products, summary):
    folder = root / "data" / f"account{account}"
    folder.mkdir(parents=True, exist_ok=True)
    (root / f"categories{account}.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (folder / f"products_{DAY}.json").write_text(json.dumps(products), encoding="utf-8")
    (folder / f"scrape_summary_{DAY}.json").write_text(json.dumps(summary), encoding="utf-8")
    (folder / "asin_history.json").write_text(json.dumps({
        "schema": "note-amazon-asin-history-v1", "posted": []}), encoding="utf-8")


class CampaignPipelineTests(unittest.TestCase):
    def test_all_sites_accept_campaign_source_without_bypassing_offer_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for account in range(1, 21):
                with self.subTest(account=account):
                    base, _, products, summary = campaign_source(account)
                    write_source(root, account, base, products, summary)
                    with patch.object(daily, "validate_detail_offer_summary", return_value=(True, "verified")) as offers:
                        self.assertTrue(daily.validate(f"account{account}", root, DAY)[0])
                        offers.assert_called_once()

    def test_invalid_campaign_source_is_rejected_before_offer_validation(self):
        base, _, products, summary = campaign_source()
        for mutation in ("price", "marker", "missing_marker"):
            changed_products, changed_summary = copy.deepcopy(products), copy.deepcopy(summary)
            if mutation == "price":
                changed_products[0]["price_int"] = 7999
            elif mutation == "marker":
                changed_summary["campaign"]["config_sha256"] = "0" * 64
            else:
                del changed_summary["campaign"]
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_source(root, 20, base, changed_products, changed_summary)
                with patch.object(daily, "validate_detail_offer_summary") as offers:
                    self.assertFalse(daily.validate("account20", root, DAY)[0])
                    offers.assert_not_called()

    def test_account20_generic_readiness_preserves_ranked_positions(self):
        _, effective, products, summary = campaign_source()
        # Four products from the same assigned electronics shelf are still four slots.
        for index, product in enumerate(products, 1):
            product["category"] = effective["categories"][0]["name"] + f"#{index}"
        objects = [scraper.Product(**product) for product in products]
        before = copy.deepcopy(products)
        self.assertEqual(4, scraper.daily_supply_slots(objects, 20, effective["categories"], "global_ranked"))
        self.assertEqual(4, recovery.postable_count(20, products, summary))
        self.assertEqual(before, products)
        self.assertEqual(0, recovery.postable_count(20, products, {"date": DAY}))
        games = copy.deepcopy(products)
        for product in games:
            product["category"] = "Nintendo Switch 2#1"
        self.assertEqual(2, recovery.postable_count(20, games, {"date": DAY}))

    def test_recovery_never_mixes_legacy_and_campaign_sources(self):
        _, _, products, summary = campaign_source()
        legacy = copy.deepcopy(summary)
        del legacy["campaign"]
        with self.assertRaisesRegex(recovery.RecoveryStop, "supplement_campaign_allocation_changed"):
            recovery.merge_short_source(20, products[:1], legacy, products, summary, None)

    def test_saved_campaign_marker_keeps_raw_product_schema_and_summary_separation(self):
        _, _, products, summary = campaign_source()
        # Saving new output follows the current floor; the historical 8000-yen
        # source fixtures used by the replay tests above remain untouched.
        for product in products:
            product.update(price="12,000円", price_int=12000, original_price="15,000円")
        objects = [scraper.Product(**product) for product in products]
        stats = {**copy.deepcopy(summary["categories"]), "_campaign": summary["campaign"],
                 "_selection_policy": summary["selection_policy"]}
        async def fetch(config_path, associate_tag, reference_date):
            self.assertEqual("categories20.yaml", config_path)
            self.assertEqual(DAY, reference_date)
            return objects, copy.deepcopy(stats)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data" / "account20" / f"products_{DAY}.json"
            with patch.object(scraper, "fetch_products", side_effect=fetch), \
                 patch.object(scraper, "save_scraped_asins_to_history") as history:
                scraper.fetch_and_save(str(output), "categories20.yaml", "noteamazon20-22")
                history.assert_called_once()
            saved = json.loads(output.read_text(encoding="utf-8"))
            saved_summary = json.loads(output.with_name(f"scrape_summary_{DAY}.json").read_text(encoding="utf-8"))
            self.assertEqual(products, saved)
            self.assertTrue(all(len(product) == 13 for product in saved))
            self.assertEqual(summary["campaign"], saved_summary["campaign"])
            self.assertNotIn("_campaign", saved_summary["categories"])
            self.assertEqual(summary["categories"], saved_summary["categories"])


if __name__ == "__main__":
    unittest.main()
