"""Offline regression coverage for one bounded, pre-source-lock supply completion."""
import asyncio
import copy
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import ensure_daily_scrape
import scraper
from detail_offer import DISCOUNT_POLICY, discount_row, validate_discount_contract_summary
from product_identity import ProductIdentityRegistry, extract_product_identity
from test_detail_offer import KEYS, evidence, product
from test_discount_integration import record_for


def candidate(number, *, category="A", price=8000, rate=20, title=None, model=None):
    item = product(f"B{number:09d}", category=category, price=price, rate=f"{rate}%OFF")
    item.original_price = f"￥{round(price / (1-rate/100)):,}"
    item.title = title or f"単品商品 {number}"
    item.specs = f"ブランド名 FIXTUREBRAND\n型番 {model or 'MODEL' + str(number)}"
    return item


def accepted(item):
    rate = int(item.discount_rate.rstrip("%OFF"))
    value = evidence(asin=item.asin, price=item.price_int,
                     original=round(item.price_int / (1-rate/100)), rate=rate, label="")
    value["coupon_regions"] = []
    return record_for(item, value)


def initial_stats(selected, *, extra=None, stop=None):
    observations = [accepted(item) for item in selected] + list(extra or [])
    return {"_detail_offer_verification": dict(
        schema_version=1, enabled=True, offer_scope="unified_discounts", sale_name="Amazon セール",
        candidate_count=len(observations), raw_candidate_count=len(observations),
        accepted_count=len(selected), rejected_count=len(observations)-len(selected),
        rejection_reasons={}, observations=observations,
        limits=dict(max_candidates=24, timeout_seconds=240),
        detail_visits_started=len(observations), elapsed_seconds=1,
        budget_stop_reason=stop)}


class SupplyCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, selected, pool, *, account=1, cats=None, registry=None,
                   stats=None, observer=None, **overrides):
        cats = cats or [dict(name="A"), dict(name="B")]
        registry = registry or ProductIdentityRegistry()
        stats = initial_stats(selected) if stats is None else stats
        for item in selected:
            registry.add_identity(extract_product_identity(item))
        options = dict(account=account, min_price=3000, max_price=0,
                       sort_order="sale_first", max_total=10, min_discount_pct=0,
                       max_per_category=5 if account == 20 else 2,
                       exclude_title_patterns=["セット"], category_min_prices={})
        options.update(overrides)

        async def observe(_page, item, _reader, **kwargs):
            self.assertEqual("unified_discounts", kwargs["offer_scope"])
            return accepted(item)

        with patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=observer or observe)) as pdp, \
             patch("scraper.asyncio.sleep", new=AsyncMock()), \
             patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=AssertionError("unexpected extra detail visit"))), \
             patch("scraper.verify_discount_candidates", wraps=scraper.verify_discount_candidates) as batches:
            result = await scraper.complete_discount_supply(
                None, selected, pool, registry, cats, stats, **options)
        return result, stats, pdp, batches

    async def test_four_existing_items_need_no_extra_pdp_and_are_not_reordered(self):
        selected = [candidate(n) for n in range(1, 5)]
        saved = copy.deepcopy(selected)
        result, _, pdp, batches = await self.call(selected, [candidate(99, rate=50)])
        self.assertEqual(saved, result)
        pdp.assert_not_awaited()
        self.assertEqual(0, batches.call_count)

    async def test_three_existing_items_are_preserved_and_only_unseen_asins_are_read(self):
        selected = [candidate(n) for n in range(1, 4)]
        rejected = dict(asin="B000000004", category="A#1", status="rejected", reason="detail_no_discount")
        stats = initial_stats(selected, extra=[rejected])
        result, stats, pdp, batches = await self.call(
            selected, selected + [candidate(4), candidate(5), candidate(5), candidate(6)], stats=stats)
        self.assertEqual([p.asin for p in selected], [p.asin for p in result[:3]])
        self.assertEqual(4, len(result))
        called = [call.args[1].asin for call in pdp.await_args_list]
        self.assertEqual(len(called), len(set(called)))
        self.assertTrue(set(called).issubset({"B000000005", "B000000006"}))
        self.assertEqual(1, batches.call_count)
        self.assertIn(rejected, stats["_detail_offer_verification"]["observations"])

    async def test_existing_evidence_survives_and_final_discount_contract_replays(self):
        selected = [candidate(1), candidate(2)]
        stats = initial_stats(selected)
        old = copy.deepcopy(stats["_detail_offer_verification"]["observations"])
        result, stats, _, _ = await self.call(selected, [candidate(3), candidate(4)], stats=stats)
        verification = stats["_detail_offer_verification"]
        for item in old:
            self.assertIn(item, verification["observations"])
        observations = {item["asin"]: item for item in verification["observations"] if item["status"] == "accepted"}
        summary = dict(date="2026-09-28",
            selection_policy=dict(offer_scope="unified_discounts", discount_contract=DISCOUNT_POLICY),
            discount_contract=dict(schema_version=1, policy=DISCOUNT_POLICY, account=1, date="2026-09-28",
                products=[discount_row(item, observations[item.asin], index) for index, item in enumerate(result, 1)]),
            detail_offer_verification=verification)
        ok, reason = validate_discount_contract_summary("account1", [asdict(item) for item in result], summary)
        self.assertTrue(ok, reason)

    async def test_no_unseen_pool_preserves_one_usable_item(self):
        selected = [candidate(1)]
        result, _, pdp, _ = await self.call(selected, selected)
        self.assertEqual(selected, result)
        pdp.assert_not_awaited()

    async def test_supplement_exception_keeps_each_original_one_to_three_items_and_evidence(self):
        for count in [1, 2, 3]:
            with self.subTest(count=count):
                selected = [candidate(n) for n in range(1, count + 1)]
                stats = initial_stats(selected)
                saved_items = copy.deepcopy(selected)
                saved_proof = copy.deepcopy(stats["_detail_offer_verification"])
                async def fail(*args, **kwargs):
                    raise RuntimeError("fixture connection ended")
                result, stats, pdp, _ = await self.call(selected, [candidate(99)], stats=stats, observer=fail)
                self.assertEqual(saved_items, result)
                self.assertEqual(saved_proof, stats["_detail_offer_verification"])
                self.assertEqual(1, pdp.await_count)

    async def test_timeout_keeps_original_proof_without_starting_second_new_visit(self):
        selected = [candidate(1), candidate(2)]
        stats = initial_stats(selected)
        saved = copy.deepcopy(stats["_detail_offer_verification"]["observations"])
        async def timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
        result, stats, pdp, _ = await self.call(selected, [candidate(3), candidate(4)], stats=stats, observer=timeout)
        self.assertEqual(selected, result)
        self.assertEqual(saved, stats["_detail_offer_verification"]["observations"])
        self.assertEqual(1, pdp.await_count)
        self.assertEqual("detail_budget_exhausted", stats["_detail_offer_verification"]["budget_stop_reason"])

    async def test_explicit_lower_output_limit_is_respected(self):
        selected = [candidate(1)]
        result, _, pdp, _ = await self.call(selected, [candidate(2), candidate(3), candidate(4)], max_total=3)
        self.assertEqual(selected, result)
        pdp.assert_not_awaited()

    async def test_existing_price_title_history_and_category_floor_filters_are_kept(self):
        selected = [candidate(1), candidate(2), candidate(3)]
        registry = ProductIdentityRegistry(asins={"B000000008"})
        pool = [candidate(4, price=2000), candidate(5, price=12000),
                candidate(6, title="単品ではないセット"), candidate(7, category="B", price=4000),
                candidate(8), candidate(9, price=7000, rate=30), candidate(10, price=8000, rate=20)]
        result, _, pdp, _ = await self.call(selected, pool, registry=registry,
            max_price=10000, category_min_prices={"B":5000})
        visited = [call.args[1].asin for call in pdp.await_args_list]
        self.assertEqual(["B000000009", "B000000010"], visited)
        self.assertEqual([p.asin for p in selected], [p.asin for p in result[:3]])

    async def test_newly_confirmed_price_below_floor_is_not_added(self):
        selected = [candidate(1), candidate(2), candidate(3)]
        async def observer(_page, item, _reader, **kwargs):
            item.price_int = 2000
            item.price = "￥2,000"
            return accepted(item)
        result, _, _, _ = await self.call(selected, [candidate(4)], observer=observer)
        self.assertEqual(selected, result)

    async def test_existing_registered_identity_is_not_self_excluded_but_duplicate_new_model_is(self):
        selected = [candidate(1), candidate(2), candidate(3)]
        stats = initial_stats(selected)
        stats.update(_skipped_product_identity=3, _skipped_product_identity_reasons={"BRAND_MODEL":3})
        result, stats, _, _ = await self.call(selected, [candidate(4, model="MODEL1"), candidate(5)], stats=stats)
        self.assertEqual(["B000000001", "B000000002", "B000000003", "B000000005"], [p.asin for p in result])
        self.assertEqual(4, stats["_skipped_product_identity"])
        self.assertEqual(4, stats["_skipped_product_identity_reasons"]["BRAND_MODEL"])

    async def test_existing_challenge_stops_all_further_detail_visits(self):
        selected = [candidate(1)]
        stats = initial_stats(selected, stop="detail_challenge")
        result, _, pdp, _ = await self.call(selected, [candidate(2)], stats=stats)
        self.assertEqual(selected, result)
        pdp.assert_not_awaited()

    async def test_new_challenge_preserves_old_and_prior_new_success_and_stops_navigation(self):
        selected = [candidate(1)]
        async def observer(_page, item, _reader, **kwargs):
            if item.asin == "B000000003":
                return dict(asin=item.asin, category=item.category, status="rejected", reason="detail_challenge")
            return accepted(item)
        result, stats, pdp, _ = await self.call(selected, [candidate(2), candidate(3), candidate(4)], observer=observer)
        self.assertEqual(["B000000001", "B000000002"], [p.asin for p in result])
        self.assertEqual(2, pdp.await_count)
        self.assertEqual("detail_challenge", stats["_detail_offer_verification"]["budget_stop_reason"])

    async def test_additional_batch_has_at_most_twenty_four_visits_and_does_not_loop(self):
        selected = [candidate(1)]
        async def observer(_page, item, _reader, **kwargs):
            return dict(asin=item.asin, category=item.category, status="rejected", reason="detail_no_discount")
        result, _, pdp, batches = await self.call(selected, [candidate(n) for n in range(2, 70)], observer=observer)
        self.assertEqual(selected, result)
        self.assertEqual(24, pdp.await_count)
        self.assertEqual(1, batches.call_count)
        self.assertLessEqual(batches.call_args.kwargs.get("max_candidates", 24), 24)
        self.assertLessEqual(batches.call_args.kwargs.get("timeout_seconds", 240), 240)

    async def test_rejection_counts_and_observations_accumulate_across_both_batches(self):
        selected = [candidate(1)]
        rejected = dict(asin="B000000002", category="A#1", status="rejected", reason="detail_no_discount")
        stats = initial_stats(selected, extra=[rejected])
        stats["_detail_offer_verification"]["rejection_reasons"] = {"detail_no_discount":1}
        async def reject(_page, item, _reader, **kwargs):
            return dict(asin=item.asin, category=item.category, status="rejected", reason="detail_no_discount")
        _, stats, _, _ = await self.call(selected, [candidate(3)], stats=stats, observer=reject)
        verification = stats["_detail_offer_verification"]
        self.assertEqual(3, len(verification["observations"]))
        self.assertEqual(3, verification["candidate_count"])
        self.assertEqual(1, verification["accepted_count"])
        self.assertEqual(2, verification["rejected_count"])
        self.assertEqual({"detail_no_discount":2}, verification["rejection_reasons"])

    async def test_account20_two_per_shelf_is_sufficient_without_additional_visits(self):
        selected = [candidate(1, category="A"), candidate(2, category="A"),
                    candidate(3, category="B"), candidate(4, category="B")]
        result, _, pdp, _ = await self.call(selected, [candidate(5, category="B")], account=20)
        self.assertEqual(selected, result)
        pdp.assert_not_awaited()

    async def test_account20_short_shelf_is_filled_instead_of_already_full_shelf(self):
        selected = [candidate(n, category="A") for n in range(1, 5)]
        result, _, pdp, _ = await self.call(selected,
            [candidate(5, category="A", rate=50), candidate(6, category="B"), candidate(7, category="B")], account=20)
        visited = {call.args[1].category.split("#")[0] for call in pdp.await_args_list}
        self.assertEqual({"B"}, visited)
        counts = Counter(p.category.split("#")[0] for p in result)
        self.assertGreaterEqual(counts["A"], 2)
        self.assertGreaterEqual(counts["B"], 2)
        self.assertLessEqual(len(result), 10)

    async def test_account20_no_candidate_for_short_shelf_does_not_visit_full_shelf(self):
        selected = [candidate(n, category="A") for n in range(1, 5)]
        result, _, pdp, _ = await self.call(selected, [candidate(5, category="A")], account=20)
        self.assertEqual(selected, result)
        pdp.assert_not_awaited()

    async def test_account20_full_ten_can_replace_excess_tail_for_missing_second_shelf_slot(self):
        selected = [candidate(n, category="A") for n in range(1, 10)] + [candidate(10, category="B")]
        result, _, _, _ = await self.call(selected, [candidate(11, category="B")], account=20)
        self.assertEqual(10, len(result))
        self.assertEqual(2, sum(p.category.startswith("B#") for p in result))
        self.assertTrue({"B000000001", "B000000002", "B000000010", "B000000011"}.issubset({p.asin for p in result}))
        self.assertNotIn("B000000009", {p.asin for p in result})

    async def test_account20_missing_first_shelf_also_replaces_only_overfull_tail(self):
        selected = [candidate(n, category="B") for n in range(1, 11)]
        result, _, _, _ = await self.call(selected, [candidate(11, category="A"), candidate(12, category="A")], account=20)
        self.assertEqual(10, len(result))
        self.assertEqual(2, sum(p.category.startswith("A#") for p in result))
        keys = {p.asin for p in result}
        self.assertTrue({"B000000001", "B000000002", "B000000011", "B000000012"}.issubset(keys))
        self.assertFalse({"B000000009", "B000000010"} & keys)


