# pinefield

Playwright-based scraper for Amazon.co.jp category listings.

Daily GitHub Actions workflows collect items from configured category shelves
(`categoriesN.yaml`) and store JSON snapshots under `data/`.

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
