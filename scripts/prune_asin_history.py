"""ASIN履歴の剪定（ver3.0・2026-07-19運用裁定：昨日の分だけ保持・他は削除）。

account1〜10の data/accountN/asin_history.json から、posted_at が
「当日(JST)または前日」以外の項目を削除する。恒久BAN（categoriesN.yamlのblocked_asins）は別機構につき無傷。
実行：python scripts/prune_asin_history.py（リポジトリ直下から）
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
# ver4.3（2026-07-24）: 人気順運転の7日窓に合わせ、保持を直近7日へ拡大（旧: 当日+前日のみ）
keep_dates = {str(today - timedelta(days=d)) for d in range(0, 7)}

for n in range(1, 11):
    p = BASE / "data" / f"account{n}" / "asin_history.json"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        skeleton = {
            "schema": "note-amazon-asin-history-v1",
            "updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
            "description": f"Single source of truth for account{n} ASIN exclusion.",
            "posted": [],
        }
        p.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")
        print(f"account{n}: ファイルなし → 空帳簿を新設")
        continue
    d = json.loads(p.read_text(encoding="utf-8-sig"))
    before = len(d.get("posted", []))
    # ver5.0: 公開リポジトリ化に伴い、帳簿は痩せた3項目のみ保持（URL・名義等の身元情報を持たない）
    d["posted"] = [
        {"asin": e.get("asin"), "status": e.get("status", "posted"), "posted_at": e.get("posted_at")}
        for e in d.get("posted", [])
        if str(e.get("posted_at", ""))[:10] in keep_dates
    ]
    d["updated_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")
    print(f"account{n}: {before}件 → {len(d['posted'])}件（当日・前日のみ保持）")

print("完了。commit/pushは運用者の手で（掟どおりファイル名指定add）。")