class SupplyCompletionIntegrationTests(unittest.TestCase):
    def test_fetch_save_summary_and_downstream_validator_keep_completed_four_and_all_evidence(self):
        """Run both real PDP batches and final contract, without any external calls."""
        pool = [candidate(n, category="A" if n % 2 else "B") for n in range(1, 31)]
        for item in pool:
            item.affiliate_url = scraper.make_affiliate_url(item.asin, "noteamazon1-22")
        categories = [dict(name=name,
            url=f"https://www.amazon.co.jp/s?rh=n%3A{index}%2Cp_n_deal_type%3A10343616051",
            is_search=True, max_items=20) for index, name in enumerate(("A", "B"), 1)]
        config = dict(categories=categories, filters=dict(
            verify_detail_offer=True, offer_scope="unified_discounts", discount_contract=DISCOUNT_POLICY,
            sort_order="sale_first", selection_mode="global_ranked", min_price=3000,
            max_total_items=10, max_per_category=2), exclusion=dict(
            exclude_product_identifiers=True, exclude_scraped_candidates=False,
            exclude_within_days=20, posted_asins_file="data/account1/asin_history.json"))
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = MagicMock()
        manager.__aenter__.return_value = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        visited = []

        async def observer(_page, item, _reader, **kwargs):
            visited.append(item.asin)
            # Exactly one of the first 24 search candidates has a verified offer.
            if 1 < len(visited) <= 24:
                return dict(asin=item.asin, category=item.category, status="rejected", reason="detail_no_discount")
            return accepted(item)

        with tempfile.TemporaryDirectory(prefix="supply-contract-") as directory:
            root = Path(directory)
            target = root / "data" / "account1"
            target.mkdir(parents=True)
            config_path = root / "categories1.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            history = target / "asin_history.json"
            history.write_text(json.dumps(dict(schema="note-amazon-asin-history-v1", posted=[])), encoding="utf-8")
            history_before = history.read_bytes()
            parser = Path(scraper.__file__).with_name("detail_offer.py")
            (root / parser.name).write_bytes(parser.read_bytes())
            output = target / "products_2026-09-28.json"
            with patch("scraper.load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
                 patch("scraper.async_playwright", return_value=manager), \
                 patch("scraper.asyncio.sleep", new=AsyncMock()), \
                 patch("scraper.scrape_search", new=AsyncMock(side_effect=[pool[::2], pool[1::2], [], []])), \
                 patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=observer)), \
                 patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=AssertionError("unexpected unverified enrichment"))), \
                 patch("scraper.verify_discount_candidates", wraps=scraper.verify_discount_candidates) as batches:
                result = scraper.fetch_and_save(str(output), str(config_path), "noteamazon1-22")

            self.assertEqual(2, batches.call_count)
            self.assertEqual(24, len(batches.call_args_list[0].args[1]))
            self.assertEqual(6, len(batches.call_args_list[1].args[1]))
            self.assertEqual(30, len(visited))
            self.assertEqual(30, len(set(visited)))
            self.assertEqual(4, len(result))
            self.assertEqual(visited[0], result[0].asin)
            products = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(all(set(item) == KEYS for item in products))
            summary = json.loads((target / "scrape_summary_2026-09-28.json").read_text(encoding="utf-8"))
            completion = summary["supply_completion"]
            self.assertEqual(1, completion["ready_before"])
            self.assertEqual(4, completion["ready_after"])
            self.assertEqual(1, completion["extra_batch_count"])
            self.assertEqual(3, len(completion["added_asins"]))
            self.assertNotIn("supply_completion", summary["categories"])
            self.assertNotIn("_supply_completion", summary["categories"])
            verification = summary["detail_offer_verification"]
            self.assertEqual(30, verification["candidate_count"])
            self.assertEqual(7, verification["accepted_count"])
            self.assertEqual(23, verification["rejected_count"])
            self.assertEqual(visited, [row["asin"] for row in verification["observations"]])
            self.assertEqual([item.asin for item in result],
                [row["asin"] for row in summary["discount_contract"]["products"]])
            ok, reason = validate_discount_contract_summary("account1", products, summary)
            self.assertTrue(ok, reason)
            ok, reason = ensure_daily_scrape.validate("account1", root, "2026-09-28")
            self.assertTrue(ok, reason)
            self.assertEqual(history_before, history.read_bytes())
            browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
