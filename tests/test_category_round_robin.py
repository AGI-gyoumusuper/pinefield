import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scraper import Product, filter_and_sort, search_url_with_min_price
from scripts.update_category_rotation import build_updated_state


def categories(count: int) -> list[dict]:
    return [
        {
            "name": f"C{index}",
            "url": f"https://www.amazon.co.jp/s?rh=n%3A{1000 + index}",
        }
        for index in range(1, count + 1)
    ]


def product(category_number: int, rank: int, reviews: int) -> Product:
    asin = f"B{category_number:02d}{rank:02d}ABCDE"[:10]
    return Product(
        asin=asin,
        title=f"C{category_number} product {rank}",
        price="￥1,000",
        price_int=1000,
        original_price="",
        discount_rate="",
        image_url="https://example.com/image.jpg",
        affiliate_url=f"https://www.amazon.co.jp/dp/{asin}",
        category=f"C{category_number}#{rank}",
        rating="4.5",
        review_count=str(reviews),
    )


def candidate_pool(category_count: int) -> list[Product]:
    result = []
    for number in range(1, category_count + 1):
        result.append(product(number, 1, 10))
        result.append(product(number, 2, 100 + number))
    return result


class CategoryRoundRobinTests(unittest.TestCase):
    def select(self, category_count: int, max_total: int, state: dict | None = None):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "category_rotation.json"
            if state is not None:
                state_path.write_text(json.dumps(state), encoding="utf-8")
            return filter_and_sort(
                candidate_pool(category_count),
                min_price=800,
                sort_order="review_desc",
                max_total=max_total,
                posted_asins=set(),
                max_per_category=1,
                selection_mode="category_round_robin",
                categories=categories(category_count),
                rotation_state_file=str(state_path),
            )

    def test_nine_categories_returns_nine_without_fixed_ten_assumption(self):
        selected = self.select(9, 10)
        self.assertEqual(9, len(selected))
        self.assertEqual([f"C{i}" for i in range(1, 10)], [p.category.split("#")[0] for p in selected])

    def test_eleven_categories_resume_after_saved_sixth_category(self):
        selected = self.select(
            11,
            4,
            {
                "last_category_key": "node:1006",
                "last_category_name": "C6",
            },
        )
        self.assertEqual(["C7", "C8", "C9", "C10"], [p.category.split("#")[0] for p in selected])

    def test_twelve_categories_wrap_to_first(self):
        selected = self.select(
            12,
            4,
            {
                "last_category_key": "node:1010",
                "last_category_name": "C10",
            },
        )
        self.assertEqual(["C11", "C12", "C1", "C2"], [p.category.split("#")[0] for p in selected])

    def test_saved_next_category_survives_removal_of_last_category(self):
        selected = self.select(
            9,
            3,
            {
                "last_category_key": "node:9999",
                "last_category_name": "removed",
                "next_category_key": "node:1007",
                "next_category_name": "C7",
                "next_category_position": 7,
            },
        )
        self.assertEqual(["C7", "C8", "C9"], [p.category.split("#")[0] for p in selected])

    def test_highest_review_product_is_selected_inside_each_category(self):
        selected = self.select(9, 9)
        self.assertTrue(all(product_item.category.endswith("#2") for product_item in selected))

    def test_posted_asin_is_removed_before_category_leader_selection(self):
        posted = product(1, 2, 101).asin
        selected = filter_and_sort(
            candidate_pool(9),
            min_price=800,
            sort_order="review_desc",
            max_total=9,
            posted_asins={posted},
            max_per_category=1,
            selection_mode="category_round_robin",
            categories=categories(9),
        )
        self.assertEqual(product(1, 1, 10).asin, selected[0].asin)

    def test_category_min_price_is_enforced_before_leader_selection(self):
        cheap_high_review = product(1, 2, 1000)
        cheap_high_review.price_int = 6000
        eligible_lower_review = product(1, 1, 100)
        eligible_lower_review.price_int = 8000
        selected = filter_and_sort(
            [cheap_high_review, eligible_lower_review],
            min_price=800,
            max_total=1,
            selection_mode="category_round_robin",
            categories=categories(1),
            category_min_prices={"C1": 7000},
        )
        self.assertEqual(eligible_lower_review.asin, selected[0].asin)

    def test_category_min_price_is_added_to_amazon_search_url(self):
        url = search_url_with_min_price(
            "https://www.amazon.co.jp/s?rh=n%3A2151977051&s=exact-aware-popularity-rank",
            7000,
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(["n:2151977051,p_36:700000-"], query["rh"])

    def test_posting_sync_saves_position_six_and_next_position_seven(self):
        config_categories = categories(11)
        asins = [product(number, 2, 100 + number).asin for number in range(1, 7)]
        asin_categories = {asin: f"C{number}" for number, asin in enumerate(asins, 1)}
        state, matched = build_updated_state(config_categories, {}, asins, asin_categories)
        self.assertEqual(asins, matched)
        self.assertEqual(6, state["last_category_position"])
        self.assertEqual(7, state["next_category_position"])
        self.assertEqual("node:1006", state["last_category_key"])
        self.assertEqual("node:1007", state["next_category_key"])

    def test_category_quota_takes_five_from_each_game_shelf(self):
        products = [product(category, rank, 1000 - rank) for category in (1, 2) for rank in range(1, 8)]
        selected = filter_and_sort(
            products,
            min_price=800,
            sort_order="review_desc",
            max_total=10,
            max_per_category=5,
            selection_mode="category_quota",
            categories=categories(2),
        )
        counts = {
            name: sum(item.category.split("#")[0] == name for item in selected)
            for name in ("C1", "C2")
        }
        self.assertEqual({"C1": 5, "C2": 5}, counts)
        self.assertEqual(10, len({item.asin for item in selected}))

    def test_category_quota_fills_short_shelf_from_other_shelf(self):
        products = [product(1, rank, 1000 - rank) for rank in range(1, 3)]
        products += [product(2, rank, 900 - rank) for rank in range(1, 10)]
        selected = filter_and_sort(
            products,
            min_price=800,
            sort_order="review_desc",
            max_total=10,
            max_per_category=5,
            selection_mode="category_quota",
            categories=categories(2),
        )
        counts = {
            name: sum(item.category.split("#")[0] == name for item in selected)
            for name in ("C1", "C2")
        }
        self.assertEqual({"C1": 2, "C2": 8}, counts)


if __name__ == "__main__":
    unittest.main()
