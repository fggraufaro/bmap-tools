# Rate Radar: how a rate's source and trust are decided (rate integrity)

DRAFT on `fix/rate-radar-accuracy`, nothing deployed. Triggered by the first real run through the Hub (run 20261117_103944, 4 banks), where every saved value was compared with the bank's own public page.
Code: `rate_integrity.py` (pure rules, unit tested), hooks in `scrapers.py` / `storage.py` / `extraction.py`, tests in `tests/integrity` (`python -m unittest discover -s tests/integrity`, then `python tests/integrity/run_mutations.py`).

## What went wrong (traced in the code)

| Symptom | Cause |
| --- | --- |
| A money-market value read by AI vision from a screenshot was saved `site` / `high` with the CD page as its source | `money_market` had no source URL of its own (hard-coded `None` in `build_rate_observations`; the crawler tracked it and dropped it), so it inherited the bank's general `source_url` (the first page read) and any `http` URL meant `site`, which meant `high` |
| Search-filled checking and high-yield values pointed at the CD page | the search merge recorded no source; `str(specific_url or source_url)` stamped the first page read |
| Savings read by AI vision was labelled `search` | `"savings from search" in note` also matched inside `"high_yield_savings from search"` |
| AI-vision values were `high` | `_confidence` mapped `ai_vision` to `high`; nothing checked that the number is on the page. A cached vision answer for one savings page contained a whole CD ladder that is on no page of that bank |
| The rates on a page were missed | only `inner_text` (visible text) was read; hidden or collapsed blocks are in `textContent` only (one bank savings page: 724 visible characters with no rates against 39,000 with 61 rates); `harvest_hidden_text` ran only if the page had NO rate at all. The patterns also needed `N.NN% APY` within 60 to 80 characters of the product word, which tiered blocks ("$10,000 or less -- 0.05% APY") and header-labelled tables ("Savings Accounts ($2,499 or less) 0.12% 0.12%") do not have |
| No consistent tier | DOM regex: best pattern tier then the HIGHEST value; vision prompt: "HIGHEST advertised APY"; search prompt: no rule; across pages: highest wins |
| A site branded for another bank was accepted | no identity check (`detect_rebrand` only lowered the status; `rate_observations` has no flag column) |
| A bank that says "inquire for rates" got search values | no "not published" outcome; the search merge fills any gap |

## The rules now

1. **Source and method are recorded when the value is assigned**, per product (money market included): `site` (read by the DOM extractor from that page's text), `ai_vision`, `aggregator`, `search`, `last_known_good`. They are never reconstructed from a note and never borrow another product's or the general URL. A value with no recorded source is `search` / `low` with no URL and the flag `source_unknown`.
2. **Confidence.** `high` only for a `site` value read by the DOM extractor from the product's own page on the bank's own domain, on a page that names the bank. An `ai_vision` value is at most `medium`, and only when its number is present in that page's `textContent`; a vision number the page does not contain is DROPPED when the page shows rates (kept `low`, flagged `not_in_page_text`, only when the page has no readable rates, for example an image-only page). `search` and `last_known_good` are always `low`; `aggregator` is `medium`. A value the person confirmed in the verify UI keeps its existing treatment (`site` / `high` / manually verified).
3. **Tier rule, ONE setting.** `RATE_RADAR_TIER_RULE` (environment variable, read at call time, default `entry`; any other value falls back to the default):
   - `entry` (default, the proposal): the rate that applies at the product's minimum opening balance; with no stated minimum, the lowest-balance tier;
   - `highest`: the highest advertised rate for the product (its top tier).
   Either way the other tiers are listed in `tier_note` and the value carries the `tiered` flag. The active rule is quoted in the vision and search prompts and applied by the page parser (`extract_tiered_products`), whose result wins over a generic pattern match. It is a product decision (Francisco's): the answer is a one-line change of this setting, not a rework.
4. **Identity.** `check_identity` compares the bank's distinctive name tokens with the page title, `og:site_name`, the full page text and the bank's domain ("South State" and "Southstate" agree). A page with text that never names the bank is `mismatch`: its values are `low` with the flag `identity_mismatch` (the bank on file may still be right, as with a bank whose FDIC website is shared with a sibling brand, so it is a review flag, not a deletion).
5. **Not published.** "Interest Bearing: Inquire for Rates", "rates available upon request", "call for rates" (per product, or for all) are recorded on the result (`not_published`, with the evidence sentence and its page) and in the note. A search, aggregator, vision, chat or last-known value is never used to fill a product the bank says it does not publish, and `rate_observations` gets no row for it. A product the site DID give a value for is published after all. The outcome is stored in its own small table, `public.rate_not_published` (one row per run, bank and product; product `all` = every product), written after the rates of the run.
   Why a separate table and not a flag on the bank-run row: `rate_observations.apy` is NOT NULL, so the outcome cannot be a row there; `raw.raw_rate_radar` is the legacy wide row, one per bank and run, which cannot say WHICH product ("checking yes, savings no") without another array column on a legacy table the views read; and a small additive table keeps `rate_observations` a table of observed numbers, lets the Hub show "does not publish its checking rate" next to the rates, and is dropped cleanly by the rollback.
6. **Hidden text.** On a rate page that is still missing a product, the full `textContent` is read as well and fills only the gaps.
7. **Standing rules kept.** NerdWallet is never collected (the search prompt refuses it and a result that cites it is discarded); `robots.txt` is not bypassed (no crawl behaviour changed); the manual-confirm path is unchanged; nothing in `rate_observations` is rewritten.

## Database

`sql/RR01_Apply.sql`, `RR01_Verify.sql`, `RR01_Rollback.sql` (DRAFT, not applied; dry-run on a scratch PostgreSQL: applies, a second apply is refused, `service_role` can upsert, `anon` is refused, a bad product is rejected, rollback restores 18 columns) add, in one transaction:
- `public.rate_observations.review_flags text[] not null default '{}'` and `tier_note text` (additive; existing rows keep their values and get an empty array);
- the new table `public.rate_not_published` (row security on, no policy, `service_role` only).
The code checks that each object exists before writing to it (once per process), so the collector can be deployed before or after RR01. Without them the same rate rows are written as before, with the corrected `source_url`, `extraction_method` and `confidence`; not-published outcomes are then only logged.

## Not in this change

Re-reading history (existing rows are untouched; Francisco is correcting the affected ones by hand), `ai_agent_crawl` (its results carry no per-value provenance, so they take the conservative legacy path), and a lookup of the bank's website on the FDIC list (the page-name check is what catches a sibling brand).
