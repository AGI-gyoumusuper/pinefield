"""Synthetic Git and offline selection checks; no real ledgers or network."""
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from new40_history import (HISTORY_GIT_PATH, HISTORY_SCHEMA, New40HistoryError,
                           empty_history, merge_verified_publication,
                           read_new40_selection_context, validate_history)
from product_identity import ProductIdentityRegistry, extract_product_identity
import scraper

DAY = "2026-10-24"
COMMIT = "a" * 40
ASIN = "B000000001"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def seal(manifest):
    result = copy.deepcopy(manifest)
    result.pop("manifest_sha256", None)
    result["manifest_sha256"] = hashlib.sha256(encode(result)).hexdigest()
    return result


def product(asin=ASIN, price=12000, model="TESTMODEL"):
    return scraper.Product(asin=asin, title="Fixture product", price=f"￥{price:,}", price_int=price,
        original_price="￥15,000", discount_rate="20%OFF", image_url="https://example.invalid/image.png",
        affiliate_url=f"https://www.amazon.co.jp/dp/{asin}?tag=fixture201-22", category="A#1",
        rating="4.0", review_count="100", description="Synthetic fixture", specs=f"ブランド: ACME\n型番: {model}")


def manifest(day=DAY):
    rows = []
    for index in range(40):
        source, position = [1, 2, 4, 15][index // 10], index % 10 + 1
        destination = 201 + index
        p = asdict(product())
        rows.append({"planning_slot": f"N{index+1:02d}", "destination_account": destination,
            "source_account": source, "source_position": position, "asin": ASIN,
            "enabled": index == 0, "input_ready": index == 0, "publish_ready": False,
            "destination_note_url": f"https://note.com/fixture{destination}",
            "destination_registration_saved_verified": True, "canonical_input": p if index == 0 else None,
            "price_check": {"eligible": True, "existing_price_floor_verified": True}, "hold_reasons": [],
            "source": {"source_account": source, "source_position": position, "asin": ASIN,
                "commit": COMMIT, "files": {"products": {"sha256": "b" * 64}}},
            "history_check": {"schema": "note40_history_intake.v1", "verified": True,
                "history_git_path": HISTORY_GIT_PATH, "source_commit": COMMIT, "source_account": source,
                "source_products_sha256": "b" * 64, "current_history_sha256": "d" * 64, "held_source_positions": []},
            "amazon_store_id": "fixture-store-22", "amazon_tracking_id": "fixture201-22",
            "canonical_affiliate_url": p["affiliate_url"],
            "affiliate_evidence": {"amazon_store_id": "fixture-store-22", "amazon_tracking_id": "fixture201-22",
                "canonical_affiliate_url": p["affiliate_url"], "verification_evidence": {"sha256": "c" * 64},
                "verified_at_jst": day + "T09:00:00+09:00"}})
    return seal({"schema": "note40_daily_product_allocation.v1", "mode": "LOCAL_INPUT_PREPARATION_ONLY",
                 "date": day, "source_commit": COMMIT, "slots": rows})


def receipt(m, status="published", note_key="nfixture01", event_at=None):
    row = m["slots"][0]
    result = {"schema": "note40_verified_publication.v1", **{k: row[k] for k in
        ("planning_slot", "destination_account", "source_account", "source_position", "asin")},
        "manifest_sha256": m["manifest_sha256"], "source_commit": m["source_commit"], "status": status,
        "note_key": note_key, "note_url": row["destination_note_url"] + "/n/" + note_key,
        "verification": {"schema": "note40_note_readback.v1", "status": "VERIFIED_READBACK",
            "management_list_confirmed": True, "identity_verified": True, "destination_account": row["destination_account"],
            "note_key": note_key, "note_url": row["destination_note_url"] + "/n/" + note_key,
            "publication_status": status, "checked_at": m["date"] + "T12:01:00+09:00"}}
    result["published_at" if status == "published" else "reserved_at"] = event_at or m["date"] + "T12:00:00+09:00"
    return result


class MergeTests(unittest.TestCase):
    def test_pure_idempotent_and_keeps_identity_without_claiming_publish_ready(self):
        m = manifest(); r = receipt(m); h = empty_history(); before = copy.deepcopy((h, r, m))
        merged = merge_verified_publication(h, r, m)
        self.assertEqual(before, (h, r, m))
        self.assertEqual(merged, merge_verified_publication(merged, r, m))
        self.assertFalse(m["slots"][0]["publish_ready"])
        self.assertEqual(ASIN, merged["posted"][0]["asin"])
        self.assertTrue(merged["posted"][0]["product_identity"]["brand_model_keys"])

    def test_wrong_source_destination_position_asin_and_fingerprint_rejected(self):
        m = manifest()
        for key, value in {"destination_account": 202, "source_account": 2, "source_position": 2,
                           "planning_slot": "N02", "asin": "B000000099", "source_commit": "f" * 40,
                           "manifest_sha256": "0" * 64}.items():
            with self.subTest(key=key):
                r = receipt(m); r[key] = value
                with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), r, m)

    def test_unverified_and_wrong_note_url_are_rejected(self):
        m = manifest()
        for change in (lambda r: r["verification"].update(identity_verified=False),
                       lambda r: r["verification"].update(management_list_confirmed=False),
                       lambda r: r.update(note_url="https://note.com/other/n/nfixture01"),
                       lambda r: r["verification"].update(destination_account=202),
                       lambda r: r.update(status="draft")):
            r = receipt(m); change(r)
            with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), r, m)

    def test_blocked_offline_price_and_missing_history_inputs_are_rejected(self):
        changes = [lambda m: m.update(mode="OFFLINE_SAVED_SNAPSHOT"),
            lambda m: m["slots"][0].update(input_ready=False),
            lambda m: m["slots"][0].update(hold_reasons=["history_missing"]),
            lambda m: m["slots"][0]["history_check"].update(verified=False),
            lambda m: m["slots"][0]["history_check"].update(history_git_path="data/account1/asin_history.json"),
            lambda m: m["slots"][0]["history_check"].update(held_source_positions=[1]),
            lambda m: m["slots"][0]["canonical_input"].update(price="￥8,000", price_int=8000)]
        for change in changes:
            m = manifest(); change(m); m = seal(m)
            with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), receipt(m), m)

    def test_store_and_blank_second_tracking_parameter_are_rejected(self):
        for duplicate_tag in (False, True):
            m = manifest(); row = m["slots"][0]
            if duplicate_tag:
                url = row["canonical_affiliate_url"] + "&tag="
                row["canonical_affiliate_url"] = row["canonical_input"]["affiliate_url"] = row["affiliate_evidence"]["canonical_affiliate_url"] = url
            else:
                row["affiliate_evidence"]["amazon_store_id"] = "different-22"
            m = seal(m)
            with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), receipt(m), m)

    def test_publisher_url_and_dedicated_tracking_contract(self):
        for case in ("bare_host", "default_store", "duplicate_query", "old_url"):
            m = manifest(); row = m["slots"][0]
            url = row["canonical_affiliate_url"]
            if case == "bare_host": url = url.replace("www.amazon", "amazon")
            if case == "duplicate_query": url += "&x=1&x=2"
            if case == "default_store":
                row["amazon_store_id"] = row["amazon_tracking_id"]
                row["affiliate_evidence"]["amazon_store_id"] = row["amazon_tracking_id"]
            if case == "old_url": row["source"]["original_affiliate_url_provenance_only"] = url
            row["canonical_affiliate_url"] = url
            row["canonical_input"]["affiliate_url"] = url
            row["affiliate_evidence"]["canonical_affiliate_url"] = url
            m = seal(m)
            with self.subTest(case=case), self.assertRaises(New40HistoryError):
                merge_verified_publication(empty_history(), receipt(m), m)

    def test_future_verification_and_publication_rejected_but_reservation_allowed(self):
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        m = manifest()
        for field in ("verification", "published_at", "affiliate"):
            r = receipt(m); current = copy.deepcopy(m)
            if field == "verification": r["verification"]["checked_at"] = future
            elif field == "published_at": r["published_at"] = future
            else:
                current["slots"][0]["affiliate_evidence"]["verified_at_jst"] = future
                current = seal(current); r = receipt(current)
            with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), r, current)
        result = merge_verified_publication(empty_history(), receipt(m, status="reserved", event_at=future), m)
        self.assertEqual("reserved", result["posted"][0]["status"])

    def test_note_key_and_same_daily_slot_cannot_be_reassigned(self):
        m = manifest(); original = receipt(m); history = merge_verified_publication(empty_history(), original, m)
        with self.assertRaises(New40HistoryError):
            merge_verified_publication(history, receipt(m, note_key="ndifferent"), m)
        changed = manifest("2026-10-25")
        with self.assertRaises(New40HistoryError): merge_verified_publication(history, receipt(changed), changed)
        new_note = receipt(changed, note_key="nnextday")
        self.assertEqual(2, len(merge_verified_publication(history, new_note, changed)["posted"]))

    def test_later_readback_remains_idempotent_and_reservation_can_publish(self):
        m = manifest(); r = receipt(m, status="reserved")
        history = merge_verified_publication(empty_history(), r, m)
        p = receipt(m); p["verification"]["checked_at"] = "2026-10-25T12:00:00+09:00"
        published = merge_verified_publication(history, p, m)
        self.assertEqual("reserved_at" in published["posted"][0], True)
        self.assertEqual(published, merge_verified_publication(published, p, m))
        with self.assertRaises(New40HistoryError): merge_verified_publication(published, r, m)

    def test_malformed_legacy_schema_and_duplicate_rows_fail_closed(self):
        with self.assertRaises(New40HistoryError): validate_history({"schema": "note-amazon-asin-history-v1", "posted": []})
        m = manifest(); h = merge_verified_publication(empty_history(), receipt(m), m)
        h["posted"].append(copy.deepcopy(h["posted"][0]))
        with self.assertRaises(New40HistoryError): validate_history(h)

    def test_corrupt_identity_types_and_values_fail_closed(self):
        m = manifest(); h = merge_verified_publication(empty_history(), receipt(m), m)
        invalid = [None, {}, {"brand_model_keys": 123, "global_trade_numbers": {}},
            {"brand_model_keys": [123]}, {"brand_model_keys": []},
            {"brand_model_keys": ["malformed"]}, {"brand_model_keys": ["ACME::TESTMODEL", "ACME::TESTMODEL"]},
            {"brand_model_keys": ["acme::test-model"]}, {"global_trade_numbers": "04912345678904"},
            {"global_trade_numbers": ["04912345678905"]}, {"global_trade_numbers": ["4912345678904"]},
            {"brand": 123, "brand_model_keys": ["ACME::TESTMODEL"]},
            {"manufacturer_model": [], "brand_model_keys": ["ACME::TESTMODEL"]},
            {"unknown": "value", "brand_model_keys": ["ACME::TESTMODEL"]}]
        for identity in invalid:
            with self.subTest(identity=identity):
                current = copy.deepcopy(h); current["posted"][0]["product_identity"] = identity
                with self.assertRaises(New40HistoryError): validate_history(current)

    def test_generated_gtin_and_brand_model_identity_preserve_exclusion_keys(self):
        m = manifest(); row = m["slots"][0]
        row["canonical_input"]["specs"] += "\nJAN: 4912345678904"
        m = seal(m); h = merge_verified_publication(empty_history(), receipt(m), m)
        expected = extract_product_identity(row["canonical_input"])
        self.assertTrue(expected.brand_model_keys); self.assertTrue(expected.global_trade_numbers)
        self.assertEqual(expected.to_dict(), h["posted"][0]["product_identity"])
        self.assertEqual(h, validate_history(h))
        self.assertEqual(expected, extract_product_identity(h["posted"][0]))

    def test_readback_before_publication_rejected_but_future_reservation_allowed(self):
        m = manifest(); r = receipt(m)
        r["verification"]["checked_at"] = DAY + "T11:59:59+09:00"
        with self.assertRaises(New40HistoryError): merge_verified_publication(empty_history(), r, m)
        r["verification"]["checked_at"] = r["published_at"]
        self.assertEqual("published", merge_verified_publication(empty_history(), r, m)["posted"][0]["status"])
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        r = receipt(m, status="reserved", event_at=future)
        self.assertEqual("reserved", merge_verified_publication(empty_history(), r, m)["posted"][0]["status"])


class GitFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="new40-history-test-")
        self.repo = Path(self.tmp.name)
        self.git("init", "-q"); self.git("config", "user.name", "fixture"); self.git("config", "user.email", "fixture@example.invalid")
        self.legacy = self.repo / "data/account1/asin_history.json"
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_bytes(b"DO NOT READ synthetic legacy sentinel")
        self.rotation = self.repo / "data/account1/category_rotation.json"
        self.rotation.write_bytes(b"DO NOT READ synthetic cursor sentinel")
        self.commit = self.save_history(empty_history())

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def save_history(self, value):
        p = self.repo / HISTORY_GIT_PATH; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encode(value)); self.git("add", "."); self.git("commit", "-qm", "synthetic history")
        return self.git("rev-parse", "HEAD")

    def context(self, commit=None, **kwargs):
        return read_new40_selection_context(self.repo, commit or self.commit, 1,
            reference_date=kwargs.pop("reference_date", DAY), **kwargs)


class GitHistoryTests(GitFixture, unittest.TestCase):
    def test_pinned_git_blob_ignores_dirty_file_without_reading_legacy(self):
        (self.repo / HISTORY_GIT_PATH).write_text("invalid dirty local file", encoding="utf-8")
        ctx = self.context()
        self.assertEqual(frozenset(), ctx.excluded_asins)
        self.assertEqual(hashlib.sha256(encode(empty_history())).hexdigest(), ctx.history_sha256)
        self.assertEqual(HISTORY_GIT_PATH, ctx.public_evidence()["history_git_path"])
        self.assertEqual(b"DO NOT READ synthetic legacy sentinel", self.legacy.read_bytes())

    def test_missing_bad_schema_and_unpinned_ref_are_rejected(self):
        for commit in ("main", "0" * 40):
            with self.assertRaises(New40HistoryError): self.context(commit)
        invalid = self.save_history({"schema": "wrong", "posted": []})
        with self.assertRaises(New40HistoryError): self.context(invalid)
        self.git("rm", "-q", HISTORY_GIT_PATH); self.git("commit", "-qm", "remove synthetic history")
        with self.assertRaises(New40HistoryError): self.context(self.git("rev-parse", "HEAD"))

    def test_corrupt_pinned_identity_stops_reader_even_when_identity_exclusion_disabled(self):
        m = manifest(); h = merge_verified_publication(empty_history(), receipt(m), m)
        h["posted"][0]["product_identity"] = {"brand_model_keys": 123, "global_trade_numbers": {}}
        commit = self.save_history(h)
        for enabled in (True, False):
            with self.subTest(identity_exclusion=enabled):
                with self.assertRaises(New40HistoryError): self.context(commit, include_product_identities=enabled)

    def test_jst_twenty_day_boundary_future_reservations_and_global_scope(self):
        h = empty_history()
        for day, key, status, event in [
            ("2026-10-04", "nexpired", "published", "2026-10-04T23:59:00+09:00"),
            ("2026-10-05", "ncutoff", "published", "2026-10-04T15:00:00+00:00"),
            ("2026-10-06", "nfuture", "reserved", "2026-11-10T12:00:00+09:00")]:
            m = manifest(day); r = receipt(m, status=status, note_key=key, event_at=event)
            # Each synthetic day has a different ASIN without changing binding relationships.
            asin = {"nexpired": "B000000010", "ncutoff": "B000000011", "nfuture": "B000000012"}[key]
            row = m["slots"][0]
            row["asin"] = row["source"]["asin"] = row["canonical_input"]["asin"] = asin
            url = f"https://www.amazon.co.jp/dp/{asin}?tag=fixture201-22"
            row["canonical_affiliate_url"] = row["canonical_input"]["affiliate_url"] = row["affiliate_evidence"]["canonical_affiliate_url"] = url
            m = seal(m); r = receipt(m, status=status, note_key=key, event_at=event)
            if status == "published":
                r["verification"]["checked_at"] = max(datetime.fromisoformat(r["verification"]["checked_at"]),
                    datetime.fromisoformat(event) + timedelta(minutes=1)).isoformat()
            h = merge_verified_publication(h, r, m)
        commit = self.save_history(h); ctx = self.context(commit)
        self.assertEqual({"B000000011", "B000000012"}, set(ctx.excluded_asins))
        other_source = read_new40_selection_context(self.repo, commit, 2, reference_date=DAY)
        self.assertEqual(ctx.excluded_asins, other_source.excluded_asins)
        self.assertEqual(3, len(self.context(commit, within_days=0).excluded_asins))
        self.assertTrue(ctx.registry.brand_model_keys)
        self.assertFalse(self.context(commit, include_product_identities=False).registry.brand_model_keys)


