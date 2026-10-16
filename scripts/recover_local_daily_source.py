"""One explicit same-day local recovery using canonical Pinefield code in a fresh worktree.

Default: preflight only. --execute requires --cloud-run-completed; the operator
must first ensure this account's Cloud run and other manual scrapes are finished.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import types
import uuid
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sale_campaign import apply_campaign_config, source_campaign_config
from detail_offer import observation_sha256, prime_event_phase, prime_event_proof, validate_offer
FACTORY_BASE = Path(r'D:\_0NOTEKATU')
OUTPUT_BASE = FACTORY_BASE / 'scraping'
PRODUCT_FIELDS = {'asin', 'title', 'price', 'price_int', 'original_price', 'discount_rate', 'image_url',
                  'affiliate_url', 'category', 'rating', 'review_count', 'description', 'specs'}


class RecoveryStop(RuntimeError):
    pass


def today_jst():
    return datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(content):
    return hashlib.sha256(content).hexdigest().upper()


def write_json(path, value, *, exclusive=False):
    with Path(path).open('x' if exclusive else 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def expected_origin(value):
    return value.rstrip('/').lower() in {
        'https://github.com/agi-gyoumusuper/pinefield.git',
        'https://github.com/agi-gyoumusuper/pinefield',
        'git@github.com:agi-gyoumusuper/pinefield.git',
    }


class Commands:
    def __init__(self, log):
        self.log = log

    def run(self, arguments, *, cwd, check=True, env=None, timeout=180):
        environment = os.environ.copy()
        environment.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never',
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        environment.update(env or {})
        result = subprocess.run(arguments, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        # Never log git-show contents (in particular ledgers), credentials, or raw Git errors.
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': utc_now(), 'command': arguments, 'exit': result.returncode}) + '\n')
        if check and result.returncode:
            raise RecoveryStop('command_failed:' + Path(arguments[0]).name + ':' + str(result.returncode))
        return result

    def git(self, repo, *args, check=True):
        return self.run(['git', '-C', str(repo), *args], cwd=repo, check=check)

    def text(self, repo, *args):
        return self.git(repo, *args).stdout.decode('utf-8').strip()


def validate_date(account, target_date, *, allow_next_day=False):
    if type(account) is not int or not 1 <= account <= 20:
        raise RecoveryStop('account_must_be_1_to_20')
    try:
        valid = date.fromisoformat(target_date).isoformat() == target_date
    except (TypeError, ValueError):
        valid = False
    allowed = {today_jst()}
    if allow_next_day:
        allowed.add((date.fromisoformat(today_jst()) + timedelta(days=1)).isoformat())
    if not valid or target_date not in allowed:
        raise RecoveryStop('target_must_be_JST_today')


def prime_phase_wait_reason(target_date, *, current_date=None):
    """Do not spend a scrape on a future event phase that cannot yet be proved."""
    target_phase = prime_event_phase(target_date)
    if target_phase is not None and prime_event_phase(current_date or today_jst()) != target_phase:
        return 'prime_event_wait_for_target_phase'
    return None


def phase_transition_retry_evidence(attempt, *, account, target_date, current_date=None,
                                    remote_products=None):
    """Recognize only the archived zero-output failure at the early/main boundary.

    This is evidence checking, not a retry counter reset. The caller exclusively
    records a separate transition attempt while retaining this original attempt.
    None means that the incident is unknown or does not qualify.
    """
    try:
        current_date = current_date or today_jst()
        previous_date = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
        if (type(account) is not int or not 1 <= account <= 20 or target_date != current_date
                or prime_event_phase(target_date) != 'main' or prime_event_phase(previous_date) != 'early'
                or not isinstance(remote_products, list) or remote_products):
            return None
        attempt = Path(attempt)
        if attempt.name != f'account{account}_{target_date}.attempt.json' or attempt.parent.name != '.control':
            return None
        saved = json.loads(attempt.read_text(encoding='utf-8-sig'))
        folder = Path(saved['output_dir']).resolve()
        if (folder.parent != attempt.parent.parent.resolve() or not re.fullmatch(r'run-\d+', folder.name)
                or saved.get('account') != account or saved.get('date') != target_date):
            return None
        result_path = folder / 'result.json'
        old = json.loads(result_path.read_text(encoding='utf-8-sig'))
        if (old.get('schema') != 'pinefield-local-recovery-v1' or old.get('account') != f'account{account}'
                or old.get('date') != target_date or old.get('execute') is not True
                or old.get('status') != 'STOPPED' or old.get('reason') != 'canonical_output_validation_failed'
                or old.get('scrape_runs') != 1 or old.get('push_attempts') != 0
                or Path(old['output_dir']).resolve() != folder
                or old.get('source_origin_commit') != saved.get('source_origin_commit')
                or not re.fullmatch(r'[0-9a-f]{40}', str(saved.get('source_origin_commit', '')))):
            return None
        for field in ('started_at_utc', 'finished_at_utc'):
            stamp = datetime.fromisoformat(old[field])
            if stamp.tzinfo is None or prime_event_phase(stamp.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()) != 'early':
                return None
        started = datetime.fromisoformat(saved['started_at_utc'])
        if started.tzinfo is None or prime_event_phase(started.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()) != 'early':
            return None
        products_name = f'products_{target_date}.json'
        summary_name = f'scrape_summary_{target_date}.json'
        files = {name: (folder / name).read_bytes() for name in (products_name, summary_name)}
        if (old.get('candidate_files_sha256') != {name: digest(content) for name, content in files.items()}
                or json.loads(files[products_name].decode('utf-8-sig')) != []):
            return None
        summary = json.loads(files[summary_name].decode('utf-8-sig'))
        expected = apply_campaign_config({}, f'categories{account}.yaml', target_date).get('_sale_campaign')
        if (summary.get('date') != target_date or summary.get('total_taken') != 0
                or not expected or summary.get('campaign') != expected):
            return None
        verification = summary['detail_offer_verification']
        observations = verification['observations']
        if (not isinstance(observations, list) or not observations
                or verification.get('candidate_count') != len(observations)
                or verification.get('accepted_count') != 0
                or verification.get('rejected_count') != len(observations)
                or verification.get('final_selected_count') != 0):
            return None
        reasons = {}
        mismatch_count = 0
        for record in observations:
            reason = record.get('reason')
            if record.get('status') != 'rejected' or not isinstance(reason, str) or not reason:
                return None
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason != 'detail_prime_event_phase_mismatch':
                continue
            evidence = record['evidence']
            checked = datetime.fromisoformat(evidence['checked_at'])
            if (checked.tzinfo is None or checked.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() != previous_date
                    or record.get('checked_at') != evidence['checked_at']
                    or record.get('asin') != evidence.get('requested_asin')
                    or record.get('evidence_sha256') != observation_sha256(evidence)
                    or evidence.get('sale_region_count') != 1
                    or evidence.get('sale_region_selector') != '#dealBadge_feature_div'
                    or re.sub(r'\s+', '', str(evidence.get('sale_label', ''))) not in
                       {'プライム感謝祭先行セール', 'Amazonプライム感謝祭先行セール'}):
                return None
            replay = copy.deepcopy(evidence)
            offer, rejection = validate_offer(replay, offer_scope='unified_discounts')
            if rejection or not offer or replay != evidence:
                return None
            mismatch_count += 1
        if not mismatch_count or verification.get('rejection_reasons') != reasons:
            return None
        return {'schema': 'prime-phase-transition-retry-v1', 'account': account, 'date': target_date,
                'from_phase': 'early', 'to_phase': 'main', 'prior_output_dir': str(folder),
                'prior_attempt_sha256': digest(attempt.read_bytes()),
                'prior_result_sha256': digest(result_path.read_bytes()),
                'prior_candidate_files_sha256': old['candidate_files_sha256'],
                'phase_mismatch_count': mismatch_count}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def main_label_compatibility_retry_evidence(attempt, transition_attempt, *, account, target_date,
                                          current_date=None, remote_products=None):
    """One incident-only parser correction after account1's archived run-26.

    Preserve the original attempt and transition marker. The raw observations
    are replayed without mutation; they are never promoted into product rows.
    """
    try:
        if (account != 1 or target_date != '2026-10-16' or target_date != (current_date or today_jst())
                or not isinstance(remote_products, list) or remote_products):
            return None
        attempt, transition_attempt = Path(attempt), Path(transition_attempt)
        if (transition_attempt.parent.resolve() != attempt.parent.resolve()
                or transition_attempt.name != 'account1_2026-10-16.phase-transition-main.attempt.json'):
            return None
        prior = phase_transition_retry_evidence(attempt, account=account, target_date=target_date,
            current_date=current_date, remote_products=remote_products)
        saved = json.loads(transition_attempt.read_text(encoding='utf-8-sig'))
        folder = Path(saved['output_dir']).resolve()
        if (not prior or saved.get('phase_transition_retry') != prior or saved.get('account') != account
                or saved.get('date') != target_date or folder.parent != attempt.parent.parent.resolve()
                or folder.name != 'run-26' or not re.fullmatch(r'[0-9a-f]{40}', str(saved.get('source_origin_commit', '')))):
            return None
        result_path = folder/'result.json'
        old = json.loads(result_path.read_text(encoding='utf-8-sig'))
        if (old.get('schema') != 'pinefield-local-recovery-v1' or old.get('account') != 'account1'
                or old.get('date') != target_date or old.get('execute') is not True
                or old.get('nightly') is not False or old.get('cloud_run_completed_declared') is not True
                or old.get('status') != 'STOPPED' or old.get('reason') != 'canonical_output_validation_failed'
                or old.get('scrape_runs') != 1 or old.get('push_attempts') != 0
                or old.get('phase_transition_retry') != prior or old.get('source_origin_commit') != saved['source_origin_commit']
                or Path(old['output_dir']).resolve() != folder):
            return None
        stamps = [datetime.fromisoformat(v) for v in (old['started_at_utc'], saved['started_at_utc'], old['finished_at_utc'])]
        if (any(s.tzinfo is None or s.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() != target_date for s in stamps)
                or stamps != sorted(stamps)):
            return None
        names = (f'products_{target_date}.json', f'scrape_summary_{target_date}.json')
        files = {name: (folder/name).read_bytes() for name in names}
        if (old.get('candidate_files_sha256') != {name: digest(payload) for name, payload in files.items()}
                or json.loads(files[names[0]].decode('utf-8-sig')) != []):
            return None
        summary = json.loads(files[names[1]].decode('utf-8-sig'))
        expected = apply_campaign_config({}, 'categories1.yaml', target_date).get('_sale_campaign')
        if summary.get('date') != target_date or summary.get('total_taken') != 0 or summary.get('campaign') != expected:
            return None
        verification = summary['detail_offer_verification']; records = verification['observations']
        count = len(records)
        reason = 'detail_prime_event_label_unverified'
        if (not isinstance(records, list) or not count or verification.get('schema_version') != 1
                or verification.get('enabled') is not True or verification.get('offer_scope') != 'unified_discounts'
                or verification.get('candidate_count') != count or verification.get('rejected_count') != count
                or verification.get('accepted_count') != 0 or verification.get('final_selected_count') != 0
                or verification.get('rejection_reasons') != {reason: count}):
            return None
        replayed = []
        for record in records:
            evidence = record['evidence']; checked = datetime.fromisoformat(evidence['checked_at'])
            if (record.get('status') != 'rejected' or record.get('reason') != reason or record.get('pdp_offer') is not None
                    or record.get('asin') != evidence.get('requested_asin') or record.get('checked_at') != evidence.get('checked_at')
                    or record.get('evidence_sha256') != observation_sha256(evidence)
                    or checked.tzinfo is None or checked.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() != target_date
                    or evidence.get('sale_region_count') != 1 or evidence.get('sale_region_selector') != '#dealBadge_feature_div'
                    or evidence.get('sale_label') not in ('プライム感謝祭セール', 'Amazonプライム感謝祭セール')):
                return None
            replay = copy.deepcopy(evidence)
            offer, rejection = validate_offer(replay, offer_scope='unified_discounts')
            if rejection or replay != evidence or not offer or type(offer.get('price_int')) is not int or offer['price_int'] < 8000:
                return None
            accepted = dict(record, status='accepted', reason=None, pdp_offer=offer)
            proof = prime_event_proof(accepted, target_date)
            if proof.get('verified') is not True or proof.get('phase') != 'main' or proof.get('label') != 'プライム感謝祭':
                return None
            replayed.append({'asin': record['asin'], 'checked_at': record['checked_at'],
                'evidence_sha256': record['evidence_sha256'], 'raw_sale_label': evidence['sale_label'],
                'price_int': offer['price_int'], 'prime_event_proof': proof})
        return {'schema': 'prime-main-label-parser-correction-retry-v1', 'account': account, 'date': target_date,
            'reason': 'label_parser_correction_retry', 'prior_output_dir': str(folder),
            'prior_attempt_sha256': digest(attempt.read_bytes()),
            'prior_phase_transition_attempt_sha256': digest(transition_attempt.read_bytes()),
            'prior_result_sha256': digest(result_path.read_bytes()), 'prior_candidate_files_sha256': old['candidate_files_sha256'],
            'parser_sha256': digest((ROOT/'detail_offer.py').read_bytes()), 'replayed_main_alias_count': count,
            'raw_evidence_unchanged': True, 'replayed_observations': replayed}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def check_main_label_retry_owners(account, target_date):
    # Unlike a normal retry, this incident exception requires no A/B/source owner.
    state = FACTORY_BASE/'factory-runtime'/'state'/f'account{account}'/target_date
    if list(state.glob('*.lock.json')):
        raise RecoveryStop('existing_factory_runtime_owner_for_main_label_retry')


def postable_count(account, products, summary=None):
    """Readiness is separate from validity: keep every valid short source."""
    campaign_source = isinstance(summary, dict) and 'campaign' in summary
    if campaign_source:
        # Readiness may be checked while appending a validated supplement; its
        # count fields are updated only after the append loop completes.
        source = copy.deepcopy(summary)
        source.pop('total_taken', None)
        verification = source.get('detail_offer_verification', {})
        observations = verification.get('observations', [])
        if observations:
            verification.update(candidate_count=len(observations),
                accepted_count=sum(row.get('status') == 'accepted' for row in observations),
                rejected_count=sum(row.get('status') == 'rejected' for row in observations))
        source_campaign_config({}, source, products, account, summary.get('date'))
    if account != 20 or campaign_source:
        return min(4, len(products))
    shelves = ('Nintendo Switch 2', 'PS5ゲームソフト')
    return sum(min(2, sum(re.sub(r'#\d+$', '', str(p.get('category', '')).strip()).strip() == shelf
                          for p in products)) for shelf in shelves)


def merge_short_source(account, original_products, original_summary, fresh_products, fresh_summary,
                       identity_module):
    """Append only independently verified new identities; retain the exact original prefix."""
    if original_summary.get('selection_policy') != fresh_summary.get('selection_policy'):
        raise RecoveryStop('supplement_selection_policy_changed')
    if original_summary.get('campaign') != fresh_summary.get('campaign'):
        raise RecoveryStop('supplement_campaign_allocation_changed')
    if not original_summary.get('discount_contract') or not fresh_summary.get('discount_contract'):
        raise RecoveryStop('supplement_requires_verified_discount_contract')
    products = copy.deepcopy(original_products)
    summary = copy.deepcopy(original_summary)
    registry = identity_module.ProductIdentityRegistry()
    for item in products:
        registry.add_identity(identity_module.extract_product_identity(item))
    seen = {p['asin'] for p in products}
    additions = []
    fresh_rows = {p['asin']: p for p in fresh_summary['discount_contract']['products']}
    fresh_observations = {p['asin']: p for p in fresh_summary['detail_offer_verification']['observations']}
    for item in fresh_products:
        if postable_count(account, products, summary) >= 4 or len(products) >= 10:
            break
        if item['asin'] in seen:
            continue
        identity = identity_module.extract_product_identity(item)
        if registry.match_identity(identity):
            continue
        # account20 additions must actually fill a missing shelf, never consume capacity otherwise.
        if account == 20 and 'campaign' not in summary and postable_count(account, products + [item], summary) == postable_count(account, products, summary):
            continue
        record = fresh_observations.get(item['asin'])
        row = fresh_rows.get(item['asin'])
        if not record or record.get('status') != 'accepted' or not row:
            raise RecoveryStop('supplement_new_item_has_no_verified_observation')
        products.append(copy.deepcopy(item)); additions.append(item['asin']); seen.add(item['asin'])
        registry.add_identity(identity)
        row = copy.deepcopy(row); row['source_position'] = len(products)
        summary['discount_contract']['products'].append(row)
        observations = summary['detail_offer_verification']['observations']
        previous = next((r for r in observations if r['asin'] == item['asin']), None)
        # An earlier rejected candidate can later be accepted. Its original observation is archived.
        if previous is not None:
            summary.setdefault('local_supplement', {}).setdefault('superseded_observations', []).append(copy.deepcopy(previous))
            observations.remove(previous)
        observations.append(copy.deepcopy(record))
    verification = summary['detail_offer_verification']
    observations = verification['observations']
    verification.update(candidate_count=len(observations),
                        accepted_count=sum(r.get('status') == 'accepted' for r in observations),
                        rejected_count=sum(r.get('status') == 'rejected' for r in observations),
                        final_selected_count=len(products))
    reasons = {}
    for record in observations:
        if record.get('status') == 'rejected':
            reason = record.get('reason') or 'unspecified'
            reasons[reason] = reasons.get(reason, 0) + 1
    verification['rejection_reasons'] = reasons
    verification['raw_candidate_count'] = max(len(observations), verification.get('raw_candidate_count', 0))
    if isinstance(summary.get('supply_completion'), dict):
        summary['supply_completion']['initial_scrape_report_only'] = True
        summary['supply_completion']['final_after_local_supplement'] = postable_count(account, products, summary)
    for category, values in verification.get('categories', {}).items():
        values['final_selected'] = sum(p['category'].split('#')[0] == category for p in products)
    summary['total_taken'] = len(products)
    summary.setdefault('local_supplement', {}).update(
        schema_version=1, original_count=len(original_products), added_asins=additions,
        original_prefix_preserved=products[:len(original_products)] == original_products,
        postable_count=postable_count(account, products, summary), fresh_summary_date=fresh_summary.get('date'))
    if products[:len(original_products)] != original_products:
        raise RecoveryStop('supplement_changed_original_prefix')
    return products, summary


def load_identity_module(worktree):
    name = '_pinefield_recovery_identity_' + uuid.uuid4().hex
    path = worktree / 'product_identity.py'
    if not path.is_file():
        raise RecoveryStop('canonical_identity_module_missing')
    # Compile bytes in memory: ordinary Python invocation must not create an untracked .pyc in the checkout.
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec'), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def factory_root(account):
    base = FACTORY_BASE.resolve()
    factory = (base / f'★全体的なワークフロー_1_{account}').resolve()
    if base not in factory.parents or not (factory / '②記事制作').is_dir():
        raise RecoveryStop('canonical_factory_unavailable')
    return factory


def check_source(factory, account, target_date):
    source = factory / '②記事制作' / '実行結果' / f'account{account}' / target_date / 'source'
    if source.exists() and (not source.is_dir() or any(source.iterdir())):
        raise RecoveryStop('factory_same_day_source_already_fixed_or_partial')


@contextmanager
def account_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryStop('account_date_recovery_lock_busy') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def factory_locks(factory, account, target_date, execute):
    paths = [factory / '②記事制作' / '実行結果' / '.locks' /
             f'account{account}_{target_date}_{part}.lock.json' for part in ('p12', 'p34')]
    acquired = []
    try:
        for part, path in zip(('p12', 'p34'), paths):
            if path.exists():
                raise RecoveryStop('existing_factory_part_lock:' + part)
            if execute:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {'schema': 1, 'token': str(uuid.uuid4()), 'pid': os.getpid(),
                           'hostname': socket.gethostname(), 'started_at': utc_now(),
                           'account': account, 'date': target_date, 'part': part}
                try:
                    write_json(path, payload, exclusive=True)
                except FileExistsError as exc:
                    raise RecoveryStop('existing_factory_part_lock:' + part) from exc
                acquired.append((path, payload['token']))
        yield
    finally:
        for path, token in reversed(acquired):
            try:
                if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('token') == token:
                    path.unlink()
            except (OSError, ValueError):
                pass  # Never remove a replaced/unknown lock.


def verify_policy(worktree, account):
    config = yaml.safe_load((worktree / f'categories{account}.yaml').read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise RecoveryStop('invalid_canonical_config')
    filters, exclusion = config.get('filters', {}), config.get('exclusion', {})
    if not isinstance(filters, dict) or not isinstance(exclusion, dict):
        raise RecoveryStop('invalid_canonical_config')
    policy = {'selection_mode': 'category_quota' if account == 20 else 'global_ranked',
              'sort_order': 'price_desc', 'max_per_category': 5 if account == 20 else 0, 'max_total_items': 10,
              'selection_policy_version': 'profit_price_desc_v1', 'min_price': 8000, 'max_price': 0, 'require_sale_info': True}
    if any(type(filters.get(key)) is not type(value) or filters.get(key) != value for key, value in policy.items()):
        raise RecoveryStop('non_current_selection_policy')
    if exclusion.get('exclude_scraped_candidates') is not False:
        raise RecoveryStop('scraped_candidate_ledger_writes_must_be_disabled')
    if exclusion.get('exclude_product_identifiers') is not True or exclusion.get('exclude_within_days') != 20:
        raise RecoveryStop('non_current_product_exclusion_policy')
    ledger = worktree / 'data' / f'account{account}' / 'asin_history.json'
    declared = Path(str(exclusion.get('posted_asins_file', '')))
    if (declared if declared.is_absolute() else worktree / declared).resolve() != ledger.resolve():
        raise RecoveryStop('wrong_account_ledger_path')
    history = json.loads(ledger.read_text(encoding='utf-8-sig'))
    if not isinstance(history, dict) or history.get('schema') != 'note-amazon-asin-history-v1' or not isinstance(history.get('posted'), list):
        raise RecoveryStop('invalid_git_ledger')
    if any(not isinstance(item, dict) or item.get('account_id') not in (None, f'account{account}')
           for item in history['posted']):
        raise RecoveryStop('ledger_account_mismatch')
    return {**policy, 'require_sale_info': True, 'ledger_sha256': digest(ledger.read_bytes()),
            'ledger_entry_count': len(history['posted'])}


def changed_paths(commands, worktree, *, staged=False):
    if staged:
        raw = commands.git(worktree, 'diff', '--cached', '--name-only', '-z').stdout
        return {entry.decode('utf-8') for entry in raw.split(b'\0') if entry}
    raw = commands.git(worktree, 'status', '--porcelain=v1', '--untracked-files=all', '-z').stdout
    return {entry[3:].decode('utf-8') for entry in raw.split(b'\0') if entry}


def validate_output(commands, worktree, account, target_date, log):
    result = commands.run([sys.executable, '-B', str(worktree / 'ensure_daily_scrape.py'), '--validate-only',
        '--account', f'account{account}', '--date', target_date, '--root', str(worktree)], cwd=worktree, check=False)
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RecoveryStop('canonical_output_validation_failed')
    products = json.loads((worktree / 'data' / f'account{account}' / f'products_{target_date}.json').read_text(encoding='utf-8-sig'))
    if any(set(item) != PRODUCT_FIELDS for item in products):
        raise RecoveryStop('products_must_keep_exact_13_fields')
    # Validity deliberately allows 1..10; postable_count separately tests four-slot readiness.
    return len(products)


def existing_valid_output(commands, worktree, account, target_date, output, base, paths):
    if not all((worktree / name).is_file() for name in paths):
        return None
    try:
        count = validate_output(commands, worktree, account, target_date, output / 'existing_validation.log')
    except RecoveryStop as exc:
        if str(exc) not in {'canonical_output_validation_failed', 'products_must_keep_exact_13_fields'}:
            raise
        return None
    # Preserve the exact fetched Git blobs; a valid source needs no new scrape or push.
    blobs = {name: commands.git(worktree, 'show', f'{base}:{name}').stdout for name in paths}
    for name, content in blobs.items():
        (output / Path(name).name).write_bytes(content)
    return {'status': 'UNCHANGED_VALIDATED', 'reused_existing_valid_output': True,
            'product_count': count, 'remote_commit': base, 'published_commit': base,
            'artifact_source': 'origin/main_readback_confirmed',
            'files_sha256': {name: digest(content) for name, content in blobs.items()}}


def scrape_once(worktree, account, target_date, timeout, log):
    env = os.environ.copy()
    env.update(PINEFIELD_TARGET_DATE=target_date, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    env['PINEFIELD_SEARCH_DIAGNOSTICS_DIR'] = str(log.parent / 'public-search-diagnostics')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with log.open('wb') as stream:
        process = subprocess.Popen([sys.executable, '-B', '-u', f'scrape_main{account}.py'], cwd=worktree,
                                   env=env, stdout=stream, stderr=subprocess.STDOUT, **options)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
            raise RecoveryStop('single_scrape_timeout') from exc
    if code:
        raise RecoveryStop('single_scrape_failed:' + str(code))


def unrelated_advance(commands, repo, base, remote, account):
    if base == remote:
        return
    if commands.git(repo, 'merge-base', '--is-ancestor', base, remote, check=False).returncode:
        raise RecoveryStop('remote_history_not_descendant')
    fields = commands.git(repo, 'diff', '--name-status', '--no-renames', '-z', base, remote).stdout.split(b'\0')
    if fields[-1:] == [b'']:
        fields.pop()
    if len(fields) % 2:
        raise RecoveryStop('unrecognized_remote_diff')
    pattern = re.compile(r'(?:data/account(?P<data>\d+)/(?:asin_history\.json|category_rotation\.json|'
                         r'products_\d{4}-\d{2}-\d{2}\.json|scrape_summary_\d{4}-\d{2}-\d{2}\.json)|'
                         r'\.github/triggers/account(?P<trigger>\d+)\.json)')
    for status, path in zip(fields[::2], fields[1::2]):
        match = pattern.fullmatch(path.decode('utf-8'))
        number = int(match.group('data') or match.group('trigger')) if match else account
        if status not in (b'A', b'M') or not 1 <= number <= 20 or number == account:
            raise RecoveryStop('remote_changed_account_or_shared_inputs')


def allocate_output(account, target_date):
    parent = OUTPUT_BASE / target_date / f'local-recovery-account{account}'
    parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        path = parent / f'run-{number:02d}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RecoveryStop('too_many_local_recovery_runs')


def recover(*, account, target_date, repo=ROOT, execute=False, cloud_run_completed=False, timeout=1800,
            nightly=False, before_publish=None, deadline_utc=None):
    validate_date(account, target_date, allow_next_day=nightly)
    if not 1 <= timeout <= 3600:
        raise RecoveryStop('timeout_must_be_1_to_3600_seconds')
    if execute and not cloud_run_completed:
        raise RecoveryStop('execute_requires_cloud_run_completed')
    if nightly and execute and not callable(before_publish):
        raise RecoveryStop('nightly_execute_requires_live_cloud_publish_guard')
    def time_guard():
        if deadline_utc is not None and datetime.now(timezone.utc) >= deadline_utc:
            raise RecoveryStop('nightly_window_ended')
    time_guard()
    factory = factory_root(account)
    output = allocate_output(account, target_date)
    commands = Commands(output / 'commands.jsonl')
    repo = Path(repo).resolve()
    result = {'schema': 'pinefield-local-recovery-v1', 'account': f'account{account}', 'date': target_date,
              'execute': execute, 'cloud_run_completed_declared': cloud_run_completed, 'scrape_runs': 0,
              'nightly': nightly,
              'push_attempts': 0, 'rebase_attempts': 0, 'output_dir': str(output), 'started_at_utc': utc_now()}
    worktree = None
    try:
        wait_reason = prime_phase_wait_reason(target_date)
        if wait_reason:
            raise RecoveryStop(wait_reason)
        origin = commands.text(repo, 'remote', 'get-url', 'origin')
        if not expected_origin(origin):
            raise RecoveryStop('origin_is_not_canonical_Pinefield')
        # Shared by all local checkouts/clones, not bypassed by passing a different --repo.
        control = output.parent / '.control'
        attempt = control / f'account{account}_{target_date}.attempt.json'
        transition_attempt = control / f'account{account}_{target_date}.phase-transition-main.attempt.json'
        label_attempt = control / f'account{account}_{target_date}.main-label-compatibility.attempt.json'
        pending = control / f'account{account}_{target_date}.pending.json'
        with account_lock(control / f'account{account}_{target_date}.lock'):
            check_source(factory, account, target_date)
            with factory_locks(factory, account, target_date, execute):
                check_source(factory, account, target_date)
                commands.git(repo, 'fetch', 'origin', 'main')
                base = commands.text(repo, 'rev-parse', 'origin/main')
                result['source_origin_commit'] = base
                temporary = Path(tempfile.mkdtemp(prefix=f'pinefield-recovery-{account}-')).resolve()
                worktree = temporary / 'repo'
                commands.git(repo, 'worktree', 'add', '--detach', str(worktree), base)
                policy = verify_policy(worktree, account)
                if not (worktree / f'scrape_main{account}.py').is_file():
                    raise RecoveryStop('canonical_scrape_entrypoint_missing')
                commands.run([sys.executable, '-B', '-c', 'import scraper; import ensure_daily_scrape'], cwd=worktree)
                result['policy'] = policy
                paths = [f'data/account{account}/products_{target_date}.json',
                         f'data/account{account}/scrape_summary_{target_date}.json']
                protected = [f'categories{account}.yaml', f'data/account{account}/asin_history.json',
                             f'data/account{account}/category_rotation.json']
                protected_before = {name: digest((worktree / name).read_bytes()) if (worktree / name).exists() else None for name in protected}
                result['protected_sha256'] = protected_before
                existing = existing_valid_output(commands, worktree, account, target_date, output, base, paths)
                original_products = json.loads((worktree / paths[0]).read_text(encoding='utf-8-sig')) if existing else None
                original_summary = json.loads((worktree / paths[1]).read_text(encoding='utf-8-sig')) if existing else None
                readiness = postable_count(account, original_products, original_summary) if existing else 0
                result['postable_before'] = readiness
                supplement = bool(nightly and existing and readiness < 4 and len(original_products) < 10)
                transition_evidence = None
                label_evidence = None
                if attempt.exists() and not (nightly and pending.exists()) and existing is None:
                    # An absent pair is known empty; a partial or unreadable pair is unknown.
                    remote_products = None
                    if not any((worktree / name).exists() for name in paths):
                        remote_products = []
                    elif all((worktree / name).is_file() for name in paths):
                        remote_products = json.loads((worktree / paths[0]).read_text(encoding='utf-8-sig'))
                    if not transition_attempt.exists():
                        transition_evidence = phase_transition_retry_evidence(attempt, account=account,
                            target_date=target_date, remote_products=remote_products)
                        if transition_evidence:
                            result['phase_transition_retry'] = transition_evidence
                    elif not nightly and not label_attempt.exists():
                        label_evidence = main_label_compatibility_retry_evidence(attempt, transition_attempt,
                            account=account, target_date=target_date, remote_products=remote_products)
                        if label_evidence:
                            check_main_label_retry_owners(account, target_date)
                            if digest((worktree/'detail_offer.py').read_bytes()) != label_evidence['parser_sha256']:
                                raise RecoveryStop('fetched_main_label_parser_differs_from_replayed_parser')
                            result['label_parser_correction_retry'] = label_evidence
                if existing is not None and not supplement:
                    result.update(existing)
                    result['postable_after'] = readiness
                    if nightly and readiness < 4:
                        result['shortfall_reason'] = 'preserved_existing_source_at_maximum_capacity'
                elif attempt.exists() and not (nightly and pending.exists()) and not transition_evidence and not label_evidence:
                    raise RecoveryStop('same_account_date_already_attempted')
                elif not execute:
                    result['status'] = 'PREFLIGHT_PASS'
                else:
                    validate_date(account, target_date, allow_next_day=nightly)
                    time_guard()
                    check_source(factory, account, target_date)
                    shared_inputs = ['scraper.py', 'detail_offer.py', 'product_identity.py', 'ensure_daily_scrape.py',
                                     'scrape_target_date.py', 'sale_campaign.py', 'sale_campaign.yaml', f'scrape_main{account}.py']
                    input_names = paths + protected + shared_inputs
                    input_hashes = {name: digest((worktree / name).read_bytes()) if (worktree / name).is_file() else None
                                    for name in input_names}
                    reused_pending = False
                    if nightly and pending.exists():
                        saved = json.loads(pending.read_text(encoding='utf-8'))
                        if (saved.get('account') != account or saved.get('date') != target_date
                                or saved.get('input_sha256') != input_hashes):
                            raise RecoveryStop('pending_candidate_inputs_changed_no_reuse')
                        folder = Path(saved['output_dir']).resolve()
                        if folder.parent != output.parent.resolve() or not re.fullmatch(r'run-\d+', folder.name):
                            raise RecoveryStop('pending_candidate_path_invalid')
                        for name in paths:
                            content = (folder / ('pending-' + Path(name).name)).read_bytes()
                            if digest(content) != saved['files_sha256'][name]:
                                raise RecoveryStop('pending_candidate_hash_mismatch')
                            (worktree / name).write_bytes(content)
                        reused_pending = True
                        result['reused_pending_candidate'] = str(folder)
                    else:
                        child_timeout = timeout
                        if deadline_utc is not None:
                            child_timeout = min(timeout, int((deadline_utc - datetime.now(timezone.utc)).total_seconds()) - 30)
                            if child_timeout < 600:
                                # Defer to the next safe window without consuming the one-scrape record.
                                raise RecoveryStop('nightly_window_too_short_to_start_scrape')
                        attempt_record = {'account': account, 'date': target_date, 'started_at_utc': utc_now(),
                                          'output_dir': str(output), 'source_origin_commit': base}
                        if transition_evidence:
                            attempt_record['phase_transition_retry'] = transition_evidence
                        if label_evidence:
                            check_main_label_retry_owners(account, target_date)
                            if main_label_compatibility_retry_evidence(attempt, transition_attempt, account=account,
                                    target_date=target_date, remote_products=remote_products) != label_evidence:
                                raise RecoveryStop('main_label_retry_evidence_changed_before_scrape')
                            attempt_record['label_parser_correction_retry'] = label_evidence
                            attempt_record['schema'] = 'prime-main-label-parser-correction-attempt-v1'
                        write_json(label_attempt if label_evidence else transition_attempt if transition_evidence else attempt,
                                   attempt_record, exclusive=True)
                        # Only our isolated two outputs are removed; the remote originals remain unchanged.
                        for name in paths:
                            (worktree / name).unlink(missing_ok=True)
                        result['scrape_runs'] = 1
                        scrape_once(worktree, account, target_date, child_timeout, output / 'scrape.log')
                        validate_output(commands, worktree, account, target_date, output / 'fresh_validation.log')
                        if supplement:
                            fresh_products = json.loads((worktree / paths[0]).read_text(encoding='utf-8-sig'))
                            fresh_summary = json.loads((worktree / paths[1]).read_text(encoding='utf-8-sig'))
                            for name in paths:
                                (output / ('fresh-' + Path(name).name)).write_bytes((worktree / name).read_bytes())
                            merged_products, merged_summary = merge_short_source(account, original_products, original_summary,
                                fresh_products, fresh_summary, load_identity_module(worktree))
                            if merged_products == original_products:
                                # Keep the exact old JSON blobs, including summary and whitespace, on a no-gain run.
                                for name in paths:
                                    (worktree / name).write_bytes(commands.git(worktree, 'show', f'{base}:{name}').stdout)
                                result['shortfall_reason'] = 'no_additional_verified_distinct_products'
                            else:
                                write_json(worktree / paths[0], merged_products)
                                write_json(worktree / paths[1], merged_summary)
                            result['supplement_added_count'] = len(merged_products) - len(original_products)
                    for name in paths:
                        if not (worktree / name).is_file():
                            raise RecoveryStop('fresh_output_missing')
                        (output / Path(name).name).write_bytes((worktree / name).read_bytes())
                    result['generated_files_sha256'] = {name: digest((worktree / name).read_bytes()) for name in paths}
                    after = {name: digest((worktree / name).read_bytes()) if (worktree / name).exists() else None for name in protected}
                    if after != protected_before:
                        raise RecoveryStop('configuration_ledger_or_rotation_changed')
                    if not changed_paths(commands, worktree).issubset(set(paths)):
                        raise RecoveryStop('unexpected_scraper_file_change')
                    result['product_count'] = validate_output(commands, worktree, account, target_date, output / 'validation.log')
                    final_products = json.loads((worktree / paths[0]).read_text(encoding='utf-8-sig'))
                    if original_products is not None and nightly and final_products[:len(original_products)] != original_products:
                        raise RecoveryStop('supplement_changed_original_prefix')
                    final_summary = json.loads((worktree / paths[1]).read_text(encoding='utf-8-sig'))
                    result['postable_after'] = postable_count(account, final_products, final_summary)
                    if nightly:
                        # Persist immutable candidates before a transient Cloud wait. Never re-scrape them on retry.
                        for name in paths:
                            (output / ('pending-' + Path(name).name)).write_bytes((worktree / name).read_bytes())
                        write_json(pending, {'account': account, 'date': target_date, 'input_sha256': input_hashes,
                            'output_dir': str(output), 'files_sha256': {name: digest((worktree / name).read_bytes()) for name in paths}})
                    check_source(factory, account, target_date)
                    commands.git(worktree, 'add', '--', *paths)
                    staged = changed_paths(commands, worktree, staged=True)
                    if not staged.issubset(set(paths)):
                        raise RecoveryStop('unexpected_staged_file')
                    # Git may normalize Windows CRLF. These are the exact blobs that will be published.
                    indexed = {name: commands.git(worktree, 'show', ':' + name).stdout for name in paths}
                    result['files_sha256'] = {name: digest(content) for name, content in indexed.items()}
                    result['artifact_source'] = 'git_index_pending_remote_confirmation'
                    result['representation_changes'] = []
                    for name, content in indexed.items():
                        artifact = output / Path(name).name
                        generated = artifact.read_bytes()
                        if generated != content:
                            if generated.replace(b'\r\n', b'\n') != content.replace(b'\r\n', b'\n'):
                                raise RecoveryStop('git_filter_changed_more_than_line_endings')
                            raw = output / 'raw' / artifact.name
                            raw.parent.mkdir(exist_ok=True)
                            raw.write_bytes(generated)
                            result['representation_changes'].append({'file': name, 'raw_path': str(raw),
                                'generated_sha256': digest(generated), 'git_blob_sha256': digest(content),
                                'line_endings_only': True, 'values_and_order_unchanged': True})
                            artifact.write_bytes(content)
                    if staged:
                        commands.git(worktree, '-c', 'user.name=codex-local-recovery', '-c',
                                     'user.email=codex-local-recovery@users.noreply.github.com', 'commit', '-m',
                                     f'ver6.12 Local recovery account{account} products {target_date}')
                    commands.git(repo, 'fetch', 'origin', 'main')
                    remote = commands.text(repo, 'rev-parse', 'origin/main')
                    unrelated_advance(commands, worktree, base, remote, account)
                    if remote != base:
                        result['rebase_attempts'] = 1
                        commands.git(worktree, 'rebase', remote)
                        verify_policy(worktree, account)
                        result['product_count'] = validate_output(commands, worktree, account, target_date, output / 'validation_after_rebase.log')
                    result['publish_base_commit'] = remote
                    validate_date(account, target_date, allow_next_day=nightly)
                    time_guard()
                    check_source(factory, account, target_date)
                    protected_after = {name: digest((worktree / name).read_bytes()) if (worktree / name).exists() else None for name in protected}
                    if protected_after != protected_before:
                        raise RecoveryStop('configuration_ledger_or_rotation_changed_before_push')
                    actual = {name: digest(commands.git(worktree, 'show', ':' + name).stdout) for name in paths}
                    if actual != result['files_sha256'] or changed_paths(commands, worktree):
                        raise RecoveryStop('validated_output_changed_before_push')
                    commit = commands.text(worktree, 'rev-parse', 'HEAD')
                    result['candidate_commit'] = commit
                    committed = set(commands.git(worktree, 'diff', '--name-only', '-z', remote, commit).stdout.decode().strip('\0').split('\0')) - {''}
                    if committed != staged:
                        raise RecoveryStop('publish_commit_scope_changed')
                    if staged:
                        if before_publish is not None:
                            gate = before_publish()
                            result['pre_publish_cloud_gate'] = gate
                            if not isinstance(gate, dict) or gate.get('status') != 'READY':
                                raise RecoveryStop('cloud_not_ready_before_publish_candidate_saved')
                        time_guard()
                        check_source(factory, account, target_date)
                        result['push_attempts'] = 1
                        try:
                            push = commands.git(worktree, 'push', 'origin', 'HEAD:main', check=False)
                            result['push_exit'] = push.returncode
                        except (subprocess.TimeoutExpired, OSError) as exc:
                            result['push_uncertain_error'] = type(exc).__name__
                        # An uncertain response is read back, never retried.
                    commands.git(repo, 'fetch', 'origin', 'main')
                    confirmed = commands.text(repo, 'rev-parse', 'origin/main')
                    result['remote_commit'] = confirmed
                    if commands.git(worktree, 'merge-base', '--is-ancestor', commit, confirmed, check=False).returncode:
                        raise RecoveryStop('push_conflict_or_unconfirmed_no_retry')
                    for name in paths:
                        content = commands.git(repo, 'show', f'{confirmed}:{name}').stdout
                        if digest(content) != result['files_sha256'][name]:
                            raise RecoveryStop('remote_readback_hash_mismatch')
                    result['artifact_source'] = 'origin/main_readback_confirmed'
                    result['status'] = 'PUBLISHED' if staged else 'UNCHANGED_VALIDATED'
                    result['published_commit'] = commit
                    if nightly and pending.exists():
                        pending.unlink()
    except (RecoveryStop, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as exc:
        result['status'] = 'WAIT' if str(exc) == 'prime_event_wait_for_target_phase' else 'STOPPED'
        result['reason'] = str(exc) if isinstance(exc, RecoveryStop) else type(exc).__name__
    finally:
        if worktree is not None:
            try:
                if result['scrape_runs']:
                    archived = {}
                    for name in (f'products_{target_date}.json', f'scrape_summary_{target_date}.json'):
                        candidate = worktree / 'data' / f'account{account}' / name
                        if candidate.is_file():
                            content = candidate.read_bytes()
                            saved = output / name
                            if not saved.exists():
                                saved.write_bytes(content)
                            archived[name] = digest(saved.read_bytes())
                    result['candidate_files_sha256'] = archived
            except (OSError, ValueError) as exc:
                result['candidate_archive_error'] = type(exc).__name__
            try:
                # The sole removal target is our freshly allocated temporary/repo checkout.
                allowed = worktree.parent
                if worktree.resolve() != allowed / 'repo' or not allowed.name.startswith(f'pinefield-recovery-{account}-'):
                    raise RecoveryStop('cleanup_target_outside_allocated_worktree')
                cleanup = commands.git(repo, 'worktree', 'remove', '--force', str(worktree), check=False)
                result['worktree_removed'] = cleanup.returncode == 0
                if cleanup.returncode:
                    result['cleanup_error'] = 'worktree_remove_failed'
                elif allowed.exists():
                    allowed.rmdir()
            except (RecoveryStop, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                result['cleanup_error'] = type(exc).__name__
                result.setdefault('worktree_removed', False)
            if not result.get('worktree_removed'):
                result['remaining_worktree'] = str(worktree)
        result['finished_at_utc'] = utc_now()
        try:
            write_json(output / 'result.json', result)
        except OSError as exc:
            result['result_save_error'] = type(exc).__name__
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, type=int, choices=range(1, 21))
    parser.add_argument('--date', default=today_jst())
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--cloud-run-completed', action='store_true')
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        result = recover(account=args.account, target_date=args.date, repo=args.repo, execute=args.execute,
                         cloud_run_completed=args.cloud_run_completed, timeout=args.timeout_seconds)
    except RecoveryStop as exc:
        result = {'status': 'STOPPED', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['status'] == 'STOPPED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
