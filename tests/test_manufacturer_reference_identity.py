import unittest
from unittest.mock import AsyncMock, patch

from product_identity import ProductIdentityRegistry, extract_product_identity
from scraper import Product, select_enrich_unique_products


# Exact public detailBullets captured for account3 on 2026-09-10.
# Bidi controls are escaped explicitly; only the ASIN differs between specs.
FIXTURE = [{'asin': 'B0FVXRZZV6', 'specs': '製品サイズ \u200f : \u200e 23 x 8 x 17 cm; 495 g\nメーカー \u200f : \u200e Wcuelko\nASIN \u200f : \u200e B0FVXRZZV6\n製造元リファレンス \u200f : \u200e F28-1\nカスタマーレビュー: 4.3 \n5つ星のうち4.3\n (108)'}, {'asin': 'B0H4QHL1TW', 'specs': '製品サイズ \u200f : \u200e 23 x 8 x 17 cm; 495 g\nメーカー \u200f : \u200e Wcuelko\nASIN \u200f : \u200e B0H4QHL1TW\n製造元リファレンス \u200f : \u200e F28-1\nカスタマーレビュー: 4.3 \n5つ星のうち4.3\n (108)'}]


class ManufacturerReferenceIdentityTests(unittest.TestCase):
    def test_real_asin_variants_match_the_explicit_manufacturer_reference(self):
        first, second = [extract_product_identity(row) for row in FIXTURE]
        self.assertEqual('Wcuelko', first.brand)
        self.assertEqual('F28-1', first.manufacturer_model)
        self.assertEqual(('WCUELKO::F281',), first.brand_model_keys)
        self.assertEqual(first, second)
        registry = ProductIdentityRegistry()
        registry.add_identity(first)
        self.assertEqual('BRAND_MODEL:WCUELKO::F281', registry.match_identity(second))

    def test_colon_width_direction_marks_and_newline_field_end(self):
        for separator in (' : ', '：', ' \u200f : \u200e ', ' '):
            with self.subTest(separator=repr(separator)):
                specs = f'メーカー{separator}Wcuelko\nASIN{separator}B000000001\n製造元リファレンス{separator}F28-1\nカスタマーレビュー: 4.3'
                identity = extract_product_identity({'specs': specs})
                self.assertEqual(('WCUELKO::F281',), identity.brand_model_keys)
                self.assertEqual('F28-1', identity.manufacturer_model)

    def test_flattened_fields_have_explicit_boundaries(self):
        identity = extract_product_identity({'specs': 'メーカー Wcuelko 製造元リファレンス F28-1 ASIN B000000001'})
        self.assertEqual(('WCUELKO::F281',), identity.brand_model_keys)

    def test_different_manufacturer_or_reference_does_not_match(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(extract_product_identity(FIXTURE[0]))
        for before, after in [('Wcuelko', 'OtherMaker'), ('F28-1', 'F28-2')]:
            changed = {'specs': FIXTURE[1]['specs'].replace(before, after)}
            self.assertEqual('', registry.match_identity(extract_product_identity(changed)))

    def test_manufacturer_without_explicit_reference_is_not_a_brand(self):
        for model in ('メーカー型番 F28-1', '商品モデル番号 F28-1', '型番 F28-1', ''):
            identity = extract_product_identity({'specs': f'メーカー Wcuelko\n{model}'})
            self.assertEqual('', identity.brand)
            self.assertFalse(identity.usable)

    def test_explicit_brand_takes_precedence(self):
        identity = extract_product_identity({'specs': 'ブランド名 OwnBrand\nメーカー Wcuelko\n製造元リファレンス F28-1'})
        self.assertEqual(('OWNBRAND::F281',), identity.brand_model_keys)

    def test_empty_manufacturer_line_does_not_consume_the_next_field(self):
        identity = extract_product_identity({'specs': 'メーカー : \nASIN : B000000001\n製造元リファレンス : F28-1'})
        self.assertEqual('', identity.brand)
        self.assertFalse(identity.usable)

    def test_manufacturer_fallback_is_not_combined_with_another_model_field(self):
        identity = extract_product_identity({'specs': 'メーカー Wcuelko\n商品モデル番号 OTHER-1\n製造元リファレンス F28-1'})
        self.assertEqual('', identity.brand)
        self.assertFalse(identity.usable)

    def test_title_description_and_placeholder_manufacturer_do_not_create_keys(self):
        wording = 'メーカー Wcuelko 製造元リファレンス F28-1'
        self.assertFalse(extract_product_identity({'title': wording, 'description': wording}).usable)
        self.assertFalse(extract_product_identity({'specs': 'メーカー Generic\n製造元リファレンス F28-1'}).usable)


class ManufacturerReferenceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pair_is_deduplicated_and_next_eligible_product_fills_slot(self):
        def product(asin, specs, price):
            return Product(asin=asin, title='same-looking dryer title', price=f'￥{price}', price_int=price,
                           original_price='￥23,699', discount_rate='79%OFF', image_url='', affiliate_url='',
                           category='ヘアドライヤー#1', rating='4.3', review_count='108', specs=specs)
        candidates = [product(FIXTURE[0]['asin'], FIXTURE[0]['specs'], 4990),
                      product(FIXTURE[1]['asin'], FIXTURE[1]['specs'], 4990),
                      product('B000000003', FIXTURE[1]['specs'].replace('F28-1', 'F28-2'), 4900)]
        stats = {}
        with patch('scraper.asyncio.sleep', new=AsyncMock()), patch('scraper.enrich_product', new=AsyncMock()) as enrich:
            selected = await select_enrich_unique_products(None, candidates, ProductIdentityRegistry(),
                [{'name': 'ヘアドライヤー'}], 2, 2, 'global_ranked', '', stats, 'sale_first')
        self.assertEqual([FIXTURE[0]['asin'], 'B000000003'], [row.asin for row in selected])
        self.assertEqual(1, stats['_skipped_product_identity'])
        enrich.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
