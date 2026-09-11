import copy
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

STAGE = Path(__file__).resolve().parents[1]
REPO = STAGE
DETAIL_MODULE = STAGE / 'detail_offer.py'
sys.path.insert(0, str(STAGE))
import ensure_daily_scrape as daily
from test_ensure_daily_scrape import TEST_DATE, write_valid_output


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest().upper()


def observation(product):
    asin = product['asin']
    checked = '2026-08-23T14:30:00+00:00'  # Next-day source is a supported use case.
    evidence = dict(schema_version=1, requested_asin=asin, selected_asins=[asin],
                    page_asins=[asin], url=f'https://www.amazon.co.jp/dp/{asin}',
                    checked_at=checked, http_status=200, challenge_detected=False,
                    center_count=1, product_title=product['title'], price_region_count=1,
                    price_region_selector='#corePriceDisplay_desktop_feature_div',
                    price_texts=[f"{product['price_int']:,}"], currency_texts=['￥'],
                    discount_texts=[product['discount_rate'].replace('OFF','')],
                    reference_price_texts=[product['original_price']] if product['original_price'] else [],
                    sale_region_count=1, sale_region_selector='#dealBadge_feature_div', sale_label='タイムセール',
                    timer_count=0, timer=None, evidence_kind='label', countdown=None)
    offer = {k: product[k] for k in ('price', 'price_int', 'original_price', 'discount_rate')}
    return dict(asin=asin, category=product['category'], checked_at=checked, status='accepted',
                reason=None, card_offer=copy.deepcopy(offer), pdp_offer=offer,
                evidence=evidence, evidence_sha256=digest(evidence))


def verification(products):
    return dict(schema_version=1, enabled=True, candidate_count=len(products),
                accepted_count=len(products), rejected_count=0, rejection_reasons={},
                observations=[observation(p) for p in products])


class DetailOfferSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        write_valid_output(self.root, 'account20', 4)
        self.product_path = self.root / 'data/account20' / f'products_{TEST_DATE}.json'
        self.summary_path = self.product_path.with_name(f'scrape_summary_{TEST_DATE}.json')
        self.products = json.loads(self.product_path.read_text(encoding='utf-8'))
        for product in self.products:
            product.update(price=f"￥{product['price_int']:,}", discount_rate='20%OFF')
        dump(self.product_path, self.products)
        self.summary = json.loads(self.summary_path.read_text(encoding='utf-8'))
        self.summary['detail_offer_verification'] = verification(self.products)
        self.config = self.root / 'categories20.yaml'
        self.config.write_text('filters:\n  verify_detail_offer: true\n', encoding='utf-8')
        shutil.copyfile(DETAIL_MODULE, self.root/'detail_offer.py')

    def run_validation(self):
        dump(self.summary_path, self.summary)
        return daily.validate('account20', self.root, TEST_DATE)

    def assert_rejected(self, match):
        ok, reason = self.run_validation()
        self.assertFalse(ok, reason)
        self.assertIn(match, reason)

    def test_valid_prior_evening_observation_is_accepted_without_any_file_write(self):
        dump(self.summary_path, self.summary)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(daily.validate('account20', self.root, TEST_DATE)[0])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_legacy_summary_is_rejected_only_when_config_enabled(self):
        del self.summary['detail_offer_verification']
        self.assert_rejected('missing or unsupported summary')
        for config in ('filters: {}\n', 'filters:\n  verify_detail_offer: false\n'):
            self.config.write_text(config, encoding='utf-8')
            self.assertTrue(self.run_validation()[0])
        self.config.unlink()
        self.assertTrue(self.run_validation()[0])

    def test_other_accounts_are_not_opted_in_by_account20_config(self):
        write_valid_output(self.root, 'account1', 4)
        self.assertTrue(daily.validate('account1', self.root, TEST_DATE)[0])

    def test_aborted_batch_cannot_reuse_earlier_accepted_products(self):
        self.summary['detail_offer_verification']['aborted_reason'] = 'detail_challenge'
        self.assert_rejected('observation batch was aborted')

    def test_output_price_text_integer_reference_and_discount_must_each_match(self):
        for key, value in [('price', '9,999円'), ('price_int', 9999),
                           ('original_price', '参考9,999円'), ('discount_rate', '99%OFF')]:
            with self.subTest(key=key):
                changed_products = copy.deepcopy(self.products)
                changed_products[0][key] = value
                dump(self.product_path, changed_products)
                self.assert_rejected('output offer mismatch')

    def test_output_asin_requires_accepted_record(self):
        row = self.summary['detail_offer_verification']['observations'][0]
        row.update(status='rejected', reason='no_sale', pdp_offer=None)
        detail = self.summary['detail_offer_verification']
        detail.update(accepted_count=3, rejected_count=1, rejection_reasons={'no_sale': 1})
        self.assert_rejected('no accepted PDP record')

    def test_missing_and_duplicate_observations_are_rejected(self):
        original = copy.deepcopy(self.summary)
        detail = self.summary['detail_offer_verification']
        detail['observations'].pop()
        detail.update(candidate_count=3, accepted_count=3)
        self.assert_rejected('no accepted PDP record')
        self.summary = original
        detail = self.summary['detail_offer_verification']
        detail['observations'].append(copy.deepcopy(detail['observations'][0]))
        detail.update(candidate_count=5, accepted_count=5)
        self.assert_rejected('duplicate observation ASIN')

    def test_all_candidate_observations_can_include_nonselected_and_rejected(self):
        detail = self.summary['detail_offer_verification']
        extra = copy.deepcopy(self.products[0]); extra['asin'] = 'B000000009'
        detail['observations'].append(observation(extra))
        detail['observations'].append(dict(asin='B000000010', status='rejected', reason='no_sale'))
        detail.update(candidate_count=6, accepted_count=5, rejected_count=1, rejection_reasons={'no_sale': 1})
        self.assertTrue(self.run_validation()[0])

    def test_counts_and_schema_are_strict(self):
        original = copy.deepcopy(self.summary)
        for key, value in [('candidate_count', 5), ('accepted_count', 3), ('rejected_count', 1),
                           ('candidate_count', True), ('schema_version', True), ('enabled', 1)]:
            with self.subTest(key=key, value=value):
                self.summary = copy.deepcopy(original)
                self.summary['detail_offer_verification'][key] = value
                self.assertFalse(self.run_validation()[0])

    def test_evidence_hash_tampering_is_rejected(self):
        row = self.summary['detail_offer_verification']['observations'][0]
        row['evidence']['sale_label'] = 'tampered'
        self.assert_rejected('evidence SHA mismatch')

    def test_rehashed_wrong_identity_status_or_observation_is_still_rejected(self):
        original = copy.deepcopy(self.summary)
        bad_values = [('requested_asin', 'B000000099'), ('selected_asins', []),
                      ('selected_asins', ['B000000099']), ('page_asins', ['B000000099']),
                      ('http_status', 404), ('challenge_detected', True),
                      ('evidence_kind', None), ('checked_at', 'different'), ('schema_version', True)]
        for key, value in bad_values:
            with self.subTest(key=key):
                self.summary = copy.deepcopy(original)
                row = self.summary['detail_offer_verification']['observations'][0]
                row['evidence'][key] = value
                row['evidence_sha256'] = digest(row['evidence'])
                self.assert_rejected('evidence identity/status mismatch')

    def test_valid_countdown_record_and_empty_page_asins_are_accepted(self):
        row = self.summary['detail_offer_verification']['observations'][0]
        timer = dict(timer_selector='#detailpage-dealBadge-countdown-timer', timer_text='01:00:00')
        row['evidence'].update(evidence_kind='deal_countdown', page_asins=[], sale_label='終了まで：01:00:00',
                               timer_count=1, timer=timer,
                               countdown=dict(**timer, remaining_seconds=3600, expires_at='2026-08-23T15:30:00+00:00'))
        row['evidence_sha256'] = digest(row['evidence'])
        self.assertTrue(self.run_validation()[0])

    def test_offer_types_and_extra_offer_keys_are_rejected(self):
        original = copy.deepcopy(self.summary)
        for key, value in [('price_int', True), ('price_int', '1000'), ('discount_rate', None), ('other', 'x')]:
            with self.subTest(key=key):
                self.summary = copy.deepcopy(original)
                self.summary['detail_offer_verification']['observations'][0]['pdp_offer'][key] = value
                self.assert_rejected('invalid PDP offer')

    def test_category_mismatch_is_rejected(self):
        self.summary['detail_offer_verification']['observations'][0]['category'] = 'PS5ゲームソフト#999'
        self.assert_rejected('output category mismatch')

    def test_invalid_existing_config_is_fail_closed(self):
        for value in ('filters: [invalid]\n', 'filters: [\n', 'filters:\n  verify_detail_offer: "true"\n'):
            with self.subTest(config=value):
                self.config.write_text(value, encoding='utf-8')
                self.assert_rejected('config invalid')

    def test_existing_policy_checks_are_not_bypassed(self):
        self.summary['selection_policy']['sort_order'] = 'review_desc'
        self.assert_rejected('selection policy')

    def test_rehashed_evidence_price_or_rate_must_reconstruct_the_saved_offer(self):
        original = copy.deepcopy(self.summary)
        for key, value in [('price_texts', ['9999']), ('discount_texts', ['99%']),
                           ('sale_label', '通常価格'), ('currency_texts', ['$'])]:
            with self.subTest(key=key):
                self.summary = copy.deepcopy(original)
                row = self.summary['detail_offer_verification']['observations'][0]
                row['evidence'][key] = value
                row['evidence_sha256'] = digest(row['evidence'])
                self.assert_rejected('recorded PDP offer mismatch')

    def test_missing_shared_parser_is_rejected(self):
        (self.root/'detail_offer.py').unlink()
        self.assert_rejected('shared PDP validator unavailable')

    def test_invalid_or_naive_timestamp_is_rejected_without_source_day_constraint(self):
        original = copy.deepcopy(self.summary)
        for checked_at in ['invalid', '2026-08-23T14:30:00']:
            self.summary = copy.deepcopy(original)
            row = self.summary['detail_offer_verification']['observations'][0]
            row['checked_at'] = row['evidence']['checked_at'] = checked_at
            row['evidence_sha256'] = digest(row['evidence'])
            self.assert_rejected('recorded PDP verification failed')

    def test_malformed_rehashed_evidence_is_rejected_without_crashing(self):
        row = self.summary['detail_offer_verification']['observations'][0]
        row['evidence']['url'] = {'not': 'a URL'}
        row['evidence_sha256'] = digest(row['evidence'])
        self.assert_rejected('recorded PDP verification failed')


class ControllerBlobIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load only definitions; all Git calls below are mocked. The unchanged
        # local helper supplies policy constants and is never executed.
        sys.path.append(str(REPO))
        spec = importlib.util.spec_from_file_location('staged_recovery_controller', STAGE/'scripts/recover_missing_daily_sources.py')
        cls.controller = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.controller
        spec.loader.exec_module(cls.controller)

    def test_canonical_config_blob_controls_remote_audit_without_ledger_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture'; fixture.mkdir()
            for account in range(1, 21):
                write_valid_output(fixture, f'account{account}', 4)
            products_file = fixture/f'data/account20/products_{TEST_DATE}.json'
            products = json.loads(products_file.read_text(encoding='utf-8'))
            for product in products:
                product.update(price=f"￥{product['price_int']:,}", discount_rate='20%OFF')
            dump(products_file, products)
            files = {p.relative_to(fixture).as_posix(): p.read_bytes() for p in fixture.rglob('*') if p.is_file()}
            files['categories20.yaml'] = b'filters:\n  verify_detail_offer: true\n'
            files['detail_offer.py'] = DETAIL_MODULE.read_bytes()
            code = (STAGE/'ensure_daily_scrape.py').read_bytes()
            calls = []
            def git(repo, *args, **kwargs):
                calls.append(args)
                if args[:2] == ('remote','get-url'): return b'https://github.com/AGI-gyoumusuper/pinefield.git'
                if args[0] == 'fetch': return b''
                if args[0] == 'rev-parse': return b'a'*40
                if args[0] == 'show':
                    name = args[1].split(':',1)[1]
                    return code if name == 'ensure_daily_scrape.py' else files.get(name)
                raise AssertionError(args)
            empty_repo = Path(directory)/'audit'; empty_repo.mkdir()
            with patch.object(self.controller, 'git', side_effect=git):
                result = self.controller.audit_remote(empty_repo, TEST_DATE)
                self.assertEqual(result['missing_accounts'], [20])
                self.assertEqual(result['valid_count'], 19)
                self.assertEqual(len([a for a in calls if a[0]=='show' and a[1].endswith(':categories20.yaml')]), 1)
                self.assertEqual(len([a for a in calls if a[0]=='show' and a[1].endswith(':detail_offer.py')]), 1)
                products = json.loads(files[f'data/account20/products_{TEST_DATE}.json'])
                summary_name = f'data/account20/scrape_summary_{TEST_DATE}.json'
                summary = json.loads(files[summary_name]); summary['detail_offer_verification'] = verification(products)
                files[summary_name] = json.dumps(summary, ensure_ascii=False).encode()
                self.assertEqual(self.controller.audit_remote(empty_repo, TEST_DATE)['valid_count'], 20)
                files['detail_offer.py'] = b'def validate_offer(evidence):\n    return None, "fixture_remote_reject"\n'
                self.assertEqual(self.controller.audit_remote(empty_repo, TEST_DATE)['missing_accounts'], [20])
                files['detail_offer.py'] = DETAIL_MODULE.read_bytes()
                del summary['detail_offer_verification']; files[summary_name] = json.dumps(summary).encode()
                files['categories20.yaml'] = b'filters: {}\n'
                self.assertEqual(self.controller.audit_remote(empty_repo, TEST_DATE)['valid_count'], 20)
            self.assertEqual(list(empty_repo.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
