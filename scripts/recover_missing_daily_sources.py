"""Audit today's 20 Git sources; explicitly --execute one local recovery per missing account.

The existing per-account helper owns scraping, attempt records, factory locks and
publishing. This controller only audits and sequences it after public Cloud run
evidence is complete. No credential or ASIN-ledger copies are written.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import types
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import recover_local_daily_source as recovery

OUTPUT_BASE = Path(r'D:\_0NOTEKATU\scraping')
API = 'https://api.github.com/repos/AGI-gyoumusuper/pinefield'
JST = ZoneInfo('Asia/Tokyo')
ACCOUNTS = tuple(range(1, 21))
REPAIR_WORKFLOWS = {'ensure-scrape.yml', 'late-repair-scrape.yml'}


class ControllerStop(RuntimeError):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def today_jst():
    return now_utc().astimezone(JST).date().isoformat()


def require_today(target_date):
    if date.fromisoformat(target_date).isoformat() != target_date or target_date != today_jst():
        raise ControllerStop('target_must_be_fixed_JST_today')


def preparation_window(target_date=None):
    """Evening targets tomorrow; after midnight target today. Stop before Ensure starts."""
    current = now_utc().astimezone(JST)
    day = current.date()
    expected = (day + timedelta(days=1)).isoformat() if current.hour >= 20 else day.isoformat()
    target_date = target_date or expected
    if target_date != expected:
        raise ControllerStop('nightly_target_outside_today_or_tomorrow_window')
    if current.hour >= 20:
        deadline = current.replace(hour=23, minute=25, second=0, microsecond=0)
        remaining = (deadline - current).total_seconds()
        if remaining < 120:
            return {'status': 'WAIT', 'date': target_date, 'reason': 'nightly_wait_for_midnight_repair_wave'}
        return {'status': 'READY', 'date': target_date, 'before_midnight': True,
                'deadline_utc': deadline.astimezone(timezone.utc), 'remaining_seconds': int(remaining)}
    return {'status': 'READY', 'date': target_date, 'before_midnight': False,
            'deadline_utc': None, 'remaining_seconds': None}


def sha(content):
    return hashlib.sha256(content).hexdigest().upper()


def save_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def controller_lock():
    # A stable OS-held byte lock works across Windows sessions and checkouts.
    # It is released by the OS on process death; an old file is never a stale owner.
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    with recovery.account_lock(OUTPUT_BASE / '.daily-recovery-controller.lock'):
        yield


def allocate_output(target_date):
    parent = OUTPUT_BASE / target_date / 'daily-recovery-controller'
    parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        candidate = parent / f'run-{number:02d}'
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise ControllerStop('too_many_controller_runs')


def git(repo, *arguments, missing_ok=False):
    environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never'}
    result = subprocess.run(['git', '-C', str(repo), *arguments], capture_output=True,
                            env=environment, timeout=180)
    if result.returncode:
        if missing_ok:
            return None
        # Do not retain arbitrary Git errors, URLs containing credentials, or blobs.
        raise ControllerStop('git_command_failed:' + arguments[0])
    return result.stdout


class BlobPath:
    """The existing validator's read-only Path interface, backed by Git blobs in RAM."""
    def __init__(self, files, relative=''):
        self.files, self.relative = files, relative

    def __truediv__(self, value):
        return BlobPath(self.files, '/'.join(filter(None, (self.relative, str(value)))))

    def __str__(self):
        return 'origin/main:' + self.relative

    def exists(self):
        return self.relative in self.files

    def read_bytes(self):
        if not self.exists():
            raise FileNotFoundError(str(self))
        return self.files[self.relative]

    def read_text(self, encoding='utf-8'):
        return self.read_bytes().decode(encoding)

    def open(self, mode='r', encoding=None):
        if mode not in ('r', 'rb'):
            raise ControllerStop('validator_requested_write')
        return io.BytesIO(self.read_bytes()) if mode == 'rb' else io.StringIO(self.read_text(encoding or 'utf-8'))


