"""account17（ゴルフ・釣り）スクレイピング入口。"""

from pathlib import Path

from scraper import fetch_and_save
from scrape_target_date import resolve_target_date


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = resolve_target_date("account17", BASE_DIR)
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account17" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories17.yaml"),
        associate_tag="noteamazon17-22",
    )
