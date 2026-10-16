"""Prime-only acquisition, sealed compatibility, evidence replay and phase guards."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import ensure_daily_scrape
import scraper
from detail_offer import (discount_row, observation_sha256, observe_detail_offer,
                          prime_event_proof, validate_discount_contract_summary)
from sale_campaign import (CAMPAIGN_CONFIG_PATH, CAMPAIGN_POLICY, LEGACY_CAMPAIGN_POLICY,
                           LEGACY_CONFIG_SHA256, CampaignConfigError, apply_campaign_config,
                           source_campaign_config)
from scripts import recover_local_daily_source
from test_detail_offer import ASIN, evidence, observed, product
from test_discount_integration import record_for

ROOT = Path(__file__).resolve().parents[1]


def prime_source(day="2026-10-15", checked="2026-10-13T03:00:00+00:00", label="プライム感謝祭先行セール", account=8):
    base = yaml.safe_load((ROOT / f"categories{account}.yaml").read_text(encoding="utf-8-sig"))
    config = apply_campaign_config(base, f"categories{account}.yaml", day)
    marker = config["_sale_campaign"]
    candidate = product(category=marker["categories"][0]["name"], price=8000, rate="20%OFF")
    candidate.original_price = "￥10,000"
    candidate.affiliate_url = scraper.make_affiliate_url(candidate.asin, f"noteamazon{account}-22")
    value = evidence(price=8000, original=10000, rate=20, label=label)
    value["checked_at"] = checked
    record = record_for(candidate, value)
    row = discount_row(candidate, record, 1, prime_source_date=day)
    summary = dict(date=day, campaign=marker, total_taken=1,
        categories={category["name"]: {} for category in marker["categories"]},
        selection_policy=dict(selection_mode="global_ranked", sort_order="price_desc", require_sale_info=True,
            selection_policy_version="profit_price_desc_v1", min_price=8000, max_price=0,
            max_per_category=0, max_total_items=10, require_prime_event=True, verify_detail_offer=True,
            offer_scope="unified_discounts", discount_contract="amazon_direct_discounts_v1"),
        detail_offer_verification=dict(schema_version=1, enabled=True, offer_scope="unified_discounts",
            candidate_count=1, accepted_count=1, rejected_count=0, observations=[record]),
        discount_contract=dict(schema_version=1, policy="amazon_direct_discounts_v1", account=account, date=day, products=[row]))
    return base, [asdict(candidate)], summary, record


class PrimeProofTests(unittest.TestCase):
    def test_exact_main_sale_aliases_preserve_raw_evidence_and_canonical_proof(self):
        for label in ("プライム感謝祭セール", "Amazonプライム感謝祭セール"):
            with self.subTest(label=label):
                base, products, summary, record = prime_source("2026-10-16", "2026-10-16T00:10:00+09:00", label)
                before = copy.deepcopy(record)
                proof = prime_event_proof(record, "2026-10-16")
                self.assertEqual(("main", "プライム感謝祭"), (proof["phase"], proof["label"]))
                self.assertEqual(label, record["evidence"]["sale_label"])
                self.assertEqual(before, record)
                self.assertEqual(observation_sha256(record["evidence"]), proof["evidence_sha256"])
                self.assertTrue(validate_discount_contract_summary("account8", products, summary)[0])
                self.assertEqual(summary["campaign"], source_campaign_config(base, summary, products, 8, "2026-10-16")["_sale_campaign"])

    def test_main_alias_does_not_relax_badge_identity_phase_or_price_floor(self):
        _, _, _, record = prime_source("2026-10-16", "2026-10-16T00:10:00+09:00", "プライム感謝祭セール")
        from detail_offer import validate_offer
        for field, value in (("requested_asin", "B000000099"), ("sale_region_count", 2),
                             ("sale_region_selector", "#recommendations"),
                             ("checked_at", "2026-10-15T23:59:59+09:00"), ("http_status", 503)):
            with self.subTest(field=field):
                changed = copy.deepcopy(record)
                changed["evidence"][field] = value
                if field == "checked_at":
                    changed["checked_at"] = value
                changed["pdp_offer"], _ = validate_offer(changed["evidence"], offer_scope="unified_discounts")
                changed["evidence_sha256"] = observation_sha256(changed["evidence"])
                with self.assertRaises(ValueError):
                    prime_event_proof(changed, "2026-10-16")
        for day in ("2026-10-15", "2026-10-20"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                prime_event_proof(record, day)
        base, products, summary, _ = prime_source("2026-10-16", "2026-10-16T00:10:00+09:00", "プライム感謝祭セール")
        products[0]["price_int"] = 7999
        with self.assertRaises(CampaignConfigError):
            source_campaign_config(base, summary, products, 8, "2026-10-16")

    def test_main_sale_alias_is_not_a_generic_prime_or_text_match(self):
        _, _, _, record = prime_source("2026-10-16", "2026-10-16T00:10:00+09:00", "プライム感謝祭")
        from detail_offer import validate_offer
        for label in ("プライム会員セール", "プライム会員限定セール", "プライムセール", "プライム感謝祭セール中",
                      "プライム感謝祭セールについて", "商品タイトル：プライム感謝祭セール", "AmazonPrimeプライム感謝祭セール"):
            with self.subTest(label=label):
                changed = copy.deepcopy(record)
                changed["evidence"]["sale_label"] = label
                changed["pdp_offer"], _ = validate_offer(changed["evidence"], offer_scope="unified_discounts")
                changed["evidence_sha256"] = observation_sha256(changed["evidence"])
                with self.assertRaisesRegex(ValueError, "label_unverified"):
                    prime_event_proof(changed, "2026-10-16")

    def test_early_and_main_boundaries_have_proof_and_keep_thirteen_fields(self):
        for day, label in [("2026-10-13", "プライム感謝祭先行セール"), ("2026-10-15", "プライム感謝祭先行セール"),
                           ("2026-10-16", "プライム感謝祭"), ("2026-10-19", "プライム感謝祭")]:
            with self.subTest(day=day):
                base, products, summary, record = prime_source(day, day + "T03:00:00+09:00", label)
                self.assertEqual(13, len(products[0]))
                self.assertTrue(validate_discount_contract_summary("account8", products, summary)[0])
                self.assertEqual(summary["campaign"], source_campaign_config(base, summary, products, 8, day)["_sale_campaign"])
                self.assertEqual(record["checked_at"], summary["discount_contract"]["products"][0]["prime_event"]["observed_at"])
        self.assertNotIn("_sale_campaign", apply_campaign_config({}, "categories8.yaml", "2026-10-20"))

    def test_original_early_observation_can_be_replayed_for_same_phase_later_date(self):
        _, products, summary, record = prime_source()
        before = copy.deepcopy((products, summary))
        with patch("sale_campaign._today_jst", return_value=__import__("datetime").date(2026, 10, 21)):
            source_campaign_config({}, summary, products, 8, summary["date"])
        self.assertEqual(before, (products, summary))
        self.assertTrue(record["checked_at"].startswith("2026-10-13"))

    def test_early_observation_never_becomes_main_by_target_date(self):
        _, _, _, record = prime_source()
        for day in ("2026-10-12", "2026-10-16", "2026-10-20"):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, "phase_mismatch"):
                prime_event_proof(record, day)

    def test_ordinary_discount_coupon_or_wrong_badge_does_not_prove_prime(self):
        _, _, _, record = prime_source()
        for label in ("タイムセール", "", "プライム感謝祭についての説明", "商品タイトル：プライム感謝祭先行セール"):
            changed = copy.deepcopy(record)
            changed["evidence"]["sale_label"] = label
            # Keep valid ordinary offer evidence, including its recomputed hash.
            from detail_offer import validate_offer
            changed["pdp_offer"], _ = validate_offer(changed["evidence"], offer_scope="unified_discounts")
            changed["evidence_sha256"] = observation_sha256(changed["evidence"])
            with self.subTest(label=label), self.assertRaises(ValueError):
                prime_event_proof(changed, "2026-10-15")

    def test_visible_badge_first_line_may_include_following_countdown(self):
        _, _, _, record = prime_source(label="Amazon プライム感謝祭 先行セール\n終了まで 12時間")
        self.assertEqual("プライム感謝祭先行セール", prime_event_proof(record, "2026-10-15")["label"])

    def test_self_declared_proof_and_record_tampering_are_rejected(self):
        base, products, summary, _ = prime_source()
        for key, value in {"verified": 1, "phase": "main", "source_date": "2026-10-16", "asin": "B000000099",
                           "evidence_sha256": "0" * 64, "observed_at": "2026-10-16T00:00:00+09:00"}.items():
            changed = copy.deepcopy(summary)
            changed["discount_contract"]["products"][0]["prime_event"][key] = value
            with self.subTest(key=key):
                self.assertFalse(validate_discount_contract_summary("account8", products, changed)[0])
                with self.assertRaises(CampaignConfigError):
                    source_campaign_config(base, changed, products, 8, summary["date"])
        changed = copy.deepcopy(summary)
        changed["detail_offer_verification"]["observations"][0]["evidence"]["sale_label"] = "タイムセール"
        self.assertFalse(validate_discount_contract_summary("account8", products, changed)[0])
        changed = copy.deepcopy(summary)
        del changed["discount_contract"]["products"][0]["prime_event"]
        self.assertFalse(validate_discount_contract_summary("account8", products, changed)[0])

    def test_price_floor_and_generic_readiness_require_valid_v2_proof(self):
        _, products, summary, _ = prime_source(account=20)
        self.assertEqual(1, recover_local_daily_source.postable_count(20, products, summary))
        changed = copy.deepcopy(products)
        changed[0]["price_int"] = 7999
        with self.assertRaises(CampaignConfigError):
            source_campaign_config({}, summary, changed, 20, summary["date"])
        changed_summary = copy.deepcopy(summary)
        changed_summary["discount_contract"]["products"][0]["prime_event"]["verified"] = False
        with self.assertRaises(CampaignConfigError):
            recover_local_daily_source.postable_count(20, products, changed_summary)

    def test_restoring_yaml_v1_keeps_v2_frozen_source_readable(self):
        base, products, summary, _ = prime_source()
        legacy_yaml = yaml.safe_load(CAMPAIGN_CONFIG_PATH.read_text(encoding="utf-8"))
        legacy_yaml["policy"] = LEGACY_CAMPAIGN_POLICY
        legacy_yaml.update(sort_order="sale_first", max_per_category=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sale_campaign.yaml"
            path.write_text(yaml.safe_dump(legacy_yaml, allow_unicode=True), encoding="utf-8")
            with patch("sale_campaign.CAMPAIGN_CONFIG_PATH", path):
                restored = source_campaign_config(base, summary, products, 8, summary["date"])
                self.assertEqual(CAMPAIGN_POLICY, restored["_sale_campaign"]["policy"])
                new_generic = apply_campaign_config(base, "categories8.yaml", summary["date"])
                self.assertEqual(LEGACY_CONFIG_SHA256, new_generic["_sale_campaign"]["config_sha256"])
                self.assertNotIn("require_prime_event", new_generic["filters"])

    def test_pinned_ensure_accepts_proven_source_and_rejects_missing_proof(self):
        base, products, summary, _ = prime_source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "data/account8"
            folder.mkdir(parents=True)
            (root / "categories8.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
            shutil.copyfile(ROOT / "detail_offer.py", root / "detail_offer.py")
            (folder / "asin_history.json").write_text(json.dumps({"schema": "note-amazon-asin-history-v1", "posted": []}), encoding="utf-8")
            (folder / "products_2026-10-15.json").write_text(json.dumps(products), encoding="utf-8")
            summary_path = folder / "scrape_summary_2026-10-15.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            self.assertTrue(ensure_daily_scrape.validate("account8", root, "2026-10-15")[0])
            del summary["discount_contract"]["products"][0]["prime_event"]
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            self.assertFalse(ensure_daily_scrape.validate("account8", root, "2026-10-15")[0])

    def test_fresh_v1_requires_reacquisition_but_sealed_v1_and_yaml_rollback_continue(self):
        base, products, summary, _ = prime_source()
        summary["campaign"]["policy"] = LEGACY_CAMPAIGN_POLICY
        summary["campaign"]["config_sha256"] = LEGACY_CONFIG_SHA256
        summary["selection_policy"].pop("require_prime_event")
        summary["selection_policy"].pop("selection_policy_version")
        summary["selection_policy"].update(sort_order="sale_first",max_per_category=2)
        summary["discount_contract"]["products"][0].pop("prime_event")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "data/account8"
            folder.mkdir(parents=True)
            (root / "categories8.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
            shutil.copyfile(ROOT / "detail_offer.py", root / "detail_offer.py")
            shutil.copyfile(ROOT / "sale_campaign.yaml", root / "sale_campaign.yaml")
            (folder / "asin_history.json").write_text(json.dumps({"schema": "note-amazon-asin-history-v1", "posted": []}), encoding="utf-8")
            product_path, summary_path = folder / "products_2026-10-15.json", folder / "scrape_summary_2026-10-15.json"
            product_path.write_text(json.dumps(products), encoding="utf-8")
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            valid, message = ensure_daily_scrape.validate("account8", root, "2026-10-15")
            self.assertFalse(valid)
            self.assertIn("unfixed source", message)
            manifest = dict(schema=1, account=8, date="2026-10-15",
                products_sha256=hashlib.sha256(product_path.read_bytes()).hexdigest(),
                summary_sha256=hashlib.sha256(summary_path.read_bytes()).hexdigest())
            (folder / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(ensure_daily_scrape.validate("account8", root, "2026-10-15")[0])
            (folder / "source_manifest.json").unlink()
            legacy_yaml = yaml.safe_load((root / "sale_campaign.yaml").read_text(encoding="utf-8"))
            legacy_yaml["policy"] = LEGACY_CAMPAIGN_POLICY
            legacy_yaml.update(sort_order="sale_first", max_per_category=2)
            (root / "sale_campaign.yaml").write_text(yaml.safe_dump(legacy_yaml), encoding="utf-8")
            self.assertTrue(ensure_daily_scrape.validate("account8", root, "2026-10-15")[0])


class PrimeAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_main_alias_is_accepted_without_rewriting_raw_badge(self):
        for label in ("プライム感謝祭セール", "Amazonプライム感謝祭セール"):
            with self.subTest(label=label):
                page = MagicMock(url=f"https://www.amazon.co.jp/dp/{ASIN}")
                page.goto = AsyncMock(return_value=MagicMock(status=200))
                page.wait_for_timeout = AsyncMock()
                page.evaluate = AsyncMock(side_effect=[observed(price=8000, original=10000, rate=20, label=label), [ASIN]])
                reader = AsyncMock(return_value=("description", "specs"))
                with patch("detail_offer.datetime") as clock:
                    from datetime import datetime, timezone
                    clock.now.return_value = datetime(2026, 10, 15, 16, tzinfo=timezone.utc)
                    clock.fromisoformat.side_effect = datetime.fromisoformat
                    record = await observe_detail_offer(page, product(price=8000), reader,
                        offer_scope="unified_discounts", require_prime_event=True, source_date="2026-10-16")
                self.assertEqual("accepted", record["status"])
                self.assertEqual(label, record["evidence"]["sale_label"])
                self.assertEqual(observation_sha256(record["evidence"]), record["evidence_sha256"])
                self.assertEqual("プライム感謝祭", prime_event_proof(record, "2026-10-16")["label"])
                reader.assert_awaited_once()

    async def test_new_acquisition_checks_prime_before_detail_enrichment(self):
        for label, accepted in [("タイムセール", False), ("プライム感謝祭先行セール", True)]:
            page = MagicMock()
            page.url = f"https://www.amazon.co.jp/dp/{ASIN}"
            page.goto = AsyncMock(return_value=MagicMock(status=200))
            page.wait_for_timeout = AsyncMock()
            page.evaluate = AsyncMock(side_effect=[observed(price=8000, original=10000, rate=20, label=label), [ASIN]])
            reader = AsyncMock(return_value=("description", "specs"))
            with patch("detail_offer.datetime") as clock:
                from datetime import datetime, timezone
                clock.now.return_value = datetime(2026, 10, 14, 3, tzinfo=timezone.utc)
                clock.fromisoformat.side_effect = datetime.fromisoformat
                record = await observe_detail_offer(page, product(price=8000), reader,
                    offer_scope="unified_discounts", require_prime_event=True, source_date="2026-10-15")
            self.assertEqual("accepted" if accepted else "rejected", record["status"])
            self.assertEqual(1 if accepted else 0, reader.await_count)

    async def test_bounded_verifier_forwards_required_event_without_new_retry(self):
        _, _, _, record = prime_source()
        stats = {}
        with patch.object(scraper, "observe_detail_offer", AsyncMock(return_value=record)) as observer:
            await scraper.verify_discount_candidates(None, [product()], stats, require_prime_event=True, source_date="2026-10-15")
        self.assertTrue(observer.call_args.kwargs["require_prime_event"])
        self.assertEqual("2026-10-15", observer.call_args.kwargs["source_date"])
        observer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