def audit_remote(repo, target_date):
    origin = git(repo, 'remote', 'get-url', 'origin').decode().strip()
    if not recovery.expected_origin(origin):
        raise ControllerStop('origin_is_not_canonical_Pinefield')
    git(repo, 'fetch', '--quiet', 'origin', 'main')
    commit = git(repo, 'rev-parse', 'origin/main').decode().strip()
    code = git(repo, 'show', f'{commit}:ensure_daily_scrape.py')
    # Execute the canonical validator definitions, not stale local checkout code.
    validator = types.ModuleType('_daily_controller_remote_validator')
    validator.__file__ = str(ROOT / 'ensure_daily_scrape.py')
    exec(compile(code.decode('utf-8-sig'), 'origin/main:ensure_daily_scrape.py', 'exec'), validator.__dict__)
    # Unified discounts use this same pinned evidence parser for every account.
    detail_code = git(repo, 'show', f'{commit}:detail_offer.py', missing_ok=True)
    rows = []
    for account in ACCOUNTS:
        prefix = f'data/account{account}/'
        names = [prefix + f'products_{target_date}.json', prefix + f'scrape_summary_{target_date}.json',
                 prefix + 'asin_history.json']
        if account == 20:
            names.append('categories20.yaml')
        files = {'detail_offer.py': detail_code} if detail_code is not None else {}
        for name in names:
            content = git(repo, 'show', f'{commit}:{name}', missing_ok=True)
            if content is not None:
                files[name] = content
        valid, reason = validator.validate(f'account{account}', BlobPath(files), target_date)
        raw_products = None
        try:
            raw_products = json.loads(files[names[0]])
        except (KeyError, ValueError):
            pass
        count = len(raw_products) if isinstance(raw_products, list) else None
        if valid and any(set(item) != recovery.PRODUCT_FIELDS for item in raw_products):
            valid, reason = False, 'products_must_keep_exact_13_fields'
        ready = recovery.postable_count(account, raw_products) if valid else 0
        rows.append({'account': account, 'date': target_date, 'valid': bool(valid), 'count': count,
                     'postable_count': ready, 'four_slots_ready': bool(valid and ready >= 4),
                     'reason': reason, 'products_sha256': sha(files[names[0]]) if names[0] in files else None,
                     'summary_sha256': sha(files[names[1]]) if names[1] in files else None,
                     'ledger_sha256': sha(files[names[2]]) if names[2] in files else None})
    return {'checked_at_utc': now_utc().isoformat(), 'origin_main_commit': commit,
            'validator_sha256': sha(code), 'valid_count': sum(row['valid'] for row in rows),
            'missing_accounts': [row['account'] for row in rows if not row['valid']],
            'short_accounts': [row['account'] for row in rows if row['valid'] and not row['four_slots_ready']],
            'ready_account_count': sum(row['four_slots_ready'] for row in rows),
            'postable_count': sum(row['postable_count'] for row in rows), 'rows': rows}


def api_json(url):
    # Deliberately public: never read tokens or credential helpers.
    request = Request(url, headers={'Accept': 'application/vnd.github+json',
                                   'User-Agent': 'pinefield-daily-recovery-controller'})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def fetch_runs(target_date):
    # Includes the previous evening's regular and Ensure wave. Never use a 5h
    # look-back that loses unfinished overnight work. All pages must be present.
    since = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
    runs, total = [], None
    for page in range(1, 11):
        query = urlencode({'branch': 'main', 'created': '>=' + since,
                           'per_page': 100, 'page': page, 'exclude_pull_requests': 'true'})
        body = api_json(API + '/actions/runs?' + query)
        if not isinstance(body, dict) or type(body.get('total_count')) is not int or not isinstance(body.get('workflow_runs'), list):
            raise ControllerStop('cloud_runs_invalid_response')
        total = max(total or 0, body['total_count'])
        rows = body['workflow_runs']
        if not all(isinstance(run, dict) and isinstance(run.get('id'), int) for run in rows):
            raise ControllerStop('cloud_runs_invalid_rows')
        runs.extend(rows)
        unique = {run['id']: run for run in runs}
        if len(unique) >= total:
            return list(unique.values())
        if len(rows) < 100:
            raise ControllerStop('cloud_runs_incomplete_pagination')
    raise ControllerStop('cloud_runs_pagination_limit')


