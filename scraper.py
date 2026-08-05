"""
Amazon 人気順商品スクレイパー (Playwright版)

- categoriesN.yaml で巡回カテゴリと価格フィルタを管理
- Amazon検索結果を人気順で巡回し、カテゴリ内レビュー件数順で代表商品を選定
- 割引率・元値は商品情報として取得するが、採用条件には使用しない
- 投稿済みASIN除外
- 投稿・予約済みカテゴリの次から可変カテゴリ数で循環
- 個別商品ページから商品説明文・スペック欄も取得（v2追加）
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import yaml
from playwright.async_api import async_playwright, Page, BrowserContext

from product_identity import (
    ProductIdentityRegistry,
    extract_product_identity,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ASSOCIATE_TAG = "noteamazon1-22"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "categories1.yaml")


@dataclass
class Product:
    asin: str
    title: str
    price: str
    price_int: int
    original_price: str
    discount_rate: str
    image_url: str
    affiliate_url: str
    category: str
    rating: str
    review_count: str
    description: str = ""  # v2追加：商品説明文
    specs: str = ""        # v2追加：スペック・仕様


def make_affiliate_url(asin: str, associate_tag: str = ASSOCIATE_TAG) -> str:
    base = f"https://www.amazon.co.jp/dp/{asin}"
    return f"{base}?tag={associate_tag}" if associate_tag else base


def expected_associate_tag(output_path: str, config_path: str) -> str:
    """Return the account-specific tag for an active account1-account10 run."""
    output_match = re.search(r"(?:^|[\\/])account(10|[1-9])(?:[\\/]|$)", output_path)
    config_match = re.search(r"categories(10|[1-9])\.ya?ml$", os.path.basename(config_path))

    output_account = output_match.group(1) if output_match else None
    config_account = config_match.group(1) if config_match else None
    if output_account and config_account and output_account != config_account:
        raise ValueError(
            f"Account routing mismatch: output=account{output_account}, "
            f"config=account{config_account}"
        )

    account = output_account or config_account
    if not account:
        raise ValueError(
            "Cannot determine affiliate account from output_path or config_path"
        )
    return f"noteamazon{account}-22"


def validate_affiliate_output(
    products: List[Product],
    output_path: str,
    config_path: str,
    associate_tag: str,
) -> None:
    """Run one lightweight affiliate URL check immediately after scraping."""
    expected_tag = expected_associate_tag(output_path, config_path)
    if associate_tag != expected_tag:
        raise ValueError(
            f"Affiliate tag mismatch: expected={expected_tag}, actual={associate_tag or '(empty)'}"
        )

    mismatched_asins = [
        product.asin
        for product in products
        if product.affiliate_url != make_affiliate_url(product.asin, expected_tag)
    ]
    if mismatched_asins:
        sample = ", ".join(mismatched_asins[:5])
        raise ValueError(
            f"Affiliate URL mismatch: {len(mismatched_asins)} item(s); ASIN={sample}"
        )


def parse_price(price_str: str) -> int:
    if not price_str:
        return 0
    digits = re.sub(r"[^\d]", "", price_str)
    return int(digits) if digits else 0


def calc_discount_rate(price_int: int, original_int: int) -> str:
    if original_int <= 0 or price_int <= 0 or original_int <= price_int:
        return ""
    rate = int((1 - price_int / original_int) * 100)
    return f"{rate}%OFF"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_product_exclusion_registry(
    path: str,
    within_days: int = 0,
    include_scraped: bool = True,
    include_product_identities: bool = True,
) -> ProductIdentityRegistry:
    """投稿済み/予約済み商品の高確度な除外識別子を返す。

    - within_days=0: 全期間（過去に一度でも紹介したASINをすべて除外）
    - within_days=N: JSTの「今日」を含む直近N日に紹介済みのASINを除外。
      N=3 なら 当日・前日・2日前 ＝ 旧⑦ASIN履歴の「3日ルール」と同義。
      未来日の予約（reserved_at）は常に除外する。
    - スキーマ: {"posted": [...]} 形式と素のリスト形式の両方に対応。
    - 判定日は posted_at / reserved_at のうち新しい方（日付文字列比較）。
    - 実行環境のタイムゾーンに依存しないよう JST(+9) を明示する。
    """
    full_path = os.path.join(os.path.dirname(__file__), path) if not os.path.isabs(path) else path
    if not os.path.exists(full_path):
        logger.warning(f"ASIN履歴が見つかりません（除外なしで続行）: {full_path}")
        return ProductIdentityRegistry()
    try:
        with open(full_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"ASIN履歴の読込に失敗（除外なしで続行）: {e}")
        return ProductIdentityRegistry()
    entries = data.get("posted", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return ProductIdentityRegistry()

    def date_part(value) -> str:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or "").strip())
        return m.group(1) if m else ""

    from datetime import datetime, timedelta, timezone
    today_jst = datetime.now(timezone(timedelta(hours=9))).date()
    cutoff = (today_jst - timedelta(days=max(within_days - 1, 0))).isoformat()
    registry = ProductIdentityRegistry()
    for p in entries:
        if not isinstance(p, dict):
            continue
        asin = str(p.get("asin", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        # 人気順運転では、noteで実際に投稿・予約されたASINだけを出禁にする。
        # scrapedだけでなく、rejected/failed/draft等も投稿実績ではないため除外対象にしない。
        status = str(p.get("status", "")).strip().lower()
        if not include_scraped and status not in {"posted", "published", "reserved", "scheduled"}:
            continue
        active = within_days <= 0
        if not active:
            base = max(date_part(p.get("posted_at")), date_part(p.get("reserved_at")))
            active = not base or base >= cutoff  # 日付不明と未来予約は安全側で除外
        if not active:
            continue
        registry.asins.add(asin)
        if include_product_identities:
            registry.add_identity(extract_product_identity(p))
    return registry


def load_posted_asins(path: str, within_days: int = 0, include_scraped: bool = True) -> set:
    """後方互換用。投稿済み/予約済みASINだけを返す。"""
    return load_product_exclusion_registry(
        path,
        within_days,
        include_scraped,
        include_product_identities=False,
    ).asins


# ============================================
# 既存：一覧ページスクレイピング（無改修）
# ============================================

async def scrape_bestsellers(page: Page, url: str, category: str, max_items: int = 10, associate_tag: str = ASSOCIATE_TAG) -> List[Product]:
    products: List[Product] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(2000, 4000))
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(700)
        items = await page.query_selector_all("#gridItemRoot, .zg-item-immersion")
        logger.info(f"[{category}] {len(items)} 件")
        for i, item in enumerate(items[:max_items]):
            try:
                title_el = await item.query_selector(".p13n-sc-truncated, ._cDEzb_p13n-sc-css-line-clamp-1_1Fn1y, ._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, ._cDEzb_p13n-sc-css-line-clamp-2_EWgCb")
                price_el = await item.query_selector(".p13n-sc-price, ._cDEzb_p13n-sc-price_3mJ9Z")
                img_el = await item.query_selector("img")
                link_el = await item.query_selector("a[href*='/dp/']")
                if not link_el:
                    continue
                href = await link_el.get_attribute("href") or ""
                asin = href.split("/dp/")[1].split("/")[0].split("?")[0] if "/dp/" in href else ""
                if not asin:
                    continue
                title = (await title_el.inner_text()).strip() if title_el else ""
                price = (await price_el.inner_text()).strip() if price_el else ""
                price_int = parse_price(price)
                image_url = await img_el.get_attribute("src") if img_el else ""
                products.append(Product(asin=asin, title=title, price=price or "価格不明", price_int=price_int, original_price="", discount_rate="", image_url=image_url or "", affiliate_url=make_affiliate_url(asin, associate_tag), category=f"{category}#{i+1}", rating="", review_count=""))
            except Exception:
                continue
    except Exception as e:
        logger.error(f"scrape error: {e}")
    return products


async def scrape_timesale(page: Page, url: str, category: str, max_items: int = 15, associate_tag: str = ASSOCIATE_TAG) -> List[Product]:
    products: List[Product] = []
    seen_asins = set()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(3000, 5000))
        for _ in range(10):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)
        links = await page.query_selector_all("a[href*='/dp/']")
        logger.info(f"[{category}] /dp/ {len(links)} 件")
        for link in links:
            if len(products) >= max_items:
                break
            try:
                href = await link.get_attribute("href") or ""
                if "/dp/" not in href:
                    continue
                asin = href.split("/dp/")[1].split("/")[0].split("?")[0]
                if not asin or asin in seen_asins:
                    continue
                card = await link.evaluate_handle("""el => { let n = el; for (let i = 0; i < 6; i++) { n = n.parentElement; if (!n) break; if (n.querySelector('.a-price') || n.querySelector('[data-testid*=\"price\"]')) return n; } return el.closest('[data-testid], .a-section, li, article, div') || el.parentElement; }""")
                price_el = await card.query_selector("[data-testid='price'], .a-price .a-offscreen, .a-price-whole, .p13n-sc-price")
                price = (await price_el.inner_text()).strip() if price_el else ""
                price_int = parse_price(price)
                orig_el = await card.query_selector("[data-testid='original-price'], .a-text-strike, .a-text-price .a-offscreen")
                original_price = (await orig_el.inner_text()).strip() if orig_el else ""
                original_int = parse_price(original_price)
                discount_text_el = await card.query_selector("[data-testid='discount'], [class*='savingPriceDiscount'], [class*='Discount'], .savingsPercentage")
                discount_rate = ""
                if discount_text_el:
                    raw = (await discount_text_el.inner_text()).strip()
                    if "%" in raw or "％" in raw:
                        discount_rate = raw.replace("％", "%")
                if not discount_rate:
                    discount_rate = calc_discount_rate(price_int, original_int)
                title_el = await card.query_selector("[data-testid='title'], .a-truncate-cut, h2, h3, ._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, img[alt]")
                title = ""
                if title_el:
                    inner = (await title_el.inner_text()).strip()
                    title = inner if inner else (await title_el.get_attribute("alt") or "")
                img_el = await card.query_selector("img")
                image_url = await img_el.get_attribute("src") if img_el else ""
                if price_int <= 0 or not title:
                    continue
                seen_asins.add(asin)
                products.append(Product(asin=asin, title=title[:300], price=price or "価格不明", price_int=price_int, original_price=original_price, discount_rate=discount_rate, image_url=image_url or "", affiliate_url=make_affiliate_url(asin, associate_tag), category=f"{category}#{len(products)+1}", rating="", review_count=""))
            except Exception:
                continue
        logger.info(f"[{category}] 取得 {len(products)} 件（割引付 {sum(1 for p in products if p.discount_rate)} 件)")
    except Exception as e:
        logger.error(f"scrape error: {e}")
    return products


# ============================================
# v3新規：検索結果ページスクレイピング（/s?rh=...）
# ============================================
# Amazon検索結果ページは [data-component-type="s-search-result"] で
# 各商品カードがマークアップされており、ベストセラー/タイムセールページより
# 構造が安定している。人気順などの検索結果URL用。

async def scrape_search(page: Page, url: str, category: str, max_items: int = 10, associate_tag: str = ASSOCIATE_TAG, excluded: Optional[set] = None, stats: Optional[dict] = None) -> List[Product]:
    """Amazon検索結果ページ (/s?rh=...) から商品を取得する。

    - s-search-result カードを順に走査
    - スポンサー枠も含めてASIN単位で重複排除
    - 価格・タイトル・画像・割引率を抽出
    - v2.1: 最大2ページ巡回。投稿済みASIN（excluded）は枠を消費せずスキップし、
      ページ深部の新顔で枠を埋める。カテゴリ別統計を stats に記録（死枠診断用）。
    """
    products: List[Product] = []
    seen_asins = set()
    excluded = excluded or set()
    cat_stats: dict = {"pages": [], "taken": 0, "skipped_posted": 0, "error": ""}

    async def _consume_cards(cards) -> None:
        for card in cards:
            if len(products) >= max_items:
                return
            try:
                # ASIN は data-asin 属性から直接取得（最も信頼できる）
                asin = await card.get_attribute("data-asin") or ""
                if not asin or asin in seen_asins:
                    continue
                if asin in excluded:
                    seen_asins.add(asin)
                    cat_stats["skipped_posted"] += 1
                    continue

                # タイトル
                title_el = await card.query_selector(
                    "h2 a span, h2 span, .a-link-normal .a-text-normal, "
                    ".s-line-clamp-2, .s-line-clamp-3, .s-line-clamp-4"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""
                if not title:
                    img_el_for_alt = await card.query_selector("img.s-image, img[alt]")
                    if img_el_for_alt:
                        title = (await img_el_for_alt.get_attribute("alt") or "").strip()
                if not title:
                    continue

                # 価格（販売価格）— .a-offscreen が最も汎用的
                price_el = await card.query_selector(
                    ".a-price:not(.a-text-price) .a-offscreen, "
                    ".a-price .a-offscreen, "
                    ".a-price-whole"
                )
                price = (await price_el.inner_text()).strip() if price_el else ""
                price_int = parse_price(price)

                # 元値（取り消し線）
                orig_el = await card.query_selector(
                    ".a-text-price .a-offscreen, .a-text-strike, "
                    "[data-a-strike='true'] .a-offscreen"
                )
                original_price = (await orig_el.inner_text()).strip() if orig_el else ""
                original_int = parse_price(original_price)

                # 割引率（バッジ/テキスト）
                discount_rate = ""
                discount_el = await card.query_selector(
                    "[class*='savingsPercentage'], [class*='savingPriceDiscount'], "
                    ".a-color-price.s-coupon-highlight-color, "
                    "span.a-color-price:not(.a-offscreen)"
                )
                if discount_el:
                    raw = (await discount_el.inner_text()).strip()
                    # 「ポイント」を含むテキストはAmazonポイント還元率であり、値引き率ではないので無視
                    if "ポイント" not in raw and ("%" in raw or "％" in raw):
                        m = re.search(r"(\d+)\s*[%％]", raw)
                        if m:
                            discount_rate = f"{m.group(1)}%OFF"
                if not discount_rate:
                    discount_rate = calc_discount_rate(price_int, original_int)

                # 画像
                img_el = await card.query_selector("img.s-image, img")
                image_url = await img_el.get_attribute("src") if img_el else ""

                # 評価・レビュー件数（ver5.1・2026-07-24運用裁定：DOMの飾りでなくカード全文の文字列から型で読む）
                # 第一網＝星の代替文「5つ星のうち◯◯」（画面非表示だがHTML内に必ず刷られる読み上げ用定型句）
                # 第二網＝その直後に立つ括弧数字「(5,627)」等をレビュー数として拾う（￥を跨がない＝価格誤食い防止）
                rating = ""
                review_count = ""
                try:
                    card_text = await card.inner_text()
                except Exception:
                    card_text = ""
                m = re.search(r"5つ星のうち\s*([\d.]+)", card_text)
                if m:
                    rating = m.group(1)
                    m2 = re.search(r"5つ星のうち\s*" + re.escape(rating) + r"[^\d￥円]{0,12}([\d,]+)", card_text)
                    if m2:
                        review_count = m2.group(1)
                if not rating:
                    # 旧DOM網（保険）：星アイコンの代替文セレクタ
                    rating_el = await card.query_selector("i[class*='a-icon-star'] .a-icon-alt, .a-icon-alt")
                    if rating_el:
                        rating_text = (await rating_el.inner_text()).strip()
                        rm = re.search(r"([\d.]+)", rating_text)
                        rating = rm.group(1) if rm else ""

                # 価格0は除外（広告枠やSponsoredで価格未取得のケース）
                if price_int <= 0:
                    continue

                seen_asins.add(asin)
                products.append(Product(
                    asin=asin,
                    title=title[:300],
                    price=price or "価格不明",
                    price_int=price_int,
                    original_price=original_price,
                    discount_rate=discount_rate,
                    image_url=image_url or "",
                    affiliate_url=make_affiliate_url(asin, associate_tag),
                    category=f"{category}#{len(products)+1}",
                    rating=rating,
                    review_count=review_count,
                ))
            except Exception:
                continue

    try:
        for page_no in (1, 2):  # v2.1: 最大2ページまで巡回して鮮度を確保
            if len(products) >= max_items:
                break
            page_url = url if page_no == 1 else f"{url}&page={page_no}"
            cards = []
            for attempt in (1, 2):  # v2.2: エラーページ（ご迷惑をおかけしています）検出時は1回だけ再試行
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(random.randint(2500, 4500))
                # 検索結果は遅延読込されることがあるので軽くスクロール
                for _ in range(4):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await page.wait_for_timeout(600)
                cards = await page.query_selector_all('[data-component-type="s-search-result"]')
                if cards:
                    break
                title_text = ""
                try:
                    title_text = (await page.title()) or ""
                except Exception:
                    pass
                is_error_page = "ご迷惑" in title_text or "申し訳" in title_text
                if is_error_page:
                    cat_stats["error_page_hits"] = cat_stats.get("error_page_hits", 0) + 1
                    if attempt == 1:
                        logger.warning(f"[{category}] p{page_no}: Amazonエラーページ検出。トップページ経由で再試行")
                        try:  # v2.3: 直リロードでなくトップページを踏み直してセッション信頼を回復
                            await page.goto("https://www.amazon.co.jp/", wait_until="domcontentloaded", timeout=45000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(random.randint(7000, 12000))
                        continue
                    cat_stats.setdefault("error_page_exhausted_pages", []).append(page_no)
                break
            logger.info(f"[{category}] p{page_no}: s-search-result {len(cards)} 件")
            cat_stats["pages"].append(len(cards))
            if not cards:
                if page_no == 1:
                    try:
                        cat_stats["page1_title"] = ((await page.title()) or "")[:80]
                    except Exception:
                        pass
                break
            await _consume_cards(cards)

    except Exception as e:
        cat_stats["error"] = str(e)[:150]
        logger.error(f"scrape_search error for [{category}]: {e}")
    cat_stats["taken"] = len(products)
    logger.info(
        f"[{category}] 取得 {len(products)} 件"
        f"（割引表示 {sum(1 for p in products if p.discount_rate)} 件・"
        f"投稿済スキップ {cat_stats['skipped_posted']} 件）"
    )
    if stats is not None:
        stats[category] = cat_stats
    return products


def needs_deferred_search_retry(cat_stats: dict) -> bool:
    """Retry a zero-result search after an exhausted Amazon page or transient error."""
    return (
        int(cat_stats.get("taken", 0) or 0) == 0
        and bool(cat_stats.get("error_page_exhausted_pages") or cat_stats.get("error"))
    )


def merge_deferred_search_stats(initial: dict, retry: dict, unique_added: int) -> dict:
    """Preserve first-pass diagnostics while recording the deferred retry result."""
    initial_snapshot = dict(initial)
    retry_snapshot = dict(retry)
    merged = dict(initial_snapshot)
    merged["pages"] = list(initial_snapshot.get("pages", [])) + list(retry_snapshot.get("pages", []))
    merged["taken"] = int(initial_snapshot.get("taken", 0) or 0) + int(unique_added)
    merged["skipped_posted"] = (
        int(initial_snapshot.get("skipped_posted", 0) or 0)
        + int(retry_snapshot.get("skipped_posted", 0) or 0)
    )
    merged["error_page_hits"] = (
        int(initial_snapshot.get("error_page_hits", 0) or 0)
        + int(retry_snapshot.get("error_page_hits", 0) or 0)
    )
    merged["deferred_retry_attempted"] = True
    merged["deferred_retry_recovered"] = unique_added > 0
    merged["deferred_retry_unique_added"] = int(unique_added)
    merged["deferred_retry"] = {
        "attempted": True,
        "recovered": unique_added > 0,
        "unique_added": int(unique_added),
        "initial": initial_snapshot,
        "retry": retry_snapshot,
    }
    return merged


def review_num(product: Product) -> int:
    return int(re.sub(r"[^\d]", "", product.review_count or "") or 0)


def category_key(category: dict) -> str:
    """カテゴリ追加・削除後も追跡できる安定キーを返す。"""
    name = str(category.get("name", "")).strip()
    decoded_url = unquote(str(category.get("url", "")))
    node_match = re.search(r"(?:^|[=,&])n:(\d+)", decoded_url)
    if node_match:
        return f"node:{node_match.group(1)}"
    return f"name:{name}"


def search_url_with_min_price(url: str, min_price: int) -> str:
    """Amazon検索URLへカテゴリ固有の下限価格を付ける（円→p_36の100倍値）。"""
    if min_price <= 0:
        return url
    parsed = urlparse(url)
    if parsed.path != "/s":
        return url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    updated_query = []
    found_rh = False
    for key, value in query:
        if key == "rh":
            found_rh = True
            parts = [part for part in value.split(",") if not part.startswith("p_36:")]
            parts.append(f"p_36:{min_price * 100}-")
            value = ",".join(parts)
        elif key == "low-price":
            value = str(min_price)
        updated_query.append((key, value))
    if not found_rh:
        updated_query.append(("rh", f"p_36:{min_price * 100}-"))
    return urlunparse(parsed._replace(query=urlencode(updated_query)))


def load_rotation_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception as e:
        logger.warning(f"カテゴリ巡回位置の読込失敗（先頭から続行）: {path}: {e}")
        return {}


def ordered_rotation_categories(
    categories: List[dict], rotation_state_file: str
) -> Tuple[List[dict], str]:
    """Return active categories in the saved round-robin order."""
    active_categories = [
        category for category in categories
        if str(category.get("name", "")).strip() and str(category.get("url", "")).strip()
    ]
    if not active_categories:
        return [], "なし"

    state = load_rotation_state(rotation_state_file)
    keys = [category_key(category) for category in active_categories]
    names = [str(category.get("name", "")).strip() for category in active_categories]
    last_key = str(state.get("last_category_key", "")).strip()
    last_name = str(state.get("last_category_name", "")).strip()
    next_key = str(state.get("next_category_key", "")).strip()
    next_name = str(state.get("next_category_name", "")).strip()
    start_index = 0
    if last_key in keys:
        start_index = (keys.index(last_key) + 1) % len(active_categories)
    elif last_name in names:
        start_index = (names.index(last_name) + 1) % len(active_categories)
    elif next_key in keys:
        start_index = keys.index(next_key)
    elif next_name in names:
        start_index = names.index(next_name)
    else:
        try:
            saved_next_position = int(state.get("next_category_position", 1))
        except (TypeError, ValueError):
            saved_next_position = 1
        start_index = max(saved_next_position - 1, 0) % len(active_categories)

    ordered = active_categories[start_index:] + active_categories[:start_index]
    return ordered, last_name or last_key or "なし"


def select_category_round_robin(
    products: List[Product],
    categories: List[dict],
    max_total: int,
    rotation_state_file: str,
) -> List[Product]:
    """各カテゴリの評価数首位を、前回使用カテゴリの次から順番に採用する。"""
    ordered_categories, previous = ordered_rotation_categories(categories, rotation_state_file)
    if not ordered_categories:
        return []

    grouped: dict[str, List[Product]] = {}
    for product in products:
        name = (product.category or "").split("#")[0]
        grouped.setdefault(name, []).append(product)
    for candidates in grouped.values():
        candidates.sort(key=review_num, reverse=True)

    names = [str(category.get("name", "")).strip() for category in ordered_categories]
    active_count = len(ordered_categories)
    limit = active_count if max_total <= 0 else min(max_total, active_count)
    selected: List[Product] = []
    missing: List[str] = []
    for category in ordered_categories:
        name = str(category.get("name", "")).strip()
        candidates = grouped.get(name, [])
        if not candidates:
            missing.append(name)
            continue
        selected.append(candidates[0])
        if len(selected) >= limit:
            break

    logger.info(
        "カテゴリ循環選定: 有効=%d 開始=%s 採用=%d/%d 前回=%s",
        active_count,
        names[0],
        len(selected),
        limit,
        previous,
    )
    if missing:
        logger.warning(f"有効商品なしカテゴリ: {', '.join(missing)}")
    return selected


def filter_and_sort(
    products: List[Product],
    min_price: int = 3000,
    max_price: int = 0,
    sort_order: str = "price_desc",
    max_total: int = 50,
    posted_asins: Optional[set] = None,
    min_discount_pct: int = 0,
    max_per_category: int = 0,
    exclude_title_patterns: Optional[List[str]] = None,
    selection_mode: str = "",
    categories: Optional[List[dict]] = None,
    rotation_state_file: str = "",
    category_min_prices: Optional[dict[str, int]] = None,
) -> List[Product]:
    posted_asins = posted_asins or set()
    seen = set()
    deduped = []
    for p in products:
        if p.asin in seen:
            continue
        seen.add(p.asin)
        deduped.append(p)
    if posted_asins:
        before = len(deduped)
        deduped = [p for p in deduped if p.asin not in posted_asins]
        logger.info(f"投稿済みASIN除外: {before - len(deduped)} 件")
    title_patterns = []
    for pattern in exclude_title_patterns or []:
        try:
            title_patterns.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Invalid exclude_title_patterns entry ignored: {pattern!r} ({e})")
    if title_patterns:
        before = len(deduped)
        deduped = [p for p in deduped if not any(pattern.search(p.title or "") for pattern in title_patterns)]
        logger.info(f"Title pattern exclusions: {before - len(deduped)} items")
    filtered = []
    for p in deduped:
        category_name = (p.category or "").split("#")[0]
        effective_min_price = max(min_price, (category_min_prices or {}).get(category_name, min_price))
        if p.price_int < effective_min_price:
            continue
        if max_price > 0 and p.price_int > max_price:
            continue
        filtered.append(p)
    logger.info(f"フィルタ後: {len(filtered)} 件")

    def discount_pct(p: Product) -> int:
        m = re.search(r"(\d+)%", p.discount_rate or "")
        return int(m.group(1)) if m else 0

    def discount_amount(p: Product) -> int:
        """割引額（円）。元値があれば実額、無ければ割引率から逆算する。"""
        orig = parse_price(p.original_price)
        if orig > p.price_int > 0:
            return orig - p.price_int
        pct = discount_pct(p)
        if 0 < pct < 100 and p.price_int > 0:
            return int(p.price_int * pct / (100 - pct))
        return 0

    low_pool: List[Product] = []
    if min_discount_pct > 0:
        # ver2.6: 下限をソフト化。10%未満は「除外」ではなく後備に降格し、
        # 正規プールで max_total に届かない日だけ割引額の大きい順に補充する（34件死守）
        low_pool = [p for p in filtered if discount_pct(p) < min_discount_pct]
        filtered = [p for p in filtered if discount_pct(p) >= min_discount_pct]
        logger.info(f"割引率 {min_discount_pct}% 未満: {len(low_pool)} 件を後備へ降格（不足時のみ補充・ver2.6）")

    if selection_mode == "category_round_robin":
        if low_pool:
            filtered = filtered + low_pool
        return select_category_round_robin(
            filtered,
            categories or [],
            max_total,
            rotation_state_file,
        )

    if sort_order == "sale_first":
        filtered.sort(key=lambda p: (1 if p.discount_rate else 0, discount_pct(p), p.price_int), reverse=True)
    elif sort_order == "amount_first":  # v2.4実装: ①割引有無 → ②割引"額"(円) → ③価格
        filtered.sort(key=lambda p: (1 if p.discount_rate else 0, discount_amount(p), p.price_int), reverse=True)
    elif sort_order == "review_desc":
        # ver4.0（2026-07-24運用裁定・人気順運転）: 各棚の人気上位を「レビュー数」ただ一つで横断番付する。
        # 棚同士の1位は素では比較不能のため、レビュー数を人気の近似に用いる。
        # 評価（★）は水増し警戒で不使用（収集は継続＝分析用）。第二鍵も置かない——1万の位で同数はまず起きず、
        # 万一の同数は浚った順のまま（安定ソート＝決定的で説明可能。ランダムは再現性が消えるため不採用）。
        filtered.sort(key=review_num, reverse=True)
    elif sort_order == "price_desc":
        filtered.sort(key=lambda x: x.price_int, reverse=True)
    elif sort_order == "price_asc":
        filtered.sort(key=lambda x: x.price_int)
    elif sort_order == "discount_desc":
        filtered.sort(key=discount_pct, reverse=True)
    if low_pool:
        # 後備は常に「割引有無→割引額→価格」で並べ、正規プールの後ろへ接続
        low_pool.sort(key=lambda p: (1 if p.discount_rate else 0, discount_amount(p), p.price_int), reverse=True)
        filtered = filtered + low_pool
    if max_per_category > 0 and max_total > 0:
        # ver2.5: 同一カテゴリの独占防止（額順は高単価カテゴリが上位を占めやすいため）
        picked: List[Product] = []
        overflow: List[Product] = []
        counts: dict = {}
        for p in filtered:
            c = (p.category or "").split("#")[0]
            if counts.get(c, 0) < max_per_category:
                picked.append(p)
                counts[c] = counts.get(c, 0) + 1
            else:
                overflow.append(p)
            if len(picked) >= max_total:
                break
        if len(picked) < max_total and overflow:
            need = max_total - len(picked)
            picked.extend(overflow[:need])
            logger.info(f"カテゴリ上限を超えて {need} 件補充（母数不足時の安全弁・34件死守）")
        filtered = picked
        logger.info(f"カテゴリ上限 {max_per_category} 件適用: {len(filtered)} 件／使用カテゴリ {len(counts)} 種")
    elif max_total > 0:
        filtered = filtered[:max_total]
    return filtered


# ============================================
# v2新規：個別商品ページから description / specs を取得
# ============================================

DESCRIPTION_SELECTORS = [
    "#productDescription",
    "#feature-bullets",
    "#aplus_feature_div",
    "#bookDescription_feature_div",
    "#renewedProgramDescriptionAndFAQHybrid_feature_div",
    ".a-unordered-list.a-vertical.a-spacing-mini",
]

SPECS_SELECTORS = [
    "#productDetails_techSpec_section_1",
    "#productDetails_techSpec_section_2",
    "#productDetails_detailBullets_sections1",
    "#detailBullets_feature_div",
    "#technicalSpecifications_feature_div",
    "#productDetails_db_sections",
    ".prodDetTable",
]


def _clean_text(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[ \t]+", " ", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "..."
    return cleaned


async def _try_selectors(page: Page, selectors: List[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return _clean_text(text)
        except Exception:
            continue
    return ""


async def scrape_product_detail(page: Page, asin: str) -> Tuple[str, str]:
    url = f"https://www.amazon.co.jp/dp/{asin}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(800, 1500))
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(400)
        description = await _try_selectors(page, DESCRIPTION_SELECTORS)
        specs = await _try_selectors(page, SPECS_SELECTORS)
        return description, specs
    except Exception as e:
        logger.error(f"detail page error for {asin}: {e}")
        return "", ""


async def enrich_product(page: Page, product: Product) -> bool:
    """Populate one product's detail fields and report whether any were found."""
    try:
        description, specs = await scrape_product_detail(page, product.asin)
        product.description = description
        product.specs = specs
        return bool(description or specs)
    except Exception as e:
        logger.error(f"  enrich failed for {product.asin}: {e}")
        product.description = ""
        product.specs = ""
        return False


