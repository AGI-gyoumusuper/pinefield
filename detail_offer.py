"""Opt-in public PDP offer evidence; never reads cookies, storage or account data.

Product fields remain unchanged. Only accepted observations may replace card offers.
The caller must run price/ranking/quota/identity selection after this verification.
"""
import hashlib
import json
import re
import unicodedata
from types import SimpleNamespace
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
 const challengeSignals=[];
 // Hidden templates and product copy mentioning CAPTCHA are not challenges.
 // Visible controls still stop the visit even if product content is also present.
 if(all(document,'form[action*="validateCaptcha"],#captchacharacters').some(shown))challengeSignals.push('visible_challenge_control');
 // A challenge overlay may leave the underlying PDP visible and use an iframe
 // instead of the usual Amazon form. Strong instructions/headings still stop it.
 if(/ロボットではない|文字を入力してください/i.test(body))challengeSignals.push('visible_challenge_instruction');
 const challengeHeading=/^(?:Amazon(?:\.co\.jp)?\s*[:|–-]?\s*)?(?:Robot Check|CAPTCHA|ロボットチェック)$/i;
 if(all(document,'h1,h2,h3,[role="heading"]').some(n=>shown(n)&&!n.closest('#productTitle')&&challengeHeading.test(text(n))))challengeSignals.push('visible_challenge_heading');
 const productVisible=centers.length===1&&titles.length===1&&!!text(titles[0]);
 const challengeText=/robot check|captcha|ロボットではない|文字を入力してください/i;
 if(!productVisible){
   if(challengeText.test(document.title))challengeSignals.push('challenge_page_title');
   if(challengeText.test(body))challengeSignals.push('challenge_page_text');
 }
 const challenge=challengeSignals.length>0;
 const couponSelectors=['#coupon_feature_div','#promoPriceBlockMessage_feature_div','#couponTextpctch','#vpcButton'];
 const couponNodes=center?couponSelectors.flatMap(s=>all(center,s)).filter(shown):[];
 const couponRoots=[...new Set(couponNodes)].filter(n=>!couponNodes.some(p=>p!==n&&p.contains(n)));
 const coupons=couponRoots.filter(n=>/クーポン|coupon|コード/i.test(text(n))).map(n=>({
   selector:'#'+n.id,text:text(n).slice(0,1500),
   asins:[...new Set([n,...all(n,'[data-asin]')].map(e=>e.getAttribute('data-asin')).filter(Boolean))],
   links:all(n,'a[href]').map(a=>a.href).filter(u=>/^https?:/.test(u)).slice(0,8),
   seller_ids:[...new Set([n,...all(n,'[data-merchant-id],[data-seller-id]')].map(e=>e.getAttribute('data-merchant-id')||e.getAttribute('data-seller-id')).filter(Boolean))]
 }));
 const primarySellers=all(document,'#merchantInfoFeature_feature_div [data-merchant-id],#merchantInfoFeature_feature_div [data-seller-id],#merchant-info [data-merchant-id],#merchant-info [data-seller-id]').filter(shown).map(n=>n.getAttribute('data-merchant-id')||n.getAttribute('data-seller-id'));
 return {
   selected_asins:all(document,'input#ASIN').filter(n=>!n.closest('template')).map(n=>n.value.trim()),
   page_asins:all(document,'#page-load-asin').filter(n=>!n.closest('template')).map(n=>n.textContent.trim()),
   center_count:centers.length,product_title:titles.length===1?text(titles[0]).slice(0,400):'',
   price_region_count:regions.length,price_region_selector:'#corePriceDisplay_desktop_feature_div',
   price_texts:prices.map(text),currency_texts:currencies.map(text),discount_texts:rates.map(text),reference_price_texts:references.map(text),
   sale_region_count:saleRegions.length,sale_region_selector:'#dealBadge_feature_div',sale_label:text(sale).slice(0,500),
   timer_count:timers.length,timer:timer?{timer_selector:timer.id==='detailpage-dealBadge-countdown-timer'?'#detailpage-dealBadge-countdown-timer':'.detailpage-dealBadge-countdown-timer',timer_text:text(timer)}:null,
   challenge_detected:challenge,challenge_signals:challengeSignals,coupon_regions:coupons,primary_seller_ids:[...new Set(primarySellers)]
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
            "sale_region_count", "sale_region_selector", "sale_label", "timer_count", "timer", "challenge_detected", "coupon_regions", "primary_seller_ids")
    evidence = {key: observed.get(key) for key in keys}
    # Optional diagnostics preserve replay/hash compatibility with older evidence.
    # Keep fixed labels only, never page text, control values or arbitrary strings.
    if isinstance(observed.get("challenge_signals"), list):
        allowed_signals = {"visible_challenge_control", "visible_challenge_instruction",
                           "visible_challenge_heading", "challenge_page_title", "challenge_page_text"}
        evidence["challenge_signals"] = list(dict.fromkeys(
            value for value in observed["challenge_signals"]
            if isinstance(value, str) and value in allowed_signals))
    if isinstance(evidence.get("coupon_regions"), list):
        public_rows = []
        for raw in evidence["coupon_regions"]:
            if not isinstance(raw, dict):
                public_rows.append(None); continue
            row = {key: raw.get(key) for key in ("selector", "text", "asins", "seller_ids")}
            row["links"] = [urlunparse(urlparse(str(link))._replace(query="", fragment=""))
                            for link in raw.get("links", [])] if isinstance(raw.get("links"), list) else None
            public_rows.append(row)
        evidence["coupon_regions"] = public_rows
    parsed = urlparse(url)
    evidence.update(schema_version=1, requested_asin=asin, url=urlunparse(parsed._replace(query="", fragment="")),
                    http_status=http_status, checked_at=checked_at, evidence_kind=None, countdown=None)
    return evidence


