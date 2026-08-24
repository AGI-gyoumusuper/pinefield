"""人格5 食品・酒・消耗品（セール番長） スクレイピング入口（2026-07-10 Fable建立 ver2.8）。"""

from pathlib import Path

from scraper import fetch_and_save
from scrape_target_date import resolve_target_date


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = resolve_target_date("account5", BASE_DIR)
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account5" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories5.yaml"),
        associate_tag="noteamazon5-22",
    )