async def enrich_products(page: Page, products: List[Product]) -> None:
    total = len(products)
    success = 0
    for i, p in enumerate(products):
        logger.info(f"  [{i+1}/{total}] enrich {p.asin} - {p.title[:40]}")
        if await enrich_product(page, p):
            success += 1
        await asyncio.sleep(random.uniform(2, 4))
    logger.info(f"enrich完了: {success}/{total} 件で説明文/スペック取得成功")


async def select_enrich_unique_products(
    page: Page,
    products: List[Product],
    registry: ProductIdentityRegistry,
    categories: List[dict],
    max_total: int,
    selection_mode: str,
    rotation_state_file: str,
    stats: dict,
) -> List[Product]:
    """Enrich candidates in rank order, excluding stable-identifier matches.

    In category round-robin mode, an excluded category leader is replaced by
    the next ranked item from the same category.  Thus an identity exclusion
    does not consume one of the requested output slots.
    """
    selected: List[Product] = []
    skipped_reasons: dict[str, int] = {}

    def record_skip(product: Product, reason: str) -> None:
        prefix = reason.split(":", 1)[0]
        skipped_reasons[prefix] = skipped_reasons.get(prefix, 0) + 1
        logger.info(f"同一商品識別子で除外: {product.asin} ({reason})")

    async def consider(candidate: Product) -> bool:
        logger.info(f"  identity check {candidate.asin} - {candidate.title[:40]}")
        if not candidate.specs:
            await enrich_product(page, candidate)
        identity = extract_product_identity(candidate)
        reason = registry.match_identity(identity)
        if reason:
            record_skip(candidate, reason)
            await asyncio.sleep(random.uniform(2, 4))
            return False
        selected.append(candidate)
        registry.add_identity(identity)  # 同一スクレイピング内の別ASIN重複も防ぐ
        await asyncio.sleep(random.uniform(2, 4))
        return True

    if selection_mode == "category_round_robin":
        ordered_categories, previous = ordered_rotation_categories(categories, rotation_state_file)
        grouped: dict[str, List[Product]] = {}
        for product in products:
            name = (product.category or "").split("#")[0]
            grouped.setdefault(name, []).append(product)
        for candidates in grouped.values():
            candidates.sort(key=review_num, reverse=True)

        limit = len(ordered_categories) if max_total <= 0 else min(max_total, len(ordered_categories))
        missing: List[str] = []
        for category in ordered_categories:
            name = str(category.get("name", "")).strip()
            candidates = grouped.get(name, [])
            accepted = False
            for candidate in candidates:
                if await consider(candidate):
                    accepted = True
                    break
            if not accepted:
                missing.append(name)
            if len(selected) >= limit:
                break
        logger.info(
            "識別子対応カテゴリ循環選定: 有効=%d 開始=%s 採用=%d/%d 前回=%s",
            len(ordered_categories),
            str(ordered_categories[0].get("name", "")) if ordered_categories else "なし",
            len(selected),
            limit,
            previous,
        )
        if missing:
            logger.warning(f"同一商品除外後も有効商品なしカテゴリ: {', '.join(missing)}")
    else:
        limit = len(products) if max_total <= 0 else max_total
        for candidate in products:
            await consider(candidate)
            if len(selected) >= limit:
                break

    stats["_skipped_product_identity"] = sum(skipped_reasons.values())
    stats["_skipped_product_identity_reasons"] = skipped_reasons
    return selected


