"""account6（撮影・カメラ／ふーけい）スクレイピング入口。"""

from pathlib import Path

from scraper import fetch_and_save
from scrape_target_date import resolve_target_date


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = resolve_target_date("account6", BASE_DIR)
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account6" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories6.yaml"),
        associate_tag="noteamazon6-22",
    )
