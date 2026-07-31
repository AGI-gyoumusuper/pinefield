# note投稿後のASIN記帳契約

- note予約・投稿を実行した作業は、管理APIで実在を確認した商品のASINを `data/accountN/asin_history.json` へ同期するまで完了ではない。
- 同期には `scripts/sync_asin_history1.ps1` を使う。`status=reserved` は `reserved_list_confirmed=true` の行だけを記帳する。
- スクレイプしただけ、下書き、失敗、却下、スキップの商品は投稿実績として記帳しない。
- 通常の人気順運転では元の商品JSONを `-ProductJson` で渡し、実際に使用したカテゴリまで照合して `category_rotation.json` を進める。
- Git反映は `HEAD:main` で行い、`origin/main` の履歴に対象ASINが存在することを確認して初めて成功とする。
- 同期失敗時にnoteの予約を取り消さない。ただし作業は未完了として終了し、同期を復旧する。