# ============================================
# ASIN履歴：取得した商品も既定日数の再登場を防ぐ
# ============================================

def save_scraped_asins_to_history(products: List[Product], config_path: str, date_tag: str, output_path: str) -> None:
    """当日のスクレイプ採用品をアカウント別ASIN履歴へ保存する。

    投稿済みだけでなく、前日に取得した商品そのものも除外窓の対象にする。
    同日・同一ASINの投稿済み詳細がある場合は上書きしない。
    """
    config = load_config(config_path)
    if not bool(config.get("exclusion", {}).get("exclude_scraped_candidates", True)):
        logger.info("スクレイプ候補のASIN履歴保存を省略（投稿・予約成功分だけを同期する人気順運転）")
        return
    posted_path = config.get("exclusion", {}).get("posted_asins_file", "posted_asins.json")
    history_path = posted_path if os.path.isabs(posted_path) else os.path.join(os.path.dirname(__file__), posted_path)
    os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)

    history = {
        "schema": "note-amazon-asin-history-v1",
        "updated_at": "",
        "description": "ASIN exclusion history",
        "posted": [],
    }
    if os.path.exists(history_path):
        try:
            with open(history_path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                history.update(loaded)
        except Exception as e:
            logger.warning(f"ASIN履歴の更新前読込に失敗（新規作成）: {e}")

    entries = history.get("posted", [])
    if not isinstance(entries, list):
        entries = []

    def entry_date(entry: dict) -> str:
        for name in ("posted_at", "reserved_at"):
            match = re.match(r"(\d{4}-\d{2}-\d{2})", str(entry.get(name, "")))
            if match:
                return match.group(1)
        return "unknown"

    existing = {
        (str(entry.get("asin", "")).strip().upper(), entry_date(entry))
        for entry in entries if isinstance(entry, dict)
    }
    account_match = re.search(r"data[/\\](account\d+)[/\\]", output_path)
    account_id = account_match.group(1) if account_match else "unknown"
    added = 0
    for index, product in enumerate(products, 1):
        asin = str(product.asin).strip().upper()
        key = (asin, date_tag)
        if key in existing:
            continue
        entries.append({
            "asin": asin,
            "title": product.title,
            "status": "scraped",
            "posted_at": f"{date_tag}T00:00:00+09:00",
            "reserved_at": None,
            "account_id": account_id,
            "account_name": account_id,
            "note_url": None,
            "edit_url": None,
            "thumbnail_path": None,
            "source_file": output_path.replace("\\", "/"),
            "source_index": index,
        })
        existing.add(key)
        added += 1

    from datetime import datetime, timedelta, timezone
    history["updated_at"] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    history["description"] = (
        f"Single source of truth for {account_id} ASIN exclusion. "
        "Scraped and posted ASINs are retained for the configured exclusion window."
    )
    history["posted"] = sorted(entries, key=lambda e: (entry_date(e), str(e.get("asin", ""))))
    temp_path = history_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, history_path)
    logger.info(f"スクレイプASIN履歴保存: added={added} total={len(entries)} / {history_path}")