def validate_offer(evidence, *, offer_scope="time_sale"):
    """Return (canonical offer, rejection code), using only the recorded PDP."""
    if offer_scope not in ("time_sale", "all_discounts", "unified_discounts"):
        return None, "detail_offer_scope_invalid"
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
    if offer_scope == "unified_discounts":
        return validate_unified_offer(evidence, price, rate, references, reference)
    if rate is None:
        return None, "detail_discount_missing_or_ambiguous"
    if references:
        if reference is None or reference <= price:
            return None, "detail_reference_ambiguous_or_invalid"
        calculated_rate = ((reference - price) * 200 + reference) // (2 * reference)
        if calculated_rate != rate:
            return None, "detail_discount_reference_inconsistent"
    if offer_scope == "all_discounts" and reference is None:
        return None, "detail_reference_missing"
    sale_rejection = _validate_time_sale(evidence)
    if sale_rejection:
        if offer_scope != "all_discounts":
            return None, sale_rejection
        # Verified price/reference/rate is sufficient for an ordinary discount.
        # Never infer a time-sale badge or a countdown from a discounted price.
        evidence["evidence_kind"] = "ordinary_discount"
        evidence["countdown"] = None
    return {"price": f"￥{price:,}", "price_int": price, "original_price": f"￥{reference:,}" if reference else "", "discount_rate": f"{rate}%OFF"}, None


DISCOUNT_POLICY = "amazon_direct_discounts_v1"


def empty_coupon(evidence, price, status="not_displayed", reason=None):
    return dict(status=status, kind=None, value=None, basis_price_yen=price,
                application="unknown", conditions=[], expires_at=None,
                expiry_status="not_displayed", same_offer=False, stackable=False,
                final_price_yen=None, reason=reason, evidence_text="",
                source_url=evidence.get("url", ""), asin=evidence.get("requested_asin", ""))


def coupon_hint(text):
    """Public card hints rank candidates only; never count as verified savings."""
    value = unicodedata.normalize("NFKC", str(text))
    if not re.search(r"クーポン|coupon|プロモーションコード", value, re.I):
        return None
    rates = {int(x) for x in re.findall(r"(\d{1,2})\s*%", value)}
    money = r"(?:[¥￥]\s*([\d,]+)|([\d,]+)\s*円)"
    benefit_amounts = re.findall(money + r"\s*(?:OFF|オフ|引き|割引|クーポン)", value, re.I)
    amounts = {int(amount.replace(",", "")) for pair in benefit_amounts for amount in pair if amount}
    if not amounts and not rates:
        amounts = {int(amount.replace(",", "")) for pair in re.findall(money, value) for amount in pair if amount}
    if len(rates) == 1 and not amounts and 0 < next(iter(rates)) < 100:
        return {"kind": "percent", "value": next(iter(rates))}
    if len(amounts) == 1 and not rates and next(iter(amounts)) > 0:
        return {"kind": "amount", "value": next(iter(amounts))}
    return None


