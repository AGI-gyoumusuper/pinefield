# 日次スクレイピングの不足補完

夜20:00〜23:10の既存Windowsタスク20件は、翌日分のGitHub取得を10分間隔で起動します。タスクの終了コード0はGitHubへの起動要求の送信成功です。商品取得の完了は、GitHub上の当日商品JSONとsummaryが現行検査に合格した時点で判断します。

`pinefield-local-recovery-all` は毎日07:00から18:30まで30分間隔で起動し、当日分の20工場を監査します。GitHubの通常取得・Ensure・Late Repairの終了を確認できた場合だけ、不足工場をPC側で順番に補完します。PCが停止していた場合は、起動可能になってから実行します。Windowsへログインしておく必要があります。

- 有効な取得済みデータは再取得・上書きしません。
- GitHubが実行中、終了証拠が不足、APIに接続できない場合は取得せず、次回起動で再確認します。
- 既存の工場source固定、アカウント別ロック、同一アカウント・日付の1回限りの取得記録を守ります。
- 1工場が失敗しても、独立した残りの工場を処理します。
- 通常工場は4〜10商品、20番はSwitch 2とPS5を各2商品以上含む4〜10商品という現行検査を適用します。
- 同時起動はしません。1回の処理は3時間で新規工場の開始を止め、実行中の1工場を終えてから次回へ引き継ぎます。
- 結果は `D:\_0NOTEKATU\scraping\YYYY-MM-DD\daily-recovery-controller\run-NN\result.json` に保存します。個別の取得物は同日付配下の `local-recovery-accountN\run-NN` に保存します。ASIN台帳は成果物フォルダへ複製しません。

設定の再登録は、正本リポジトリの `scripts\Install-LocalScrapeRecoveryTask.ps1` を実行します。手動の読み取り確認は `python scripts/recover_missing_daily_sources.py`、不足分の実行は同コマンドに `--execute` を付けます。

GitHub側の失敗候補と公開検索ページの診断は、当該Actions実行のartifactに3日間保存します。候補データを正本へ反映するのは、従来どおり検査に合格した場合だけです。
