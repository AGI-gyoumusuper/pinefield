from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import run_local_account14_source as runner  # noqa: E402


TARGET_DATE = "2026-08-27"


def run_git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def product(index: int, category: str | None = None) -> dict:
    asin = f"B{index:09d}"
    return {
        "asin": asin,
        "title": f"商品{index}",
        "price": "￥3,500",
        "price_int": 3500,
        "original_price": "",
        "discount_rate": "",
        "image_url": f"https://example.test/{asin}.jpg",
        "affiliate_url": f"https://www.amazon.co.jp/dp/{asin}?tag=noteamazon14-22",
        "category": f"{category or f'カテゴリ{index}'}#1",
        "rating": "4.5",
        "review_count": "100",
        "description": "説明",
        "specs": "仕様",
    }


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_origin(tmp_path: Path, *, partial_remote: bool = False) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, stdout=subprocess.PIPE)
    run_git(seed, "config", "user.name", "test")
    run_git(seed, "config", "user.email", "test@example.invalid")
    shutil.copy2(REPO_ROOT / "ensure_daily_scrape.py", seed / "ensure_daily_scrape.py")
    (seed / "scrape_main14.py").write_text(
        textwrap.dedent(
            """
            import json, os
            from pathlib import Path

            mode = os.environ.get("FAKE_SCRAPE_MODE", "success")
            counter = Path(os.environ["FAKE_SCRAPE_COUNTER"])
            count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(count + 1), encoding="utf-8")
            if mode == "fail":
                raise SystemExit(9)
            date = os.environ["PINEFIELD_TARGET_DATE"]
            root = Path("data/account14")
            root.mkdir(parents=True, exist_ok=True)
            def item(index):
                asin = f"B{index:09d}"
                return {
                    "asin": asin, "title": f"商品{index}", "price": "￥3,500",
                    "price_int": 3500, "original_price": "", "discount_rate": "",
                    "image_url": f"https://example.test/{asin}.jpg",
                    "affiliate_url": f"https://www.amazon.co.jp/dp/{asin}?tag=noteamazon14-22",
                    "category": f"カテゴリ{index}#1", "rating": "4.5",
                    "review_count": "100", "description": "説明", "specs": "仕様"
                }
            products = [item(index) for index in range(1, 5)]
            (root / f"products_{date}.json").write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
            summary = {"date": date, "total_taken": 4, "categories": {}}
            (root / f"scrape_summary_{date}.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
            if mode == "extra":
                Path("unexpected.txt").write_text("unexpected", encoding="utf-8")
            if mode == "history":
                (root / "asin_history.json").write_text("{}", encoding="utf-8")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    account_root = seed / "data" / "account14"
    write_json(
        account_root / "asin_history.json",
        {"schema": "note-amazon-asin-history-v1", "updated_at": "", "posted": []},
    )
    write_json(account_root / "category_rotation.json", {"schema": "amazon-category-rotation-v1"})
    if partial_remote:
        write_json(account_root / f"products_{TARGET_DATE}.json", [product(i) for i in range(1, 5)])
    run_git(seed, "add", "--all")
    run_git(seed, "commit", "-m", "seed")
    run_git(seed, "remote", "add", "origin", str(bare))
    run_git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    return bare, seed


def execute(seed: Path, work_root: Path, log_path: Path) -> dict:
    return runner.execute(
        source_repo=seed,
        work_root=work_root,
        target_date=TARGET_DATE,
        timeout_seconds=60,
        log=runner.RunLog(log_path),
    )


def remote_has(bare: Path, relative_path: str) -> bool:
    completed = subprocess.run(
        ["git", "--git-dir", str(bare), "cat-file", "-e", f"main:{relative_path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def advance_origin(bare: Path, root: Path, relative_path: str, content: str) -> None:
    other = root / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, stdout=subprocess.PIPE)
    run_git(other, "config", "user.name", "other")
    run_git(other, "config", "user.email", "other@example.invalid")
    path = other / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run_git(other, "add", "--", relative_path)
    run_git(other, "commit", "-m", "advance remote")
    run_git(other, "push", "origin", "HEAD:main")


class LocalAccount14RunnerTests(unittest.TestCase):
    def test_clone_main_fast_forwards_when_origin_moves_after_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, _ = create_origin(root)
            original_git = runner.git
            advanced = False

            def advance_before_fetch(repo: Path, *arguments: str, log, check: bool = True):
                nonlocal advanced
                if arguments and arguments[0] == "fetch" and not advanced:
                    advanced = True
                    advance_origin(
                        bare,
                        root,
                        "data/account15/asin_history.json",
                        '{"posted": []}\n',
                    )
                return original_git(repo, *arguments, log=log, check=check)

            destination = root / "clone"
            with patch.object(runner, "git", side_effect=advance_before_fetch):
                commit = runner.clone_main(
                    str(bare),
                    destination,
                    cwd=root,
                    log=runner.RunLog(root / "clone.log"),
                )

            head = run_git(destination, "rev-parse", "HEAD").stdout.strip()
            remote = run_git(destination, "rev-parse", "origin/main").stdout.strip()
            self.assertEqual(commit, head)
            self.assertEqual(head, remote)
            self.assertTrue((destination / "data" / "account15" / "asin_history.json").is_file())

    def test_missing_remote_scrapes_once_publishes_only_dated_files_and_then_reuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, seed = create_origin(root)
            counter = root / "counter.txt"
            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(counter), "FAKE_SCRAPE_MODE": "success"},
            ):
                first = execute(seed, root / "runner-work", root / "first.log")
                second = execute(seed, root / "runner-work", root / "second.log")

            self.assertEqual(first["status"], "PUBLISHED")
            self.assertEqual(first["scrape_runs"], 1)
            self.assertEqual(second["status"], "REUSED")
            self.assertEqual(second["scrape_runs"], 0)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")
            self.assertTrue(remote_has(bare, f"data/account14/products_{TARGET_DATE}.json"))
            self.assertTrue(remote_has(bare, f"data/account14/scrape_summary_{TARGET_DATE}.json"))
            history = run_git(seed, "show", "origin/main:data/account14/asin_history.json").stdout
            self.assertEqual(json.loads(history)["posted"], [])

    def test_failure_modes_do_not_publish(self):
        cases = [
            ("fail", runner.SCRAPE_FAILED, "SCRAPE_FAILED"),
            ("extra", runner.SCOPE_VIOLATION, "UNEXPECTED_CHANGES"),
            ("history", runner.SCOPE_VIOLATION, "HISTORY_CHANGED"),
        ]
        for mode, expected_code, expected_status in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bare, seed = create_origin(root)
                with patch.dict(
                    os.environ,
                    {
                        "FAKE_SCRAPE_COUNTER": str(root / "counter.txt"),
                        "FAKE_SCRAPE_MODE": mode,
                    },
                ):
                    with self.assertRaises(runner.RunnerFailure) as caught:
                        execute(seed, root / "runner-work", root / "failure.log")

                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.status, expected_status)
                self.assertEqual(caught.exception.scrape_runs, 1)
                self.assertFalse(remote_has(bare, f"data/account14/products_{TARGET_DATE}.json"))
                self.assertFalse(remote_has(bare, f"data/account14/scrape_summary_{TARGET_DATE}.json"))

    def test_partial_remote_is_conflict_and_never_scrapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, seed = create_origin(root, partial_remote=True)
            counter = root / "counter.txt"
            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(counter), "FAKE_SCRAPE_MODE": "success"},
            ):
                with self.assertRaises(runner.RunnerFailure) as caught:
                    execute(seed, root / "runner-work", root / "conflict.log")

            self.assertEqual(caught.exception.code, runner.REMOTE_CONFLICT)
            self.assertEqual(caught.exception.status, "REMOTE_INVALID")
            self.assertEqual(caught.exception.scrape_runs, 0)
            self.assertFalse(counter.exists())

    def test_remote_advance_after_scrape_stops_without_rebase_or_stale_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, seed = create_origin(root)
            original_git = runner.git
            calls: list[tuple[str, ...]] = []
            advanced = False

            def advance_before_push(repo: Path, *arguments: str, log, check: bool = True):
                nonlocal advanced
                calls.append(arguments)
                if arguments and arguments[0] == "push" and not advanced:
                    advanced = True
                    advance_origin(bare, root, "unrelated.txt", "remote moved\n")
                return original_git(repo, *arguments, log=log, check=check)

            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(root / "counter.txt"), "FAKE_SCRAPE_MODE": "success"},
            ), patch.object(runner, "git", side_effect=advance_before_push):
                with self.assertRaises(runner.RunnerFailure) as caught:
                    execute(seed, root / "runner-work", root / "race.log")

            self.assertEqual(caught.exception.status, "REMOTE_ADVANCED")
            self.assertEqual(caught.exception.scrape_runs, 1)
            self.assertFalse(any(arguments and arguments[0] == "rebase" for arguments in calls))
            self.assertFalse(remote_has(bare, f"data/account14/products_{TARGET_DATE}.json"))
            self.assertFalse(remote_has(bare, f"data/account14/scrape_summary_{TARGET_DATE}.json"))
            self.assertTrue(remote_has(bare, "unrelated.txt"))

    def test_other_account_remote_advance_is_safely_carried_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, seed = create_origin(root)
            original_scrape = runner.run_scrape_once

            def scrape_then_advance(*arguments, **kwargs):
                exit_code = original_scrape(*arguments, **kwargs)
                advance_origin(
                    bare,
                    root,
                    "data/account15/asin_history.json",
                    '{"posted": []}\n',
                )
                return exit_code

            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(root / "counter.txt"), "FAKE_SCRAPE_MODE": "success"},
            ), patch.object(runner, "run_scrape_once", side_effect=scrape_then_advance):
                result = execute(seed, root / "runner-work", root / "other-account-race.log")

            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(result["scrape_runs"], 1)
            self.assertTrue(remote_has(bare, "data/account15/asin_history.json"))
            self.assertTrue(remote_has(bare, f"data/account14/products_{TARGET_DATE}.json"))
            self.assertTrue(remote_has(bare, f"data/account14/scrape_summary_{TARGET_DATE}.json"))

    def test_account14_input_change_during_scrape_stops_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, seed = create_origin(root)
            original_scrape = runner.run_scrape_once

            def scrape_then_advance(*arguments, **kwargs):
                exit_code = original_scrape(*arguments, **kwargs)
                advance_origin(
                    bare,
                    root,
                    "data/account14/categories14.yaml",
                    "selection_mode: category_round_robin\n",
                )
                return exit_code

            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(root / "counter.txt"), "FAKE_SCRAPE_MODE": "success"},
            ), patch.object(runner, "run_scrape_once", side_effect=scrape_then_advance):
                with self.assertRaises(runner.RunnerFailure) as caught:
                    execute(seed, root / "runner-work", root / "account14-race.log")

            self.assertEqual(caught.exception.status, "REMOTE_INPUT_CHANGED")
            self.assertEqual(caught.exception.scrape_runs, 1)
            self.assertFalse(remote_has(bare, f"data/account14/products_{TARGET_DATE}.json"))
            self.assertFalse(remote_has(bare, f"data/account14/scrape_summary_{TARGET_DATE}.json"))

    def test_product_contract_requires_four_distinct_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = [product(i, category="同じ棚") for i in range(1, 5)]
            write_json(root / "data" / "account14" / f"products_{TARGET_DATE}.json", products)
            valid, message, count = runner.product_contract(root, TARGET_DATE)
            self.assertFalse(valid)
            self.assertEqual(count, 4)
            self.assertIn("distinct categories", message)

    def test_exclusive_lock_returns_busy_for_second_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "account14.lock"
            with runner.exclusive_lock(lock_path):
                with self.assertRaises(runner.RunnerFailure) as caught:
                    with runner.exclusive_lock(lock_path):
                        pass
            self.assertEqual(caught.exception.code, runner.LOCK_BUSY)

    def test_lock_busy_does_not_overwrite_active_runs_latest_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, seed = create_origin(root)
            work_root = root / "runner-work"
            latest_path = work_root / "latest.json"
            sentinel = {"status": "ACTIVE_RUN_RESULT"}
            runner.atomic_json(latest_path, sentinel)
            with runner.exclusive_lock(work_root / "account14.lock"), patch.object(
                runner, "jst_today", return_value=TARGET_DATE
            ):
                exit_code = runner.main(
                    [
                        "--date",
                        TARGET_DATE,
                        "--source-repo",
                        str(seed),
                        "--work-root",
                        str(work_root),
                    ]
                )

            self.assertEqual(exit_code, runner.LOCK_BUSY)
            self.assertEqual(json.loads(latest_path.read_text(encoding="utf-8")), sentinel)

    def test_main_writes_latest_and_preserves_each_run_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, seed = create_origin(root)
            work_root = root / "runner-work"
            arguments = [
                "--date",
                TARGET_DATE,
                "--source-repo",
                str(seed),
                "--work-root",
                str(work_root),
            ]
            with patch.dict(
                os.environ,
                {"FAKE_SCRAPE_COUNTER": str(root / "counter.txt"), "FAKE_SCRAPE_MODE": "success"},
            ), patch.object(runner, "jst_today", return_value=TARGET_DATE):
                first_exit = runner.main(arguments)
                first_latest = json.loads((work_root / "latest.json").read_text(encoding="utf-8"))
                second_exit = runner.main(arguments)
                second_latest = json.loads((work_root / "latest.json").read_text(encoding="utf-8"))

            self.assertEqual(first_exit, 0)
            self.assertEqual(first_latest["status"], "PUBLISHED")
            self.assertEqual(first_latest["scrape_runs"], 1)
            self.assertEqual(second_exit, 0)
            self.assertEqual(second_latest["status"], "REUSED")
            self.assertEqual(second_latest["scrape_runs"], 0)
            self.assertEqual(len(list((work_root / "results").glob("*.json"))), 2)

    def test_target_date_is_today_only(self):
        self.assertEqual(
            runner.validate_target_date("2026-08-27", today="2026-08-27"),
            "2026-08-27",
        )
        with self.assertRaises(runner.RunnerFailure) as caught:
            runner.validate_target_date("2026-08-26", today="2026-08-27")
        self.assertEqual(caught.exception.code, runner.DATE_REJECTED)
        self.assertEqual(caught.exception.scrape_runs, 0)


if __name__ == "__main__":
    unittest.main()
