"""Opt-in public PDP offer evidence; never reads cookies, storage or account data.

Product fields remain unchanged. Only accepted observations may replace card offers.
The caller must run price/ranking/quota/identity selection after this verification.
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


OBSERVE_OFFER_JS = r'''() => {
 const shown=n=>{
   // aria-hidden removes content from accessibility, not from the visual page.
   if(!n || n.closest('template,script,style,noscript,[hidden],.a-offscreen'))return false;
   for(let p=n;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||s.opacity==='0')return false;}
   return n.getClientRects().length>0;
 };
 const text=n=>n&&shown(n)?n.innerText.trim():'';
 const all=(root,s)=>[...root.querySelectorAll(s)];
 const centers=all(document,'#centerCol').filter(shown);
 const center=centers.length===1?centers[0]:null;
 const titles=all(document,'#productTitle').filter(shown);
 const regions=center?all(center,'#corePriceDisplay_desktop_feature_div').filter(shown):[];
 // The primary desktop offer is unambiguous. Never use buybox, recommendations,
 // installments, unit prices, strike prices or accessibility-only template prices.
 const priceRegion=regions.length===1?regions[0]:null;
 const prices=priceRegion?all(priceRegion,'.priceToPay .a-price-whole').filter(shown):[];
 const currencies=priceRegion?all(priceRegion,'.priceToPay .a-price-symbol').filter(shown):[];
 const rates=priceRegion?all(priceRegion,'.savingsPercentage').filter(shown):[];
 const references=priceRegion?all(priceRegion,'.basisPrice').filter(shown):[];
 const saleRegions=center?all(center,'#dealBadge_feature_div').filter(shown):[];
 const sale=saleRegions.length===1?saleRegions[0]:null;
 const timers=sale?all(sale,'#detailpage-dealBadge-countdown-timer,.detailpage-dealBadge-countdown-timer').filter(shown):[];
 const timer=timers.length===1?timers[0]:null;
 const body=(document.body?.innerText??'').slice(0,3000);
 const challenge=!!document.querySelector('form[action*="validateCaptcha"],#captchacharacters') || /robot check|captcha|ロボットではない|文字を入力してください/i.test(document.title+'\n'+body);
 return {
   selected_asins:all(document,'input#ASIN').filter(n=>!n.closest('template')).map(n=>n.value.trim()),
   page_asins:all(document,'#page-load-asin').filter(n=>!n.closest('template')).map(n=>n.textContent.trim()),
   center_count:centers.length,product_title:titles.length===1?text(titles[0]).slice(0,400):'',
   price_region_count:regions.length,price_region_selector:'#corePriceDisplay_desktop_feature_div',
   price_texts:prices.map(text),currency_texts:currencies.map(text),discount_texts:rates.map(text),reference_price_texts:references.map(text),
   sale_region_count:saleRegions.length,sale_region_selector:'#dealBadge_feature_div',sale_label:text(sale).slice(0,500),
   timer_count:timers.length,timer:timer?{timer_selector:timer.id==='detailpage-dealBadge-countdown-timer'?'#detailpage-dealBadge-countdown-timer':'.detailpage-dealBadge-countdown-timer',timer_text:text(timer)}:null,
   challenge_detected:challenge
 };
}'''


def observation_sha256(evidence):
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()


def without_search_price_filter(url):
    """Only the opt-in PDP path defers numerical price gating until the PDP."""
    parsed = urlparse(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "rh":
            value = ",".join(part for part in value.split(",") if not part.startswith("p_36:"))
        if key not in {"low-price", "high-price"}:
            query.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _text(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def _unique_number(values, parser):
    if not isinstance(values, list) or not values:
        return None
    parsed = [parser(_text(value)) for value in values]
    return parsed[0] if None not in parsed and len(set(parsed)) == 1 else None


def _amount(text):
    return int(text.replace(",", "")) if re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)", text) else None


def _rate(text):
    match = re.fullmatch(r"[-−]?(\d{1,2})%", text)
    return int(match.group(1)) if match and 0 < int(match.group(1)) < 100 else None


def _reference(text):
    # Explicit reference-price container only. Repeated accessible/visible values
    # may agree, but unit-price or competing reference values are never guessed.
    amounts = re.findall(r"[¥￥]((?:\d{1,3}(?:,\d{3})+|\d+))", text)
    values = [_amount(value) for value in amounts]
    return values[0] if values and len(set(values)) == 1 else None


def public_evidence(observed, asin, url, http_status, checked_at):
    """Whitelist product-only fields; strip query/fragment from the final URL."""
    keys = ("selected_asins", "page_asins", "center_count", "product_title", "price_region_count",
            "price_region_selector", "price_texts", "currency_texts", "discount_texts", "reference_price_texts",
            "sale_region_count", "sale_region_selector", "sale_label", "timer_count", "timer", "challenge_detected")
    evidence = {key: observed.get(key) for key in keys}
    parsed = urlparse(url)
    evidence.update(schema_version=1, requested_asin=asin, url=urlunparse(parsed._replace(query="", fragment="")),
                    http_status=http_status, checked_at=checked_at, evidence_kind=None, countdown=None)
    return evidence


def validate_offer(evidence):
    """Return (canonical offer, rejection code), using only the recorded PDP."""
    asin = evidence["requested_asin"]
    if evidence.get("challenge_detected") is not False:
        return None, "detail_challenge"
    if evidence.get("http_status") != 200:
        return None, "detail_http_error"
    url = urlparse(evidence.get("url", ""))
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", url.path)
    if url.scheme != "https" or url.hostname not in {"www.amazon.co.jp", "amazon.co.jp"} or not match or match.group(1) != asin:
        return None, "detail_url_asin_mismatch"
    selected, page = evidence.get("selected_asins"), evidence.get("page_asins")
    if not isinstance(selected, list) or not selected or any(value != asin for value in selected) or not isinstance(page, list) or any(value != asin for value in page):
        return None, "detail_selected_asin_mismatch"
    if evidence.get("center_count") != 1 or not evidence.get("product_title"):
        return None, "detail_product_region_missing"
    if evidence.get("price_region_count") != 1 or evidence.get("price_region_selector") != "#corePriceDisplay_desktop_feature_div":
        return None, "detail_price_region_ambiguous"
    currency = evidence.get("currency_texts")
    price = _unique_number(evidence.get("price_texts"), _amount)
    if not currency or any(_text(value) != "¥" for value in currency) or price is None or price <= 0:
        return None, "detail_primary_price_missing_or_ambiguous"
    rate = _unique_number(evidence.get("discount_texts"), _rate)
    references = evidence.get("reference_price_texts")
    reference = _unique_number(references, _reference)
    if rate is None:
        return None, "detail_discount_missing_or_ambiguous"
    if references:
        if reference is None or reference <= price:
            return None, "detail_reference_ambiguous_or_invalid"
        calculated_rate = ((reference - price) * 200 + reference) // (2 * reference)
        if calculated_rate != rate:
            return None, "detail_discount_reference_inconsistent"
    if evidence.get("sale_region_count") != 1 or evidence.get("sale_region_selector") != "#dealBadge_feature_div":
        return None, "detail_sale_region_missing_or_ambiguous"
    label = _text(evidence.get("sale_label", ""))
    if re.fullmatch(r"(?:Amazon)?タイムセール", label):
        evidence["evidence_kind"] = "label"
    else:
        timer = evidence.get("timer")
        if evidence.get("timer_count") != 1 or not isinstance(timer, dict) or timer.get("timer_selector") not in {
            "#detailpage-dealBadge-countdown-timer", ".detailpage-dealBadge-countdown-timer"
        }:
            return None, "detail_time_sale_unverified"
        duration = re.fullmatch(r"(\d{1,3}):([0-5]\d):([0-5]\d)", _text(timer.get("timer_text", "")))
        if not duration or not re.search(r"終了まで[:：]", label) or duration.group(0) not in label:
            return None, "detail_countdown_invalid"
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + int(duration[3])
        if seconds <= 0:
            return None, "detail_countdown_expired"
        try:
            checked = datetime.fromisoformat(evidence["checked_at"])
            if checked.tzinfo is None:
                raise ValueError("timezone missing")
        except (TypeError, ValueError):
            return None, "detail_observation_time_invalid"
        evidence["evidence_kind"] = "deal_countdown"
        evidence["countdown"] = {**timer, "remaining_seconds": seconds, "expires_at": (checked + timedelta(seconds=seconds)).isoformat()}
    return {"price": f"￥{price:,}", "price_int": price, "original_price": f"￥{reference:,}" if reference else "", "discount_rate": f"{rate}%OFF"}, None


def offer_fields(product):
    return {key: getattr(product, key) for key in ("price", "price_int", "original_price", "discount_rate")}


async def observe_detail_offer(page, product, description_reader):
    """One same-ASIN visit; description/specs and accepted offer share that page."""
    status, observed = None, {}
    checked = datetime.now(timezone.utc).isoformat()
    try:
        response = await page.goto(f"https://www.amazon.co.jp/dp/{product.asin}?th=1", wait_until="domcontentloaded", timeout=45000)
        status = response.status if response else None
        await page.wait_for_timeout(1500)
        observed = await page.evaluate(OBSERVE_OFFER_JS)
        checked = datetime.now(timezone.utc).isoformat()
        evidence = public_evidence(observed, product.asin, page.url, status, checked)
        offer, reason = validate_offer(evidence)
        if offer:
            description, specs = await description_reader(page)
            # A variant switch while lazy content loads invalidates the visit.
            identity = await page.evaluate("() => [...document.querySelectorAll('input#ASIN')].filter(n=>!n.closest('template')).map(n=>n.value.trim())")
            if not identity or any(asin != product.asin for asin in identity):
                offer, reason = None, "detail_selected_asin_changed"
        else:
            description, specs = "", ""
    except Exception as exc:
        # Do not serialize exception strings, which may carry request/transport data.
        evidence = public_evidence(observed, product.asin, getattr(page, "url", ""), status, checked)
        offer, reason, description, specs = None, "detail_observation_error", "", ""
        evidence["error_type"] = type(exc).__name__
    record = {"asin": product.asin, "category": product.category, "checked_at": checked,
              "status": "accepted" if offer else "rejected", "reason": reason,
              "card_offer": offer_fields(product), "pdp_offer": offer,
              "evidence": evidence, "evidence_sha256": observation_sha256(evidence)}
    if offer:
        for key, value in offer.items():
            setattr(product, key, value)
        product.description, product.specs = description, specs
    return record
