"""人格3 セルフケア（美容・健康） スクレイピング入口（2026-07-10 Fable建立 ver2.8）。"""

from pathlib import Path

from scraper import fetch_and_save
from scrape_target_date import resolve_target_date


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = resolve_target_date("account3", BASE_DIR)
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account3" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories3.yaml"),
        associate_tag="noteamazon3-22",
    )
