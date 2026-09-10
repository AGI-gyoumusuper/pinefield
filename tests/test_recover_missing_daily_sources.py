"""Offline fixtures only: no Amazon, real Git fetch/push, Cloud dispatch or browser."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import recover_missing_daily_sources as controller

DATE = '2026-09-11'


def run(account=None, *, workflow=None, identifier=None, status='completed', conclusion='success', event='push'):
    workflow = workflow or ('scrape.yml' if account <= 5 else f'scrape{account}.yml')
    return {'id': identifier or account, 'path': '.github/workflows/' + workflow, 'event': event,
            'status': status, 'conclusion': conclusion, 'created_at': '2026-09-10T14:30:00Z' if workflow == 'ensure-scrape.yml' else '2026-09-10T15:05:00Z',
            'head_sha': 'a' * 40, 'html_url': 'https://example.invalid/run',
            'display_title': f'Trigger [scrape:account{account}] [target:{DATE}]' if account else workflow,
            'head_commit': {'message': 'unrelated commit'}, 'run_attempt': 1}


def complete_runs():
    return [run(n) for n in range(1, 21)] + [
        run(workflow='ensure-scrape.yml', identifier=21, event='schedule'),
        run(workflow='late-repair-scrape.yml', identifier=22, event='schedule')]


def audit(missing=()):
    return {'valid_count': 20-len(missing), 'missing_accounts': list(missing), 'origin_main_commit': 'a'*40,
            'rows': [{'account': n, 'valid': n not in missing, 'count': None if n in missing else 4} for n in range(1, 21)]}


class CloudGateTests(unittest.TestCase):
    def gate(self, runs):
        with patch.object(controller, 'fetch_runs', return_value=runs):
            return controller.cloud_gate(DATE)

    def test_all_twenty_and_insurances_complete_even_failure_allows_local_repair(self):
        rows = complete_runs(); rows[5]['conclusion'] = 'failure'
        result = self.gate(rows)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(len(result['latest_completed_candidates']), 22)

    def test_missing_regular_and_not_yet_created_late_wait(self):
        rows = [r for r in complete_runs() if r['id'] not in (6, 22)]
        result = self.gate(rows)
        self.assertEqual(result['status'], 'WAIT')
        self.assertEqual(set(result['missing_completion_keys']), {'account6', 'late-repair-scrape.yml'})

    def test_any_active_regular_ensure_late_blocks_even_older_than_latest(self):
        for index in (5, 20, 21):
            with self.subTest(index=index):
                rows = complete_runs()
                older = {**rows[index], 'id': -index-1, 'status': 'in_progress', 'conclusion': None}
                result = self.gate(rows + [older])
                self.assertEqual(result['status'], 'WAIT')
                self.assertEqual(len(result['active_runs']), 1)

    def test_newer_retry_running_and_unknown_dispatch_target_fail_closed(self):
        for candidate in (run(6, identifier=100, status='queued', conclusion=None),
                          run(workflow='late-repair-scrape.yml', identifier=100,
                              event='workflow_dispatch', status='in_progress', conclusion=None)):
            self.assertEqual(self.gate(complete_runs() + [candidate])['status'], 'WAIT')

    def test_previous_or_tomorrow_runs_never_prove_today_and_skipped_not_proof(self):
        for changed in ('previous', 'skipped'):
            rows = complete_runs()
            if changed == 'previous':
                rows[0]['display_title'] = 'Trigger [scrape:account1] [target:2026-09-10]'
            else:
                rows[0]['conclusion'] = 'skipped'
            self.assertEqual(self.gate(rows)['status'], 'WAIT')

    def test_repair_target_never_inherits_unrelated_head_commit_marker(self):
        rows = complete_runs()
        for row in rows[-2:]:
            row['head_commit'] = {'message': '[scrape:account20] [target:2026-09-12]'}
        self.assertEqual(self.gate(rows)['status'], 'READY')

    def test_api_unavailable_or_malformed_is_stop(self):
        for exception in (HTTPError('https://example.invalid', 403, 'limited', {}, None), ValueError('invalid')):
            with patch.object(controller, 'fetch_runs', side_effect=exception):
                self.assertEqual(controller.cloud_gate(DATE)['status'], 'STOP')

    def test_partial_pagination_is_not_empty_success(self):
        with patch.object(controller, 'api_json', return_value={'total_count': 101, 'workflow_runs': [{'id': 1}]}):
            with self.assertRaisesRegex(controller.ControllerStop, 'incomplete_pagination'):
                controller.fetch_runs(DATE)

    def test_pagination_keeps_all_runs_and_uses_public_endpoint(self):
        first = [{'id': n} for n in range(100)]
        with patch.object(controller, 'api_json', side_effect=[{'total_count': 101, 'workflow_runs': first},
                                                              {'total_count': 101, 'workflow_runs': [{'id': 100}]}]) as api:
            self.assertEqual(len(controller.fetch_runs(DATE)), 101)
            self.assertIn('created=%3E%3D2026-09-10', api.call_args_list[0].args[0])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='controller-fixture-')
        self.root = Path(self.temp.name)
        self.patches = [patch.object(controller, 'OUTPUT_BASE', self.root),
                        patch.object(controller, 'today_jst', return_value=DATE)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_complete_sources_skip_api_and_recovery(self):
        with patch.object(controller, 'audit_remote', return_value=audit()) as check, \
             patch.object(controller, 'cloud_gate') as gate, patch.object(controller.recovery, 'recover') as recover:
            result = controller.run_controller(target_date=DATE, execute=True)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(check.call_count, 2)
            gate.assert_not_called(); recover.assert_not_called()

    def test_final_remote_loss_never_keeps_complete(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit(), audit([6])]), \
             patch.object(controller, 'cloud_gate') as gate, patch.object(controller.recovery, 'recover') as recover:
            result = controller.run_controller(target_date=DATE, execute=True)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertEqual(result['counts']['remaining_after'], 1)
            gate.assert_not_called(); recover.assert_not_called()

    def test_preflight_never_calls_recovery(self):
        with patch.object(controller, 'audit_remote', return_value=audit([6])), \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller.recovery, 'recover') as recover:
            result = controller.run_controller(target_date=DATE)
            self.assertEqual(result['status'], 'PREFLIGHT_PASS')
            self.assertEqual(result['counts']['remaining_after'], 1)
            recover.assert_not_called()

    def test_cloud_wait_and_stop_never_call_recovery(self):
        for status in ('WAIT', 'STOP'):
            with patch.object(controller, 'audit_remote', return_value=audit([6])), \
                 patch.object(controller, 'cloud_gate', return_value={'status':status,'reason':'fixture'}), \
                 patch.object(controller.recovery, 'recover') as recover:
                result = controller.run_controller(target_date=DATE, execute=True)
                self.assertEqual(result['status'], status)
                recover.assert_not_called()

    def test_one_stop_continues_next_independent_account_exactly_once(self):
        calls = []
        def recover(**kwargs):
            calls.append(kwargs)
            return {'status':'STOPPED' if kwargs['account']==6 else 'PUBLISHED','scrape_runs':1}
        with patch.object(controller, 'audit_remote', side_effect=[audit([6,12]), audit([6])]), \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller.recovery, 'recover', side_effect=recover):
            result = controller.run_controller(repo=self.root, target_date=DATE, execute=True)
        self.assertEqual([c['account'] for c in calls], [6,12])
        self.assertTrue(all(c['execute'] and c['cloud_run_completed'] and c['timeout']==1800 and c['target_date']==DATE for c in calls))
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['counts'], {'valid_before':18,'valid_after':19,'remaining_after':1,'helper_calls':2,'helper_stopped':1})
        self.assertTrue((Path(result['output_dir'])/'result.json').exists())

    def test_helper_exception_is_recorded_and_next_runs(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit([6,12]), audit([6])]), \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller.recovery, 'recover', side_effect=[RuntimeError('fixture'), {'status':'PUBLISHED'}]) as recover:
            result = controller.run_controller(target_date=DATE, execute=True)
            self.assertEqual(recover.call_count, 2)
            self.assertEqual(result['counts']['helper_stopped'], 1)

    def test_remote_became_valid_helper_reuse_counts_complete(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit([6]), audit()]), \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller.recovery, 'recover', return_value={'status':'UNCHANGED_VALIDATED','scrape_runs':0}) as recover:
            result = controller.run_controller(target_date=DATE, execute=True)
            recover.assert_called_once()
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(result['recovery_results'][0]['scrape_runs'], 0)

    def test_cloud_appearing_between_accounts_stops_new_recovery(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit([6,12]),audit([12])]), \
             patch.object(controller, 'cloud_gate', side_effect=[{'status':'READY'},{'status':'READY'},{'status':'WAIT','reason':'new_cloud'}]), \
             patch.object(controller.recovery, 'recover', return_value={'status':'PUBLISHED'}) as recover:
            result = controller.run_controller(target_date=DATE, execute=True)
            recover.assert_called_once()
            self.assertEqual(result['status'], 'WAIT')
            self.assertEqual(result['counts']['remaining_after'], 1)

    def test_budget_wait_does_not_start_next_account_and_reaudits_remote(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit([6,12]),audit([12])]) as check, \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller.time, 'monotonic', side_effect=[0,0,0,11]), \
             patch.object(controller.recovery, 'recover', return_value={'status':'PUBLISHED'}) as recover:
            result = controller.run_controller(target_date=DATE, execute=True,max_runtime_seconds=10)
            recover.assert_called_once(); self.assertEqual(check.call_count,2)
            self.assertEqual(result['status'], 'WAIT_BUDGET')

    def test_wrong_date_rejected_before_any_output(self):
        with self.assertRaises(controller.ControllerStop):
            controller.run_controller(target_date='2026-09-10',execute=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_date_rollover_stops_next_account_but_still_audits_fixed_day(self):
        with patch.object(controller, 'audit_remote', side_effect=[audit([6,12]), audit([12])]) as check, \
             patch.object(controller, 'cloud_gate', return_value={'status':'READY'}), \
             patch.object(controller, 'require_today', side_effect=[None,None,controller.ControllerStop('date_changed')]), \
             patch.object(controller.recovery, 'recover', return_value={'status':'PUBLISHED'}) as recover:
            result = controller.run_controller(target_date=DATE,execute=True)
            recover.assert_called_once()
            self.assertEqual(check.call_count, 2)
            self.assertEqual(check.call_args.args[1], DATE)
            self.assertEqual(result['status'],'STOP')
            self.assertEqual(result['reason'],'date_changed')

    def test_actual_os_lock_blocks_other_process_and_releases(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;'
                'from scripts import recover_missing_daily_sources as c;c.OUTPUT_BASE=Path(sys.argv[2]);'
                '\ntry:\n with c.controller_lock(): print("ACQUIRED")\n'
                'except c.recovery.RecoveryStop: print("BUSY")\n')
        def child():
            return subprocess.check_output([sys.executable,'-B','-c',code,str(ROOT),str(self.root)],timeout=15).decode().strip()
        with controller.controller_lock():
            self.assertEqual(child(), 'BUSY')
        self.assertEqual(child(), 'ACQUIRED')


class RemoteAuditTests(unittest.TestCase):
    def test_current_validator_runs_in_memory_without_ledger_copy(self):
        code = (ROOT/'ensure_daily_scrape.py').read_bytes()
        files = {}
        for n in range(1,21):
            prefix=f'data/account{n}/'
            products=[]
            for i in range(4):
                asin=f'B{i:09d}'
                category=('Nintendo Switch 2' if i<2 else 'PS5ゲームソフト') if n==20 else 'fixture'
                products.append(dict(asin=asin,title='Product',price='3000',price_int=3000,original_price='4000',
                    discount_rate='25%OFF',image_url='https://example.invalid/photo.png',
                    affiliate_url=f'https://www.amazon.co.jp/dp/{asin}?tag=noteamazon{n}-22',category=category+'#1',
                    rating='',review_count='',description='',specs=''))
            files[prefix+f'products_{DATE}.json']=json.dumps(products).encode()
            files[prefix+f'scrape_summary_{DATE}.json']=json.dumps(dict(date=DATE,total_taken=4,categories={},
                selection_policy=dict(selection_mode='category_quota' if n==20 else 'global_ranked',sort_order='sale_first',
                require_sale_info=True,max_per_category=5 if n==20 else 2,max_total_items=10))).encode()
            files[prefix+'asin_history.json']=b'{"schema":"note-amazon-asin-history-v1","posted":[]}'
        def git(repo,*args,**kwargs):
            if args[:2]==('remote','get-url'): return b'https://github.com/AGI-gyoumusuper/pinefield.git'
            if args[0]=='fetch': return b''
            if args[0]=='rev-parse': return b'a'*40
            if args[0]=='show':
                name=args[1].split(':',1)[1]
                return code if name=='ensure_daily_scrape.py' else files.get(name)
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as directory, patch.object(controller,'git',side_effect=git):
            result=controller.audit_remote(Path(directory),DATE)
            self.assertEqual(result['valid_count'],20)
            self.assertEqual(list(Path(directory).iterdir()),[])
            # Missing and invalid outputs remain shortages, never filled from another date.
            del files[f'data/account6/products_{DATE}.json']
            files[f'data/account12/products_{DATE}.json']=b'[]'
            result=controller.audit_remote(Path(directory),DATE)
            self.assertEqual(result['missing_accounts'],[6,12])
            self.assertEqual(result['rows'][11]['count'],0)
            self.assertEqual(list(Path(directory).iterdir()),[])


if __name__ == '__main__':
    unittest.main()
