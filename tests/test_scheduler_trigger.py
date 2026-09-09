"""Windows-only integration tests; all Git remotes are temporary local directories."""
import ctypes
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'Trigger-GitHubScrape.ps1'
POWERSHELL = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')


@unittest.skipUnless(os.name == 'nt', 'Windows Task Scheduler trigger uses Windows PowerShell')
class SchedulerTriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinefield-trigger-test-')
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.client = self.root / 'client'
        self.git(self.root, 'init', '--bare', '--quiet', str(self.remote))
        self.git(self.root, 'clone', '--quiet', str(self.remote), str(self.seed))
        self.author(self.seed)
        (self.seed / 'tracked.txt').write_text('original\n', encoding='utf-8')
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '--quiet', '-m', 'initial')
        self.git(self.seed, 'branch', '-M', 'main')
        self.git(self.seed, 'push', '--quiet', '-u', 'origin', 'main')
        self.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git(self.root, 'clone', '--quiet', str(self.remote), str(self.client))
        self.author(self.client)
        self.git(self.client, 'switch', '--quiet', '-c', 'feature/keep-local')
        (self.client / 'feature-only.txt').write_text('never publish\n', encoding='utf-8')
        self.git(self.client, 'add', 'feature-only.txt')
        self.git(self.client, 'commit', '--quiet', '-m', 'local feature commit')
        (self.client / 'tracked.txt').write_text('unstaged local edit\n', encoding='utf-8')
        (self.client / 'staged.txt').write_text('staged local data\n', encoding='utf-8')
        self.git(self.client, 'add', 'staged.txt')
        (self.client / 'staged.txt').write_text('additional unstaged data\n', encoding='utf-8')
        self.before = self.snapshot()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, directory, *arguments, check=True):
        result = subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if check and result.returncode:
            self.fail(result.stderr.decode('utf-8', errors='replace'))
        return result.stdout.decode('utf-8', errors='replace').strip()

    def author(self, directory):
        self.git(directory, 'config', 'user.name', 'Scheduler Fixture')
        self.git(directory, 'config', 'user.email', 'scheduler@example.invalid')

    def snapshot(self):
        return {
            'head': self.git(self.client, 'rev-parse', 'HEAD'),
            'branch': self.git(self.client, 'symbolic-ref', 'HEAD'),
            'index': (self.client / '.git/index').read_bytes(),
            'tracked': (self.client / 'tracked.txt').read_bytes(),
            'staged': (self.client / 'staged.txt').read_bytes(),
            'feature': (self.client / 'feature-only.txt').read_bytes(),
            'stash': self.git(self.client, 'stash', 'list'),
        }

    def invoke(self, *arguments, success=True, repo=None):
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(SCRIPT), '-RepoDir', str(repo or self.client), '-RetryDelaySeconds', '0',
                                 *arguments], capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        text = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        if success:
            self.assertEqual(result.returncode, 0, text)
        else:
            self.assertNotEqual(result.returncode, 0, text)
        self.assertEqual(self.snapshot(), self.before, 'original checkout or index changed')
        listed = self.git(self.client, 'worktree', 'list', '--porcelain')
        self.assertEqual(sum(line.startswith('worktree ') for line in listed.splitlines()), 1, listed)
        return text

    def remote_head(self):
        return self.git(self.remote, 'rev-parse', 'refs/heads/main')

    def remote_payload(self, account):
        return json.loads(self.git(self.remote, 'show', f'main:.github/triggers/account{account}.json'))

    def test_remote_base_trigger_only_and_dirty_checkout_preserved(self):
        (self.seed / 'remote-new.txt').write_text('new upstream data\n', encoding='utf-8')
        self.git(self.seed, 'add', 'remote-new.txt')
        self.git(self.seed, 'commit', '--quiet', '-m', 'upstream advanced')
        self.git(self.seed, 'push', '--quiet', 'origin', 'main')
        latest = self.remote_head()
        self.invoke('-Account', '1', '-TargetDate', '2026-09-10')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main^'), latest)
        self.assertEqual(self.git(self.remote, 'diff', '--name-only', latest, 'main'), '.github/triggers/account1.json')
        payload = self.remote_payload(1)
        self.assertEqual(payload['account'], 'account1')
        self.assertEqual(payload['target_date'], '2026-09-10')
        self.assertEqual(payload['source'], 'windows-task-scheduler')
        self.assertNotIn('feature-only.txt', self.git(self.remote, 'ls-tree', '-r', '--name-only', 'main'))

    def test_prepare_next_day_and_account20(self):
        earliest = (datetime.now() + timedelta(days=1)).date().isoformat()
        self.invoke('-Account', '20', '-PrepareNextDay')
        latest = (datetime.now() + timedelta(days=1)).date().isoformat()
        self.assertIn(self.remote_payload(20)['target_date'], {earliest, latest})
        self.assertEqual(self.remote_payload(20)['account'], 'account20')

    def test_dry_run_does_not_require_git_repository(self):
        no_repo = self.root / 'not-a-repository'
        old_head = self.remote_head()
        output = self.invoke('-Account', '3', '-PrepareNextDay', '-DryRun', repo=no_repo)
        self.assertIn('DryRun completed; no commit or push performed.', output)
        self.assertEqual(self.remote_head(), old_head)
        self.assertFalse((no_repo / '.git').exists())

    def test_existing_scheduler_arguments_resolve_default_repository(self):
        script_directory = self.client / 'scripts'
        script_directory.mkdir()
        copied_script = script_directory / SCRIPT.name
        shutil.copyfile(SCRIPT, copied_script)
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(copied_script), '-Account', '1', '-PrepareNextDay', '-DryRun'],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        logs = list((self.client / 'work/scheduler-logs').glob('trigger-account1-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('DryRun completed; no commit or push performed.', logs[0].read_text(encoding='utf-8-sig'))
        self.assertEqual(self.snapshot(), self.before)

    def hooks(self):
        directory = self.root / 'hooks'
        directory.mkdir(exist_ok=True)
        self.git(self.client, 'config', 'core.hooksPath', directory.as_posix())
        return directory

    def test_concurrent_remote_push_retries_from_new_remote_head(self):
        seed = shlex.quote(self.seed.as_posix())
        marker = shlex.quote((self.root / 'race-once').as_posix())
        hook = ('#!/bin/sh\n'
                'unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n'
                f'if [ ! -e {marker} ]; then\n'
                f'  touch {marker}\n'
                f'  printf concurrent > {seed}/concurrent.txt\n'
                f'  git -C {seed} add concurrent.txt || exit 1\n'
                f'  git -C {seed} commit --quiet -m concurrent || exit 1\n'
                f'  git -C {seed} push --quiet origin main || exit 1\n'
                'fi\n')
        (self.hooks() / 'pre-push').write_text(hook, encoding='utf-8', newline='\n')
        output = self.invoke('-Account', '4', '-TargetDate', '2026-09-10')
        self.assertIn('Attempt 1 failed:', output)
        self.assertIn('Isolated trigger attempt 2/5', output)
        self.assertEqual(self.git(self.remote, 'show', 'main:concurrent.txt'), 'concurrent')
        self.assertEqual(self.remote_payload(4)['account'], 'account4')

    def test_rejected_push_fails_after_five_attempts_without_checkout_changes(self):
        (self.remote / 'hooks/pre-receive').write_text('#!/bin/sh\nexit 1\n', encoding='utf-8', newline='\n')
        old_head = self.remote_head()
        output = self.invoke('-Account', '5', '-TargetDate', '2026-09-10', success=False)
        self.assertIn('Attempt 5 failed:', output)
        self.assertEqual(self.remote_head(), old_head)

    def test_hook_cannot_smuggle_another_file_into_trigger_commit(self):
        (self.hooks() / 'pre-commit').write_text('#!/bin/sh\nprintf unwanted > unwanted.txt\ngit add unwanted.txt\n',
                                               encoding='utf-8', newline='\n')
        old_head = self.remote_head()
        output = self.invoke('-Account', '6', '-TargetDate', '2026-09-10', success=False)
        self.assertIn('Commit contains a file other than the requested account trigger.', output)
        self.assertEqual(self.remote_head(), old_head)

    def test_invalid_or_conflicting_date_stops_before_git(self):
        old_head = self.remote_head()
        self.invoke('-Account', '7', '-TargetDate', '2026-02-30', success=False)
        self.invoke('-Account', '7', '-TargetDate', '2026-09-10', '-PrepareNextDay', success=False)
        self.assertEqual(self.remote_head(), old_head)

    def test_mutex_wait_is_bounded_and_does_not_touch_git(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, True, 'Local\\CodexPinefieldGitTrigger')
        self.assertTrue(handle)
        try:
            self.assertNotEqual(ctypes.get_last_error(), 183, 'another trigger owns the test mutex')
            old_head = self.remote_head()
            started = time.monotonic()
            output = self.invoke('-Account', '8', '-TargetDate', '2026-09-10', '-MutexWaitSeconds', '0', success=False)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn('Pinefield Git lock timed out', output)
            self.assertEqual(self.remote_head(), old_head)
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
