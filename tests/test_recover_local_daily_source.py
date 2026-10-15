"""Local bare Git and synthetic subprocess fixtures only; no Amazon or GitHub calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import recover_local_daily_source as recovery
from detail_offer import discount_row, observation_sha256, public_evidence, validate_offer
from sale_campaign import apply_campaign_config

DATE = '2026-09-10'
MAIN_DATE = '2026-10-16'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode('utf-8').strip()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def failed_early_phase_fixture(output_base, account=1, commit='a' * 40):
    """An archived zero-selection failure, with replayable same-ASIN early evidence."""
    parent = output_base / MAIN_DATE / f'local-recovery-account{account}'
    folder = parent / 'run-01'
    attempt = parent / '.control' / f'account{account}_{MAIN_DATE}.attempt.json'
    checked = '2026-10-15T09:31:15+00:00'
    asin = 'B000000001'
    observed = dict(selected_asins=[asin], page_asins=[], center_count=1, product_title='Fixture',
        price_region_count=1, price_region_selector='#corePriceDisplay_desktop_feature_div',
        price_texts=['8,000'], currency_texts=['￥'], discount_texts=['-20%'],
        reference_price_texts=['過去価格: ￥10,000'], sale_region_count=1,
        sale_region_selector='#dealBadge_feature_div', sale_label='プライム感謝祭先行セール',
        timer_count=0, timer=None, challenge_detected=False, coupon_regions=[])
    evidence = public_evidence(observed, asin, f'https://www.amazon.co.jp/dp/{asin}', 200, checked)
    offer, reason = validate_offer(evidence, offer_scope='unified_discounts')
    assert offer and reason is None
    record = dict(asin=asin, checked_at=checked, status='rejected',
        reason='detail_prime_event_phase_mismatch', pdp_offer=None, evidence=evidence,
        evidence_sha256=observation_sha256(evidence))
    summary = dict(date=MAIN_DATE, total_taken=0,
        campaign=apply_campaign_config({}, f'categories{account}.yaml', MAIN_DATE)['_sale_campaign'],
        selection_policy=dict(selection_mode='global_ranked', sort_order='sale_first',
            require_sale_info=True, max_per_category=2, max_total_items=10, require_prime_event=True,
            verify_detail_offer=True, offer_scope='unified_discounts', discount_contract='amazon_direct_discounts_v1'),
        detail_offer_verification=dict(schema_version=1, enabled=True, offer_scope='unified_discounts',
            candidate_count=1, accepted_count=0, rejected_count=1, final_selected_count=0,
            rejection_reasons={'detail_prime_event_phase_mismatch': 1}, observations=[record]))
    products_path = folder / f'products_{MAIN_DATE}.json'
    summary_path = folder / f'scrape_summary_{MAIN_DATE}.json'
    save_json(products_path, [])
    save_json(summary_path, summary)
    metadata = dict(account=account, date=MAIN_DATE, started_at_utc='2026-10-15T09:30:00+00:00',
        output_dir=str(folder), source_origin_commit=commit)
    save_json(attempt, metadata)
    result = dict(schema='pinefield-local-recovery-v1', account=f'account{account}', date=MAIN_DATE,
        execute=True, scrape_runs=1, push_attempts=0, status='STOPPED',
        reason='canonical_output_validation_failed', output_dir=str(folder), source_origin_commit=commit,
        started_at_utc=metadata['started_at_utc'], finished_at_utc='2026-10-15T09:35:00+00:00',
        candidate_files_sha256={p.name: recovery.digest(p.read_bytes()) for p in (products_path, summary_path)})
    save_json(folder / 'result.json', result)
    return attempt, folder, summary


def failed_main_label_fixture(output_base, commit='a' * 40):
    attempt, _, summary = failed_early_phase_fixture(output_base, commit=commit)
    prior = recovery.phase_transition_retry_evidence(attempt, account=1, target_date=MAIN_DATE,
        current_date=MAIN_DATE, remote_products=[])
    assert prior
    folder = attempt.parent.parent/'run-26'
    transition = attempt.with_name('account1_2026-10-16.phase-transition-main.attempt.json')
    metadata = dict(account=1, date=MAIN_DATE, started_at_utc='2026-10-15T22:32:30+00:00',
        output_dir=str(folder), source_origin_commit=commit, phase_transition_retry=prior)
    save_json(transition, metadata)
    summary = copy.deepcopy(summary)
    record = summary['detail_offer_verification']['observations'][0]
    record['evidence'].update(sale_label='プライム感謝祭セール', checked_at='2026-10-15T22:33:15+00:00')
    record.update(checked_at=record['evidence']['checked_at'], reason='detail_prime_event_label_unverified',
                  evidence_sha256=observation_sha256(record['evidence']))
    summary['detail_offer_verification']['rejection_reasons'] = {'detail_prime_event_label_unverified': 1}
    save_json(folder/f'products_{MAIN_DATE}.json', [])
    save_json(folder/f'scrape_summary_{MAIN_DATE}.json', summary)
    save_json(folder/'result.json', dict(schema='pinefield-local-recovery-v1', account='account1', date=MAIN_DATE,
        execute=True, nightly=False, cloud_run_completed_declared=True, scrape_runs=1, push_attempts=0,
        status='STOPPED', reason='canonical_output_validation_failed', output_dir=str(folder), source_origin_commit=commit,
        phase_transition_retry=prior, started_at_utc='2026-10-15T22:32:00+00:00', finished_at_utc='2026-10-15T22:38:00+00:00',
        candidate_files_sha256={name: recovery.digest((folder/name).read_bytes()) for name in
            (f'products_{MAIN_DATE}.json', f'scrape_summary_{MAIN_DATE}.json')}))
    return attempt, transition, folder, summary


class PrimeMainLabelRetryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attempt, self.transition, self.folder, self.summary = failed_main_label_fixture(self.root)

    def evidence(self, **kwargs):
        return recovery.main_label_compatibility_retry_evidence(self.attempt, self.transition,
            account=kwargs.pop('account', 1), target_date=kwargs.pop('target_date', MAIN_DATE),
            current_date=kwargs.pop('current_date', MAIN_DATE), remote_products=kwargs.pop('remote_products', []), **kwargs)

    def update(self, summary=None, result_changes=None, products=None):
        if summary is not None: save_json(self.folder/f'scrape_summary_{MAIN_DATE}.json', summary)
        if products is not None: save_json(self.folder/f'products_{MAIN_DATE}.json', products)
        result = json.loads((self.folder/'result.json').read_text(encoding='utf-8'))
        result.update(result_changes or {})
        result['candidate_files_sha256'] = {name: recovery.digest((self.folder/name).read_bytes()) for name in
            (f'products_{MAIN_DATE}.json', f'scrape_summary_{MAIN_DATE}.json')}
        save_json(self.folder/'result.json', result)

    def test_exact_incident_replays_main_alias_without_changing_raw_or_archives(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        proof = self.evidence()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['replayed_main_alias_count'], 1)
        self.assertEqual(proof['replayed_observations'][0]['raw_sale_label'], 'プライム感謝祭セール')
        self.assertEqual(proof['replayed_observations'][0]['prime_event_proof']['label'], 'プライム感謝祭')
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_only_account1_today_and_empty_remote_are_eligible(self):
        for change in ({'account': 2}, {'current_date': '2026-10-17'}, {'target_date': '2026-10-15'},
                       {'remote_products': None}, {'remote_products': [{}]}, {'remote_products': {}}):
            with self.subTest(change=change): self.assertIsNone(self.evidence(**change))

    def test_alias_phase_identity_price_and_http_cannot_be_relaxed(self):
        for key, value in [('sale_label', 'プライム会員セール'), ('sale_label', 'プライム感謝祭先行セール'),
                           ('sale_label', 'プライム感謝祭'), ('sale_label', ' プライム感謝祭セール'),
                           ('requested_asin', 'B999999999'), ('sale_region_selector', '#other'),
                           ('checked_at', '2026-10-15T09:00:00+00:00'), ('http_status', 503),
                           ('price_texts', ['7,999']), ('challenge_detected', True)]:
            with self.subTest(key=key, value=value):
                summary = copy.deepcopy(self.summary); record = summary['detail_offer_verification']['observations'][0]
                record['evidence'][key] = value
                if key == 'checked_at': record['checked_at'] = value
                record['evidence_sha256'] = observation_sha256(record['evidence'])
                self.update(summary=summary)
                self.assertIsNone(self.evidence())

    def test_any_other_rejection_acceptance_or_nonzero_product_is_rejected(self):
        for changes in ({'accepted_count': 1}, {'final_selected_count': 1}, {'candidate_count': 2},
                        {'rejection_reasons': {'detail_http_error': 1}}, {'observations': []}):
            summary = copy.deepcopy(self.summary); summary['detail_offer_verification'].update(changes)
            self.update(summary=summary)
            with self.subTest(changes=changes): self.assertIsNone(self.evidence())
        self.update(summary=self.summary, products=[{'asin': 'B000000001'}])
        self.assertIsNone(self.evidence())

    def test_tampered_marker_hash_result_and_wrong_incident_are_rejected(self):
        original = self.transition.read_bytes()
        for change in ({'output_dir': str(self.folder.parent/'run-27')}, {'account': 2}, {'date': '2026-10-17'},
                       {'phase_transition_retry': {}}, {'source_origin_commit': 'b'*40}):
            value = json.loads(original); value.update(change); save_json(self.transition, value)
            with self.subTest(change=change): self.assertIsNone(self.evidence())
        self.transition.write_bytes(original)
        (self.folder/f'scrape_summary_{MAIN_DATE}.json').write_bytes(b'{}')
        self.assertIsNone(self.evidence())

    def test_other_failure_or_push_cannot_gain_parser_retry(self):
        path = self.folder/'result.json'; original = path.read_bytes()
        for change in ({'reason': 'single_scrape_timeout'}, {'push_attempts': 1}, {'nightly': True},
                       {'scrape_runs': 2}, {'status': 'PUBLISHED'}, {'cloud_run_completed_declared': False}):
            path.write_bytes(original); self.update(result_changes=change)
            with self.subTest(change=change): self.assertIsNone(self.evidence())


class PrimePhaseRetryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='phase-retry-fixture-')
        self.root = Path(self.temporary.name)
        self.attempt, self.folder, self.summary = failed_early_phase_fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def evidence(self, **kwargs):
        return recovery.phase_transition_retry_evidence(self.attempt, account=1, target_date=MAIN_DATE,
            current_date=kwargs.pop('current_date', MAIN_DATE), remote_products=kwargs.pop('remote_products', []), **kwargs)

    def update_archive(self, *, summary=None, products=None, result_change=None):
        if summary is not None:
            save_json(self.folder / f'scrape_summary_{MAIN_DATE}.json', summary)
        if products is not None:
            save_json(self.folder / f'products_{MAIN_DATE}.json', products)
        path = self.folder / 'result.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        result['candidate_files_sha256'] = {name: recovery.digest((self.folder / name).read_bytes())
            for name in (f'products_{MAIN_DATE}.json', f'scrape_summary_{MAIN_DATE}.json')}
        result.update(result_change or {})
        save_json(path, result)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_exact_old_zero_failure_allows_evidence_without_modifying_any_archive(self):
        before = self.snapshot()
        self.assertIsNotNone(self.evidence())
        self.assertEqual(before, self.snapshot())

    def test_phase_wait_tracks_actual_jst_day_and_does_not_guess_from_target(self):
        self.assertEqual(recovery.prime_phase_wait_reason(MAIN_DATE, current_date='2026-10-15'),
                         'prime_event_wait_for_target_phase')
        self.assertIsNone(recovery.prime_phase_wait_reason(MAIN_DATE, current_date=MAIN_DATE))
        self.assertIsNone(recovery.prime_phase_wait_reason('2026-09-11', current_date='2026-09-10'))
        self.assertIsNone(recovery.prime_phase_wait_reason('2026-10-15', current_date='2026-10-14'))

    def test_not_yet_main_or_later_main_day_never_reopens_the_previous_attempt(self):
        for day in ('2026-10-15', '2026-10-17', '2026-10-20'):
            with self.subTest(day=day):
                self.assertIsNone(self.evidence(current_date=day))

    def test_remote_unknown_nonempty_or_nonlist_never_authorizes_retry(self):
        for remote in (None, [{'asin': 'B000000001'}], {}, '[]'):
            with self.subTest(remote=remote):
                self.assertIsNone(self.evidence(remote_products=remote))

    def test_nonzero_archive_is_preserved_even_when_failure_reason_is_same(self):
        self.update_archive(products=[{'asin': 'B000000001'}])
        before = self.snapshot()
        self.assertIsNone(self.evidence())
        self.assertEqual(before, self.snapshot())

    def test_other_failure_result_or_wrong_identity_does_not_gain_a_retry(self):
        path = self.folder / 'result.json'
        original = path.read_bytes()
        cases = ({'reason': 'single_scrape_timeout'}, {'status': 'PUBLISHED'}, {'scrape_runs': 0},
                 {'push_attempts': 1}, {'execute': False}, {'account': 'account2'},
                 {'date': '2026-10-15'}, {'source_origin_commit': 'b' * 40})
        for change in cases:
            with self.subTest(change=change):
                path.write_bytes(original)
                self.update_archive(result_change=change)
                self.assertIsNone(self.evidence())

    def test_wrong_attempt_identity_date_or_started_after_boundary_is_rejected(self):
        original = self.attempt.read_bytes()
        for change in ({'account': 2}, {'date': '2026-10-15'},
                       {'started_at_utc': '2026-10-16T00:00:00+09:00'},
                       {'started_at_utc': '2026-10-15T09:30:00'}):
            with self.subTest(change=change):
                value = json.loads(original)
                value.update(change)
                save_json(self.attempt, value)
                self.assertIsNone(self.evidence())

    def test_untrusted_output_directory_never_gets_read_as_a_recovery_run(self):
        original = self.attempt.read_bytes()
        for folder in (self.root / 'outside' / 'run-01', self.folder.parent / 'not-a-run',
                       self.folder.parent.parent / 'local-recovery-account2' / 'run-01'):
            with self.subTest(folder=folder):
                value = json.loads(original)
                value['output_dir'] = str(folder)
                save_json(self.attempt, value)
                self.assertIsNone(self.evidence())

    def test_missing_malformed_or_hash_tampered_archive_is_not_retry_proof(self):
        summary = self.folder / f'scrape_summary_{MAIN_DATE}.json'
        original = summary.read_bytes()
        for replacement in (None, b'{', original + b' '):
            with self.subTest(replacement=replacement is None):
                if replacement is None:
                    summary.unlink()
                else:
                    summary.write_bytes(replacement)
                self.assertIsNone(self.evidence())
                summary.write_bytes(original)

    def test_wrong_campaign_date_account_or_policy_is_not_the_phase_transition(self):
        for key, value in (('account', 2), ('date', '2026-10-15'),
                           ('policy', 'prime_appreciation_2026_electronics_v1'), ('min_price', 7999)):
            with self.subTest(key=key):
                summary = copy.deepcopy(self.summary)
                summary['campaign'][key] = value
                self.update_archive(summary=summary)
                self.assertIsNone(self.evidence())

    def test_inconsistent_counts_and_unrelated_rejection_do_not_prove_transition(self):
        for changes in ({'candidate_count': 2}, {'accepted_count': 1}, {'final_selected_count': 1},
                        {'rejected_count': 0}, {'rejection_reasons': {}},
                        {'observations': []}):
            with self.subTest(changes=changes):
                summary = copy.deepcopy(self.summary)
                summary['detail_offer_verification'].update(changes)
                self.update_archive(summary=summary)
                self.assertIsNone(self.evidence())
        summary = copy.deepcopy(self.summary)
        verification = summary['detail_offer_verification']
        verification['observations'][0]['reason'] = 'detail_http_error'
        verification['rejection_reasons'] = {'detail_http_error': 1}
        self.update_archive(summary=summary)
        self.assertIsNone(self.evidence())

    def test_other_rejected_candidates_are_retained_when_a_real_phase_mismatch_exists(self):
        summary = copy.deepcopy(self.summary)
        verification = summary['detail_offer_verification']
        verification['observations'].append(dict(asin='B000000002', status='rejected', reason='detail_http_error'))
        verification.update(candidate_count=2, rejected_count=2,
                            rejection_reasons={'detail_prime_event_phase_mismatch': 1, 'detail_http_error': 1})
        self.update_archive(summary=summary)
        self.assertIsNotNone(self.evidence())

    def test_recomputed_archive_hash_cannot_hide_wrong_asin_badge_or_observed_phase(self):
        cases = (('requested_asin', 'B000000099'), ('sale_label', 'プライム感謝祭'),
                 ('sale_label', 'タイムセール'), ('checked_at', '2026-10-16T00:01:00+09:00'),
                 ('http_status', 503), ('challenge_detected', True))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                summary = copy.deepcopy(self.summary)
                record = summary['detail_offer_verification']['observations'][0]
                record['evidence'][key] = value
                if key == 'checked_at':
                    record['checked_at'] = value
                record['evidence_sha256'] = observation_sha256(record['evidence'])
                self.update_archive(summary=summary)
                self.assertIsNone(self.evidence())


SCRAPER_FIXTURE = r'''
import json, os, time
from pathlib import Path

def generate(account):
    counter = Path(os.environ['RECOVERY_FIXTURE_COUNTER'])
    counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
    mode = os.environ.get('RECOVERY_FIXTURE_MODE', 'success')
    if mode == 'fail': raise SystemExit(9)
    if mode == 'noop': return
    if mode == 'timeout': time.sleep(10)
    count = {'empty': 0, 'one': 1, 'many': 11}.get(mode, 4)
    date = os.environ['PINEFIELD_TARGET_DATE']
    target = Path('data') / f'account{account}'
    products = []
    for i in range(1, count + 1):
        asin = f'B{i:09d}'
        category = ('Nintendo Switch 2' if i <= 2 else 'PS5ゲームソフト') if account == 20 else f'カテゴリ{i}'
        if mode == 'bad_quota': category = 'Nintendo Switch 2'
        item = dict(asin=asin, title=f'新しい商品{i}', price='￥3000', price_int=3000,
                    original_price='￥4000', discount_rate='25%OFF', image_url='https://example.invalid/image.png',
                    affiliate_url=f'https://www.amazon.co.jp/dp/{asin}?tag=noteamazon{account}-22',
                    category=category+'#1', rating='4.5', review_count='100', description='説明', specs='仕様')
        if mode == 'extra_key': item['unexpected'] = True
        products.append(item)
    policy = dict(selection_mode='category_quota' if account == 20 else 'global_ranked',
                  sort_order='sale_first', require_sale_info=True, max_per_category=5 if account == 20 else 2, max_total_items=10)
    (target / f'products_{date}.json').write_text(json.dumps(products,ensure_ascii=False,indent=2), encoding='utf-8')
    (target / f'scrape_summary_{date}.json').write_text(json.dumps(dict(date=date,total_taken=count,categories={},selection_policy=policy),indent=2),encoding='utf-8')
    if mode == 'ledger': (target / 'asin_history.json').write_text('{}')
    if mode == 'rotation': (target / 'category_rotation.json').write_text('{"changed":true}')
    if mode == 'config': Path(f'categories{account}.yaml').write_text('changed: true')
    if mode == 'extra_file': Path('unexpected.txt').write_text('unexpected')
'''


class RecoveryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recovery-tests-')
        self.root = Path(self.temporary.name)
        self.bare, self.seed = self.root/'origin.git', self.root/'seed'
        subprocess.run(['git','init','--bare','--quiet',str(self.bare)],check=True)
        subprocess.run(['git','init','--quiet','-b','main',str(self.seed)],check=True)
        git(self.seed,'config','user.name','fixture')
        git(self.seed,'config','user.email','fixture@example.invalid')
        shutil.copyfile(ROOT/'ensure_daily_scrape.py', self.seed/'ensure_daily_scrape.py')
        shutil.copyfile(ROOT/'detail_offer.py', self.seed/'detail_offer.py')
        shutil.copyfile(ROOT/'sale_campaign.py', self.seed/'sale_campaign.py')
        shutil.copyfile(ROOT/'sale_campaign.yaml', self.seed/'sale_campaign.yaml')
        (self.seed/'scraper.py').write_text(textwrap.dedent(SCRAPER_FIXTURE),encoding='utf-8')
        (self.seed/'README.md').write_text('fixture\n')
        self.factory_base = self.root/'factories'
        for account in range(1,21):
            (self.factory_base/f'★全体的なワークフロー_1_{account}'/'②記事制作').mkdir(parents=True)
            (self.seed/f'scrape_main{account}.py').write_text(f'from scraper import generate\nif __name__ == "__main__": generate({account})\n')
            config = {'categories': [], 'filters': {'selection_mode': 'category_quota' if account==20 else 'global_ranked',
                'sort_order':'sale_first','max_per_category':5 if account==20 else 2,'max_total_items':10},
                'exclusion':{'exclude_scraped_candidates':False,'exclude_product_identifiers':True,'exclude_within_days':20,
                             'posted_asins_file':f'data/account{account}/asin_history.json'}}
            (self.seed/f'categories{account}.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
            save_json(self.seed/f'data/account{account}/asin_history.json',{'schema':'note-amazon-asin-history-v1','posted':[]})
            save_json(self.seed/f'data/account{account}/category_rotation.json',{'fixture':True})
            # Preexisting dated output must never be mistaken for this invocation's output.
            save_json(self.seed/f'data/account{account}/products_{DATE}.json',[{'stale_fixture':True}])
            save_json(self.seed/f'data/account{account}/scrape_summary_{DATE}.json',{'stale_fixture':True})
        git(self.seed,'add','--all');git(self.seed,'commit','-qm','synthetic fixture')
        git(self.seed,'remote','add','origin',str(self.bare));git(self.seed,'push','-q','origin','main')
        git(self.bare,'symbolic-ref','HEAD','refs/heads/main')
        self.initial = git(self.seed,'rev-parse','HEAD')
        self.counter = self.root/'scrape-count.txt'
        self.patches = [patch.object(recovery,'FACTORY_BASE',self.factory_base),
                        patch.object(recovery,'OUTPUT_BASE',self.root/'outputs'),
                        patch.object(recovery,'today_jst',return_value=DATE),
                        patch.object(recovery,'expected_origin',side_effect=lambda value:value==str(self.bare)),
                        patch.dict(os.environ,{'RECOVERY_FIXTURE_COUNTER':str(self.counter),'RECOVERY_FIXTURE_MODE':'success'})]
        for context in self.patches: context.start()

    def tearDown(self):
        for context in reversed(self.patches): context.stop()
        self.temporary.cleanup()

    def execute(self, account=1, **kwargs):
        return recovery.recover(account=account,target_date=DATE,repo=self.seed,
                                execute=kwargs.pop('execute',True),cloud_run_completed=kwargs.pop('cloud_run_completed',True),
                                timeout=kwargs.pop('timeout',30),**kwargs)

    def count(self):
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def source(self, account=1):
        return self.factory_base/f'★全体的なワークフロー_1_{account}'/'②記事制作'/'実行結果'/f'account{account}'/DATE/'source'/'source_manifest.json'

    def advance(self, relative, value='remote change'):
        other=self.root/('other-'+str(len(list(self.root.glob('other-*')))))
        subprocess.run(['git','clone','--quiet',str(self.bare),str(other)],check=True)
        git(other,'config','user.name','other');git(other,'config','user.email','other@example.invalid')
        path=other/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value,encoding='utf-8')
        git(other,'add','--',relative);git(other,'commit','-qm','independent fixture update');git(other,'push','-q','origin','HEAD:main')

    def publish_valid_fixture(self, account):
        environment = os.environ.copy()
        environment.update(PINEFIELD_TARGET_DATE=DATE, PYTHONDONTWRITEBYTECODE='1')
        subprocess.run([sys.executable, '-B', f'scrape_main{account}.py'], cwd=self.seed,
                       env=environment, check=True)
        self.counter.unlink()
        paths = [f'data/account{account}/products_{DATE}.json', f'data/account{account}/scrape_summary_{DATE}.json']
        git(self.seed, 'add', '--', *paths)
        git(self.seed, 'commit', '-qm', 'valid existing fixture')
        git(self.seed, 'push', '-q', 'origin', 'main')
        return git(self.bare, 'rev-parse', 'main'), paths

    def test_existing_valid_git_source_is_reused_without_scrape_attempt_or_push(self):
        for account in (1, 20):
            with self.subTest(account=account):
                base, paths = self.publish_valid_fixture(account)
                before = {name: subprocess.check_output(['git', '-C', str(self.bare), 'show', 'main:' + name]) for name in paths}
                result = self.execute(account=account)
                self.assertEqual(result['status'], 'UNCHANGED_VALIDATED')
                self.assertTrue(result['reused_existing_valid_output'])
                self.assertEqual(result['product_count'], 4)
                self.assertEqual((result['scrape_runs'], result['push_attempts'], self.count()), (0, 0, 0))
                self.assertEqual(result['source_origin_commit'], base)
                self.assertEqual(result['remote_commit'], base)
                self.assertEqual(git(self.bare, 'rev-parse', 'main'), base)
                self.assertEqual(result['artifact_source'], 'origin/main_readback_confirmed')
                self.assertTrue(result['worktree_removed'])
                self.assertFalse((Path(result['output_dir']).parent / f'.control/account{account}_{DATE}.attempt.json').exists())
                for name, content in before.items():
                    self.assertEqual(result['files_sha256'][name], hashlib.sha256(content).hexdigest().upper())
                    self.assertEqual((Path(result['output_dir']) / Path(name).name).read_bytes(), content)

    def test_invalid_existing_policy_is_repaired_instead_of_reused(self):
        _, paths = self.publish_valid_fixture(1)
        summary = json.loads((self.seed / paths[1]).read_text(encoding='utf-8'))
        summary['selection_policy']['sort_order'] = 'review_desc'
        self.advance(paths[1], json.dumps(summary))
        result = self.execute()
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertNotIn('reused_existing_valid_output', result)
        self.assertEqual((result['scrape_runs'], self.count()), (1, 1))

    def test_scrape_uses_its_own_diagnostic_directory_instead_of_inherited_path(self):
        actual = recovery.subprocess.Popen
        observed = []
        inherited = self.root / 'untrusted-inherited-diagnostics'
        def inspect(arguments, **kwargs):
            if 'scrape_main1.py' in arguments:
                observed.append(kwargs['env']['PINEFIELD_SEARCH_DIAGNOSTICS_DIR'])
            return actual(arguments, **kwargs)
        with patch.dict(os.environ, {'PINEFIELD_SEARCH_DIAGNOSTICS_DIR': str(inherited)}), \
             patch.object(recovery.subprocess, 'Popen', side_effect=inspect):
            result = self.execute()
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(observed, [str(Path(result['output_dir']) / 'public-search-diagnostics')])
        self.assertFalse(inherited.exists())

    def test_default_preflight_never_scrapes_pushes_or_claims_attempt(self):
        result=self.execute(execute=False,cloud_run_completed=False)
        self.assertEqual(result['status'],'PREFLIGHT_PASS');self.assertEqual(self.count(),0)
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)
        self.assertFalse((Path(result['output_dir']).parent/f'.control/account1_{DATE}.attempt.json').exists())
        self.assertTrue(result['worktree_removed'])

    def test_subprocess_success_publishes_only_two_fresh_files_and_preserves_dirty_seed(self):
        (self.seed/'README.md').write_text('staged user change');git(self.seed,'add','README.md')
        (self.seed/'scraper.py').write_text('unstaged user change')
        index_before=(self.seed/'.git/index').read_bytes()
        ledger_before=(self.seed/'data/account1/asin_history.json').read_bytes()
        ledger_git_before=git(self.bare,'show','main:data/account1/asin_history.json')
        result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(self.count(),1)
        self.assertEqual(result['push_attempts'],1);self.assertTrue(result['worktree_removed'])
        self.assertEqual(git(self.seed,'rev-parse','HEAD'),self.initial)
        self.assertEqual((self.seed/'.git/index').read_bytes(),index_before)
        self.assertEqual((self.seed/'scraper.py').read_text(),'unstaged user change')
        changed=set(git(self.bare,'diff','--name-only',self.initial,'main').splitlines())
        self.assertEqual(changed,{f'data/account1/products_{DATE}.json',f'data/account1/scrape_summary_{DATE}.json'})
        self.assertEqual(git(self.bare,'show','main:data/account1/asin_history.json'),ledger_git_before)
        self.assertEqual((self.seed/'data/account1/asin_history.json').read_bytes(),ledger_before)
        for name,digest in result['files_sha256'].items():
            remote=subprocess.check_output(['git','-C',str(self.bare),'show','main:'+name])
            self.assertEqual(hashlib.sha256(remote).hexdigest().upper(),digest)
            self.assertEqual((Path(result['output_dir'])/Path(name).name).read_bytes(),remote)
        self.assertEqual(result['artifact_source'],'origin/main_readback_confirmed')
        for change in result['representation_changes']:
            raw=Path(change['raw_path']).read_bytes()
            canonical=(Path(result['output_dir'])/Path(change['file']).name).read_bytes()
            self.assertEqual(raw.replace(b'\r\n',b'\n'),canonical.replace(b'\r\n',b'\n'))
            self.assertEqual(hashlib.sha256(raw).hexdigest().upper(),change['generated_sha256'])
            self.assertTrue(change['values_and_order_unchanged'])
        self.assertFalse(any(Path(result['output_dir']).rglob('asin_history.json')))

    def test_missing_cloud_completion_and_other_date_stop_before_subprocess(self):
        with self.assertRaisesRegex(recovery.RecoveryStop,'cloud_run_completed'):self.execute(cloud_run_completed=False)
        with self.assertRaisesRegex(recovery.RecoveryStop,'JST_today'):
            recovery.recover(account=1,target_date='2026-09-09',repo=self.seed,execute=True,cloud_run_completed=True)
        self.assertEqual(self.count(),0)

    def test_existing_source_and_existing_unknown_part_lock_stop(self):
        save_json(self.source(),{'fixed':True})
        self.assertIn('source_already',self.execute()['reason'])
        lock=self.factory_base/'★全体的なワークフロー_1_2'/'②記事制作'/'実行結果'/'.locks'/f'account2_{DATE}_p34.lock.json'
        lock.parent.mkdir(parents=True);lock.write_text('unknown lock')
        self.assertIn('existing_factory_part_lock',self.execute(account=2)['reason'])
        self.assertEqual(lock.read_text(),'unknown lock');self.assertEqual(self.count(),0)

    def test_part_locks_have_exact_runner_shape_and_are_released(self):
        actual=recovery.scrape_once
        def inspect(*args):
            for part in ('p12','p34'):
                lock=self.factory_base/'★全体的なワークフロー_1_1'/'②記事制作'/'実行結果'/'.locks'/f'account1_{DATE}_{part}.lock.json'
                value=json.loads(lock.read_text())
                self.assertEqual(set(value),{'schema','token','pid','hostname','started_at','account','date','part'})
                self.assertEqual(value['pid'],os.getpid());self.assertEqual(value['part'],part)
            return actual(*args)
        with patch.object(recovery,'scrape_once',side_effect=inspect):result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED')
        self.assertEqual(list(self.factory_base.rglob('*.lock.json')),[])

    def test_noop_cannot_validate_stale_daily_files_and_repeat_is_refused(self):
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'noop'}):first=self.execute()
        self.assertEqual(first['reason'],'canonical_output_validation_failed')
        self.assertEqual(first['status'],'STOPPED');self.assertEqual(first['push_attempts'],0)
        second=self.execute();self.assertEqual(second['reason'],'same_account_date_already_attempted')
        self.assertEqual(self.count(),1);self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_invalid_counts_and_extra_field_are_rejected_by_local_validation(self):
        for account,mode in enumerate(('empty','many','extra_key'),1):
            with self.subTest(mode=mode),patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':mode}):result=self.execute(account=account)
            self.assertEqual(result['status'],'STOPPED');self.assertEqual(result['push_attempts'],0)
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_one_product_is_valid_but_only_one_daily_slot_is_ready(self):
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'one'}):result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['product_count'],1)
        self.assertEqual(result['postable_after'],1);self.assertEqual(result['push_attempts'],1)
        self.assertEqual(git(self.bare,'rev-parse','main'),result['published_commit'])

    def test_ledger_rotation_config_or_extra_file_change_never_reaches_push(self):
        for account,mode in enumerate(('ledger','rotation','config','extra_file'),1):
            with self.subTest(mode=mode),patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':mode}):result=self.execute(account=account)
            self.assertEqual(result['status'],'STOPPED');self.assertEqual(result['push_attempts'],0)
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_account20_two_plus_two_passes_current_validator(self):
        result=self.execute(account=20)
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['product_count'],4)

    def test_account20_one_shelf_is_valid_but_only_two_daily_slots_are_ready(self):
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'bad_quota'}):result=self.execute(account=20)
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['product_count'],4)
        self.assertEqual(result['postable_after'],2)
        self.assertEqual(git(self.bare,'rev-parse','main'),result['published_commit'])

    def test_child_nonzero_and_timeout_do_not_push(self):
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'fail'}):result=self.execute(account=1)
        self.assertEqual(result['reason'],'single_scrape_failed:9')
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'timeout'}):result=self.execute(account=2,timeout=1)
        self.assertEqual(result['reason'],'single_scrape_timeout')
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_other_account_advance_rebases_once_and_preserves_other_output(self):
        actual=recovery.scrape_once
        def advance(*args):
            actual(*args);self.advance(f'data/account2/products_{DATE}.json','{"other_account":true}')
        with patch.object(recovery,'scrape_once',side_effect=advance):result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['rebase_attempts'],1)
        self.assertEqual(git(self.bare,'show',f'main:data/account2/products_{DATE}.json'),'{'+'"other_account":true}')

    def test_own_ledger_or_shared_code_advance_stops_without_push(self):
        actual=recovery.scrape_once
        for account,relative in ((1,'data/account1/asin_history.json'),(2,'scraper.py')):
            def advance(*args,relative=relative):
                actual(*args);self.advance(relative,'changed upstream')
            with self.subTest(relative=relative),patch.object(recovery,'scrape_once',side_effect=advance):result=self.execute(account=account)
            self.assertEqual(result['reason'],'remote_changed_account_or_shared_inputs')
            self.assertEqual(result['push_attempts'],0)

    def test_factory_source_created_during_scrape_stops_before_publish(self):
        actual=recovery.scrape_once
        def source_race(*args):
            actual(*args);save_json(self.source(),{'external_fixed':True})
        with patch.object(recovery,'scrape_once',side_effect=source_race):result=self.execute()
        self.assertIn('source_already',result['reason']);self.assertEqual(result['push_attempts'],0)

    def test_push_race_does_not_retry_or_overwrite_competing_products(self):
        actual=recovery.Commands.git
        raced=[]
        def push_race(commands,repo,*args,**kwargs):
            if args[:1]==('push',) and not raced:
                raced.append(True);self.advance(f'data/account1/products_{DATE}.json','{"competing":true}')
            return actual(commands,repo,*args,**kwargs)
        with patch.object(recovery.Commands,'git',new=push_race):result=self.execute()
        self.assertEqual(result['reason'],'push_conflict_or_unconfirmed_no_retry')
        self.assertEqual(result['push_attempts'],1);self.assertEqual(self.count(),1)
        self.assertEqual(git(self.bare,'show',f'main:data/account1/products_{DATE}.json'),'{'+'"competing":true}')

    def test_midnight_crossing_stops_before_push(self):
        actual=recovery.scrape_once
        def change_date(*args):
            actual(*args);recovery.today_jst.return_value='2026-09-11'
        with patch.object(recovery,'scrape_once',side_effect=change_date):result=self.execute()
        self.assertEqual(result['reason'],'target_must_be_JST_today');self.assertEqual(result['push_attempts'],0)

    def test_same_account_date_lock_is_exclusive(self):
        lock=self.root/'lock'
        with recovery.account_lock(lock):
            with self.assertRaisesRegex(recovery.RecoveryStop,'lock_busy'):
                with recovery.account_lock(lock):pass

    def test_preflight_checks_current_policy_and_canonical_ledger_path(self):
        self.advance('categories1.yaml','filters: {}\nexclusion: {}\n')
        result=self.execute(execute=False)
        self.assertEqual(result['reason'],'non_current_selection_policy');self.assertEqual(self.count(),0)

    def test_cleanup_error_preserves_published_result_and_remaining_checkout(self):
        actual=recovery.Commands.git
        def cleanup_failure(commands,repo,*args,**kwargs):
            if args[:2]==('worktree','remove'):raise OSError('synthetic cleanup failure')
            return actual(commands,repo,*args,**kwargs)
        with patch.object(recovery.Commands,'git',new=cleanup_failure):result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED');self.assertFalse(result['worktree_removed'])
        self.assertEqual(result['cleanup_error'],'OSError')
        saved=json.loads((Path(result['output_dir'])/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'],'PUBLISHED')
        remaining=Path(result['remaining_worktree']).resolve()
        self.assertEqual(remaining.name,'repo');self.assertTrue(remaining.parent.name.startswith('pinefield-recovery-1-'))
        git(self.seed,'worktree','remove','--force',str(remaining))
        remaining.parent.rmdir()

    def test_uncertain_push_response_is_read_back_without_second_push(self):
        actual=recovery.Commands.git
        def uncertain(commands,repo,*args,**kwargs):
            result=actual(commands,repo,*args,**kwargs)
            if args[:1]==('push',):raise subprocess.TimeoutExpired('synthetic response lost',1)
            return result
        with patch.object(recovery.Commands,'git',new=uncertain):result=self.execute()
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['push_attempts'],1)
        self.assertEqual(result['push_uncertain_error'],'TimeoutExpired')

    def test_wrong_declared_ledger_path_and_scraped_history_writes_fail_preflight(self):
        for account,change in ((1,{'posted_asins_file':'data/account2/asin_history.json'}),
                               (2,{'exclude_scraped_candidates':True})):
            config=yaml.safe_load((self.seed/f'categories{account}.yaml').read_text())
            config['exclusion'].update(change)
            self.advance(f'categories{account}.yaml',yaml.safe_dump(config))
            result=self.execute(account=account,execute=False)
            self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(self.count(),0)

    def test_different_clone_cannot_repeat_same_account_day(self):
        first=self.execute()
        self.assertEqual(first['status'],'PUBLISHED')
        other=self.root/'separate-clone'
        subprocess.run(['git','clone','--quiet',str(self.bare),str(other)],check=True)
        second=recovery.recover(account=1,target_date=DATE,repo=other,execute=True,cloud_run_completed=True)
        self.assertEqual(second['status'],'UNCHANGED_VALIDATED')
        self.assertTrue(second['reused_existing_valid_output'])
        self.assertEqual(second['scrape_runs'],0)
        self.assertEqual(second['push_attempts'],0)
        self.assertEqual(self.count(),1)

    def execute_main(self, account=1, **kwargs):
        with patch.object(recovery, 'today_jst', return_value=MAIN_DATE):
            return recovery.recover(account=account, target_date=MAIN_DATE, repo=self.seed,
                execute=kwargs.pop('execute', True), cloud_run_completed=True, timeout=30, **kwargs)

    def test_future_main_phase_wait_does_not_fetch_scrape_or_consume_an_attempt(self):
        with patch.object(recovery, 'today_jst', return_value='2026-10-15'), \
             patch.object(recovery.Commands, 'git', side_effect=AssertionError('No Git calls while waiting')), \
             patch.object(recovery, 'scrape_once', side_effect=AssertionError('No scrape while waiting')), \
             patch.object(recovery, 'factory_locks', side_effect=AssertionError('No factory lock while waiting')):
            result = recovery.recover(account=1, target_date=MAIN_DATE, repo=self.seed, execute=True,
                cloud_run_completed=True, nightly=True, before_publish=lambda: {'status': 'READY'})
        self.assertEqual((result['status'], result['reason'], result['scrape_runs'], result['push_attempts']),
                         ('WAIT', 'prime_event_wait_for_target_phase', 0, 0))
        self.assertEqual(self.count(), 0)
        self.assertEqual(list((self.root / 'outputs').rglob('*.attempt.json')), [])
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_phase_retry_preflight_then_one_failed_retry_preserves_old_attempt_and_archives(self):
        attempt, folder, _ = failed_early_phase_fixture(self.root / 'outputs', commit=self.initial)
        old_files = {p: p.read_bytes() for p in (attempt, *folder.iterdir()) if p.is_file()}
        transition = attempt.with_name(f'account1_{MAIN_DATE}.phase-transition-main.attempt.json')
        preflight = self.execute_main(execute=False)
        self.assertEqual(preflight['status'], 'PREFLIGHT_PASS')
        self.assertIn('phase_transition_retry', preflight)
        self.assertFalse(transition.exists())
        self.assertEqual(self.count(), 0)
        with patch.dict(os.environ, {'RECOVERY_FIXTURE_MODE': 'empty'}):
            first = self.execute_main()
        self.assertEqual((first['status'], first['reason'], first['scrape_runs']),
                         ('STOPPED', 'canonical_output_validation_failed', 1))
        self.assertTrue(transition.is_file())
        marker_before = transition.read_bytes()
        second = self.execute_main()
        self.assertEqual(second['reason'], 'same_account_date_already_attempted')
        self.assertEqual((second['scrape_runs'], second['push_attempts'], self.count()), (0, 0, 1))
        self.assertEqual(marker_before, transition.read_bytes())
        self.assertEqual(old_files, {p: p.read_bytes() for p in old_files})
        self.assertEqual(git(self.bare, 'rev-parse', 'main'), self.initial)

    def test_phase_retry_can_publish_valid_new_output_without_resetting_the_original(self):
        attempt, folder, old_summary = failed_early_phase_fixture(self.root / 'outputs', commit=self.initial)
        old_files = {p: p.read_bytes() for p in (attempt, *folder.iterdir()) if p.is_file()}
        def main_scrape(worktree, account, target_date, timeout, log):
            # Use genuine validator/Prime proof for the newly acquired candidate.
            self.counter.write_text('1')
            summary = copy.deepcopy(old_summary)
            records, products, rows = [], [], []
            for position, category in enumerate(summary['campaign']['categories'], 1):
                asin = f'B{position:09d}'
                evidence = copy.deepcopy(old_summary['detail_offer_verification']['observations'][0]['evidence'])
                evidence.update(requested_asin=asin, selected_asins=[asin], url=f'https://www.amazon.co.jp/dp/{asin}',
                    sale_label='プライム感謝祭', checked_at='2026-10-16T00:10:00+09:00')
                offer, reason = validate_offer(evidence, offer_scope='unified_discounts')
                self.assertIsNone(reason)
                item = dict(asin=asin, title=f'本祭の商品{position}', **offer,
                    image_url='https://example.invalid/image.png',
                    affiliate_url=f'https://www.amazon.co.jp/dp/{asin}?tag=noteamazon{account}-22',
                    category=category['name'] + '#1', rating='', review_count='', description='', specs='')
                record = dict(asin=asin, category=item['category'], checked_at=evidence['checked_at'],
                    status='accepted', reason=None, pdp_offer=offer, evidence=evidence,
                    evidence_sha256=observation_sha256(evidence))
                products.append(item); records.append(record)
                rows.append(discount_row(SimpleNamespace(**item), record, position, prime_source_date=target_date))
            summary.update(total_taken=4, categories={},
                discount_contract=dict(schema_version=1, policy='amazon_direct_discounts_v1',
                                       account=account, date=target_date, products=rows))
            summary['detail_offer_verification'].update(candidate_count=4, accepted_count=4, rejected_count=0,
                final_selected_count=4, rejection_reasons={}, observations=records)
            save_json(worktree / f'data/account{account}/products_{target_date}.json', products)
            save_json(worktree / f'data/account{account}/scrape_summary_{target_date}.json', summary)
        with patch.object(recovery, 'scrape_once', side_effect=main_scrape):
            result = self.execute_main()
        self.assertEqual(result['status'], 'PUBLISHED', result)
        self.assertEqual((result['scrape_runs'], result['push_attempts'], self.count()), (1, 1, 1))
        self.assertIn('phase_transition_retry', result)
        self.assertEqual(old_files, {p: p.read_bytes() for p in old_files})
        changed = set(git(self.bare, 'diff', '--name-only', self.initial, 'main').splitlines())
        self.assertEqual(changed, {f'data/account1/products_{MAIN_DATE}.json',
                                   f'data/account1/scrape_summary_{MAIN_DATE}.json'})

    def test_phase_retry_does_not_bypass_sealed_source_or_an_existing_factory_owner(self):
        for account, protected in ((1, 'source'), (2, 'owner')):
            with self.subTest(protected=protected):
                attempt, _, _ = failed_early_phase_fixture(self.root / 'outputs', account, self.initial)
                transition = attempt.with_name(f'account{account}_{MAIN_DATE}.phase-transition-main.attempt.json')
                if protected == 'source':
                    path = self.factory_base / f'★全体的なワークフロー_1_{account}' / '②記事制作' / '実行結果' / f'account{account}' / MAIN_DATE / 'source' / 'source_manifest.json'
                    save_json(path, {'sealed': True})
                    expected_reason = 'source_already'
                else:
                    path = self.factory_base / f'★全体的なワークフロー_1_{account}' / '②記事制作' / '実行結果' / '.locks' / f'account{account}_{MAIN_DATE}_p12.lock.json'
                    save_json(path, {'token': 'other-owner', 'pid': os.getpid()})
                    expected_reason = 'existing_factory_part_lock'
                before = path.read_bytes()
                result = self.execute_main(account=account)
                self.assertIn(expected_reason, result['reason'])
                self.assertFalse(transition.exists())
                self.assertEqual(before, path.read_bytes())
                self.assertEqual((result['scrape_runs'], result['push_attempts'], self.count()), (0, 0, 0))

    def test_main_label_parser_retry_is_exclusive_and_preserves_both_prior_attempts(self):
        attempt, transition, folder, _ = failed_main_label_fixture(self.root/'outputs', commit=self.initial)
        old_files = {p: p.read_bytes() for p in (attempt, transition, *folder.iterdir()) if p.is_file()}
        marker = attempt.with_name('account1_2026-10-16.main-label-compatibility.attempt.json')
        preflight = self.execute_main(execute=False)
        self.assertEqual(preflight['status'], 'PREFLIGHT_PASS', preflight)
        self.assertIn('label_parser_correction_retry', preflight)
        self.assertFalse(marker.exists())
        with patch.dict(os.environ, {'RECOVERY_FIXTURE_MODE': 'empty'}):
            first = self.execute_main()
        self.assertEqual(first['status'], 'STOPPED', first)
        self.assertEqual(first['scrape_runs'], 1)
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text())['schema'], 'prime-main-label-parser-correction-attempt-v1')
        marker_bytes = marker.read_bytes()
        second = self.execute_main()
        self.assertEqual(second['reason'], 'same_account_date_already_attempted')
        self.assertEqual((second['scrape_runs'], second['push_attempts'], self.count()), (0, 0, 1))
        self.assertEqual(marker.read_bytes(), marker_bytes)
        self.assertEqual(old_files, {p: p.read_bytes() for p in old_files})

    def test_main_label_retry_rejects_runtime_owner_without_consuming_marker(self):
        attempt, _, _, _ = failed_main_label_fixture(self.root/'outputs', commit=self.initial)
        owner = self.factory_base/'factory-runtime/state/account1'/MAIN_DATE/'role-B.lock.json'
        save_json(owner, {'token': 'another-owner', 'pid': os.getpid()})
        before = owner.read_bytes()
        result = self.execute_main()
        self.assertEqual(result['reason'], 'existing_factory_runtime_owner_for_main_label_retry')
        self.assertEqual((result['scrape_runs'], result['push_attempts'], self.count()), (0, 0, 0))
        self.assertFalse(attempt.with_name('account1_2026-10-16.main-label-compatibility.attempt.json').exists())
        self.assertEqual(owner.read_bytes(), before)

    def test_main_label_retry_requires_fetched_parser_to_match_successful_replay(self):
        attempt, _, _, _ = failed_main_label_fixture(self.root/'outputs', commit=self.initial)
        self.advance('detail_offer.py', (ROOT/'detail_offer.py').read_text(encoding='utf-8')+'\n# different fetched parser fixture\n')
        result = self.execute_main()
        self.assertEqual(result['reason'], 'fetched_main_label_parser_differs_from_replayed_parser', result)
        self.assertEqual((result['scrape_runs'], result['push_attempts'], self.count()), (0, 0, 0))
        self.assertFalse(attempt.with_name('account1_2026-10-16.main-label-compatibility.attempt.json').exists())

    def test_nightly_cloud_wait_reuses_saved_candidate_without_another_scrape(self):
        # A validated candidate waiting for Cloud is unrelated to a failed phase transition.
        first = self.execute(nightly=True, before_publish=lambda: {'status': 'WAIT', 'reason': 'fixture-active'})
        self.assertEqual(first['reason'], 'cloud_not_ready_before_publish_candidate_saved')
        self.assertEqual(self.count(), 1)
        attempt = Path(first['output_dir']).parent / '.control' / f'account1_{DATE}.attempt.json'
        before = attempt.read_bytes()
        pending = attempt.with_name(f'account1_{DATE}.pending.json')
        self.assertTrue(pending.exists())
        with patch.object(recovery, 'scrape_once', side_effect=AssertionError('Reuse the validated candidate')):
            second = self.execute(nightly=True, before_publish=lambda: {'status': 'READY'})
        self.assertEqual(second['status'], 'PUBLISHED', second)
        self.assertEqual((second['scrape_runs'], second['push_attempts'], self.count()), (0, 1, 1))
        self.assertIn('reused_pending_candidate', second)
        self.assertEqual(before, attempt.read_bytes())
        self.assertFalse(pending.exists())


if __name__=='__main__':unittest.main()
