## 2026-10-26: 実販売価格8,000円以下を除外

新規取得では `price_int` と円表記の `price` が一致し、実販売価格が **8,000円を超える** 商品だけを採用する。8,000円ちょうど、価格不明、表示価格との不一致は見送り。`original_price`（元値・参考価格）からの代用や価格推測はしない。検証済みクーポンがある場合は確定した適用後価格も8,000円超が必要で、適用後価格が不明なら保留する。未検証クーポンの効果は推測しない。

既存の高い全体・棚別下限、高額順、カテゴリ対応、採用上限を維持する。補充・商品差替え・順序変更の新しい処理は追加しない。新規summaryに `selection_policy.actual_sale_price_policy: actual_sale_price_gt_8000_v1` を記録し、入力検査でも同条件を確認する。旧 `min_price: 8000` は既存契約との互換用であり、新規取得を許す境界値ではない。

凍結済み入力のバイト列、過去契約の監査、公開・予約記事、ASIN履歴は変更しない。工場側の新規Claude呼出しにも価格ガードを設け、旧入力を再利用する場合も8,000円以下の商品を新しく制作しない。新40サイトはアカウント対応・実運用接続前であり、この検査の再利用先として引き継ぐ。停止中の投稿・FC2/livedoorやスケジュールは再開しない。

## 2026-10-16: 以前の価格方針（凍結入力の読取用）

本人指定により、販売価格8,000円以上・上限なし・`price_desc`へ変更。既存棚別下限が高い場合は高い方を維持。1〜19およびPrime期間は棚別優先枠なしで検証済候補を高額順に採用する。通常20のSwitch 2/PS5対応は維持し、棚内を高額順にする。確認済候補が不足した場合は下限を下げず実在候補だけを使用する。元値18,000円条件、30%割引条件、高額商品専用の少数枠は新設しない。Prime v3を新規sourceへ使い、固定v1/v2と過去通常sourceは読取互換を保つ。投稿・予約・ASIN履歴は変更しない。10/17までの取得済みPrime v2はそのまま使用し、10/18以降の新規未固定sourceにv3を要求する。v1は従来どおり固定済みのみ。以下の旧`割引率順`記述は旧source契約の説明として保持する。

# pinefield

Playwright-based scraper for Amazon.co.jp category listings.

Daily GitHub Actions workflows collect items from configured category shelves
(`categoriesN.yaml`) and store JSON snapshots under `data/`.

For the 2026 Prime Appreciation Sale, `sale_campaign.yaml` temporarily overrides
new account1-20 acquisitions dated October 13-19 (JST), including the early sale.
All twenty sites share PC/gadget and household-electronics shelves regardless of
their normal niche; each site receives four daily shelves, two from each family.
The assignment rotates daily, and eligible products retain the existing
`sale_first` ranking, ten-item target and successful-post exclusions. The current
listed price (`price_int`, before coupon deductions) must be at least 8,000 yen;
both the search URL and parsed prices enforce this floor. Profile100 is excluded.

The overlay automatically ends at October 20 00:00 JST. Ordinary category YAML
files stay unchanged, including account20's normal game quota. A `campaign` record
in the dated scrape summary fixes the exact assignment and configuration hash;
products keep the existing thirteen-field schema. Validators, recovery and ASIN
synchronization replay this saved evidence even after the event ends. Existing
markerless snapshots retain their original policy and are never mixed with
campaign supplements. Already frozen factory sources and posted articles are
preserved. FC2/livedoor continue to reuse the selected note products.

The v2 campaign uses the all-discounts search facet only to discover candidates.
New selections require the same-ASIN visible `#dealBadge_feature_div` to say
`プライム感謝祭先行セール` (October 13-15 JST) or `プライム感謝祭`
(October 16-19 JST), in addition to the existing verified offer contract.
Ordinary discounts or coupons alone cannot enter a new v2 source. The saved
`discount_contract.products[].prime_event` is replayed against the original PDP
observation, ASIN, checked time, offer and evidence hash before Prime wording is
allowed. Observation phase and source-date phase must both match; an early-sale
observation obtained on October 15 cannot label an October 16 source as the main
sale. The transition needs an acquisition after October 16 00:00 JST, before the
factory fixes that day's source; the guard does not relax this requirement.