# ============================================
# メイン処理：一覧→フィルタ→個別ページ取得
# ============================================

async def fetch_products(config_path: str = CONFIG_PATH, associate_tag: str = ASSOCIATE_TAG) -> Tuple[List[Product], dict]:
    config = load_config(config_path)
    cats = config.get("categories", [])
    flt = config.get("filters", {})
    min_price = int(flt.get("min_price", 3000))
    category_min_prices = {
        str(cat.get("name", "")).strip(): max(min_price, int(cat.get("min_price", min_price)))
        for cat in cats
        if str(cat.get("name", "")).strip()
    }
    max_price = int(flt.get("max_price", 0))
    sort_order = str(flt.get("sort_order", "price_desc"))
    max_total = int(flt.get("max_total_items", 50))
    min_discount_pct = int(flt.get("min_discount_pct", 0))
    max_per_category = int(flt.get("max_per_category", 0))
    selection_mode = str(flt.get("selection_mode", "")).strip()
    rotation_state_file = str(flt.get("rotation_state_file", "")).strip()
    if rotation_state_file and not os.path.isabs(rotation_state_file):
        rotation_state_file = os.path.join(os.path.dirname(config_path), rotation_state_file)
    exclude_title_patterns = [str(pattern) for pattern in flt.get("exclude_title_patterns", []) or []]
    excl = config.get("exclusion", {})
    posted_path = excl.get("posted_asins_file", "posted_asins.json")
    within_days = int(excl.get("exclude_within_days", 0))
    include_scraped = bool(excl.get("exclude_scraped_candidates", True))  # ver4.3: false=浚っただけの候補は焼かない（人気順用）
    exclude_product_identifiers = bool(excl.get("exclude_product_identifiers", False))
    product_registry = load_product_exclusion_registry(
        posted_path,
        within_days,
        include_scraped,
        include_product_identities=exclude_product_identifiers,
    )
    posted_asins = product_registry.asins
    logger.info(
        "投稿済み商品除外: ASIN=%d GTIN=%d ブランド型番=%d "
        "（除外窓 %d 日 / %s）",
        len(posted_asins),
        len(product_registry.global_trade_numbers),
        len(product_registry.brand_model_keys),
        within_days,
        posted_path,
    )
    # ver2.8: 恒久除外リスト（運用裁定 2026-07-12: カテゴリ誤登録商品等、二度と扱わないASIN）
    blocked_asins = {str(a).strip().upper() for a in excl.get("blocked_asins", []) or []
                     if re.fullmatch(r"[A-Z0-9]{10}", str(a).strip().upper())}
    if blocked_asins:
        posted_asins |= blocked_asins
        logger.info(f"恒久除外ASIN: {len(blocked_asins)} 件（blocked_asins）")
    all_products: List[Product] = []
    scrape_stats: dict = {
        "_excluded_loaded": len(posted_asins),
        "_excluded_gtins_loaded": len(product_registry.global_trade_numbers),
        "_excluded_brand_models_loaded": len(product_registry.brand_model_keys),
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        async def new_context_and_page() -> Tuple[BrowserContext, Page]:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="ja-JP",
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
            )
            # ver2.9: 海外IP起因の通貨自動換算(USD表示)防止。
            await context.add_cookies([
                {"name": "i18n-prefs", "value": "JPY", "domain": ".amazon.co.jp", "path": "/"},
                {"name": "lc-main", "value": "ja_JP", "domain": ".amazon.co.jp", "path": "/"},
            ])
            page = await context.new_page()
            try:
                await page.goto("https://www.amazon.co.jp/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(random.randint(4000, 6000))
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(random.randint(1500, 2500))
            except Exception as e:
                logger.warning(f"ウォームアップ失敗（続行）: {e}")
            return context, page

        context, page = await new_context_and_page()
        # Phase 1: 各カテゴリから商品リスト収集
        for cat in cats:
            name = cat.get("name", "unknown")
            url = cat.get("url", "")
            max_items = int(cat.get("max_items", 5))
            category_min_price = category_min_prices.get(str(name).strip(), min_price)
            # 取得方式判定（優先度: is_search > is_timesale > bestseller）
            is_search = bool(cat.get("is_search", False))
            is_timesale = bool(cat.get("is_timesale", False))
            if not url:
                continue
            logger.info(f"=== {name} 開始 ===")
            try:
                if is_search:
                    search_url = search_url_with_min_price(url, category_min_price)
                    products = await scrape_search(page, search_url, name, max_items, associate_tag, excluded=posted_asins, stats=scrape_stats)
                elif is_timesale:
                    products = await scrape_timesale(page, url, name, max_items, associate_tag)
                else:
                    products = await scrape_bestsellers(page, url, name, max_items, associate_tag)
                all_products.extend(products)
                logger.info(f"=== {name} 完了: {len(products)} 件 ===")
            except Exception as e:
                logger.error(f"=== {name} 失敗: {e} ===")
            await asyncio.sleep(random.uniform(2, 4))

        deferred_retry_categories = [
            cat
            for cat in cats
            if bool(cat.get("is_search"))
            and bool(cat.get("url"))
            and needs_deferred_search_retry(
                scrape_stats.get(str(cat.get("name", "unknown")), {})
            )
        ]
        if deferred_retry_categories:
            logger.warning(
                "Amazonエラーページ未回復カテゴリを遅延再巡回: %d 件",
                len(deferred_retry_categories),
            )
            await context.close()
            await asyncio.sleep(random.uniform(30, 45))
            context, page = await new_context_and_page()
            known_asins = {product.asin for product in all_products}

            for retry_index, cat in enumerate(deferred_retry_categories):
                name = str(cat.get("name", "unknown"))
                url = str(cat.get("url", ""))
                max_items = int(cat.get("max_items", 5))
                category_min_price = category_min_prices.get(name, min_price)
                retry_stats: dict = {}
                try:
                    retried = await scrape_search(
                        page,
                        search_url_with_min_price(url, category_min_price),
                        name,
                        max_items,
                        associate_tag,
                        excluded=posted_asins,
                        stats=retry_stats,
                    )
                except Exception as exc:
                    retried = []
                    retry_stats[name] = {
                        "pages": [],
                        "taken": 0,
                        "skipped_posted": 0,
                        "error": str(exc)[:150],
                    }

                unique_retried = [product for product in retried if product.asin not in known_asins]
                all_products.extend(unique_retried)
                known_asins.update(product.asin for product in unique_retried)
                scrape_stats[name] = merge_deferred_search_stats(
                    scrape_stats.get(name, {}),
                    retry_stats.get(name, {}),
                    len(unique_retried),
                )
                logger.info(
                    "[%s] 遅延再巡回: 新規 %d 件 / 回復=%s",
                    name,
                    len(unique_retried),
                    bool(unique_retried),
                )
                if retry_index < len(deferred_retry_categories) - 1:
                    await asyncio.sleep(random.uniform(3, 6))
        logger.info(f"全カテゴリ合計: {len(all_products)} 件")
        # Phase 2: 重複除去・価格フィルタ・ソート
        if exclude_product_identifiers:
            # 全候補を残し、仕様取得後の識別子除外で首位が落ちたカテゴリは
            # 同じカテゴリの次点を繰り上げる。
            candidates = filter_and_sort(
                all_products,
                min_price=min_price,
                max_price=max_price,
                sort_order=sort_order,
                max_total=0,
                posted_asins=posted_asins,
                min_discount_pct=min_discount_pct,
                max_per_category=0,
                exclude_title_patterns=exclude_title_patterns,
                selection_mode="",
                categories=cats,
                rotation_state_file=rotation_state_file,
                category_min_prices=category_min_prices,
            )
            logger.info(f"=== 商品識別子確認開始: 候補 {len(candidates)} 件 ===")
            filtered = await select_enrich_unique_products(
                page,
                candidates,
                product_registry,
                cats,
                max_total,
                selection_mode,
                rotation_state_file,
                scrape_stats,
            )
        else:
            filtered = filter_and_sort(
                all_products,
                min_price=min_price,
                max_price=max_price,
                sort_order=sort_order,
                max_total=max_total,
                posted_asins=posted_asins,
                min_discount_pct=min_discount_pct,
                max_per_category=max_per_category,
                exclude_title_patterns=exclude_title_patterns,
                selection_mode=selection_mode,
                categories=cats,
                rotation_state_file=rotation_state_file,
                category_min_prices=category_min_prices,
            )
            # Phase 3: 個別商品ページから description / specs を取得
            if filtered:
                logger.info(f"=== 個別商品ページ取得開始: {len(filtered)} 件 ===")
                await enrich_products(page, filtered)
        await browser.close()
    return filtered, scrape_stats


def fetch_and_save(output_path: str = "products.json", config_path: str = CONFIG_PATH, associate_tag: str = ASSOCIATE_TAG) -> List[Product]:
    logger.info(f"=== Playwright スクレイピング開始: {config_path} ===")
    products, scrape_stats = asyncio.run(fetch_products(config_path, associate_tag))
    validate_affiliate_output(products, output_path, config_path, associate_tag)
    logger.info(f"Amazon affiliate URL check OK: {associate_tag} ({len(products)} items)")
    logger.info(f"最終取得: {len(products)} 件")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)
    # v2.1: カテゴリ別の取得サマリを隣へ保存（死枠診断・運転記録用）
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(output_path))
    date_tag = m.group(1) if m else "latest"
    summary_path = os.path.join(os.path.dirname(output_path) or ".", f"scrape_summary_{date_tag}.json")
    summary = {
        "date": date_tag,
        "total_taken": len(products),
        "excluded_loaded": scrape_stats.pop("_excluded_loaded", 0),
        "excluded_gtins_loaded": scrape_stats.pop("_excluded_gtins_loaded", 0),
        "excluded_brand_models_loaded": scrape_stats.pop("_excluded_brand_models_loaded", 0),
        "skipped_product_identity": scrape_stats.pop("_skipped_product_identity", 0),
        "skipped_product_identity_reasons": scrape_stats.pop("_skipped_product_identity_reasons", {}),
        "categories": scrape_stats,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info(f"カテゴリ別サマリ保存: {summary_path}")
    save_scraped_asins_to_history(products, config_path, date_tag, output_path)
    for i, p in enumerate(products[:5]):
        logger.info(
            f"  TOP{i+1}: {p.price} - {p.title[:40]}... "
            f"[{p.discount_rate or '通常'}] desc={len(p.description)}c specs={len(p.specs)}c"
        )
    return products


if __name__ == "__main__":
    fetch_and_save()
