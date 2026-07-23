"""人格3 セルフケア（美容・健康） スクレイピング入口（2026-07-10 Fable建立 ver2.8）。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scraper import fetch_and_save


BASE_DIR = Path(__file__).resolve().parent


if __name__ == "__main__":
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    fetch_and_save(
        output_path=str(BASE_DIR / "data" / "account3" / f"products_{today}.json"),
        config_path=str(BASE_DIR / "categories3.yaml"),    )
