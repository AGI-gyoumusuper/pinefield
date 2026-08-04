"""High-confidence product identity extraction and matching.

Only stable identifiers are used:

* ASIN (handled by the caller)
* valid JAN/EAN/UPC/GTIN values, canonicalized as GTIN-14
* brand + manufacturer model number

Descriptions, images, and semantic similarity are intentionally excluded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


GTIN_LENGTHS = {8, 12, 13, 14}
MODEL_DASHES = "-‐‑‒–—―−﹘﹣－"
_MODEL_SEPARATORS = re.compile(rf"[\s{re.escape(MODEL_DASHES)}]+")
_BRAND_SPACE = re.compile(r"\s+")
_PAREN_CONTENT = re.compile(r"[（(]([^()（）]+)[）)]")
_INVALID_MODELS = {
    "",
    "NONE",
    "NULL",
    "UNKNOWN",
    "NOTAPPLICABLE",
    "NA",
    "N/A",
    "なし",
    "該当なし",
    "不明",
}
_PLACEHOLDER_BRANDS = {
    "GENERIC",
    "NOBRAND",
    "OEM",
    "UNBRANDED",
    "UNKNOWN",
    "ジェネリック",
    "ノーブランド",
    "ノーブランド品",
    "無印",
}

# Amazon's details table is flattened into whitespace-separated text by
# Playwright.  These labels provide conservative field boundaries.
_SPEC_LABELS = (
    "GTIN (Global Trade Identification Number)",
    "Global Trade Identification Number",
    "商品モデル番号",
    "メーカー型番",
    "品番・型番",
    "ブランド名",
    "ブランド",
    "モデル名",
    "モデル",
    "型番",
    "JANコード",
    "JAN",
    "EAN",
    "UPC",
    "GTIN",
    "メーカー名",
    "お客様の年齢層",
    "商品の推奨用途",
    "商品の用途",
    "対象となる動植物",
    "対象",
    "犬種サイズ",
    "特殊機能",
    "セット名",
    "セット数",
    "世代",
    "スタイル名",
    "構成",
    "サイズ",
    "カラー",
    "色",
    "材質",
    "素材",
    "容量",
    "電源",
    "電圧",
    "ワット数",
    "ユニット数",
    "パッケージ内に含まれる商品の数",
    "生産国",
    "原産国",
    "付属コンポーネント",
    "同梱コンポーネント",
    "付属品",
    "同梱商品",
    "商品タイプ名",
    "商品種別",
    "商品の重量",
    "保証の説明",
    "保証内容",
    "Amazon 売れ筋ランキング",
    "ASIN",
    "おすすめ度",
)
_LABEL_BOUNDARY = "|".join(
    re.escape(label) for label in sorted(_SPEC_LABELS, key=len, reverse=True)
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping_value(data: dict[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if value is not None and _text(value):
            return _text(value)
    return ""


def _iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple, set)):
        return value
    if value in (None, ""):
        return ()
    return (value,)


def normalize_brand(value: Any) -> str:
    """Normalize only Unicode width, case, and whitespace for a brand."""
    raw = _text(value).replace("®", "").replace("™", "").replace("℠", "")
    text = unicodedata.normalize("NFKC", raw).upper()
    return _BRAND_SPACE.sub("", text).strip()


def brand_aliases(value: Any) -> tuple[str, ...]:
    """Return conservative aliases for ``日本語(English)`` style brands."""
    raw = unicodedata.normalize("NFKC", _text(value))
    if not raw:
        return ()
    parenthetical = [match.group(1) for match in _PAREN_CONTENT.finditer(raw)]
    candidates = list(parenthetical)
    outside = _PAREN_CONTENT.sub("", raw).strip()
    if outside:
        candidates.append(outside)
    if not parenthetical:
        candidates.append(raw)
    normalized = {normalize_brand(candidate) for candidate in candidates}
    return tuple(
        sorted(
            value
            for value in normalized
            if value and value not in _PLACEHOLDER_BRANDS
        )
    )


def normalize_model(value: Any) -> str:
    """Normalize model-number case, whitespace, and hyphen variants."""
    text = unicodedata.normalize("NFKC", _text(value)).upper()
    return _MODEL_SEPARATORS.sub("", text).strip()


def _valid_model(value: str) -> bool:
    normalized = normalize_model(value)
    return len(normalized) >= 4 and normalized not in _INVALID_MODELS


def canonical_gtin(value: Any) -> str:
    """Return a validated GTIN-14 representation or an empty string."""
    digits = re.sub(r"\D", "", _text(value))
    if len(digits) not in GTIN_LENGTHS:
        return ""
    body = digits[:-1]
    expected = int(digits[-1])
    total = 0
    for index, digit in enumerate(reversed(body), 1):
        total += int(digit) * (3 if index % 2 == 1 else 1)
    check = (10 - total % 10) % 10
    return digits.zfill(14) if check == expected else ""


def _labeled_value(specs: str, label: str) -> str:
    pattern = re.compile(
        rf"(?:^|\s){re.escape(label)}\s+(.+?)(?=\s+(?:{_LABEL_BOUNDARY})(?:\s|$)|$)",
        re.IGNORECASE,
    )
    match = pattern.search(specs)
    return match.group(1).strip() if match else ""


def _labeled_values(specs: str, labels: Iterable[str]) -> list[str]:
    values: list[str] = []
    for label in labels:
        value = _labeled_value(specs, label)
        if value:
            values.append(value)
    return values


def _codes_from_value(value: Any) -> set[str]:
    result: set[str] = set()
    for digits in re.findall(r"(?<!\d)\d{8,14}(?!\d)", _text(value)):
        normalized = canonical_gtin(digits)
        if normalized:
            result.add(normalized)
    return result


def normalize_brand_model_key(value: Any) -> str:
    raw = _text(value)
    if "::" not in raw:
        return ""
    brand, model = raw.rsplit("::", 1)
    brand_key = normalize_brand(brand)
    model_key = normalize_model(model)
    if not brand_key or not _valid_model(model_key):
        return ""
    return f"{brand_key}::{model_key}"


@dataclass(frozen=True)
class ProductIdentity:
    brand: str = ""
    manufacturer_model: str = ""
    brand_model_keys: tuple[str, ...] = ()
    global_trade_numbers: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.brand_model_keys or self.global_trade_numbers)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.brand:
            value["brand"] = self.brand
        if self.manufacturer_model:
            value["manufacturer_model"] = self.manufacturer_model
        if self.brand_model_keys:
            value["brand_model_keys"] = list(self.brand_model_keys)
        if self.global_trade_numbers:
            value["global_trade_numbers"] = list(self.global_trade_numbers)
        return value


def merge_product_identities(*identities: ProductIdentity) -> ProductIdentity:
    brand = next((identity.brand for identity in identities if identity.brand), "")
    model = next(
        (identity.manufacturer_model for identity in identities if identity.manufacturer_model),
        "",
    )
    brand_models = {
        normalize_brand_model_key(key)
        for identity in identities
        for key in identity.brand_model_keys
        if normalize_brand_model_key(key)
    }
    codes = {
        canonical_gtin(code)
        for identity in identities
        for code in identity.global_trade_numbers
        if canonical_gtin(code)
    }
    return ProductIdentity(
        brand=brand,
        manufacturer_model=model,
        brand_model_keys=tuple(sorted(brand_models)),
        global_trade_numbers=tuple(sorted(codes)),
    )


def identity_from_key(value: Any) -> ProductIdentity:
    key = normalize_brand_model_key(value)
    if not key:
        return ProductIdentity()
    brand, model = _text(value).rsplit("::", 1)
    return ProductIdentity(
        brand=brand.strip(),
        manufacturer_model=model.strip(),
        brand_model_keys=(key,),
    )


def extract_product_identity(value: Any) -> ProductIdentity:
    """Extract only structured identifiers from a product/history mapping."""
    if hasattr(value, "__dict__") and not isinstance(value, dict):
        data = dict(vars(value))
    elif isinstance(value, dict):
        data = value
    else:
        return ProductIdentity()

    embedded = data.get("product_identity")
    embedded_identity = ProductIdentity()
    if isinstance(embedded, dict):
        embedded_keys = {
            normalize_brand_model_key(key)
            for key in _iter_values(embedded.get("brand_model_keys"))
        }
        embedded_codes = {
            canonical_gtin(code)
            for code in _iter_values(embedded.get("global_trade_numbers"))
        }
        embedded_identity = ProductIdentity(
            brand=_text(embedded.get("brand")),
            manufacturer_model=_text(embedded.get("manufacturer_model")),
            brand_model_keys=tuple(sorted(key for key in embedded_keys if key)),
            global_trade_numbers=tuple(sorted(code for code in embedded_codes if code)),
        )

    specs = _text(data.get("specs"))
    brand = _mapping_value(data, "brand", "brand_name", "brandName")
    if not brand and specs:
        brand = _labeled_value(specs, "ブランド名") or _labeled_value(specs, "ブランド")

    manufacturer_model = _mapping_value(
        data,
        "manufacturer_model",
        "manufacturer_model_number",
        "manufacturerModelNumber",
        "model_number",
        "modelNumber",
    )
    if not manufacturer_model and specs:
        for label in ("メーカー型番", "商品モデル番号", "品番・型番", "型番"):
            candidate = _labeled_value(specs, label)
            if _valid_model(candidate):
                manufacturer_model = candidate
                break

    brand_model_keys: set[str] = set()
    if brand and _valid_model(manufacturer_model):
        model_key = normalize_model(manufacturer_model)
        brand_model_keys.update(f"{alias}::{model_key}" for alias in brand_aliases(brand))

    codes: set[str] = set()
    for field in (
        "gtin",
        "GTIN",
        "jan",
        "JAN",
        "ean",
        "EAN",
        "upc",
        "UPC",
        "global_trade_number",
        "global_trade_numbers",
    ):
        raw = data.get(field)
        for item in _iter_values(raw):
            codes.update(_codes_from_value(item))
    if specs:
        for raw in _labeled_values(
            specs,
            (
                "GTIN (Global Trade Identification Number)",
                "Global Trade Identification Number",
                "GTIN",
                "JANコード",
                "JAN",
                "EAN",
                "UPC",
            ),
        ):
            codes.update(_codes_from_value(raw))

    extracted = ProductIdentity(
        brand=brand,
        manufacturer_model=manufacturer_model,
        brand_model_keys=tuple(sorted(brand_model_keys)),
        global_trade_numbers=tuple(sorted(codes)),
    )
    return merge_product_identities(embedded_identity, extracted)


@dataclass
class ProductIdentityRegistry:
    asins: set[str] = field(default_factory=set)
    global_trade_numbers: set[str] = field(default_factory=set)
    brand_model_keys: set[str] = field(default_factory=set)

    def add_identity(self, identity: ProductIdentity) -> None:
        self.global_trade_numbers.update(identity.global_trade_numbers)
        self.brand_model_keys.update(identity.brand_model_keys)

    def match_identity(self, identity: ProductIdentity) -> str:
        codes = self.global_trade_numbers.intersection(identity.global_trade_numbers)
        if codes:
            return "GTIN:" + sorted(codes)[0]
        keys = self.brand_model_keys.intersection(identity.brand_model_keys)
        if keys:
            return "BRAND_MODEL:" + sorted(keys)[0]
        return ""
