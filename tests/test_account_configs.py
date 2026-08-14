import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COUNTS = {
    9: 23,
    10: 15,
    11: 33,
    12: 13,
    13: 15,
    14: 15,
    15: 14,
    16: 14,
    17: 17,
    18: 15,
    19: 17,
    20: 2,
}


def load_config(account: int) -> dict:
    with (ROOT / f"categories{account}.yaml").open("r", encoding="utf-8-sig") as file:
        return yaml.safe_load(file)


def node_id(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    rh = unquote(query.get("rh", [""])[0])
    match = re.search(r"(?:^|,)n:(\d+)(?:,|$)", rh)
    if not match:
        raise AssertionError(f"official Browse Node missing: {url}")
    return match.group(1)


class AccountConfigTests(unittest.TestCase):
    def test_final_category_counts_and_official_node_urls(self):
        for account, expected_count in EXPECTED_COUNTS.items():
            config = load_config(account)
            categories = config["categories"]
            self.assertEqual(expected_count, len(categories), f"account{account}")
            for category in categories:
                self.assertTrue(category.get("name"), f"account{account}")
                self.assertEqual(10, category.get("max_items"), category.get("name"))
                self.assertIs(True, category.get("is_search"), category.get("name"))
                parsed = urlparse(category["url"])
                self.assertEqual("www.amazon.co.jp", parsed.netloc)
                self.assertEqual("/s", parsed.path)
                self.assertEqual(["exact-aware-popularity-rank"], parse_qs(parsed.query).get("s"))
                node_id(category["url"])

    def test_new_accounts_use_three_thousand_yen_floor(self):
        for account in range(10, 21):
            config = load_config(account)
            self.assertEqual(3000, config["filters"]["min_price"], f"account{account}")
            for category in config["categories"]:
                rh = unquote(parse_qs(urlparse(category["url"]).query)["rh"][0])
                self.assertIn("p_36:300000-", rh, f"account{account} {category['name']}")

    def test_selection_modes_match_the_approved_design(self):
        for account in range(10, 20):
            filters = load_config(account)["filters"]
            self.assertEqual("category_round_robin", filters["selection_mode"])
            self.assertEqual(1, filters["max_per_category"])
            self.assertEqual(10, filters["max_total_items"])
        filters = load_config(20)["filters"]
        self.assertEqual("category_quota", filters["selection_mode"])
        self.assertEqual(5, filters["max_per_category"])
        self.assertEqual(10, filters["max_total_items"])

    def test_all_active_nodes_are_unique(self):
        owners: dict[str, tuple[int, str]] = {}
        for account in range(1, 21):
            for category in load_config(account)["categories"]:
                node = node_id(category["url"])
                self.assertNotIn(
                    node,
                    owners,
                    f"node {node}: account{account}/{category['name']} and {owners.get(node)}",
                )
                owners[node] = (account, category["name"])

    def test_manual_removals_stay_removed_and_additions_are_present(self):
        selected = {
            account: {node_id(category["url"]) for category in load_config(account)["categories"]}
            for account in range(9, 21)
        }
        removed = {
            11: {"2045249051", "2045203051", "3891441051"},
            12: {"2189605051", "5361910051", "22013663051"},
            15: {"2359695051"},
            16: {"89426051"},
            17: {"87783051", "15320261"},
        }
        for account, nodes in removed.items():
            self.assertTrue(nodes.isdisjoint(selected[account]), f"account{account}")
        account4_nodes = {node_id(category["url"]) for category in load_config(4)["categories"]}
        self.assertNotIn("15334611", account4_nodes)
        self.assertIn("15334611", selected[11])
        self.assertNotIn("5341881051", selected[9])
        self.assertTrue({"5341882051", "2378230051"}.issubset(selected[9]))
        self.assertIn("15322441", selected[17])
        self.assertTrue({"2285020051", "14616589051"}.issubset(selected[19]))
        self.assertEqual({"206233864051", "8019286051"}, selected[20])

    def test_account_files_exist_and_history_is_valid(self):
        for account in range(10, 21):
            self.assertTrue((ROOT / f"scrape_main{account}.py").is_file())
            self.assertTrue((ROOT / ".github" / "triggers" / f"account{account}.json").is_file())
            if account >= 11:
                workflow = ROOT / ".github" / "workflows" / f"scrape{account}.yml"
                self.assertTrue(workflow.is_file())
                self.assertIn(
                    f'data/account{account}/asin_history.json',
                    workflow.read_text(encoding="utf-8"),
                )
            history_path = ROOT / "data" / f"account{account}" / "asin_history.json"
            history = json.loads(history_path.read_text(encoding="utf-8-sig"))
            self.assertEqual("note-amazon-asin-history-v1", history["schema"])
            self.assertIsInstance(history["posted"], list)


if __name__ == "__main__":
    unittest.main()
