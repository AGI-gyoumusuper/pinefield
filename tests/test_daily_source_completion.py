"""Offline source completion: real Git bare read-back, frozen source and evidence preservation."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from scripts import recover_local_daily_source as recovery
from scripts import recover_missing_daily_sources as controller
import product_identity
from detail_offer import (DISCOUNT_POLICY, public_evidence, validate_offer, observation_sha256,
                          discount_row, validate_discount_contract_summary)
from test_recover_local_daily_source import RecoveryFixtureTests, DATE, git, save_json


def bundle(account, numbers, *, shelves=None, price=8000):
    products, records, rows = [], [], []
    for pos, number in enumerate(numbers, 1):
        asin = f'B{number:09d}'
        category = (shelves[pos-1] if shelves else 'カテゴリ') + '#1'
        raw = dict(selected_asins=[asin], page_asins=[], center_count=1, product_title=asin,
            price_region_count=1, price_region_selector='#corePriceDisplay_desktop_feature_div',
            price_texts=[f'{price:,}'], currency_texts=['￥'], discount_texts=['-20%'],
            reference_price_texts=[f'過去価格: ￥{int(price/0.8):,}'], sale_region_count=1,
            sale_region_selector='#dealBadge_feature_div', sale_label='', timer_count=0,
            timer=None, challenge_detected=False, coupon_regions=[])
        evidence = public_evidence(raw, asin, f'https://www.amazon.co.jp/dp/{asin}', 200, '2026-09-10T02:00:00+00:00')
        offer, reason = validate_offer(evidence, offer_scope='unified_discounts')
        assert offer and reason is None
        item = dict(asin=asin, title=f'元の説明を保持する商品{number}', image_url='https://example.invalid/image.png',
            affiliate_url=f'https://www.amazon.co.jp/dp/{asin}?tag=noteamazon{account}-22',
            category=category, rating='4.5', review_count='100', description='保存済み説明', specs='仕様', **offer)
        record = dict(asin=asin, category=category, checked_at=evidence['checked_at'], status='accepted', reason=None,
            pdp_offer=offer, evidence=evidence, evidence_sha256=observation_sha256(evidence))
        products.append(item); records.append(record); rows.append(discount_row(SimpleNamespace(**item), record, pos))
    policy = dict(selection_mode='category_quota' if account == 20 else 'global_ranked', sort_order='sale_first',
        require_sale_info=True, max_per_category=5 if account == 20 else 2, max_total_items=10,
        discount_contract=DISCOUNT_POLICY, offer_scope='unified_discounts')
    summary = dict(date=DATE, total_taken=len(products), selection_policy=policy, categories={},
        detail_offer_verification=dict(schema_version=1, enabled=True, offer_scope='unified_discounts',
            candidate_count=len(records), accepted_count=len(records), rejected_count=0,
            final_selected_count=len(products), observations=records, categories={}),
        discount_contract=dict(schema_version=1, policy=DISCOUNT_POLICY, account=account, date=DATE, products=rows))
    assert validate_discount_contract_summary(f'account{account}', products, summary)[0]
    return products, summary


class MergeTests(unittest.TestCase):
    def test_prefix_price_evidence_and_order_unchanged_after_supplement(self):
        original = bundle(1, [1, 2]); fresh = bundle(1, [1, 3, 4, 5], price=16000)
        original_before = copy.deepcopy(original)
        products, summary = recovery.merge_short_source(1, *original, *fresh, product_identity)
        self.assertEqual(products[:2], original[0]); self.assertEqual(original, original_before)
        self.assertEqual([p['asin'] for p in products], ['B000000001', 'B000000002', 'B000000003', 'B000000004'])
        self.assertEqual(summary['discount_contract']['products'][:2], original[1]['discount_contract']['products'])
        self.assertEqual(summary['detail_offer_verification']['observations'][:2], original[1]['detail_offer_verification']['observations'])
        self.assertTrue(validate_discount_contract_summary('account1', products, summary)[0])

    def test_same_identity_is_excluded_while_new_identity_can_fill(self):
        original = bundle(1, [1]); fresh = bundle(1, [2, 3, 4, 5])
        original[0][0]['specs'] = 'ブランド: Canon\n型番: EXAMPLE-1234'
        fresh[0][0]['specs'] = original[0][0]['specs']
        products, summary = recovery.merge_short_source(1, *original, *fresh, product_identity)
        self.assertEqual([p['asin'] for p in products], ['B000000001', 'B000000003', 'B000000004', 'B000000005'])

    def test_account20_six_existing_plus_five_candidates_keeps_six_and_adds_two(self):
        original = bundle(20, range(1, 7), shelves=['Nintendo Switch 2'] * 6)
        fresh = bundle(20, range(11, 16), shelves=['PS5ゲームソフト'] * 5)
        products, summary = recovery.merge_short_source(20, *original, *fresh, product_identity)
        self.assertEqual(products[:6], original[0]); self.assertEqual(len(products), 8)
        self.assertEqual(recovery.postable_count(20, products), 4)
        self.assertTrue(validate_discount_contract_summary('account20', products, summary)[0])

    def test_account20_ten_existing_cannot_be_reordered_or_trimmed(self):
        original = bundle(20, range(1, 11), shelves=['Nintendo Switch 2'] * 10)
        fresh = bundle(20, [11, 12], shelves=['PS5ゲームソフト'] * 2)
        products, summary = recovery.merge_short_source(20, *original, *fresh, product_identity)
        self.assertEqual(products, original[0]); self.assertEqual(recovery.postable_count(20, products), 2)

    def test_no_original_discount_evidence_is_not_silently_upgraded(self):
        original = bundle(1, [1]); fresh = bundle(1, [2, 3, 4])
        original[1].pop('discount_contract')
        with self.assertRaisesRegex(recovery.RecoveryStop, 'verified_discount_contract'):
            recovery.merge_short_source(1, *original, *fresh, product_identity)


class CompletionGitTests(unittest.TestCase):
    setUpBase = RecoveryFixtureTests.setUp
    tearDown = RecoveryFixtureTests.tearDown
    advance = RecoveryFixtureTests.advance
    source = RecoveryFixtureTests.source
    count = RecoveryFixtureTests.count

    def setUp(self):
        self.setUpBase()
        for name in ('detail_offer.py', 'product_identity.py'):
            shutil.copyfile(ROOT / name, self.seed / name)
        (self.seed / 'scraper.py').write_text('''import json, os
from pathlib import Path
def generate(account):
    counter=Path(os.environ['RECOVERY_FIXTURE_COUNTER'])
    counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
    if os.environ.get('COMPLETION_FAIL') == '1': raise SystemExit(9)
    value=json.loads(Path(os.environ['COMPLETION_FIXTURE']).read_text(encoding='utf-8'))
    target=Path('data')/f'account{account}'
    date=os.environ['PINEFIELD_TARGET_DATE']
    value[1]['date']=date; value[1]['discount_contract']['date']=date
    (target/f'products_{date}.json').write_text(json.dumps(value[0],ensure_ascii=False),encoding='utf-8')
    (target/f'scrape_summary_{date}.json').write_text(json.dumps(value[1],ensure_ascii=False),encoding='utf-8')
''', encoding='utf-8')
        self.original = bundle(1, [1, 2])
        self.fresh = bundle(1, [1, 3, 4, 5], price=16000)
        self.fixture = self.root / 'fresh.json'
        save_json(self.fixture, self.fresh)
        save_json(self.seed / f'data/account1/products_{DATE}.json', self.original[0])
        save_json(self.seed / f'data/account1/scrape_summary_{DATE}.json', self.original[1])
        git(self.seed, 'add', '--all'); git(self.seed, 'commit', '-qm', 'completion fixture')
        git(self.seed, 'push', '-q', 'origin', 'main')
        self.initial = git(self.seed, 'rev-parse', 'HEAD')
        env = patch.dict(os.environ, {'COMPLETION_FIXTURE': str(self.fixture), 'COMPLETION_FAIL': '0'})
        self.patches.append(env); env.start()

    def execute(self, **kwargs):
        return recovery.recover(account=kwargs.pop('account', 1), target_date=kwargs.pop('target_date', DATE),
            repo=self.seed, execute=kwargs.pop('execute', True), cloud_run_completed=True, timeout=30,
            nightly=kwargs.pop('nightly', True), before_publish=kwargs.pop('before_publish', lambda: {'status': 'READY'}), **kwargs)

    def remote(self, name):
        return json.loads(git(self.bare, 'show', f'main:data/account1/{name}_{DATE}.json'))

    def test_preserves_original_prefix_and_ledger_then_confirms_remote_exact_blobs(self):
        ledger = git(self.bare, 'show', 'main:data/account1/asin_history.json')
        result = self.execute()
        self.assertEqual(result['status'], 'PUBLISHED', result)
        self.assertEqual(self.count(), 1); self.assertEqual(result['postable_after'], 4)
        self.assertEqual(self.remote('products')[:2], self.original[0])
        self.assertEqual(git(self.bare, 'show', 'main:data/account1/asin_history.json'), ledger)
        self.assertEqual(set(git(self.bare, 'diff', '--name-only', self.initial, 'main').splitlines()),
            {f'data/account1/products_{DATE}.json', f'data/account1/scrape_summary_{DATE}.json'})
        for name, expected in result['files_sha256'].items():
            body = subprocess.check_output(['git', '-C', str(self.bare), 'show', 'main:' + name])
            self.assertEqual(hashlib.sha256(body).hexdigest().upper(), expected)
        self.assertTrue(validate_discount_contract_summary('account1', self.remote('products'), self.remote('scrape_summary'))[0])

    def test_default_mode_still_preserves_valid_short_output_without_scrape(self):
        result = self.execute(nightly=False)
        self.assertEqual(result['status'], 'UNCHANGED_VALIDATED'); self.assertEqual(self.count(), 0)
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_existing_four_ready_never_scrapes(self):
        complete = bundle(1, [1, 2, 3, 4])
        self.advance(f'data/account1/products_{DATE}.json', json.dumps(complete[0], ensure_ascii=False))
        self.advance(f'data/account1/scrape_summary_{DATE}.json', json.dumps(complete[1], ensure_ascii=False))
        result = self.execute()
        self.assertEqual(result['status'], 'UNCHANGED_VALIDATED'); self.assertEqual(self.count(), 0)

    def test_cloud_wait_saves_candidate_and_reuses_once_without_second_scrape(self):
        first = self.execute(before_publish=lambda: {'status': 'WAIT'})
        self.assertEqual(first['reason'], 'cloud_not_ready_before_publish_candidate_saved', first)
        self.assertEqual(first['push_attempts'], 0); self.assertEqual(self.count(), 1)
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)
        second = self.execute()
        self.assertEqual(second['status'], 'PUBLISHED', second); self.assertEqual(second['scrape_runs'], 0)
        self.assertEqual(self.count(), 1); self.assertIn('reused_pending_candidate', second)
        self.assertEqual(self.remote('products')[:2], self.original[0])

    def test_pending_candidate_rejects_changed_ledger_without_scrape_or_push(self):
        self.execute(before_publish=lambda: {'status': 'WAIT'})
        changed = {'schema': 'note-amazon-asin-history-v1', 'posted': [{'asin': 'B000000099'}]}
        self.advance('data/account1/asin_history.json', json.dumps(changed))
        second = self.execute()
        self.assertEqual(second['reason'], 'pending_candidate_inputs_changed_no_reuse', second)
        self.assertEqual(self.count(), 1); self.assertEqual(second['push_attempts'], 0)

    def test_pending_candidate_rejects_changed_original_source(self):
        self.execute(before_publish=lambda: {'status': 'WAIT'})
        changed = copy.deepcopy(self.original[0]); changed[0]['description'] = '他の作業が更新した説明'
        self.advance(f'data/account1/products_{DATE}.json', json.dumps(changed, ensure_ascii=False))
        second = self.execute()
        self.assertEqual(second['reason'], 'pending_candidate_inputs_changed_no_reuse')
        self.assertEqual(self.count(), 1); self.assertEqual(self.remote('products'), changed)

    def test_pending_candidate_rejects_changed_configuration(self):
        self.execute(before_publish=lambda: {'status': 'WAIT'})
        config = (self.seed / 'categories1.yaml').read_text(encoding='utf-8')
        self.advance('categories1.yaml', config + '\nnew_diagnostic_setting: true\n')
        second = self.execute()
        self.assertEqual(second['reason'], 'pending_candidate_inputs_changed_no_reuse')
        self.assertEqual(self.count(), 1); self.assertEqual(second['push_attempts'], 0)

    def test_pending_candidate_rejects_corrupt_saved_bytes(self):
        first = self.execute(before_publish=lambda: {'status': 'WAIT'})
        (Path(first['output_dir']) / f'pending-products_{DATE}.json').write_text('[]', encoding='utf-8')
        second = self.execute()
        self.assertEqual(second['reason'], 'pending_candidate_hash_mismatch')
        self.assertEqual(self.count(), 1); self.assertEqual(second['push_attempts'], 0)

    def test_short_window_does_not_consume_one_scrape_permission(self):
        first = self.execute(deadline_utc=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual(first['reason'], 'nightly_window_too_short_to_start_scrape')
        self.assertEqual(self.count(), 0)
        self.assertFalse((Path(first['output_dir']).parent / '.control' / f'account1_{DATE}.attempt.json').exists())
        second = self.execute()
        self.assertEqual(second['status'], 'PUBLISHED'); self.assertEqual(self.count(), 1)

    def test_pending_reuse_can_publish_without_ten_minutes_for_new_scrape(self):
        self.execute(before_publish=lambda: {'status': 'WAIT'})
        second = self.execute(deadline_utc=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual(second['status'], 'PUBLISHED', second); self.assertEqual(self.count(), 1)

    def test_readback_audit_distinguishes_valid_short_and_four_ready(self):
        before = controller.audit_remote(self.seed, DATE)
        self.assertIn(1, before['short_accounts']); self.assertNotIn(1, before['missing_accounts'])
        self.assertEqual(before['rows'][0]['postable_count'], 2)
        self.execute()
        after = controller.audit_remote(self.seed, DATE)
        self.assertTrue(after['rows'][0]['four_slots_ready']); self.assertNotIn(1, after['short_accounts'])

    def test_frozen_source_even_during_publish_guard_prevents_write(self):
        def guard():
            save_json(self.source(), {'frozen': True}); return {'status': 'READY'}
        result = self.execute(before_publish=guard)
        self.assertIn('source_already', result['reason']); self.assertEqual(result['push_attempts'], 0)
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_existing_frozen_source_never_scrapes(self):
        save_json(self.source(), {'frozen': True})
        result = self.execute()
        self.assertIn('source_already', result['reason']); self.assertEqual(self.count(), 0)

    def test_scrape_failure_preserves_old_valid_source_and_does_not_retry(self):
        with patch.dict(os.environ, {'COMPLETION_FAIL': '1'}):
            first = self.execute()
        self.assertEqual(first['status'], 'STOPPED')
        second = self.execute()
        self.assertEqual(second['reason'], 'same_account_date_already_attempted')
        self.assertEqual(self.count(), 1); self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_no_gain_preserves_original_json_bytes(self):
        save_json(self.fixture, bundle(1, [1, 2], price=16000))
        result = self.execute()
        self.assertEqual(result['status'], 'UNCHANGED_VALIDATED', result)
        self.assertEqual(result['shortfall_reason'], 'no_additional_verified_distinct_products')
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_tampered_discount_evidence_never_replaces_original(self):
        invalid = copy.deepcopy(self.fresh); invalid[0][1]['price_int'] = 1
        save_json(self.fixture, invalid)
        result = self.execute()
        self.assertEqual(result['status'], 'STOPPED'); self.assertEqual(result['push_attempts'], 0)
        self.assertEqual(self.remote('products'), self.original[0])

    def test_tomorrow_only_allowed_in_explicit_nightly_mode(self):
        tomorrow = '2026-09-11'
        with self.assertRaisesRegex(recovery.RecoveryStop, 'JST_today'):
            self.execute(target_date=tomorrow, nightly=False)
        result = self.execute(target_date=tomorrow)
        self.assertEqual(result['status'], 'PUBLISHED', result)


def cloud_run(account, *, target='2026-09-11', status='completed', sha='a'*40, run_id=1):
    return dict(id=run_id, path='.github/workflows/' + ('scrape.yml' if account <= 5 else f'scrape{account}.yml'),
        event='push', status=status, conclusion='success' if status == 'completed' else None,
        display_title=f'Trigger [scrape:account{account}] [target:{target}]',
        created_at='2026-09-10T11:00:00Z', updated_at='2026-09-10T11:05:00Z', head_sha=sha)


class ControllerTests(unittest.TestCase):
    def test_evening_future_and_midnight_today_have_same_target(self):
        with patch.object(controller, 'now_utc', return_value=datetime(2026, 9, 10, 12, tzinfo=timezone.utc)):
            evening = controller.preparation_window()
        with patch.object(controller, 'now_utc', return_value=datetime(2026, 9, 10, 16, tzinfo=timezone.utc)):
            morning = controller.preparation_window()
        self.assertEqual(evening['date'], '2026-09-11'); self.assertEqual(morning['date'], evening['date'])
        self.assertTrue(evening['before_midnight']); self.assertFalse(morning['before_midnight'])

    def test_cutoff_stops_before_ensure_wave(self):
        with patch.object(controller, 'now_utc', return_value=datetime(2026, 9, 10, 14, 24, tzinfo=timezone.utc)):
            self.assertEqual(controller.preparation_window()['status'], 'WAIT')

    def test_future_gate_does_not_wait_for_unrelated_regular_or_other_target(self):
        runs = [cloud_run(12), cloud_run(13, status='in_progress'),
                cloud_run(12, target='2026-09-10', status='in_progress', run_id=2)]
        result = controller.cloud_gate('2026-09-11', accounts=(12,), require_repairs=False,
                                      expected_trigger=(12, 'a'*40), runs=runs)
        self.assertEqual(result['status'], 'READY', result)

    def test_latest_trigger_must_match_completed_run(self):
        result = controller.cloud_gate('2026-09-11', accounts=(12,), require_repairs=False,
                                      expected_trigger=(12, 'b'*40), runs=[cloud_run(12)])
        self.assertEqual(result['status'], 'WAIT')

    def test_manual_run_does_not_inherit_unrelated_head_trigger_identity(self):
        manual = dict(id=2, path='.github/workflows/scrape12.yml', event='workflow_dispatch',
            status='in_progress', conclusion=None, created_at='2026-09-10T13:00:00Z',
            head_commit={'message': 'Trigger [scrape:account13] [target:2026-09-10]'})
        result = controller.cloud_gate('2026-09-11', accounts=(12,), require_repairs=False,
                                      expected_trigger=(12, 'a'*40), runs=[cloud_run(12), manual])
        self.assertEqual(result['status'], 'WAIT', result)
        self.assertEqual(controller.run_target(manual, 'scrape12.yml'), (None, 'manual_target_unproven'))

    def test_active_same_target_repair_and_unknown_manual_target_block(self):
        for extra in (dict(id=2, path='.github/workflows/ensure-scrape.yml', event='schedule', status='in_progress',
                           conclusion=None, created_at='2026-09-10T14:30:00Z'),
                      dict(id=2, path='.github/workflows/scrape12.yml', event='workflow_dispatch', status='in_progress',
                           conclusion=None, created_at='2026-09-10T13:00:00Z')):
            result = controller.cloud_gate('2026-09-11', accounts=(12,), require_repairs=False,
                                          expected_trigger=(12, 'a'*40), runs=[cloud_run(12), extra])
            self.assertEqual(result['status'], 'WAIT', result)

    def test_after_midnight_requires_all_regular_and_repair_completion(self):
        result = controller.cloud_gate('2026-09-11', expected_trigger=(12, 'a'*40), runs=[cloud_run(12)])
        self.assertEqual(result['status'], 'WAIT'); self.assertIn('late-repair-scrape.yml', result['missing_completion_keys'])

    def test_cloud_api_error_never_allows_recovery(self):
        with patch.object(controller, 'fetch_runs', side_effect=OSError('unavailable')):
            result = controller.cloud_gate('2026-09-11', accounts=(12,), require_repairs=False)
        self.assertEqual(result['status'], 'STOP')

    def test_account20_readiness_counts_both_shelves_not_raw_count(self):
        items = bundle(20, range(1, 7), shelves=['Nintendo Switch 2'] * 6)[0]
        self.assertEqual(recovery.postable_count(20, items), 2)

    def test_completed_and_frozen_sources_do_not_call_cloud_or_scraper(self):
        for frozen in (False, True):
            rows = [dict(account=n, valid=True, four_slots_ready=(not frozen or n != 1),
                         postable_count=2 if frozen and n == 1 else 4) for n in range(1, 21)]
            audit = dict(rows=rows, valid_count=20, ready_account_count=19 if frozen else 20,
                         postable_count=78 if frozen else 80, short_accounts=[1] if frozen else [])
            with tempfile.TemporaryDirectory() as folder, \
                 patch.object(controller, 'OUTPUT_BASE', Path(folder)), \
                 patch.object(controller, 'now_utc', return_value=datetime(2026, 9, 10, 12, tzinfo=timezone.utc)), \
                 patch.object(controller, 'audit_remote', return_value=audit), \
                 patch.object(recovery, 'factory_root', return_value=Path(folder)), \
                 patch.object(recovery, 'check_source', side_effect=recovery.RecoveryStop('factory_same_day_source_already_fixed_or_partial')), \
                 patch.object(controller, 'fetch_runs') as api, patch.object(recovery, 'recover') as scrape:
                result = controller.run_controller(execute=True, nightly_preparation=True)
                api.assert_not_called(); scrape.assert_not_called()
                self.assertEqual(result['status'], 'INCOMPLETE' if frozen else 'COMPLETE')

    def test_long_prior_account_refreshes_cloud_snapshot_before_next_account(self):
        clock = [0]
        rows = [dict(account=n, valid=True, four_slots_ready=n > 2, postable_count=2 if n <= 2 else 4)
                for n in range(1, 21)]
        audit = dict(rows=rows, valid_count=20, ready_account_count=18, postable_count=76, short_accounts=[1, 2])
        def perform(**kwargs):
            clock[0] += 90
            return {'status': 'PUBLISHED', 'scrape_runs': 1}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(controller, 'OUTPUT_BASE', Path(folder)), \
             patch.object(controller, 'now_utc', return_value=datetime(2026, 9, 10, 12, tzinfo=timezone.utc)), \
             patch.object(controller.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(controller, 'audit_remote', return_value=audit), \
             patch.object(recovery, 'factory_root', return_value=Path(folder)), \
             patch.object(recovery, 'check_source'), patch.object(controller, 'fetch_runs', return_value=[]) as api, \
             patch.object(controller, 'nightly_cloud_gate', return_value={'status': 'READY'}), \
             patch.object(recovery, 'recover', side_effect=perform):
            controller.run_controller(execute=True, nightly_preparation=True)
            self.assertEqual(api.call_count, 2)


del RecoveryFixtureTests  # Imported fixture helpers are not a second copy of the legacy test suite.


if __name__ == '__main__':
    unittest.main()
