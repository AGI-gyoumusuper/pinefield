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

Accounts 10-20 use official Amazon Browse Nodes and a 3,000-yen floor.
Accounts 10-19 use `category_round_robin`, normally selecting one category
leader per shelf until ten products are collected. Account20 is an intentional
exception: `category_quota` selects five products from each of its two game
software shelves, and fills a shortage from the other shelf so the target
remains ten. Repeating the same shelf URL is neither required nor supported.
Console hardware, prepaid codes, and controllers appearing in those official
Amazon shelves are intentional valid results and must not be title-filtered.
