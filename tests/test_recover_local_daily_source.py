"""Local bare Git and synthetic subprocess fixtures only; no Amazon or GitHub calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import recover_local_daily_source as recovery

DATE = '2026-09-10'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode('utf-8').strip()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


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
    count = {'few': 1, 'many': 11}.get(mode, 4)
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
        self.assertEqual(first['reason'],'fresh_output_missing')
        second=self.execute();self.assertEqual(second['reason'],'same_account_date_already_attempted')
        self.assertEqual(self.count(),1);self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_invalid_counts_and_extra_field_are_rejected_by_local_validation(self):
        for account,mode in enumerate(('few','many','extra_key'),1):
            with self.subTest(mode=mode),patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':mode}):result=self.execute(account=account)
            self.assertEqual(result['status'],'STOPPED');self.assertEqual(result['push_attempts'],0)
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_ledger_rotation_config_or_extra_file_change_never_reaches_push(self):
        for account,mode in enumerate(('ledger','rotation','config','extra_file'),1):
            with self.subTest(mode=mode),patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':mode}):result=self.execute(account=account)
            self.assertEqual(result['status'],'STOPPED');self.assertEqual(result['push_attempts'],0)
        self.assertEqual(git(self.bare,'rev-parse','main'),self.initial)

    def test_account20_two_plus_two_passes_current_validator(self):
        result=self.execute(account=20)
        self.assertEqual(result['status'],'PUBLISHED');self.assertEqual(result['product_count'],4)

    def test_account20_missing_one_quota_category_fails(self):
        with patch.dict(os.environ,{'RECOVERY_FIXTURE_MODE':'bad_quota'}):result=self.execute(account=20)
        self.assertEqual(result['reason'],'canonical_output_validation_failed')

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
        self.assertEqual(second['reason'],'same_account_date_already_attempted')
        self.assertEqual(self.count(),1)


if __name__=='__main__':unittest.main()
