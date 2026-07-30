import json
import tempfile
import unittest
from pathlib import Path

from scraper import Product, filter_and_sort
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


if __name__ == "__main__":
    unittest.main()
