"""Publish one validated account14 scrape from the local Windows network.

This runner is deliberately account14-only.  It never edits the user's main
Pinefield checkout: every run works in an isolated clone of ``origin/main``.
An already valid same-day source is reused without scraping or pushing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


ACCOUNT = "account14"
ACCOUNT_NUMBER = "14"
MIN_ITEMS = 4
MAX_ITEMS = 10
LOCK_BUSY = 75
SCRAPE_FAILED = 20
VALIDATION_FAILED = 21
SCOPE_VIOLATION = 22
REMOTE_CONFLICT = 23
PUBLISH_FAILED = 24
DATE_REJECTED = 25
COMMAND_TIMEOUT_SECONDS = 180


class RunnerFailure(RuntimeError):
    def __init__(self, code: int, status: str, reason: str, *, scrape_runs: int = 0):
        super().__init__(reason)
        self.code = code
        self.status = status
        self.reason = reason
        self.scrape_runs = scrape_runs


@dataclass(frozen=True)
class RemoteState:
    status: str
    message: str
    count: int = 0
    products_sha256: str = ""
    summary_sha256: str = ""


class RunLog:
    def __init__(self, path: Path):
        self.path = path
        self.scrape_runs = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        line = f"{datetime.now(ZoneInfo('Asia/Tokyo')).isoformat(timespec='seconds')} {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def jst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")


def validate_target_date(value: str, *, today: str | None = None) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise RunnerFailure(DATE_REJECTED, "DATE_REJECTED", f"invalid target date: {value}") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise RunnerFailure(DATE_REJECTED, "DATE_REJECTED", f"invalid target date: {value}")
    expected = today or jst_today()
    if value != expected:
        raise RunnerFailure(
            DATE_REJECTED,
            "DATE_REJECTED",
            f"account14 local runner accepts JST today only: {value} != {expected}",
        )
    return value


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def command_text(arguments: list[str]) -> str:
    return " ".join(arguments)


def run_command(
    arguments: list[str],
    *,
    cwd: Path,
    log: RunLog,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    log.write(f"RUN {command_text(arguments)}")
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.stdout:
        for line in completed.stdout.rstrip().splitlines():
            log.write(f"OUT {line}")
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            arguments,
            output=completed.stdout,
        )
    return completed


def git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment


def git(repo: Path, *arguments: str, log: RunLog, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["git", "-C", str(repo), *arguments],
        cwd=repo,
        log=log,
        check=check,
        env=git_environment(),
    )


def git_blob(repo: Path, ref: str, relative_path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{relative_path}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def validate_root(repo: Path, root: Path, target_date: str, log: RunLog) -> tuple[bool, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = run_command(
        [
            sys.executable,
            "-X",
            "utf8",
            "-u",
            str(repo / "ensure_daily_scrape.py"),
            "--validate-only",
            "--account",
            ACCOUNT,
            "--date",
            target_date,
            "--root",
            str(root),
        ],
        cwd=repo,
        log=log,
        check=False,
        env=environment,
    )
    return completed.returncode == 0, (completed.stdout or "").strip()


def product_contract(root: Path, target_date: str) -> tuple[bool, str, int]:
    path = root / "data" / ACCOUNT / f"products_{target_date}.json"
    try:
        products = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return False, f"cannot read products: {exc}", 0
    if not isinstance(products, list):
        return False, "products is not a list", 0
    count = len(products)
    if count < MIN_ITEMS or count > MAX_ITEMS:
        return False, f"product count outside account14 contract: {count}", count
    first_four_categories = [
        str(item.get("category", "")).split("#", 1)[0].strip()
        for item in products[:4]
        if isinstance(item, dict)
    ]
    if len(first_four_categories) != 4 or any(not value for value in first_four_categories):
        return False, "first four products have an empty or invalid category", count
    if len(set(first_four_categories)) != 4:
        return False, "first four products are not four distinct categories", count
    return True, f"account14 contract ok: {count} items", count


def remote_state(repo: Path, target_date: str, log: RunLog) -> RemoteState:
    products_rel = f"data/{ACCOUNT}/products_{target_date}.json"
    summary_rel = f"data/{ACCOUNT}/scrape_summary_{target_date}.json"
    history_rel = f"data/{ACCOUNT}/asin_history.json"
    products = git_blob(repo, "origin/main", products_rel)
    summary = git_blob(repo, "origin/main", summary_rel)
    if products is None and summary is None:
        return RemoteState("missing", "remote dated products and summary are absent")
    if products is None or summary is None:
        return RemoteState("invalid", "remote has only one of dated products/summary")
    history = git_blob(repo, "origin/main", history_rel)
    if history is None:
        return RemoteState("invalid", "remote ASIN history is absent")

    with tempfile.TemporaryDirectory(prefix="account14-remote-check-") as directory:
        root = Path(directory)
        account_root = root / "data" / ACCOUNT
        account_root.mkdir(parents=True, exist_ok=True)
        (account_root / f"products_{target_date}.json").write_bytes(products)
        (account_root / f"scrape_summary_{target_date}.json").write_bytes(summary)
        (account_root / "asin_history.json").write_bytes(history)
        valid, message = validate_root(repo, root, target_date, log)
        contract_ok, contract_message, count = product_contract(root, target_date)
    if not valid or not contract_ok:
        return RemoteState("invalid", f"{message}; {contract_message}", count=count)
    return RemoteState(
        "valid",
        f"{message}; {contract_message}",
        count=count,
        products_sha256=sha256_bytes(products),
        summary_sha256=sha256_bytes(summary),
    )


def status_paths(repo: Path, log: RunLog) -> set[str]:
    completed = git(repo, "status", "--porcelain=v1", "--untracked-files=all", log=log)
    paths: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        if not raw_line:
            continue
        path = raw_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.replace("\\", "/").strip('"'))
    return paths


def kill_process_tree(process: subprocess.Popen, log: RunLog) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    log.write(f"terminated scrape process tree pid={process.pid}")


def run_scrape_once(
    repo: Path,
    target_date: str,
    timeout_seconds: int,
    log: RunLog,
) -> int:
    environment = os.environ.copy()
    environment["PINEFIELD_TARGET_DATE"] = target_date
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log.write("starting the single permitted local account14 scrape")
    log.scrape_runs = 1
    with log.path.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", "scrape_main14.py"],
            cwd=repo,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_process_tree(process, log)
            process.wait(timeout=30)
            raise RunnerFailure(
                SCRAPE_FAILED,
                "SCRAPE_TIMEOUT",
                f"single scrape exceeded {timeout_seconds} seconds",
                scrape_runs=1,
            )


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    try:
        handle.seek(0)
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunnerFailure(LOCK_BUSY, "LOCK_BUSY", "another account14 local runner is active") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def result_payload(
    *,
    target_date: str,
    status: str,
    exit_code: int,
    reason: str,
    scrape_runs: int,
    origin_commit: str = "",
    count: int = 0,
    products_sha256: str = "",
    summary_sha256: str = "",
    log_path: str = "",
) -> dict:
    return {
        "schema": "pinefield-account14-local-source-v1",
        "target_date": target_date,
        "status": status,
        "exit_code": exit_code,
        "reason": reason,
        "scrape_runs": scrape_runs,
        "origin_main_commit": origin_commit,
        "product_count": count,
        "products_sha256": products_sha256,
        "summary_sha256": summary_sha256,
        "log_path": log_path,
        "finished_at_jst": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
    }


def clone_main(origin: str, destination: Path, *, cwd: Path, log: RunLog) -> str:
    run_command(
        ["git", "clone", "--no-tags", "--single-branch", "--branch", "main", origin, str(destination)],
        cwd=cwd,
        log=log,
        env=git_environment(),
    )
    git(destination, "config", "user.name", "codex-local-account14", log=log)
    git(
        destination,
        "config",
        "user.email",
        "codex-local-account14@users.noreply.github.com",
        log=log,
    )
    git(destination, "fetch", "origin", "main", "--prune", log=log)
    head_commit = git(destination, "rev-parse", "HEAD", log=log).stdout.strip()
    remote_commit = git(destination, "rev-parse", "origin/main", log=log).stdout.strip()
    if head_commit != remote_commit:
        fast_forward = git(
            destination,
            "merge",
            "--ff-only",
            "origin/main",
            log=log,
            check=False,
        )
        if fast_forward.returncode != 0:
            raise RunnerFailure(
                REMOTE_CONFLICT,
                "CLONE_FAST_FORWARD_FAILED",
                fast_forward.stdout or "fresh clone could not fast-forward to origin/main",
                scrape_runs=log.scrape_runs,
            )
        head_commit = git(destination, "rev-parse", "HEAD", log=log).stdout.strip()
        remote_commit = git(destination, "rev-parse", "origin/main", log=log).stdout.strip()
    if head_commit != remote_commit:
        raise RunnerFailure(
            REMOTE_CONFLICT,
            "CLONE_HEAD_MISMATCH",
            f"fresh clone HEAD differs from origin/main: {head_commit} != {remote_commit}",
            scrape_runs=log.scrape_runs,
        )
    return head_commit


def remote_changes_are_other_accounts_only(
    repo: Path,
    base_commit: str,
    head_commit: str,
    log: RunLog,
) -> tuple[bool, str]:
    if base_commit == head_commit:
        return True, "origin/main did not move"
    ancestor = git(
        repo,
        "merge-base",
        "--is-ancestor",
        base_commit,
        head_commit,
        log=log,
        check=False,
    )
    if ancestor.returncode != 0:
        return False, "origin/main is not a descendant of the scrape start commit"
    arguments = [
        "git",
        "-C",
        str(repo),
        "diff",
        "--name-status",
        "--no-renames",
        "-z",
        base_commit,
        head_commit,
        "--",
    ]
    log.write(f"RUN {command_text(arguments)}")
    completed = subprocess.run(
        arguments,
        cwd=repo,
        env=git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if completed.stderr:
        for line in completed.stderr.decode("utf-8", errors="replace").rstrip().splitlines():
            log.write(f"OUT {line}")
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            arguments,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    fields = completed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2 != 0:
        return False, "could not parse remote name-status diff"

    accepted: list[str] = []
    rejected: list[str] = []
    data_pattern = re.compile(
        r"data/account(?P<account>[1-9]|1[0-9]|20)/"
        r"(?:asin_history\.json|category_rotation\.json|"
        r"products_[0-9]{4}-[0-9]{2}-[0-9]{2}\.json|"
        r"scrape_summary_[0-9]{4}-[0-9]{2}-[0-9]{2}\.json)"
    )
    trigger_pattern = re.compile(r"\.github/triggers/account(?P<account>[1-9]|1[0-9]|20)\.json")
    for index in range(0, len(fields), 2):
        status = fields[index].decode("ascii", errors="replace")
        path = fields[index + 1].decode("utf-8", errors="replace").replace("\\", "/")
        log.write(f"REMOTE_DIFF {status} {path}")
        match = data_pattern.fullmatch(path) or trigger_pattern.fullmatch(path)
        if status not in {"A", "M"} or match is None or match.group("account") == ACCOUNT_NUMBER:
            rejected.append(f"{status}:{path}")
        else:
            accepted.append(f"{status}:{path}")
    if rejected:
        return False, f"remote changed account14/shared inputs: {rejected}"
    return True, f"remote advanced only in allowed other-account files: {accepted}"


def execute(
    *,
    source_repo: Path,
    work_root: Path,
    target_date: str,
    timeout_seconds: int,
    log: RunLog,
) -> dict:
    scrape_runs = 0
    source_repo = source_repo.resolve()
    work_root = work_root.resolve()
    workspace_root = source_repo.parent.resolve()
    if not work_root.is_relative_to(workspace_root):
        raise RunnerFailure(
            SCOPE_VIOLATION,
            "WORK_ROOT_REJECTED",
            f"work root must stay under {workspace_root}: {work_root}",
        )
    origin = git(source_repo, "remote", "get-url", "origin", log=log).stdout.strip()
    if not origin:
        raise RunnerFailure(PUBLISH_FAILED, "NO_ORIGIN", "origin URL is empty")

    runs_root = work_root / "work"
    runs_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{target_date}-", dir=runs_root) as directory:
        run_root = Path(directory).resolve()
        if not run_root.is_relative_to(runs_root.resolve()):
            raise RunnerFailure(SCOPE_VIOLATION, "TEMP_SCOPE_REJECTED", str(run_root))
        repo = run_root / "repo"
        base_commit = clone_main(origin, repo, cwd=run_root, log=log)

        initial = remote_state(repo, target_date, log)
        if initial.status == "valid":
            commit = git(repo, "rev-parse", "origin/main", log=log).stdout.strip()
            return result_payload(
                target_date=target_date,
                status="REUSED",
                exit_code=0,
                reason=initial.message,
                scrape_runs=0,
                origin_commit=commit,
                count=initial.count,
                products_sha256=initial.products_sha256,
                summary_sha256=initial.summary_sha256,
                log_path=str(log.path),
            )
        if initial.status != "missing":
            raise RunnerFailure(REMOTE_CONFLICT, "REMOTE_INVALID", initial.message)

        history_path = repo / "data" / ACCOUNT / "asin_history.json"
        rotation_path = repo / "data" / ACCOUNT / "category_rotation.json"
        history_before = sha256_file(history_path)
        rotation_before = sha256_file(rotation_path)
        scrape_runs = 1
        scrape_exit = run_scrape_once(repo, target_date, timeout_seconds, log)
        if scrape_exit != 0:
            raise RunnerFailure(
                SCRAPE_FAILED,
                "SCRAPE_FAILED",
                f"single scrape exited {scrape_exit}",
                scrape_runs=1,
            )

        if sha256_file(history_path) != history_before:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "HISTORY_CHANGED",
                "asin_history.json changed",
                scrape_runs=1,
            )
        if sha256_file(rotation_path) != rotation_before:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "ROTATION_CHANGED",
                "category_rotation.json changed",
                scrape_runs=1,
            )
        valid, validation_message = validate_root(repo, repo, target_date, log)
        contract_ok, contract_message, count = product_contract(repo, target_date)
        if not valid or not contract_ok:
            raise RunnerFailure(
                VALIDATION_FAILED,
                "VALIDATION_FAILED",
                f"{validation_message}; {contract_message}",
                scrape_runs=1,
            )

        products_rel = f"data/{ACCOUNT}/products_{target_date}.json"
        summary_rel = f"data/{ACCOUNT}/scrape_summary_{target_date}.json"
        allowed = {products_rel, summary_rel}
        changed = status_paths(repo, log)
        if changed != allowed:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "UNEXPECTED_CHANGES",
                f"expected only {sorted(allowed)}, got {sorted(changed)}",
                scrape_runs=1,
            )

        products_sha = sha256_file(repo / products_rel)
        summary_sha = sha256_file(repo / summary_rel)
        products_content = (repo / products_rel).read_bytes()
        summary_content = (repo / summary_rel).read_bytes()

        publish_repo = run_root / "publish"
        publish_commit = clone_main(origin, publish_repo, cwd=run_root, log=log)
        before_push = remote_state(publish_repo, target_date, log)
        if before_push.status == "valid":
            return result_payload(
                target_date=target_date,
                status="REUSED_RACE",
                exit_code=0,
                reason=before_push.message,
                scrape_runs=scrape_runs,
                origin_commit=publish_commit,
                count=before_push.count,
                products_sha256=before_push.products_sha256,
                summary_sha256=before_push.summary_sha256,
                log_path=str(log.path),
            )
        if before_push.status != "missing":
            raise RunnerFailure(
                REMOTE_CONFLICT,
                "REMOTE_INVALID_RACE",
                before_push.message,
                scrape_runs=1,
            )

        safe_advance, advance_message = remote_changes_are_other_accounts_only(
            publish_repo,
            base_commit,
            publish_commit,
            log,
        )
        if not safe_advance:
            raise RunnerFailure(
                REMOTE_CONFLICT,
                "REMOTE_INPUT_CHANGED",
                advance_message,
                scrape_runs=1,
            )
        log.write(advance_message)

        (publish_repo / products_rel).write_bytes(products_content)
        (publish_repo / summary_rel).write_bytes(summary_content)
        if sha256_file(publish_repo / products_rel) != products_sha:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "PUBLISH_CLONE_PRODUCTS_HASH_MISMATCH",
                "products changed while copying to the fresh publish clone",
                scrape_runs=1,
            )
        if sha256_file(publish_repo / summary_rel) != summary_sha:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "PUBLISH_CLONE_SUMMARY_HASH_MISMATCH",
                "summary changed while copying to the fresh publish clone",
                scrape_runs=1,
            )
        if sha256_file(publish_repo / "data" / ACCOUNT / "asin_history.json") != history_before:
            raise RunnerFailure(
                REMOTE_CONFLICT,
                "PUBLISH_CLONE_HISTORY_CHANGED",
                "account14 ASIN history changed after the scrape started",
                scrape_runs=1,
            )
        if sha256_file(publish_repo / "data" / ACCOUNT / "category_rotation.json") != rotation_before:
            raise RunnerFailure(
                REMOTE_CONFLICT,
                "PUBLISH_CLONE_ROTATION_CHANGED",
                "account14 category rotation changed after the scrape started",
                scrape_runs=1,
            )
        publish_valid, publish_validation_message = validate_root(
            publish_repo,
            publish_repo,
            target_date,
            log,
        )
        publish_contract_ok, publish_contract_message, publish_count = product_contract(
            publish_repo,
            target_date,
        )
        if not publish_valid or not publish_contract_ok:
            raise RunnerFailure(
                VALIDATION_FAILED,
                "PUBLISH_CLONE_VALIDATION_FAILED",
                f"{publish_validation_message}; {publish_contract_message}",
                scrape_runs=1,
            )
        if publish_count != count:
            raise RunnerFailure(
                VALIDATION_FAILED,
                "PUBLISH_CLONE_COUNT_MISMATCH",
                f"publish clone count changed: {publish_count} != {count}",
                scrape_runs=1,
            )
        publish_changed = status_paths(publish_repo, log)
        if publish_changed != allowed:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "PUBLISH_CLONE_UNEXPECTED_CHANGES",
                f"expected only {sorted(allowed)}, got {sorted(publish_changed)}",
                scrape_runs=1,
            )

        git(publish_repo, "add", "--", products_rel, summary_rel, log=log)
        staged = set(
            git(publish_repo, "diff", "--cached", "--name-only", log=log).stdout.strip().splitlines()
        )
        if staged != allowed:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "STAGED_SCOPE_VIOLATION",
                f"staged paths: {sorted(staged)}",
                scrape_runs=1,
            )
        git(
            publish_repo,
            "commit",
            "-m",
            f"Auto-scrape account14 products {target_date}",
            log=log,
        )

        pushed_result = git(publish_repo, "push", "origin", "HEAD:main", log=log, check=False)
        if pushed_result.returncode != 0:
            git(publish_repo, "fetch", "origin", "main", "--prune", log=log)
            raced = remote_state(publish_repo, target_date, log)
            if raced.status == "valid":
                commit = git(publish_repo, "rev-parse", "origin/main", log=log).stdout.strip()
                return result_payload(
                    target_date=target_date,
                    status="REUSED_RACE",
                    exit_code=0,
                    reason=raced.message,
                    scrape_runs=scrape_runs,
                    origin_commit=commit,
                    count=raced.count,
                    products_sha256=raced.products_sha256,
                    summary_sha256=raced.summary_sha256,
                    log_path=str(log.path),
                )
            if raced.status != "missing":
                raise RunnerFailure(
                    REMOTE_CONFLICT,
                    "REMOTE_INVALID_RACE",
                    raced.message,
                    scrape_runs=1,
                )
            raise RunnerFailure(
                PUBLISH_FAILED,
                "REMOTE_ADVANCED",
                "origin/main moved after the fresh publish clone; refusing a second publish attempt",
                scrape_runs=1,
            )

        git(publish_repo, "fetch", "origin", "main", "--prune", log=log)
        final = remote_state(publish_repo, target_date, log)
        if final.status != "valid":
            raise RunnerFailure(
                PUBLISH_FAILED,
                "READBACK_INVALID",
                final.message,
                scrape_runs=1,
            )
        if final.products_sha256 != products_sha or final.summary_sha256 != summary_sha:
            raise RunnerFailure(
                PUBLISH_FAILED,
                "READBACK_HASH_MISMATCH",
                "origin/main differs from local output",
                scrape_runs=1,
            )
        commit = git(publish_repo, "rev-parse", "origin/main", log=log).stdout.strip()
        return result_payload(
            target_date=target_date,
            status="PUBLISHED",
            exit_code=0,
            reason=f"{validation_message}; {contract_message}",
            scrape_runs=scrape_runs,
            origin_commit=commit,
            count=count,
            products_sha256=products_sha,
            summary_sha256=summary_sha,
            log_path=str(log.path),
        )


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=jst_today())
    parser.add_argument("--source-repo", type=Path, default=repo_root)
    parser.add_argument("--work-root", type=Path, default=repo_root.parent / "account14-local-runs")
    parser.add_argument("--scrape-timeout-seconds", type=int, default=720)
    args = parser.parse_args(argv)

    target_date = args.date
    source_repo = args.source_repo.resolve()
    work_root = args.work_root.resolve()
    try:
        target_date = validate_target_date(target_date)
        if not source_repo.is_dir():
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "SOURCE_REPO_REJECTED",
                f"source repository does not exist: {source_repo}",
            )
        workspace_root = source_repo.parent.resolve()
        if not work_root.is_relative_to(workspace_root):
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "WORK_ROOT_REJECTED",
                f"work root must stay under {workspace_root}: {work_root}",
            )
        if args.scrape_timeout_seconds <= 0:
            raise RunnerFailure(
                SCOPE_VIOLATION,
                "TIMEOUT_REJECTED",
                "scrape timeout must be positive",
            )
    except RunnerFailure as exc:
        result = result_payload(
            target_date=target_date,
            status=exc.status,
            exit_code=exc.code,
            reason=exc.reason,
            scrape_runs=0,
        )
        print("RESULT " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return int(result["exit_code"])

    run_id = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S-%f")
    log = RunLog(work_root / "logs" / f"account14-{target_date}-{run_id}-{os.getpid()}.log")
    latest_path = work_root / "latest.json"
    result_path = work_root / "results" / f"account14-{target_date}-{run_id}-{os.getpid()}.json"

    def failure_result(exc: BaseException) -> dict:
        if isinstance(exc, RunnerFailure):
            return result_payload(
                target_date=target_date,
                status=exc.status,
                exit_code=exc.code,
                reason=exc.reason,
                scrape_runs=max(exc.scrape_runs, log.scrape_runs),
                log_path=str(log.path),
            )
        return result_payload(
            target_date=target_date,
            status="UNEXPECTED_FAILURE",
            exit_code=PUBLISH_FAILED,
            reason=f"{type(exc).__name__}: {exc}",
            scrape_runs=log.scrape_runs,
            log_path=str(log.path),
        )

    try:
        with exclusive_lock(work_root / "account14.lock"):
            try:
                result = execute(
                    source_repo=source_repo,
                    work_root=work_root,
                    target_date=target_date,
                    timeout_seconds=args.scrape_timeout_seconds,
                    log=log,
                )
            except Exception as exc:
                result = failure_result(exc)
            atomic_json(result_path, result)
            atomic_json(latest_path, result)
            log.write("RESULT " + json.dumps(result, ensure_ascii=False, sort_keys=True))
            return int(result["exit_code"])
    except Exception as exc:
        result = failure_result(exc)
        atomic_json(result_path, result)
    log.write("RESULT " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