def workflow_name(run):
    return str(run.get('path', '')).split('@', 1)[0].rsplit('/', 1)[-1]


def run_target(run, workflow):
    if run.get('event') == 'workflow_dispatch':
        # The HEAD commit describes a preceding push, not this manual run's inputs.
        return None, 'manual_target_unproven'
    message = str(run.get('display_title', ''))
    if workflow not in REPAIR_WORKFLOWS and run.get('event') == 'push':
        message += '\n' + str((run.get('head_commit') or {}).get('message', ''))
    explicit = set(re.findall(r'\[target:(\d{4}-\d{2}-\d{2})\]', message))
    if len(explicit) > 1:
        raise ControllerStop('cloud_run_ambiguous_target')
    if explicit and not (run.get('event') == 'schedule' and workflow in REPAIR_WORKFLOWS):
        return explicit.pop(), 'explicit_target_marker'
    if run.get('event') == 'schedule' and workflow in REPAIR_WORKFLOWS:
        created = datetime.fromisoformat(str(run['created_at']).replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ControllerStop('cloud_run_created_at_timezone_missing')
        if workflow == 'ensure-scrape.yml':
            # Canonical Ensure resolves scheduled target as UTC date + one day.
            return (created.astimezone(timezone.utc).date() + timedelta(days=1)).isoformat(), 'ensure_scheduled_UTC_next_day'
        return created.astimezone(JST).date().isoformat(), 'late_scheduled_JST_day'
    return None, 'unknown_target'


def cloud_gate(target_date, *, accounts=None, require_repairs=True, expected_trigger=None, runs=None):
    checked = now_utc().isoformat()
    try:
        runs = fetch_runs(target_date) if runs is None else runs
        latest, active, uncertain = {}, [], []
        evidence = []
        for run in runs:
            workflow = workflow_name(run)
            if not (re.fullmatch(r'scrape(?:[1-9]|1\d|20)?\.yml', workflow) or workflow in REPAIR_WORKFLOWS):
                continue
            if accounts is not None and workflow not in REPAIR_WORKFLOWS:
                marker_text = (str(run.get('display_title', '')) + '\n' + str((run.get('head_commit') or {}).get('message', ''))
                               if run.get('event') == 'push' else '')
                declared = set(int(n) for n in re.findall(r'\[scrape:account(\d+)\]', marker_text))
                # Explicitly unrelated regular work can continue. Unknown/manual work fails closed.
                if len(declared) == 1 and declared.isdisjoint(accounts):
                    continue
            target, basis = run_target(run, workflow)
            if target is not None and target != target_date:
                continue
            compact = {key: run.get(key) for key in ('id', 'path', 'event', 'status', 'conclusion',
                       'created_at', 'updated_at', 'head_sha', 'html_url', 'run_attempt')}
            compact.update(target_date=target, target_basis=basis)
            if run.get('status') != 'completed':
                active.append(compact)  # Unknown targets fail closed as well.
            elif not run.get('conclusion'):
                uncertain.append(compact)
            if target is None or run.get('conclusion') == 'skipped':
                continue
            key = workflow
            if workflow not in REPAIR_WORKFLOWS:
                message = str(run.get('display_title', ''))
                if run.get('event') == 'push':
                    message += '\n' + str((run.get('head_commit') or {}).get('message', ''))
                numbers = set(int(n) for n in re.findall(r'\[scrape:account(\d+)\]', message))
                if len(numbers) != 1:
                    continue
                account = numbers.pop()
                expected = 'scrape.yml' if account <= 5 else f'scrape{account}.yml'
                if account not in ACCOUNTS or workflow != expected:
                    continue
                key = f'account{account}'
            compact['completion_key'] = key
            evidence.append(compact)
            if key not in latest or run['id'] > latest[key]['id']:
                latest[key] = compact
        required = [f'account{n}' for n in (ACCOUNTS if accounts is None else accounts)]
        if require_repairs:
            required += sorted(REPAIR_WORKFLOWS)
        missing = [key for key in required if key not in latest]
        incomplete = [key for key, run in latest.items() if run['status'] != 'completed' or not run['conclusion']]
        if expected_trigger is not None:
            account, commit = expected_trigger
            if latest.get(f'account{account}', {}).get('head_sha') != commit:
                incomplete.append(f'account{account}:latest_trigger_completion_unproven')
        status = 'READY' if not (active or uncertain or missing or incomplete) else 'WAIT'
        return {'status': status, 'checked_at_utc': checked, 'date': target_date,
                'reason': 'all_relevant_cloud_runs_completed' if status == 'READY' else 'cloud_unfinished_or_unproven',
                'missing_completion_keys': missing, 'incomplete_keys': incomplete,
                'active_runs': active, 'uncertain_runs': uncertain, 'latest_completed_candidates': latest,
                'related_run_count': len(evidence)}
    except Exception as exc:
        # API failure is never evidence of no active runs. No credentials/error body saved.
        return {'status': 'STOP', 'checked_at_utc': checked, 'date': target_date,
                'reason': str(exc) if isinstance(exc, ControllerStop) else 'cloud_api_or_metadata_unavailable:' + type(exc).__name__}


def nightly_cloud_gate(repo, target_date, account, *, runs=None):
    try:
        window = preparation_window(target_date)
        if window['status'] != 'READY':
            return {key: value for key, value in window.items() if key != 'deadline_utc'}
        commit = git(repo, 'rev-parse', 'origin/main').decode().strip()
        path = f'.github/triggers/account{account}.json'
        raw = git(repo, 'show', f'{commit}:{path}', missing_ok=True)
        trigger = json.loads(raw) if raw else None
        if not isinstance(trigger, dict) or trigger.get('account') != f'account{account}' or trigger.get('target_date') != target_date:
            return {'status': 'WAIT', 'reason': 'latest_trigger_target_unproven', 'account': account, 'date': target_date}
        trigger_commit = git(repo, 'log', '-1', '--format=%H', commit, '--', path).decode().strip()
        if not re.fullmatch(r'[0-9a-f]{40}', trigger_commit):
            raise ControllerStop('latest_trigger_commit_unproven')
        return cloud_gate(target_date,
                          accounts=(account,) if window['before_midnight'] else None,
                          require_repairs=not window['before_midnight'],
                          expected_trigger=(account, trigger_commit), runs=runs)
    except Exception as exc:
        return {'status': 'STOP', 'reason': str(exc) if isinstance(exc, ControllerStop)
                else 'nightly_cloud_or_trigger_unproven:' + type(exc).__name__}


def run_nightly_controller(*, repo=ROOT, target_date=None, execute=False, max_runtime_seconds=10800):
    if not 1 <= max_runtime_seconds <= 10800:
        raise ControllerStop('max_runtime_seconds_must_be_1_to_10800')
    window = preparation_window(target_date)
    target_date = window['date']
    output = allocate_output(target_date)
    started = time.monotonic()
    result = {'schema': 'pinefield-nightly-source-completion-v1', 'date': target_date, 'execute': execute,
              'started_at_utc': now_utc().isoformat(), 'output_dir': str(output), 'status': 'STARTING',
              'max_runtime_seconds': max_runtime_seconds, 'recovery_results': [], 'cloud_checks': []}
    try:
        with controller_lock():
            initial = audit_remote(Path(repo), target_date)
            result['initial_audit'] = initial
            if window['status'] != 'READY':
                result.update(status='WAIT', reason=window['reason'])
            else:
                targets = [row['account'] for row in initial['rows'] if not row['four_slots_ready']]
                # Read all pages once for candidate gates. A fresh public gate is still required at every push.
                snapshot = None
                snapshot_at = None
                for account in targets:
                    try:
                        recovery.check_source(recovery.factory_root(account), account, target_date)
                    except recovery.RecoveryStop as exc:
                        result['recovery_results'].append({'account': account, 'status': 'PRESERVED', 'reason': str(exc)})
                        continue
                    window = preparation_window(target_date)
                    budget_left = max_runtime_seconds - int(time.monotonic() - started)
                    if window['status'] != 'READY' or budget_left < 630:
                        result.update(status='WAIT_BUDGET', reason=window.get('reason', 'controller_runtime_budget_exhausted'))
                        break
                    # Reuse only a brief snapshot; a lengthy prior scrape must not hide a new Cloud rerun.
                    if snapshot is None or time.monotonic() - snapshot_at >= 60:
                        snapshot = fetch_runs(target_date)
                        snapshot_at = time.monotonic()
                    gate = nightly_cloud_gate(Path(repo), target_date, account, runs=snapshot)
                    result['cloud_checks'].append({'account': account, **gate})
                    if gate['status'] != 'READY':
                        result['recovery_results'].append({'account': account, 'status': gate['status'], 'reason': gate['reason']})
                        continue
                    if not execute:
                        result['recovery_results'].append({'account': account, 'status': 'PREFLIGHT_PASS'})
                        continue
                    timeout = min(1800, budget_left - 30)
                    recovered = recovery.recover(account=account, target_date=target_date, repo=Path(repo),
                        execute=True, cloud_run_completed=True, timeout=timeout, nightly=True,
                        deadline_utc=window['deadline_utc'],
                        before_publish=lambda n=account: nightly_cloud_gate(Path(repo), target_date, n))
                    keys = ('status', 'reason', 'output_dir', 'product_count', 'postable_before', 'postable_after',
                            'supplement_added_count', 'shortfall_reason', 'scrape_runs', 'remote_commit',
                            'published_commit', 'artifact_source', 'reused_pending_candidate')
                    result['recovery_results'].append({'account': account, **{key: recovered[key] for key in keys if key in recovered}})
                    save_json(output / 'result.json', result)
            final = audit_remote(Path(repo), target_date)
            result['final_audit'] = final
            if final['ready_account_count'] == 20:
                result['status'] = 'COMPLETE'
            elif result['status'] == 'STARTING':
                result['status'] = 'INCOMPLETE' if execute else 'PREFLIGHT_PASS'
    except Exception as exc:
        result.update(status='WAIT', reason=str(exc) if isinstance(exc, (ControllerStop, recovery.RecoveryStop))
                      else 'nightly_preparation_unproven:' + type(exc).__name__)
    finally:
        result['finished_at_utc'] = now_utc().isoformat()
        final = result.get('final_audit', {})
        result['counts'] = {'valid_after': final.get('valid_count'), 'ready_accounts_after': final.get('ready_account_count'),
                            'postable_after': final.get('postable_count'),
                            'short_accounts_after': final.get('short_accounts'),
                            'helper_calls': sum('scrape_runs' in row for row in result['recovery_results'])}
        save_json(output / 'result.json', result)
    return result


def run_controller(*, repo=ROOT, target_date=None, execute=False, max_runtime_seconds=10800, nightly_preparation=False):
    if nightly_preparation:
        return run_nightly_controller(repo=repo, target_date=target_date, execute=execute,
                                      max_runtime_seconds=max_runtime_seconds)
    if not 1 <= max_runtime_seconds <= 10800:
        raise ControllerStop('max_runtime_seconds_must_be_1_to_10800')
    started = time.monotonic()
    target_date = target_date or today_jst()
    require_today(target_date)
    output = allocate_output(target_date)
    result = {'schema': 'pinefield-daily-recovery-controller-v1', 'date': target_date, 'execute': execute,
              'started_at_utc': now_utc().isoformat(), 'output_dir': str(output),
              'max_runtime_seconds': max_runtime_seconds, 'per_account_scrape_timeout_seconds': 1800,
              'status': 'STARTING', 'cloud_checks': [], 'recovery_results': []}
    try:
        with controller_lock():
            initial = audit_remote(Path(repo), target_date)
            result['initial_audit'] = initial
            missing = initial['missing_accounts']
            if not missing:
                result['status'] = 'COMPLETE'
            else:
                gate = cloud_gate(target_date)
                result['cloud_checks'].append(gate)
                if gate['status'] != 'READY':
                    result.update(status=gate['status'], reason=gate['reason'])
                elif not execute:
                    result['status'] = 'PREFLIGHT_PASS'
                else:
                    for account in missing:
                        try:
                            require_today(target_date)
                        except ControllerStop as exc:
                            result.update(status='STOP', reason=str(exc))
                            break
                        if time.monotonic() - started >= max_runtime_seconds:
                            result.update(status='WAIT_BUDGET', reason='controller_runtime_budget_exhausted')
                            break
                        # A newly dispatched Cloud run during an earlier scrape must stop
                        # subsequent local work. The helper also re-fetches before accepting.
                        gate = cloud_gate(target_date)
                        result['cloud_checks'].append(gate)
                        if gate['status'] != 'READY':
                            result.update(status=gate['status'], reason=gate['reason'])
                            break
                        if time.monotonic() - started >= max_runtime_seconds:
                            result.update(status='WAIT_BUDGET', reason='controller_runtime_budget_exhausted')
                            break
                        save_json(output / 'result.json', result)
                        try:
                            recovered = recovery.recover(account=account, target_date=target_date,
                                repo=Path(repo), execute=True, cloud_run_completed=True, timeout=1800)
                        except Exception as exc:
                            recovered = {'status': 'STOPPED', 'reason': 'recovery_exception:' + type(exc).__name__}
                        # Keep the helper's result path/hash and small outcome only. It already
                        # preserves candidates, original source and the one-attempt record.
                        row = {key: recovered.get(key) for key in ('status', 'reason', 'output_dir', 'product_count',
                               'scrape_runs', 'remote_commit', 'published_commit', 'artifact_source')}
                        row['account'] = account
                        result['recovery_results'].append(row)
                        save_json(output / 'result.json', result)
                    if result['status'] == 'STARTING':
                        result['status'] = 'FINISHED'
            # Always validate the latest remote 20 again; helper exit 0 is not completion.
            result['final_audit'] = audit_remote(Path(repo), target_date)
            if result['final_audit']['valid_count'] == 20:
                result['status'] = 'COMPLETE'
                result.pop('reason', None)
            elif result['status'] in ('FINISHED', 'COMPLETE'):
                result['status'] = 'INCOMPLETE'
    except recovery.RecoveryStop as exc:
        result.update(status='WAIT' if 'lock_busy' in str(exc) else 'STOP', reason=str(exc))
    except Exception as exc:
        result.update(status='STOP', reason=str(exc) if isinstance(exc, ControllerStop) else type(exc).__name__)
    finally:
        final = result.get('final_audit', {})
        result['counts'] = {'valid_before': result.get('initial_audit', {}).get('valid_count'),
                            'valid_after': final.get('valid_count'),
                            'remaining_after': len(final['missing_accounts']) if 'missing_accounts' in final else None,
                            'helper_calls': len(result['recovery_results']),
                            'helper_stopped': sum(row['status'] == 'STOPPED' for row in result['recovery_results'])}
        result['finished_at_utc'] = now_utc().isoformat()
        save_json(output / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--date')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--max-runtime-seconds', type=int, default=10800)
    parser.add_argument('--nightly-preparation', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run_controller(repo=args.repo, target_date=args.date, execute=args.execute,
                                max_runtime_seconds=args.max_runtime_seconds, nightly_preparation=args.nightly_preparation)
    except (ControllerStop, ValueError, OSError) as exc:
        result = {'status': 'STOP', 'reason': str(exc) if isinstance(exc, ControllerStop) else type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('COMPLETE', 'PREFLIGHT_PASS') else 2 if result['status'] in ('WAIT', 'WAIT_BUDGET') else 1


if __name__ == '__main__':
    raise SystemExit(main())
