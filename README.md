# pinefield

Playwright-based scraper for Amazon.co.jp category listings.

Daily GitHub Actions workflows collect items from configured category shelves
(`categoriesN.yaml`) and store JSON snapshots under `data/`.

Popular-ranking mode selects the highest-review eligible product from each
category. Output order resumes after the category recorded in
`data/accountN/category_rotation.json`, wraps at the actual configured category
count, and is advanced by `scripts/sync_asin_history1.ps1` only after products
are actually posted or reserved.

`scripts/sync_asin_history1.ps1` accepts only management-confirmed note
results, commits from an isolated `origin/main` worktree, pushes `HEAD:main`,
and verifies the exact ASIN event and category cursor on the remote branch.
Popular-ranking configs retain those successful ASINs for 20 days; scraped-only
and rejected rows are not exclusion evidence.

The same 20-day gate also excludes a different ASIN when a validated
JAN/EAN/UPC/GTIN or an exact brand + normalized manufacturer-model key matches.
Model normalization is limited to Unicode width, letter case, whitespace, and
hyphen variants. Images, descriptions, brand-only matches, and semantic
similarity are never used. Verified product identifiers are written into the
account ASIN ledger by `scripts/sync_asin_history.py`; an excluded category
leader is replaced by the next ranked product from that category.

An optional `min_price` on an individual category overrides the global floor.
The scraper applies it to the Amazon search URL and validates the parsed product
price again before category-leader selection.
