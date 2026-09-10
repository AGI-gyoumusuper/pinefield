"""Local candidates and workflow structure only; no Amazon/GitHub execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from test_ensure_daily_scrape import daily, make_product, write_valid_output, TEST_DATE


class FailureCandidateTests(unittest.TestCase):
    def test_failed_attempts_are_preserved_before_originals_are_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            artifacts = Path(temporary) / 'artifacts'
            write_valid_output(root, 'account6', 3)
            paths = daily.account_artifact_paths('account6', root, TEST_DATE)
            original = {path: path.read_bytes() for path in paths}
            calls = []
            def scrape(account, worktree, date):
                calls.append(account)
                write_valid_output(worktree, account, len(calls))
            with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: str(artifacts), daily.SEARCH_DIAGNOSTICS_ENV: ''}), \
                 patch.object(daily, 'scrape', side_effect=scrape), patch.object(daily.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'failed to create valid output'):
                    daily.ensure('account6', root, TEST_DATE)
            self.assertEqual(len(calls), 3)
            for attempt in (1, 2, 3):
                saved = artifacts / 'account6' / TEST_DATE / f'attempt-{attempt:02d}'
                candidate = saved / f'products_{TEST_DATE}.json'
                report = json.loads((saved / 'validation.json').read_text())
                self.assertEqual(len(json.loads(candidate.read_text())), attempt)
                self.assertIn(f'{attempt} < 4', report['reason'])
                self.assertFalse(report['validation_valid'])
                self.assertTrue(report['diagnostic_only'])
                self.assertEqual(report['files'][candidate.name]['sha256'], hashlib.sha256(candidate.read_bytes()).hexdigest())
                self.assertEqual({p.name for p in saved.iterdir()}, {candidate.name, f'scrape_summary_{TEST_DATE}.json', 'validation.json'})
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertFalse(list(artifacts.rglob('asin_history.json')))

    def test_repair_success_preserves_failed_candidate_and_does_not_add_a_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, artifacts = Path(temporary) / 'repo', Path(temporary) / 'artifacts'
            calls = []
            def scrape(account, worktree, date):
                calls.append(account)
                write_valid_output(worktree, account, 1 if len(calls) == 1 else 4)
            with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: str(artifacts), daily.SEARCH_DIAGNOSTICS_ENV: ''}), \
                 patch.object(daily, 'scrape', side_effect=scrape), patch.object(daily.time, 'sleep'):
                self.assertTrue(daily.ensure('account10', root, TEST_DATE))
            self.assertEqual(len(calls), 2)
            self.assertTrue(daily.validate('account10', root, TEST_DATE)[0])
            self.assertEqual([p.name for p in (artifacts / 'account10' / TEST_DATE).iterdir()], ['attempt-01'])

    def test_archive_cli_is_read_only_and_copies_only_two_candidate_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, artifacts = Path(temporary) / 'repo', Path(temporary) / 'artifacts'
            write_valid_output(root, 'account12', 1)
            account_root = root / 'data/account12'
            (account_root / 'cookies.json').write_text('PRIVATE_COOKIE')
            history = account_root / 'asin_history.json'
            history.write_text('PRIVATE_LEDGER')
            before = {p: p.read_bytes() for p in account_root.iterdir()}
            with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: str(artifacts), daily.SEARCH_DIAGNOSTICS_ENV: ''}), \
                 patch.object(daily, 'scrape') as scrape:
                code = daily.main(['--archive-failure', '--account', 'account12', '--date', TEST_DATE, '--root', str(root)])
            self.assertEqual(code, 0)
            scrape.assert_not_called()
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            for saved in artifacts.rglob('*'):
                if saved.is_file():
                    self.assertNotIn(b'PRIVATE_', saved.read_bytes())
            report = json.loads((artifacts / 'account12' / TEST_DATE / 'final/validation.json').read_text())
            self.assertIn('1 < 4', report['reason'])

    def test_existing_valid_source_produces_no_failure_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, artifacts = Path(temporary) / 'repo', Path(temporary) / 'artifacts'
            write_valid_output(root, 'account1', 4)
            with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: str(artifacts)}), patch.object(daily, 'scrape') as scrape:
                self.assertFalse(daily.ensure('account1', root, TEST_DATE))
            scrape.assert_not_called()
            self.assertFalse(artifacts.exists())

    def test_public_capture_context_keeps_first_account_and_never_resets_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {daily.SEARCH_DIAGNOSTICS_ENV: temporary}):
                (root / 'failure-01').mkdir()
                daily.annotate_search_diagnostics('account6', TEST_DATE, 1)
                (root / 'failure-02').mkdir()
                daily.annotate_search_diagnostics('account10', TEST_DATE, 2)
                daily.annotate_search_diagnostics('account12', TEST_DATE, 3)
            contexts = [json.loads((root / f'failure-{n:02d}/context.json').read_text()) for n in (1, 2)]
            self.assertEqual(contexts, [{'account': 'account6', 'date': TEST_DATE, 'attempt': 1},
                                        {'account': 'account10', 'date': TEST_DATE, 'attempt': 2}])
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_archive_disabled_or_storage_failure_does_not_escape(self):
        with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: ''}):
            self.assertFalse(daily.archive_failure_candidate('account1', Path('.'), TEST_DATE, 1, valid=False, reason='missing'))
        with patch.dict(os.environ, {daily.FAILURE_ARTIFACTS_ENV: 'synthetic-output'}), \
             patch.object(Path, 'mkdir', side_effect=OSError('synthetic storage failure')):
            self.assertFalse(daily.archive_failure_candidate('account1', Path('.'), TEST_DATE, 1, valid=False, reason='missing'))


class FailureArtifactWorkflowTests(unittest.TestCase):
    def test_all_twenty_main_jobs_and_both_repairs_use_bounded_isolated_uploads(self):
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        selected = []
        for name in ['scrape.yml'] + [f'scrape{n}.yml' for n in range(6, 21)]:
            jobs = yaml.safe_load((workflows / name).read_text(encoding='utf-8'))['jobs']
            for job_id, job in jobs.items():
                if job_id.startswith('scrape_account') and job_id != 'scrape_account0':
                    selected.append((job_id, job))
        self.assertEqual(len(selected), 20)
        for name, job_id in [('ensure-scrape.yml', 'ensure'), ('late-repair-scrape.yml', 'repair')]:
            selected.append((job_id, yaml.safe_load((workflows / name).read_text(encoding='utf-8'))['jobs'][job_id]))
        for job_id, job in selected:
            with self.subTest(job=job_id):
                self.assertNotIn(daily.FAILURE_ARTIFACTS_ENV, job.get('env', {}))
                config = job['steps'][0]
                self.assertEqual(config['name'], 'Configure failure diagnostics')
                self.assertTrue(config['continue-on-error'])
                self.assertIn('PINEFIELD_FAILURE_ARTIFACTS_DIR=$RUNNER_TEMP/pinefield-failures', config['run'])
                self.assertIn('PINEFIELD_SEARCH_DIAGNOSTICS_DIR=$RUNNER_TEMP/pinefield-failures/public-search', config['run'])
                self.assertEqual(config['run'].count('>> "$GITHUB_ENV"'), 2)
                collector = next(s for s in job['steps'] if s.get('name') == 'Preserve failure candidates')
                self.assertIn('failure()', collector['if'])
                self.assertNotIn('$PINEFIELD', collector['if'])
                self.assertTrue(collector['continue-on-error'])
                self.assertIn('--archive-failure', collector['run'])
                upload = next(s for s in job['steps'] if s.get('name') == 'Upload short-lived scrape diagnostics')
                self.assertEqual(upload['if'], '${{ !cancelled() }}')
                self.assertTrue(upload['continue-on-error'])
                self.assertEqual(upload['uses'], 'actions/upload-artifact@v4')
                self.assertEqual(upload['with']['retention-days'], 3)
                self.assertEqual(upload['with']['if-no-files-found'], 'ignore')
                self.assertEqual(upload['with']['path'], '${{ runner.temp }}/pinefield-failures/')
                self.assertFalse(any('asin_history' in str(s.get('with', {}).get('path', '')) for s in job['steps']))
                for step in job['steps']:
                    self.assertNotIn(daily.SEARCH_DIAGNOSTICS_ENV, step.get('env', {}))
                self.assertEqual(sum(s.get('uses') == 'actions/upload-artifact@v4' for s in job['steps']), 1)


if __name__ == '__main__':
    unittest.main()
