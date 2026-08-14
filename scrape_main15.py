"""account15（大物家電・映像音響）スクレイピング入口。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scraper import fetch_and_save


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account15" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories15.yaml"),
        associate_tag="noteamazon15-22",
    )
