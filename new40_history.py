"""Verified new40 publication history, isolated from all legacy account ledgers.

History reads use a pinned Git object only. Merging is pure; the publishing
owner commits and verifies the returned value separately.
"""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlparse

from product_identity import ProductIdentityRegistry, extract_product_identity
from sale_campaign import actual_sale_price_rejection

HISTORY_GIT_PATH = "data/new40/asin_history.json"
HISTORY_SCHEMA = "note40-asin-history-v1"
CONTEXT_SCHEMA = "note40_preselection_history.v1"
RECEIPT_SCHEMA = "note40_verified_publication.v1"
MANIFEST_SCHEMA = "note40_daily_product_allocation.v1"
SOURCE_ACCOUNTS = frozenset(range(1, 21)) - {10, 13, 14}
JST = timezone(timedelta(hours=9))
_CONTEXT_PROOF = object()


class New40HistoryError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise New40HistoryError(message)


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise New40HistoryError("history/manifest must contain finite JSON values") from exc


def _sha(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _day(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "explicit JST date required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise New40HistoryError("invalid calendar date") from exc


def _instant(value):
    _require(isinstance(value, str), "verified timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise New40HistoryError("invalid verified timestamp") from exc
    _require(result.tzinfo is not None, "verified timestamp requires timezone")
    return result


def empty_history():
    return {"schema": HISTORY_SCHEMA, "scope": "new40", "posted": []}


def _validate_product_identity(identity):
    """Accept only the usable canonical shape emitted by ProductIdentity.to_dict."""
    _require(isinstance(identity, dict) and
             set(identity) <= {"brand", "manufacturer_model", "brand_model_keys", "global_trade_numbers"},
             "invalid product identity schema")
    for name in ("brand", "manufacturer_model"):
        if name in identity:
            _require(isinstance(identity[name], str) and identity[name].strip() == identity[name] and
                     bool(identity[name]), "invalid product identity text: " + name)
    for name in ("brand_model_keys", "global_trade_numbers"):
        if name in identity:
            values = identity[name]
            _require(isinstance(values, list) and bool(values) and
                     all(isinstance(value, str) and bool(value) for value in values),
                     "invalid product identity identifiers: " + name)
    # The extractor normalizes and drops bad identifiers. Require a lossless
    # round trip so corruption cannot silently weaken future exclusions.
    canonical = extract_product_identity({"product_identity": identity})
    _require(canonical.usable and canonical.to_dict() == identity, "noncanonical or unusable product identity")


def _validate_entry(row):
    _require(isinstance(row, dict), "history entry must be an object")
    destination, source, position = (row.get(k) for k in ("destination_account", "source_account", "source_position"))
    _require(type(destination) is int and 201 <= destination <= 240, "invalid new40 destination")
    _require(type(source) is int and source in SOURCE_ACCOUNTS, "invalid new40 source account")
    _require(type(position) is int and 1 <= position <= 10, "invalid source position")
    _require(isinstance(row.get("planning_slot"), str) and re.fullmatch(r"N(?:0[1-9]|[12][0-9]|3[0-9]|40)", row["planning_slot"]), "invalid planning slot")
    _require(isinstance(row.get("asin"), str) and re.fullmatch(r"[A-Z0-9]{10}", row["asin"]), "invalid ASIN")
    _require(_sha(row.get("source_commit"), 40) and _sha(row.get("manifest_sha256"), 64), "missing source/manifest fingerprint")
    _require(_sha(row.get("source_products_sha256"), 64), "missing source product fingerprint")
    _day(row.get("allocation_date"))
    key, url = row.get("note_key"), row.get("note_url")
    _require(isinstance(key, str) and re.fullmatch(r"n[A-Za-z0-9_-]{1,63}", key), "invalid note key")
    _require(isinstance(url, str), "note URL required")
    parsed = urlparse(url)
    _require(parsed.scheme == "https" and parsed.netloc == "note.com" and
             re.fullmatch(r"/[^/]+/n/" + re.escape(key), parsed.path) and not parsed.query and not parsed.fragment,
             "note URL/key mismatch")
    status = row.get("status")
    _require(status in {"published", "reserved"}, "only verified publication/reservation events are accepted")
    _instant(row.get("published_at" if status == "published" else "reserved_at"))
    for name in ("published_at", "reserved_at"):
        if row.get(name) is not None:
            _instant(row[name])
    proof = row.get("verification")
    _require(isinstance(proof, dict) and proof.get("schema") == "note40_note_readback.v1" and
             proof.get("status") == "VERIFIED_READBACK" and proof.get("management_list_confirmed") is True and
             proof.get("identity_verified") is True, "verified note readback required")
    for name in ("destination_account", "note_key", "note_url"):
        _require(proof.get(name) == row[name], "note readback identity mismatch: " + name)
    _require(proof.get("publication_status") == status, "note readback publication status mismatch")
    _instant(proof.get("checked_at"))
    now = datetime.now(timezone.utc)
    _require(_instant(proof["checked_at"]) <= now, "note verification is in the future")
    if row.get("published_at") is not None:
        _require(_instant(row["published_at"]) <= now, "publication timestamp is in the future")
        _require(_instant(proof["checked_at"]) >= _instant(row["published_at"]),
                 "note verification precedes publication")
    if "product_identity" in row:
        _validate_product_identity(row["product_identity"])


def validate_history(history):
    _require(isinstance(history, dict) and history.get("schema") == HISTORY_SCHEMA and
             history.get("scope") == "new40" and isinstance(history.get("posted"), list), "invalid new40 canonical history schema")
    _encoded(history)
    notes, slots = set(), set()
    for row in history["posted"]:
        _validate_entry(row)
        note = row["note_key"]
        slot = (row["allocation_date"], row["planning_slot"])
        _require(note not in notes and slot not in slots, "duplicate note identity or daily planning slot")
        notes.add(note); slots.add(slot)
    return history


def _manifest_slot(manifest, slot):
    _require(isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA, "canonical new40 allocation required")
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    _require(hashlib.sha256(_encoded(body)).hexdigest() == manifest.get("manifest_sha256"), "manifest SHA mismatch")
    _require(manifest.get("mode") == "LOCAL_INPUT_PREPARATION_ONLY", "offline allocation cannot authorize history sync")
    _require(_sha(manifest.get("source_commit"), 40), "manifest source commit missing")
    _day(manifest.get("date"))
    rows = manifest.get("slots")
    _require(isinstance(rows, list) and all(isinstance(r, dict) for r in rows), "manifest slots missing")
    _require([r.get("planning_slot") for r in rows] == [f"N{n:02d}" for n in range(1, 41)], "manifest must retain all forty planning slots")
    counters, destinations = Counter(), set()
    for row in rows:
        source, destination = row.get("source_account"), row.get("destination_account")
        _require(type(source) is int and source in SOURCE_ACCOUNTS, "invalid manifest source")
        counters[source] += 1
        _require(type(row.get("source_position")) is int and row["source_position"] == counters[source] <= 10,
                 "manifest source order is compressed or duplicated")
        _require(type(destination) is int and 201 <= destination <= 240 and destination not in destinations,
                 "invalid or duplicate manifest destination")
        destinations.add(destination)
    matches = [r for r in rows if r["planning_slot"] == slot]
    _require(len(matches) == 1, "receipt planning slot absent from manifest")
    row = matches[0]
    _require(row.get("enabled") is True and row.get("input_ready") is True and
             row.get("destination_registration_saved_verified") is True and row.get("hold_reasons") == [],
             "manifest input is not ready for this destination")
    product, price, affiliate, source = (row.get(k) for k in ("canonical_input", "price_check", "affiliate_evidence", "source"))
    _require(all(isinstance(v, dict) for v in (product, price, affiliate, source)), "canonical product/price/affiliate/source evidence required")
    _require(product.get("asin") == row.get("asin") and price.get("eligible") is True and
             price.get("existing_price_floor_verified") is True and actual_sale_price_rejection(product, price.get("coupon")) is None,
             "canonical product is not price eligible")
    _require(source.get("commit") == manifest["source_commit"] and
             all(source.get(k) == row.get(k) for k in ("source_account", "source_position", "asin")), "manifest source identity mismatch")
    _require(_sha(source.get("files", {}).get("products", {}).get("sha256"), 64), "manifest source product hash missing")
    intake = row.get("history_check", {})
    _require(isinstance(intake, dict) and intake.get("schema") == "note40_history_intake.v1" and
             intake.get("verified") is True and intake.get("history_git_path") == HISTORY_GIT_PATH and
             intake.get("source_commit") == manifest["source_commit"] and
             intake.get("source_account") == row["source_account"] and
             intake.get("source_products_sha256") == source["files"]["products"]["sha256"] and
             _sha(intake.get("current_history_sha256"), 64) and
             isinstance(intake.get("held_source_positions"), list) and row["source_position"] not in intake["held_source_positions"],
             "verified canonical new40 history intake required")
    url = row.get("canonical_affiliate_url")
    _require(isinstance(url, str) and product.get("affiliate_url") == url and affiliate.get("canonical_affiliate_url") == url,
             "canonical affiliate URL mismatch")
    parsed = urlparse(url)
    tracking = row.get("amazon_tracking_id")
    store = row.get("amazon_store_id")
    _require(isinstance(store, str) and store == affiliate.get("amazon_store_id") and
             re.fullmatch(r"[A-Za-z0-9_-]+-22", store), "verified affiliate Store mismatch")
    _require(isinstance(tracking, str) and tracking != store and tracking == affiliate.get("amazon_tracking_id") and
             re.fullmatch(r"[A-Za-z0-9_-]+-22", tracking) and parsed.scheme == "https" and
             parsed.netloc == "www.amazon.co.jp" and
             re.fullmatch(r"/(?:dp|gp/product)/" + re.escape(row["asin"]) + r"/?", parsed.path)
             and all(len(values) == 1 for values in parse_qs(parsed.query, keep_blank_values=True).values())
             and parse_qs(parsed.query, keep_blank_values=True).get("tag") == [tracking] and not parsed.fragment and not parsed.params
             and url != source.get("original_affiliate_url_provenance_only"),
             "verified affiliate ASIN/tracking mismatch")
    evidence = affiliate.get("verification_evidence", {})
    _require(isinstance(evidence, dict) and _sha(evidence.get("sha256"), 64), "affiliate evidence fingerprint missing")
    mode = affiliate.get("evidence_mode", "live_readback")
    _require(mode in {"live_readback", "official_url_builder"}, "unknown affiliate evidence mode")
    if mode == "official_url_builder":
        # The input adapter has already verified the registered Store/Tracking,
        # fixed Git product position and construction proof before sealing this
        # manifest. Construction time must not be represented as a PDP readback.
        _require(affiliate.get("verified_at_jst") is None, "constructed link cannot claim live readback time")
        _require(affiliate.get("proof_schema") == "note40_amazon_link_construction.v1" and
                 affiliate.get("proof_status") == "VERIFIED_CONSTRUCTION" and
                 _sha(affiliate.get("proof_body_sha256"), 64), "verified construction proof identity missing")
        truthful = affiliate.get("truthfulness", {})
        _require(isinstance(truthful, dict) and all(truthful.get(key) is False for key in (
            "network_request_performed_by_builder", "amazon_product_page_live_readback_performed",
            "product_availability_verified_by_builder", "product_price_verified_by_builder",
            "attribution_or_commission_observed_by_builder")), "construction proof makes an unsupported live claim")
        _require(url == f"https://www.amazon.co.jp/dp/{row['asin']}?tag={tracking}",
                 "constructed affiliate URL is not the exact deterministic format")
        evidence_at = _instant(affiliate.get("constructed_at_jst"))
    else:
        evidence_at = _instant(affiliate.get("verified_at_jst"))
    _require(evidence_at <= datetime.now(timezone.utc), "affiliate evidence time is in the future")
    return row


def merge_verified_publication(history, receipt, manifest):
    """Pure, idempotent merge. No Git, files, browser, publishing or old ledgers."""
    validate_history(history)
    _require(isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA, "verified publication receipt schema required")
    row = _manifest_slot(manifest, receipt.get("planning_slot"))
    for name in ("planning_slot", "destination_account", "source_account", "source_position", "asin"):
        _require(receipt.get(name) == row.get(name), "receipt/manifest binding mismatch: " + name)
    for name in ("source_commit", "manifest_sha256"):
        _require(receipt.get(name) == manifest.get(name), "receipt/manifest fingerprint mismatch: " + name)
    entry = {name: copy.deepcopy(receipt[name]) for name in
             ("planning_slot", "destination_account", "source_account", "source_position", "asin",
              "note_key", "note_url", "status", "source_commit", "manifest_sha256", "verification") if name in receipt}
    for name in ("published_at", "reserved_at"):
        if receipt.get(name) is not None:
            entry[name] = receipt[name]
    entry["allocation_date"] = manifest["date"]
    entry["source_products_sha256"] = row["source"]["files"]["products"]["sha256"]
    _validate_entry(entry)
    destination_url = row.get("destination_note_url")
    _require(isinstance(destination_url, str), "manifest destination note URL missing")
    profile = urlparse(destination_url)
    _require(profile.scheme == "https" and profile.netloc == "note.com" and
             re.fullmatch(r"/[^/]+/?", profile.path) and not profile.query and not profile.fragment and
             entry["note_url"] == destination_url.rstrip("/") + "/n/" + entry["note_key"], "published note belongs to another destination")
    identity = extract_product_identity(row["canonical_input"])
    if identity.usable:
        entry["product_identity"] = identity.to_dict()
    output = copy.deepcopy(history)
    same_note = [old for old in output["posted"] if old["note_key"] == entry["note_key"]]
    same_slot = [old for old in output["posted"] if (old["allocation_date"], old["planning_slot"]) == (entry["allocation_date"], entry["planning_slot"])]
    if same_note or same_slot:
        _require(same_note and same_slot and same_note[0] is same_slot[0], "note key or daily slot already bound to another publication")
        old = same_note[0]
        immutable = ("planning_slot", "destination_account", "source_account", "source_position", "asin",
                     "note_key", "note_url", "source_commit", "manifest_sha256", "allocation_date", "source_products_sha256")
        _require(all(old.get(k) == entry.get(k) for k in immutable), "existing publication identity cannot be reassigned")
        _require(not (old["status"] == "published" and entry["status"] == "reserved"), "published history cannot be downgraded")
        for name in ("published_at", "reserved_at"):
            if old.get(name) and entry.get(name):
                _require(old[name] == entry[name], "verified event timestamp cannot be rewritten")
            elif old.get(name):
                entry[name] = old[name]
        _require(_instant(entry["verification"]["checked_at"]) >= _instant(old["verification"]["checked_at"]), "stale note verification")
        output["posted"][output["posted"].index(old)] = entry
    else:
        output["posted"].append(entry)
    output["posted"].sort(key=lambda e: (e["allocation_date"], e["planning_slot"], e["note_key"]))
    validate_history(output)
    return output


@dataclass(frozen=True)
class New40SelectionContext:
    history_commit: str
    history_sha256: str
    source_account: int
    reference_date: str
    within_days: int
    include_product_identities: bool
    excluded_asins: frozenset[str]
    global_trade_numbers: frozenset[str]
    brand_model_keys: frozenset[str]
    _proof: object = field(repr=False, compare=False)

    def public_evidence(self):
        _require(self._proof is _CONTEXT_PROOF, "unvalidated new40 selection context")
        return {"schema": CONTEXT_SCHEMA, "history_git_path": HISTORY_GIT_PATH,
                "history_commit": self.history_commit, "history_sha256": self.history_sha256,
                "history_schema": HISTORY_SCHEMA, "source_account": self.source_account,
                "reference_date": self.reference_date, "exclude_within_days": self.within_days,
                "exclude_product_identifiers": self.include_product_identities,
                "exclusion_scope": "all_new40_destinations", "exclusion_applied_before_selection": True,
                "excluded_count": len(self.excluded_asins), "policy": "verified_publications_and_reservations_jst_v1"}

    @property
    def registry(self):
        self.public_evidence()
        return ProductIdentityRegistry(set(self.excluded_asins), set(self.global_trade_numbers), set(self.brand_model_keys))

    def registry_for_selection(self, *, source_account, reference_date, within_days, include_product_identities):
        self.public_evidence()
        _require((source_account, reference_date, within_days, include_product_identities) ==
                 (self.source_account, self.reference_date, self.within_days, self.include_product_identities),
                 "new40 selection context/config/date mismatch")
        return self.registry


def read_new40_selection_context(repo_dir, history_commit, source_account, *, reference_date,
                                within_days=20, include_product_identities=True):
    """Read only the canonical new40 Git blob; missing/invalid history is fatal."""
    _require(_sha(history_commit, 40), "full pinned history Git commit required")
    _require(type(source_account) is int and source_account in SOURCE_ACCOUNTS, "invalid source account")
    today = _day(reference_date)
    _require(type(within_days) is int and within_days >= 0, "nonnegative exclusion window required")
    _require(type(include_product_identities) is bool, "identity exclusion flag must be boolean")
    repo = str(Path(repo_dir).resolve())
    try:
        result = subprocess.run(["git", "-C", repo, "show", f"{history_commit}:{HISTORY_GIT_PATH}"],
                                capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise New40HistoryError("canonical new40 Git history unavailable") from exc
    _require(result.returncode == 0, "canonical new40 Git history missing at pinned commit; no fallback")
    try:
        history = json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise New40HistoryError("canonical new40 Git history is invalid JSON") from exc
    validate_history(history)
    registry = ProductIdentityRegistry()
    cutoff = today - timedelta(days=max(within_days - 1, 0))
    for row in history["posted"]:
        event_day = max(_instant(row[k]).astimezone(JST).date() for k in ("published_at", "reserved_at") if row.get(k))
        if within_days > 0 and event_day < cutoff:
            continue
        registry.asins.add(row["asin"])
        if include_product_identities:
            registry.add_identity(extract_product_identity(row))
    return New40SelectionContext(history_commit, hashlib.sha256(result.stdout).hexdigest(), source_account,
                                 reference_date, within_days, include_product_identities,
                                 frozenset(registry.asins), frozenset(registry.global_trade_numbers),
                                 frozenset(registry.brand_model_keys), _CONTEXT_PROOF)