class SelectionTests(GitFixture, unittest.IsolatedAsyncioTestCase):
    async def run_selection(self, context, *, explicit_context=True):
        config = {"categories": [{"name": "A", "url": "https://example.invalid/a", "is_search": True, "max_items": 10}],
            "filters": {"min_price": 8000, "max_price": 0, "max_total_items": 10, "max_per_category": 0,
                "sort_order": "price_desc", "selection_mode": "category_round_robin", "rotation_state_file": str(self.rotation)},
            "exclusion": {"posted_asins_file": str(self.legacy), "exclude_within_days": 20,
                "exclude_scraped_candidates": False, "exclude_product_identifiers": True}}
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        browser_context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=browser_context), close=AsyncMock())
        manager = MagicMock(); manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        pool = [product(), product("B000000002", 13000, "DIFFERENT")]
        options = {"new40_selection_context": context} if explicit_context else {}
        new_mode = context is not None
        with patch("scraper.load_config", return_value=config), patch("scraper.async_playwright", return_value=manager), \
             patch("scraper.asyncio.sleep", new=AsyncMock()), \
             patch("scraper.scrape_search", new=AsyncMock(return_value=pool)) as search, \
             patch("scraper.load_product_exclusion_registry", side_effect=AssertionError("legacy history read") if new_mode else None,
                   return_value=ProductIdentityRegistry()) as legacy, \
             patch("scraper.load_rotation_state", side_effect=lambda p: {} if (not new_mode or p == "") else (_ for _ in ()).throw(AssertionError("legacy cursor read"))) as rotation:
            selected, stats = await scraper.fetch_products("categories1.yaml", "" if new_mode else "noteamazon1-22", DAY, **options)
        return selected, stats, search, legacy, rotation

    async def test_git_merge_commit_then_excludes_same_asin_before_selection(self):
        m = manifest(); h = merge_verified_publication(empty_history(), receipt(m), m)
        commit = self.save_history(h); ctx = self.context(commit)
        selected, stats, search, legacy, rotation = await self.run_selection(ctx)
        self.assertIn(ASIN, search.await_args.kwargs["excluded"])
        self.assertEqual(["B000000002"], [p.asin for p in selected])
        self.assertTrue(all(p.affiliate_url == "" for p in selected))
        self.assertEqual(ctx.public_evidence(), stats["_new40_selection"])
        self.assertEqual(1, stats["_new40_selection"]["excluded_count"])
        legacy.assert_not_called(); rotation.assert_called_with("")
        self.assertEqual(b"DO NOT READ synthetic legacy sentinel", self.legacy.read_bytes())
        self.assertEqual(b"DO NOT READ synthetic cursor sentinel", self.rotation.read_bytes())
        self.assertEqual("", self.git("status", "--porcelain"))

    async def test_legacy_omitted_and_explicit_none_keep_same_behavior(self):
        implicit = await self.run_selection(None, explicit_context=False)
        explicit = await self.run_selection(None)
        self.assertEqual([asdict(p) for p in implicit[0]], [asdict(p) for p in explicit[0]])
        self.assertEqual(implicit[1], explicit[1])
        self.assertNotIn("_new40_selection", explicit[1])
        explicit[3].assert_called_once_with(str(self.legacy), 20, False, include_product_identities=True, reference_date=DAY)
        explicit[4].assert_called_with(str(self.rotation))

    async def test_wrong_source_window_date_or_unvalidated_context_stops_before_browser(self):
        context = self.context()
        with patch("scraper.async_playwright", side_effect=AssertionError("browser must not start")):
            with self.assertRaises(ValueError):
                await scraper.fetch_products("categories1.yaml", "", DAY, new40_selection_context={})
            with self.assertRaises(ValueError):
                await scraper.fetch_products("categories1.yaml", "noteamazon1-22", DAY, new40_selection_context=context)
            for field, value in [("source_account", 2), ("reference_date", "2026-10-25"), ("within_days", 19), ("include_product_identities", False)]:
                args = dict(source_account=1, reference_date=DAY, within_days=20, include_product_identities=True); args[field] = value
                with self.assertRaises(New40HistoryError): context.registry_for_selection(**args)


if __name__ == "__main__":
    unittest.main()
