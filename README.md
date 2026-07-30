# pinefield

Playwright-based scraper for Amazon.co.jp category listings.

Daily GitHub Actions workflows collect items from configured category shelves
(`categoriesN.yaml`) and store JSON snapshots under `data/`.

Popular-ranking mode selects the highest-review eligible product from each
category. Output order resumes after the category recorded in
`data/accountN/category_rotation.json`, wraps at the actual configured category
count, and is advanced by `scripts/sync_asin_history1.ps1` only after products
are actually posted or reserved.
