# 当日 source のローカル復旧

Cloud 取得が不足した際、最新 `origin/main` の `scrape_mainN.py` と既存 validator をそのまま、別の Git worktree で1回だけ実行する。通常の Cloud/Windows スケジュール、カテゴリ URL、取得順位、価格条件、ASIN除外、記事・note工程は変更しない。既定は事前確認のみ。

## 呼出し

Windows の Pinefield 作業ディレクトリから、既存の Playwright/PyYAML 等が使える Python で実行する。

```powershell
python -B scripts/recover_local_daily_source.py --account 13 --date 2026-09-10
```

実取得・検証・Git反映まで行う場合だけ `--execute --cloud-run-completed` を加える。日付は実行時の JST 当日に置き換える。省略時も JST 当日になる。

```powershell
python -B scripts/recover_local_daily_source.py --account 13 --date 2026-09-10 --execute --cloud-run-completed
```

`--cloud-run-completed` は、呼出側が対象アカウントの Cloud 実行と他の手動取得が終了済みであることを確認したという宣言であり、CLI が GitHub Jobs を自動照会するフラグではない。実行中の Cloud/手動取得と重ねて呼び出さない。同アカウント・同日は、取得開始を記録した後の再実行を拒否する。失敗しても自動再取得・無限再試行はしない。

`--repo` は正規 Pinefield のローカル Git checkout を指定できる。既定はこの script の所属 repo。元 checkout の HEAD・index・未コミット変更には触れず、fetch と一時 worktree の登録/削除だけを行う。`--timeout-seconds` は1〜3600秒、既定1800秒。

## 固定条件

- `D:\_0NOTEKATU\★全体的なワークフロー_1_N` の同日 source が既存・部分作成済みなら停止。②記事制作の p12/p34 両方の実 lock 形式（schema/token/pid/hostname/started_at/account/date/part）を取得し、正規 runner の source 固定と競合させない。既存 lock は不明・古いものでも勝手に除去しない。
- 保存先の `local-recovery-accountN/.control/accountN_DATE.lock` で重複実行を排他する。取得直前に同じ場所の `accountN_DATE.attempt.json` を排他的に作成する。このPCの別 checkout/独立 clone からも同じ記録を使い、`--repo` を変えても同日再実行できない。別PCや Cloud/他の取得プログラムとの排他は、呼出前の実行終了確認で担保する。
- 1〜19は `global_ranked/sale_first/2/10`、20は `category_quota/sale_first/5/10`、`exclude_scraped_candidates=false`、20日除外・商品識別子除外、正しいアカウントの Git 台帳パスを確認する。
- 隔離 checkout 内の当日 products/summary だけを実取得前に除去し、今回の subprocess が2ファイルを新規作成しなければ停止する。Git上に残る旧当日JSONを成功扱いにしない。
- 原13キーを保ち、現 `ensure_daily_scrape.py --validate-only` を実行する。4〜10件、account20は両カテゴリ2件以上、affiliate routing、日付、現在の selection policy を既存 validator で確認する。カテゴリごとの候補補充や同一商品の除外は現 scraper の処理だけを使う。
- 設定・台帳・rotation の hash 不変、変更ファイルが当日 products/summary の範囲だけ、同日 source の不存在、日付がまだ JST 当日であることを取得後とpush前にも検査する。

## Git 競合

取得中に main が進んだ場合、他アカウントの既定 data（products/summary/ASIN/rotation）と trigger の追加・更新だけなら、限定成果物 commit を最新 origin へ最大1回 rebaseし、再検証する。対象アカウント・共通コード・設定・validator 等の変化、削除、不明な差分は停止する。

push は非 force で1回だけ。直前に競合して拒否された場合は再送しない。応答が不確実でも fetch と commit/2ファイル SHA の読戻しだけで確認する。今回の新出力が旧内容と同一なら `UNCHANGED_VALIDATED` とし、無意味な commit/push は行わない。片方だけが変わった場合も、Git差分は指定2ファイルの範囲に限定される。

## 結果

保存先は `D:\_0NOTEKATU\scraping\YYYY-MM-DD\local-recovery-accountN\run-NN`。

`result.json` に取得基点・publish基点・確認commit、hash、件数、実行/再base/push回数、停止理由、cleanup状態を保存する。`commands.jsonl`、`scrape.log`、validatorログと、取得された products/summary も保存する。台帳は成果物フォルダへ複製せず、Git checkout 内の正本を読み、hash/件数だけ記録する。

成功時の直下 products/summary は Git index と remote 読戻しで一致した正本 bytes。Windows CRLF→Git LFの改行差が生じた場合だけ、初回取得 bytes を `raw/` に保持し、before/after SHAと改行だけの差・値と商品順序が同一であることを `representation_changes` に記録する。改行以外を変える Git filter があれば停止する。`artifact_source` が `git_index_pending_remote_confirmation` のままなら、まだ GitHub確認済み成果物とは扱わない。

`PREFLIGHT_PASS` は未取得、`PUBLISHED` はremote読戻し確認済み、`UNCHANGED_VALIDATED` は新規実取得を検証したがGit内容は同一、`STOPPED` は未完了である。cleanup失敗は元の実結果を変更せず、`cleanup_error/remaining_worktree` に残す。factory source_manifest はこのutilityでは作らず、正常なGit結果を通常の①→②で固定する。

## ローカル試験

```powershell
python -B -m unittest discover -s tests -p test_recover_local_daily_source.py -v
```

試験の Git remote と scraper subprocess はすべてローカルの架空 fixture。Amazon取得・本番GitHub pushは行わない。