def parse_coupon(evidence, price):
    """Parse one public offer-scoped coupon; unsupported terms are local fallback."""
    regions = evidence.get("coupon_regions")
    if regions is None:
        return empty_coupon(evidence, price, "unverified", "coupon_region_not_observed")
    if not isinstance(regions, list):
        return empty_coupon(evidence, price, "unverified", "coupon_region_invalid")
    if not regions:
        return empty_coupon(evidence, price)
    result = empty_coupon(evidence, price, "unverified", "coupon_region_ambiguous")
    # Repeated identical labels can be rendered twice, but distinct offers cannot be merged.
    unique = {json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in regions if isinstance(row, dict)}
    if len(unique) != 1 or len(unique) != len({json.dumps(r, ensure_ascii=False, sort_keys=True) for r in regions}):
        return result
    row = next(iter(unique.values()))
    text = unicodedata.normalize("NFKC", str(row.get("text", "")))
    result["evidence_text"] = text[:1500]
    if row.get("selector") not in {"#coupon_feature_div", "#promoPriceBlockMessage_feature_div", "#couponTextpctch", "#vpcButton"}:
        result["reason"] = "coupon_region_not_primary"; return result
    asins = row.get("asins", [])
    if not isinstance(asins, list) or any(asin != evidence["requested_asin"] for asin in asins):
        result["reason"] = "coupon_asin_mismatch"; return result
    links = row.get("links", [])
    if not isinstance(links, list) or any(urlparse(str(link)).hostname not in {"amazon.co.jp", "www.amazon.co.jp"} for link in links):
        result["reason"] = "coupon_external_source"; return result
    for link in links:
        linked = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", urlparse(link).path)
        if linked and linked[1] != evidence["requested_asin"]:
            result["reason"] = "coupon_asin_mismatch"; return result
    sellers = row.get("seller_ids", [])
    if not isinstance(sellers, list) or len(set(sellers)) > 1:
        result["reason"] = "coupon_offer_ambiguous"; return result
    primary_sellers = evidence.get("primary_seller_ids") or []
    if len(set(primary_sellers)) > 1 or (sellers and set(sellers) != set(primary_sellers)):
        result["reason"] = "coupon_offer_mismatch"; return result
    unsupported = r"初回|初めて|新規|定期|まとめ買い|複数|同時|(?:[2-9]|[1-9]\d+|[二三四五六七八九十百][一二三四五六七八九十百]*)\s*(?:個|点|本|つ|台|袋|件|粒|セット|商品)|円\s*以上|会員|Prime|プライム|学生|法人|抽選|併用不可|利用できません|適用できません|対象外|上限|最大|以上の(?:購入|注文)|対象者限定|カード限定|アプリ限定|指定の支払"
    if re.search(unsupported, text, re.I):
        result["conditions"] = [text[:500]]
        result["reason"] = "coupon_unsupported_condition"; return result
    benefit = coupon_hint(text)
    if benefit is None:
        result["reason"] = "coupon_value_ambiguous"; return result
    if re.search(r"自動(?:的に)?適用|自動で適用", text):
        application = "automatic"
    elif re.search(r"コード", text):
        match = re.search(r"(?:プロモーション)?コード\s*[:：]?\s*([A-Z0-9]{4,30})(?![A-Z0-9])", text)
        if not match:
            result["reason"] = "coupon_code_not_displayed"; return result
        application = "code"; result["code"] = match[1]
    elif re.search(r"クーポン(?:を)?(?:適用|取得)|クーポンの適用|クーポンをチェック|チェック.*クーポン", text):
        application = "clip"
    else:
        result["reason"] = "coupon_application_unknown"; return result
    result.update(benefit, application=application, same_offer=True, stackable=True)
    expiration = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?[ T]*(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if expiration:
        try:
            fields = [int(x or 0) for x in expiration.groups()]
            result.update(expires_at=datetime(*fields, tzinfo=timezone(timedelta(hours=9))).isoformat(), expiry_status="known")
        except ValueError:
            result.update(expiry_status="unverified", reason="coupon_expiry_invalid"); return result
    elif re.search(r"期限|終了|まで有効|20\d{2}[年/-]", text):
        result.update(expiry_status="unverified", reason="coupon_expiry_unverified")
        return result
    reduction = price * benefit["value"] if benefit["kind"] == "percent" else benefit["value"] * 100
    final = price - reduction // 100 if reduction % 100 == 0 else None
    if final is not None and final <= 0:
        result["reason"] = "coupon_price_invalid"; return result
    result.update(status="verified", final_price_yen=final,
                  reason="coupon_rounding_unknown" if final is None else None,
                  conditions=(["クーポン取得が必要"] if application == "clip" else
                              [f"コード {result['code']} の入力が必要"] if application == "code" else []))
    return result


def validate_unified_offer(evidence, price, rate, references, reference):
    if references and (reference is None or reference < price):
        return None, "detail_reference_ambiguous_or_invalid"
    if reference is not None and reference > price:
        calculated = ((reference - price) * 200 + reference) // (2 * reference)
        if rate is not None and calculated != rate:
            return None, "detail_discount_reference_inconsistent"
        rate = calculated or None
    elif rate is not None and reference is not None:
        return None, "detail_discount_reference_inconsistent"
    coupon = parse_coupon(evidence, price)
    if coupon["status"] == "verified" and coupon["expires_at"]:
        try:
            if datetime.fromisoformat(coupon["expires_at"]) <= datetime.fromisoformat(evidence["checked_at"]):
                coupon.update(status="unverified", reason="coupon_expired", final_price_yen=None)
        except (TypeError, ValueError):
            coupon.update(status="unverified", reason="coupon_expiry_invalid", final_price_yen=None)
    evidence["coupon"] = coupon
    sale_rejection = _validate_time_sale(evidence)
    if sale_rejection:
        evidence["evidence_kind"] = "ordinary_discount" if rate else "coupon"
        evidence["countdown"] = None
    if not rate and coupon["status"] != "verified":
        return None, "detail_discount_or_coupon_unverified"
    return {"price": f"￥{price:,}", "price_int": price,
            "original_price": f"￥{reference:,}" if reference else "",
            "discount_rate": f"{rate}%OFF" if rate else ""}, None


def discount_row(product, record, position):
    evidence = record["evidence"]
    original = _reference(_text(product.original_price)) if product.original_price else None
    match = re.fullmatch(r"(\d+)%OFF", product.discount_rate)
    return dict(asin=product.asin, source_position=position, observation_status="verified",
                base_offer=dict(sale_price_yen=product.price_int, original_price_yen=original,
                                discount_rate_percent=int(match[1]) if match else None),
                time_sale=dict(verified=evidence.get("evidence_kind") in {"label", "deal_countdown"},
                               label="Amazon タイムセール" if evidence.get("evidence_kind") in {"label", "deal_countdown"} else None),
                coupon=evidence["coupon"], observed_at=record["checked_at"], evidence_url=evidence["url"])


def discount_comparison_rate(product, coupon=None):
    original = _reference(_text(product.original_price)) if product.original_price else None
    coupon = coupon or {}
    final = coupon.get("final_price_yen") if coupon.get("status") == "verified" else None
    reference = original or product.price_int
    if final is not None and reference > 0:
        return (reference - final) * 100 / reference
    match = re.search(r"(\d+)%", product.discount_rate)
    base = int(match[1]) if match else 0
    if coupon.get("status") == "verified":
        value = coupon.get("value", 0)
        rate = value if coupon.get("kind") == "percent" else value * 100 / product.price_int
        return max(base, rate)  # No precise combined rate when final-price rounding is unknown.
    return base


def validate_discount_contract_summary(account, products, summary):
    """Replay recorded evidence only; never fetch or rewrite an existing source."""
    contract = summary.get("discount_contract")
    policy = summary.get("selection_policy", {})
    if (not isinstance(contract, dict) or type(contract.get("schema_version")) is not int
            or contract.get("schema_version") != 1 or contract.get("policy") != DISCOUNT_POLICY
            or type(contract.get("account")) is not int or contract.get("account") != int(account.removeprefix("account"))
            or contract.get("date") != summary.get("date")
            or policy.get("discount_contract") != DISCOUNT_POLICY or policy.get("offer_scope") != "unified_discounts"):
        return False, "discount contract identity/policy invalid"
    rows = contract.get("products")
    if not isinstance(rows, list) or len(rows) != len(products):
        return False, "discount contract product count invalid"
    verification = summary.get("detail_offer_verification", {})
    observations = verification.get("observations")
    if (verification.get("schema_version") != 1 or verification.get("enabled") is not True
            or verification.get("offer_scope") != "unified_discounts" or not isinstance(observations, list)
            or verification.get("candidate_count") != len(observations)):
        return False, "discount observations missing or invalid"
    accepted, seen, rejected = {}, set(), 0
    for record in observations:
        if not isinstance(record, dict) or not re.fullmatch(r"[A-Z0-9]{10}", str(record.get("asin", ""))) or record["asin"] in seen:
            return False, "discount observation identity invalid"
        seen.add(record["asin"])
        if record.get("status") == "rejected":
            rejected += 1; continue
        evidence = record.get("evidence")
        if record.get("status") != "accepted" or not isinstance(evidence, dict) or record.get("reason") is not None:
            return False, "discount observation status invalid"
        if type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1:
            return False, "discount observation schema invalid"
        if (record.get("evidence_sha256") != observation_sha256(evidence) or record["asin"] != evidence.get("requested_asin")
                or record.get("checked_at") != evidence.get("checked_at")):
            return False, "discount observation evidence mismatch"
        try:
            checked = datetime.fromisoformat(record["checked_at"])
            if checked.tzinfo is None:
                raise ValueError("timezone missing")
            replay = json.loads(json.dumps(evidence))
            offer, rejection = validate_offer(replay, offer_scope="unified_discounts")
        except (TypeError, ValueError, KeyError):
            return False, "discount observation replay failed"
        if rejection or replay != evidence or offer != record.get("pdp_offer"):
            return False, "discount observation replay mismatch"
        accepted[record["asin"]] = record
    if verification.get("accepted_count") != len(accepted) or verification.get("rejected_count") != rejected:
        return False, "discount observation counts mismatch"
    for position, (product, row) in enumerate(zip(products, rows), 1):
        if not isinstance(row, dict) or type(row.get("source_position")) is not int or row["source_position"] != position:
            return False, "discount output position mismatch"
        record = accepted.get(product.get("asin"))
        if record is None or record.get("category") != product.get("category"):
            return False, "discount output identity mismatch"
        if any(product.get(key) != value for key, value in record["pdp_offer"].items()):
            return False, "discount output base price mismatch"
        if row != discount_row(SimpleNamespace(**product), record, position):
            return False, "discount output row mismatch"
    return True, "discount contract matched recorded evidence"


def _validate_time_sale(evidence):
    if evidence.get("sale_region_count") != 1 or evidence.get("sale_region_selector") != "#dealBadge_feature_div":
        return "detail_sale_region_missing_or_ambiguous"
    label = _text(evidence.get("sale_label", ""))
    if re.fullmatch(r"(?:Amazon)?タイムセール", label):
        evidence["evidence_kind"] = "label"
    else:
        timer = evidence.get("timer")
        if evidence.get("timer_count") != 1 or not isinstance(timer, dict) or timer.get("timer_selector") not in {
            "#detailpage-dealBadge-countdown-timer", ".detailpage-dealBadge-countdown-timer"
        }:
            return "detail_time_sale_unverified"
        duration = re.fullmatch(r"(\d{1,3}):([0-5]\d):([0-5]\d)", _text(timer.get("timer_text", "")))
        if not duration or not re.search(r"終了まで[:：]", label) or duration.group(0) not in label:
            return "detail_countdown_invalid"
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + int(duration[3])
        if seconds <= 0:
            return "detail_countdown_expired"
        try:
            checked = datetime.fromisoformat(evidence["checked_at"])
            if checked.tzinfo is None:
                raise ValueError("timezone missing")
        except (TypeError, ValueError):
            return "detail_observation_time_invalid"
        evidence["evidence_kind"] = "deal_countdown"
        evidence["countdown"] = {**timer, "remaining_seconds": seconds, "expires_at": (checked + timedelta(seconds=seconds)).isoformat()}
    return None


def offer_fields(product):
    return {key: getattr(product, key) for key in ("price", "price_int", "original_price", "discount_rate")}


async def observe_detail_offer(page, product, description_reader, *, offer_scope="time_sale"):
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
        offer, reason = validate_offer(evidence, offer_scope=offer_scope)
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
