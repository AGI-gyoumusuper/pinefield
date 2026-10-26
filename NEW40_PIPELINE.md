# New40 isolated selection and history

The new40 workflow has a separate Git history namespace. Existing account1–20
selection, history, queries and schedules are unchanged when the optional
new40 context is absent. This change does not enable any schedule or publisher.

## Canonical state

- Repository: `AGI-gyoumusuper/pinefield`, branch `main`.
- Sole new40 ASIN history: `data/new40/asin_history.json`.
- History schema: `note40-asin-history-v1`, scope `new40`, array `posted`.
- New40 sources: `data/new40/sources/accountN/products_YYYY-MM-DD.json` and
  `scrape_summary_YYYY-MM-DD.json`. Here `accountN` is the **source category**,
  never the new destination account. Source configs remain `categoriesN.yaml`.
- New destinations remain the explicitly confirmed bindings for N01–N40 to
  account201–240. A source number must never be passed to legacy publication sync.

History reads use `git show <full-commit>:data/new40/asin_history.json` in memory.
Missing or invalid history stops the new40 path; there is no empty fallback and
no read of legacy ledgers. Do not export a ledger copy into scraping artifacts.

## Before selection

`new40_history.read_new40_selection_context(repo, commit, source_account,
reference_date=day, within_days=20, include_product_identities=True)` validates
the pinned Git document and constructs the exclusion registry. The actual
window and identity flag come from the unchanged source configuration. Verified
published/reserved ASINs from all new40 destinations are excluded; drafts,
failures and merely scraped candidates are never written as publications.

`new40_fetch.run_new40_fetch(repo, commit, source_account, day)` defaults to a
dry run. Only an explicit `execute_fetch=True` (CLI `--execute-fetch`) enters the
network fetch. `scraper.fetch_products` receives the validated context before
search and ranking, and does not read a legacy history or rotation cursor.
The wrapper never calls legacy `fetch_and_save` or `save_scraped_asins_to_history`.

The new40 wrapper saves products in returned order, with `affiliate_url: null`.
These are unbound products, not affiliate-ready article inputs. It preserves
the discount/coupon evidence and records `summary.new40_selection` (the exact
public context evidence) and `summary.source_config` (Git path/commit/SHA256).
Both dated files are write-once; an existing file prevents a repeat fetch.
Commit the source files before supplying their resulting commit to allocation.

## Allocation and freezing

The local canonical adapter is
`D:/_0NOTEKATU/factory-runtime/new40_product_input.py`. It consumes the full
40-slot plan and confirmed destination bindings. Source positions are counted
across N01–N40, including disabled B slots. A-only operation never reallocates B
positions. Duplicate ASINs, missing products, unknown prices and used products
are held at their original position. There is no refill, swap or compaction.

Production intake accepts only the new40 source namespace. It reads back both
the preselection history commit and the current source commit's history, checks
the configuration fingerprint, and holds any source position used since
selection. Legacy saved products are available only as labelled offline samples.

Canonical affiliate URLs remain null until exact destination/ASIN/Store/Tracking
readback evidence is verified. Old affiliate URLs are provenance only. The
adapter never synthesizes or changes a tag. The verified URL uses
`https://www.amazon.co.jp/dp/ASIN` or
`https://www.amazon.co.jp/gp/product/ASIN` (optional trailing slash), with unique
query keys and the exact dedicated Tracking tag. Store and Tracking must differ.
Bare `amazon.co.jp`, duplicate parameters and the original source URL are held,
matching the publisher input contract; the adapter does not rewrite them.
Final manifests are write-once; pending previews must not be used as the filename
for a later production freeze.
`input_ready` describes verified article input only. `publish_ready` remains
false because the new40 affiliate publisher requires a separate integration.

## After verified publication or reservation

The NOTEAPI owner calls the pure function
`new40_history.merge_verified_publication(history, receipt, manifest)`.
It returns a new history object; it does not write files, publish, commit or push.
Only the exact frozen slot, source position, ASIN and destination are accepted.

Receipt schema is `note40_verified_publication.v1`. Required keys are
`planning_slot`, `destination_account`, `source_account`, `source_position`,
`asin`, `note_key`, `note_url`, `status` (`published` or `reserved`),
`published_at` or `reserved_at`, `source_commit`, `manifest_sha256`, and
`verification`. The verification object has schema `note40_note_readback.v1`,
status `VERIFIED_READBACK`, the matching `destination_account`, `note_key`,
`note_url`, `publication_status`, `management_list_confirmed: true`,
`identity_verified: true`, and timezone-bearing `checked_at`.

`manifest_sha256` is the digest stored **inside** the manifest: SHA256 of its
body after removing only the top-level `manifest_sha256` key, encoded as UTF-8
JSON with recursively sorted keys, no ASCII escaping, separators `,`/`:`, and
no NaN. Use the Python validator to avoid cross-language serialization drift.
It is not the SHA of the pretty-printed file. A writer envelope must carry the
actual file fingerprint separately as `manifest_file_sha256`, together with an
absolute `manifest_path`, `planning_slot`, and `source_commit`. The file-loading
layer verifies the file fingerprint; the pure merge verifies the body digest
and its exact receipt bindings. Substituting the file SHA for the body digest
is rejected.

The merge adds `allocation_date`, `source_products_sha256` and usable product
identity from the manifest. It is idempotent for the same receipt. It rejects
reassignment of a note key or daily slot and rejects downgrading published state.

The publication owner must then:

1. Read fresh canonical Git history in an isolated checkout, merge the verified
   receipt, and change **only** the new40 history path.
2. Commit and push normally; on a competing update, reread main and remerge.
   Never force push or overwrite another receipt.
3. Read back the resulting remote commit/path and verify the exact receipt
   before declaring sync complete.
4. Supply that or a descendant commit to the next new40 selection. An offline
   chain test exercises this exclusion path; it is not a live publication test.

Do not roll back publication history during code restoration. The initial empty
document is not permission to overwrite later receipts. No new40 live scrape,
publication or schedule execution was performed as part of this implementation.
