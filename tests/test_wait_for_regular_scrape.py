import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import wait_for_regular_scrape as wait


def run(identifier=1, status="queued"):
    return {"id": identifier, "status": status, "created_at": "2020-01-01T00:00:00Z"}


def response(rows=(), total=None):
    return {"total_count": len(rows) if total is None else total, "workflow_runs": list(rows)}


class WorkflowRoutingTests(unittest.TestCase):
    def test_accounts_one_through_five_use_shared_workflow(self):
        self.assertEqual("scrape.yml", wait.workflow_for_account("account1"))
        self.assertEqual("scrape.yml", wait.workflow_for_account("account5"))

    def test_accounts_six_through_twenty_use_dedicated_workflows(self):
        self.assertEqual("scrape6.yml", wait.workflow_for_account("account6"))
        self.assertEqual("scrape20.yml", wait.workflow_for_account("account20"))

    def test_unknown_account_is_rejected(self):
        with self.assertRaises(ValueError):
            wait.workflow_for_account("account21")


class ActiveRunQueryTests(unittest.TestCase):
    def test_every_active_state_is_checked_without_an_age_cutoff(self):
        def api(url):
            query = parse_qs(urlparse(url).query)
            self.assertNotIn("created", query)
            state = query["status"][0]
            return response([run(wait.ACTIVE_RUN_STATUSES.index(state) + 1, state)])

        with patch.object(wait, "api_json", side_effect=api) as api_mock:
            active = wait.active_regular_runs("account6")
        self.assertEqual({row["status"] for row in active}, set(wait.ACTIVE_RUN_STATUSES))
        self.assertEqual(api_mock.call_count, len(wait.ACTIVE_RUN_STATUSES))
        self.assertTrue(all("/scrape6.yml/runs?" in call.args[0] for call in api_mock.call_args_list))

    def test_more_than_twenty_runs_and_second_page_are_not_missed(self):
        def api(url):
            query = parse_qs(urlparse(url).query)
            if query["status"] != ["queued"]:
                return response()
            if query["page"] == ["1"]:
                return response([run(n) for n in range(1, 101)], total=101)
            return response([run(101)], total=101)

        with patch.object(wait, "api_json", side_effect=api) as api_mock:
            active = wait.active_regular_runs("account7")
        self.assertEqual({row["id"] for row in active}, set(range(1, 102)))
        self.assertEqual(api_mock.call_count, len(wait.ACTIVE_RUN_STATUSES) + 1)

    def test_shared_workflow_manual_or_other_account_stays_conservatively_active(self):
        candidate = {**run(), "event": "workflow_dispatch", "display_title": "unproven account"}
        with patch.object(wait, "api_json", return_value=response([candidate])) as api_mock:
            self.assertEqual(wait.active_regular_runs("account1"), [candidate])
        self.assertTrue(all("/scrape.yml/runs?" in call.args[0] for call in api_mock.call_args_list))

    def test_current_run_and_rows_that_just_completed_are_ignored(self):
        with patch.object(wait, "CURRENT_RUN_ID", "1"), patch.object(
            wait, "api_json", return_value=response([run(1), run(2, "completed")])
        ):
            self.assertEqual(wait.active_regular_runs("account2"), [])

    def test_invalid_metadata_fails_closed(self):
        for payload in (
            None, [], {}, {"total_count": True, "workflow_runs": []},
            {"total_count": -1, "workflow_runs": []},
            {"total_count": 1, "workflow_runs": {}},
            response([{}]), response([run(True)]), response([run(0)]),
            response([run(status="unknown")]), response([run()], total=0),
        ):
            with self.subTest(payload=payload), patch.object(wait, "api_json", return_value=payload):
                with self.assertRaises(wait.RunStateUnavailable):
                    wait.active_regular_runs("account8")

    def test_missing_pages_and_beyond_bound_are_unknown_not_empty(self):
        for payload in (response([run()], total=101), response([], total=1001)):
            with self.subTest(payload=payload), patch.object(wait, "api_json", return_value=payload):
                with self.assertRaises(wait.RunStateUnavailable):
                    wait.active_regular_runs("account9")

    def test_duplicate_pages_cannot_prove_complete_inventory(self):
        payload = response([run(n) for n in range(1, 101)], total=200)
        with patch.object(wait, "MAX_RUN_PAGES", 2), patch.object(wait, "api_json", return_value=payload):
            with self.assertRaisesRegex(wait.RunStateUnavailable, "pagination_limit"):
                wait.active_regular_runs("account10")


class WaitOutcomeTests(unittest.TestCase):
    def test_missing_token_stops_before_api(self):
        with patch.object(wait, "TOKEN", ""), patch.object(wait, "active_regular_runs") as query:
            self.assertEqual(wait.main(["--account", "account1"]), 1)
        query.assert_not_called()

    def test_api_failure_malformed_response_and_timeout_fail_closed(self):
        for exc in (HTTPError("https://example.invalid", 403, "denied", {}, None),
                    URLError("unavailable"), TimeoutError(), ValueError("malformed json"),
                    wait.RunStateUnavailable("invalid metadata")):
            with self.subTest(error=type(exc).__name__), patch.object(wait, "TOKEN", "fixture"), \
                 patch.object(wait, "api_json", side_effect=exc), patch.object(wait.time, "sleep") as sleep:
                self.assertEqual(wait.main(["--account", "account1"]), 1)
                sleep.assert_not_called()

    def test_active_after_wait_limit_returns_failure_without_extra_sleep(self):
        with patch.object(wait, "TOKEN", "fixture"), patch.object(wait, "MAX_WAIT_SECONDS", 10), \
             patch.object(wait.time, "monotonic", side_effect=[0, 10]), \
             patch.object(wait, "active_regular_runs", return_value=[run()]) as query, \
             patch.object(wait.time, "sleep") as sleep:
            self.assertEqual(wait.main(["--account", "account1"]), 1)
        query.assert_called_once_with("account1")
        sleep.assert_not_called()

    def test_confirmed_completion_after_poll_allows_repair(self):
        with patch.object(wait, "TOKEN", "fixture"), patch.object(wait, "MAX_WAIT_SECONDS", 100), \
             patch.object(wait.time, "monotonic", side_effect=[0, 1]), \
             patch.object(wait, "active_regular_runs", side_effect=[[run()], []]) as query, \
             patch.object(wait.time, "sleep") as sleep:
            self.assertEqual(wait.main(["--account", "account1"]), 0)
        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(wait.POLL_SECONDS)

    def test_confirmed_empty_inventory_allows_repair(self):
        with patch.object(wait, "TOKEN", "fixture"), patch.object(wait, "api_json", return_value=response()), \
             patch.object(wait.time, "sleep") as sleep:
            self.assertEqual(wait.main(["--account", "account20"]), 0)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
