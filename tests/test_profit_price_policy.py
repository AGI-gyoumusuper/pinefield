"""Regression checks for expensive-first selection and sealed-source continuity."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
import ensure_daily_scrape
from product_identity import ProductIdentityRegistry
from sale_campaign import (CAMPAIGN_POLICY, PRIME_V2_POLICY, PRIME_CONFIG_SHA256,
                           LEGACY_CAMPAIGN_POLICY, LEGACY_CONFIG_SHA256,
                           source_campaign_config)
from scraper import filter_and_sort, select_enrich_unique_products
from test_prime_campaign import prime_source
from test_sale_ranking import product

ROOT = Path(__file__).resolve().parents[1]


class PriceRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_prices_win_over_discount_percent_and_same_shelf_cap(self):
        candidates = [product(f"HIGH{i}", "A", "5%OFF", price=100000-i*1000) for i in range(5)]
        candidates += [product("BIGDISCOUNT", "B", "90%OFF", price=8000),
                       product("CHEAP", "B", "99%OFF", price=7999)]
        eligible = filter_and_sort(candidates,min_price=8000,max_price=0,sort_order="price_desc",
                                   max_total=10,max_per_category=0,selection_mode="global_ranked")
        with patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await select_enrich_unique_products(None, eligible, ProductIdentityRegistry(),
                [{"name":"A"},{"name":"B"}], 4, 0, "global_ranked", "", {},
                sort_order="price_desc",pre_enriched_asins={p.asin for p in eligible})
        self.assertEqual(["HIGH0","HIGH1","HIGH2","HIGH3"],[p.asin for p in selected])
        self.assertNotIn("CHEAP",[p.asin for p in eligible])

    def test_floor_is_sales_price_not_reference_and_stricter_shelf_survives(self):
        candidates=[product("FAKEHIGH", "A", "80%OFF", price=4000,original="￥20,000"),
                    product("EXACT", "A", "10%OFF", price=8000),
                    product("UNDER_SHELF", "VR", "10%OFF", price=19999),
                    product("AT_SHELF", "VR", "10%OFF", price=20000),
                    product("EXPENSIVE", "A", "5%OFF", price=999999)]
        selected=filter_and_sort(candidates,min_price=8000,max_price=0,sort_order="price_desc",
                                 category_min_prices={"VR":20000},max_total=10,max_per_category=0)
        self.assertEqual(["EXPENSIVE","AT_SHELF"],[p.asin for p in selected])

    def test_shortage_does_not_relax_price_floor_and_game_shelves_remain(self):
        candidates=[product("A1","Nintendo Switch 2","5%OFF",price=12000),
                    product("A2","Nintendo Switch 2","90%OFF",price=8000),
                    product("A3","Nintendo Switch 2","99%OFF",price=7000),
                    product("B1","PS5ゲームソフト","5%OFF",price=20000),
                    product("B2","PS5ゲームソフト","90%OFF",price=9000)]
        selected=filter_and_sort(candidates,min_price=8000,max_price=0,sort_order="price_desc",
              max_total=10,max_per_category=5,selection_mode="category_quota",
              categories=[{"name":"Nintendo Switch 2"},{"name":"PS5ゲームソフト"}])
        self.assertEqual(["A1","B1","B2"],[p.asin for p in selected])


class FixedSourceTests(unittest.TestCase):
    def source(self,day,legacy=None):
        base,products,summary,_=prime_source(day,day+"T10:00:00+09:00","プライム感謝祭")
        if legacy:
            summary["campaign"]["policy"]=legacy
            summary["campaign"]["config_sha256"]=PRIME_CONFIG_SHA256 if legacy==PRIME_V2_POLICY else LEGACY_CONFIG_SHA256
            summary["selection_policy"].pop("selection_policy_version")
            summary["selection_policy"].update(sort_order="sale_first",max_per_category=2)
            if legacy==LEGACY_CAMPAIGN_POLICY:
                summary["selection_policy"].pop("require_prime_event")
                summary["discount_contract"]["products"][0].pop("prime_event")
        return base,products,summary

    def validate_saved(self,day,legacy=None,sealed=False,remove_proof=False):
        base,products,summary=self.source(day,legacy)
        if remove_proof:
            summary["discount_contract"]["products"][0].pop("prime_event",None)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'data/account8';folder.mkdir(parents=True)
            (root/'categories8.yaml').write_text(yaml.safe_dump(base),encoding='utf-8')
            for name in ['detail_offer.py','sale_campaign.yaml']:
                shutil.copyfile(ROOT/name,root/name)
            (folder/'asin_history.json').write_text(json.dumps({'schema':'note-amazon-asin-history-v1','posted':[]}),encoding='utf-8')
            pp=folder/f'products_{day}.json';sp=folder/f'scrape_summary_{day}.json'
            pp.write_text(json.dumps(products),encoding='utf-8');sp.write_text(json.dumps(summary),encoding='utf-8')
            if sealed:
                (folder/'source_manifest.json').write_text(json.dumps({'schema':1,'account':8,'date':day,
                    'products_sha256':hashlib.sha256(pp.read_bytes()).hexdigest(),
                    'summary_sha256':hashlib.sha256(sp.read_bytes()).hexdigest()}),encoding='utf-8')
            return ensure_daily_scrape.validate('account8',root,day)

    def test_oct17_verified_v2_survives_without_factory_manifest(self):
        ok,reason=self.validate_saved('2026-10-17',PRIME_V2_POLICY)
        self.assertTrue(ok,reason)

    def test_oct17_unsealed_v1_and_v2_without_prime_proof_stay_rejected(self):
        self.assertFalse(self.validate_saved('2026-10-17',LEGACY_CAMPAIGN_POLICY)[0])
        self.assertFalse(self.validate_saved('2026-10-17',PRIME_V2_POLICY,remove_proof=True)[0])

    def test_oct18_fresh_v3_required_but_sealed_v1_v2_survive(self):
        self.assertTrue(self.validate_saved('2026-10-18')[0])
        for legacy in [PRIME_V2_POLICY,LEGACY_CAMPAIGN_POLICY]:
            self.assertFalse(self.validate_saved('2026-10-18',legacy)[0])
            ok,reason=self.validate_saved('2026-10-18',legacy,sealed=True)
            self.assertTrue(ok,reason)

    def test_v3_proof_and_version_are_required_and_replay_is_immutable(self):
        base,products,summary=self.source('2026-10-18')
        before=copy.deepcopy((base,products,summary))
        self.assertEqual(CAMPAIGN_POLICY,source_campaign_config(base,summary,products,8,'2026-10-18')['_sale_campaign']['policy'])
        self.assertEqual(before,(base,products,summary))
        self.assertFalse(self.validate_saved('2026-10-18',remove_proof=True)[0])
        summary['selection_policy'].pop('selection_policy_version')
        with self.assertRaises(ValueError):
            source_campaign_config(base,summary,products,8,'2026-10-18')


if __name__=='__main__': unittest.main()