The v1/v2 marker registries preserve sealed historical sources under their exact
original policies. Restoring only the backed-up v1 `sale_campaign.yaml` returns
new acquisitions to the previous generic sale policy while existing v2 sources
remain readable. Restore configuration through the central backup record, after
checking later changes, without rolling back posts, source data or ASIN ledgers.

Ordinary product-detail enrichment retries only when both description and
specifications are empty. Each ASIN gets at most one extra visit, with a
15-second timeout for the whole retry and a shared 60-second retry budget per
account run. Products with either field present receive no extra visit. Retry
outcomes are logged and saved as `detail_retry` in the daily scrape summary;
remaining empty fields do not trigger a full scrape or a new product rejection.
Recovered specifications still pass through the existing identity exclusions.
Account20's separate same-visit price/time-sale/detail verification is unchanged
and does not use this retry path.

Active account axes are:

- account1-5: existing production axes
- account6: photography and cameras
- account7-8: existing production axes
- account9: furniture, interiors, and bedding
- account10: baby and childcare
- account11: cars, motorcycles, and bicycles
- account12: adult indoor games
- account13: supplements and nutrition
- account14: staple pantry foods
- account15: large appliances, video, and audio
- account16: study and stationery
- account17: golf and fishing
- account18: maker tools and electronics
- account19: tableware and kitchen tools
- account20: Nintendo Switch 2 and PS5 game software

Accounts 1-19 use `global_ranked`: eligible products from all configured
categories are ranked together with `sale_first` (discount information present,
then discount percentage, then current price, all descending). Up to two per
category are preferred; if that does not reach ten, additional ranked products
fill the shortage. Discount amount is not the ranking key. The listing URLs
retain their Browse Nodes and price constraints and add Amazon's
`p_n_deal_type:10343614051` (本日のタイムセール) filter. Products with neither a discount rate
nor an original price are excluded; this listing data is separate from proof
of current Time Sale participation.

This follows the historical sale-order rule recorded in `note-amazon-auto`'s
`REGIME_SWITCH_RUNBOOK.md` (sale mode ver3.1; earlier 34-item snapshot tag
`backup-34item-regime-20260719`) and the user's 2026-09-10 decision to restore
ranking across all categories. Current categories, price floors, ten-item
targets, and successful-post exclusions are retained.
The historical `23534876051` search facet was not selected in Amazon's live
sidebar on 2026-09-10. Amazon's own current Time Sale link selected
`10343614051` with `aria-current=true`; the search URLs therefore use that
current facet while restoring the historical ranking rule. The broader
`10343616051` (すべての割引) filter is not a substitute.

`scripts/sync_asin_history1.ps1` accepts only management-confirmed note
results, commits from an isolated `origin/main` worktree, pushes `HEAD:main`,
and verifies the exact ASIN event on the remote branch. Successful ASINs remain
excluded for 20 days; scraped-only and rejected rows are not exclusion evidence.
`global_ranked` and `category_quota` continue this normal ASIN synchronization
without advancing a category cursor. Existing `rotation_state_file` paths and
cursor files are retained but are not used by these selection modes.

The same 20-day gate also excludes a different ASIN when a validated
JAN/EAN/UPC/GTIN or an exact brand + normalized manufacturer-model key matches.
Model normalization is limited to Unicode width, letter case, whitespace, and
hyphen variants. Images, descriptions, brand-only matches, and semantic
similarity are never used. Verified product identifiers are written into the
account ASIN ledger by `scripts/sync_asin_history.py`; excluded products are
skipped and the next eligible ranked candidates are considered.

An optional `min_price` on an individual category overrides the global floor.
The scraper applies it to the Amazon search URL and validates the parsed product
price again before selection.

Accounts 10-20 use official Amazon Browse Nodes and a 3,000-yen floor.
Account20 is an intentional exception: `category_quota` selects five products
from each of its two game
software shelves, and fills a shortage from the other shelf so the target
remains ten. Candidates within each shelf use the same `sale_first` order.
Repeating the same shelf URL is neither required nor supported.
Console hardware, prepaid codes, and controllers appearing in those official
Amazon shelves are intentional valid results and must not be title-filtered.
