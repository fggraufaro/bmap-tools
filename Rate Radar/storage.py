# ── Storage: Supabase push + local export ────────────────────────────────────
# Everything that persists a completed crawl run: the legacy wide-table push
# (raw.raw_rate_radar), the tidy per-product dual-write (rate_observations +
# rate_radar_runs), the bank_registry/url_pattern_registry learning loop, the
# extraction_cache, and local CSV/log auto-save. All Supabase-facing — no
# Playwright, no page objects. Reads/writes crawl_state (imported from
# shared.py, not owned here) purely for progress logging.

import csv
import re
import hashlib
import json
from datetime import datetime
from urllib.parse import quote

try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

from shared import (
    SUPABASE_URL, SUPABASE_KEY, crawl_state,
    SUPABASE_CALL_TIMEOUT_S, CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_PROBE_EVERY,
    _estimate_cost_usd, RATE_PATHS, BANK_EXTRA_URLS,
    EXPORTS, FIELDS, SB_NUMERIC_FIELDS,
)
from extraction import extract_domain, _parse_term_months
from rate_integrity import resolve, legacy_entry, SEARCH, LAST_KNOWN_GOOD, is_stale, meta_note, scope_flags, home_state_of, state_abbr


def build_supabase_rows(results, run_id, run_date):
    """Map result dicts to raw.raw_rate_radar rows (lowercase cols, typed)."""
    rows = []
    for r in results:
        row = {}
        for f in FIELDS:
            col = "rssdid" if f == "RSSDID" else f
            v = r.get(f)
            if f == "run_id":
                v = run_id
            elif f == "run_date":
                v = run_date
            if v in ("", None):
                row[col] = None
                continue
            if col in SB_NUMERIC_FIELDS:
                try:
                    row[col] = float(str(v).replace("%", "").replace(",", ""))
                except (ValueError, TypeError):
                    row[col] = None
            else:
                row[col] = str(v)
        if row.get("bank_name"):
            rows.append(row)
    return rows


async def fetch_last_known_good(bank_name, http_session):
    """
    Last-resort fallback for a bank that came back completely empty today.
    Looks up this bank's most recent PAST result in Supabase that actually
    had at least one rate, and returns it — clearly labeled as historical,
    never presented as fresh. Any failure here (network, schema, auth) is
    swallowed and logged; it must never break or slow down the crawl.
    """
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK and http_session):
        return None
    try:
        url = (f"{SUPABASE_URL}/rest/v1/raw_rate_radar"
               f"?bank_name=eq.{quote(bank_name)}"
               f"&or=(checking_apy.not.is.null,savings_apy.not.is.null,"
               f"cd_apy.not.is.null,money_market_apy.not.is.null)"
               f"&order=run_date.desc,crawled_at.desc"
               f"&limit=1")
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept-Profile": "raw",
        }
        async with http_session.get(url, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            rows = await resp.json()
            return rows[0] if rows else None
    except Exception as e:
        crawl_state["log"].append(f"    [History] lookup failed for {bank_name}: {e}")
        return None


async def push_to_supabase(results, run_id, run_date):
    """Upsert this run into raw.raw_rate_radar via PostgREST. Logs the outcome."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        crawl_state["log"].append(
            "Supabase push skipped — set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
        return False
    if not AIOHTTP_OK:
        crawl_state["log"].append("Supabase push skipped — aiohttp unavailable")
        return False
    rows = build_supabase_rows(results, run_id, run_date)
    if not rows:
        return False
    url = (f"{SUPABASE_URL}/rest/v1/raw_rate_radar"
           f"?on_conflict=run_id,bank_name")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Content-Profile": "raw",            # table lives in the raw schema
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(url, headers=headers, data=json.dumps(rows),
                              timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201, 204):
                    crawl_state["log"].append(
                        f"Supabase: ✓ pushed {len(rows)} rows to raw.raw_rate_radar "
                        f"(run {run_id})")
                    return True
                body = (await resp.text())[:300]
                crawl_state["log"].append(
                    f"Supabase push failed ({resp.status}): {body}")
                if "PGRST106" in body or "schema must be one of" in body:
                    crawl_state["log"].append(
                        "  → 'raw' schema is not exposed to the API. Supabase "
                        "Dashboard → Settings → API → 'Exposed schemas' → add raw.")
                return False
    except Exception as e:
        crawl_state["log"].append(f"Supabase push error: {type(e).__name__}: {e}")
        return False


# ── v3.6: tidy rate_observations dual-write (Phase 1) ────────────────────────
# Explodes each wide per-bank result into one row per product into the new
# public.rate_observations / public.rate_radar_runs tables, alongside — never
# instead of — the legacy raw.raw_rate_radar write above. Additive only: any
# failure here is swallowed and logged, and must never affect the wide-table
# write or the live dashboard, which still reads exclusively from
# vw_rate_radar_latest.

def method_is_page_read(entry):
    """True for a value read from a bank page (site / ai_vision / aggregator): the only kinds a
    page's as-of date speaks for."""
    return bool(entry) and entry.get("method") in ("site", "ai_vision", "aggregator")


def build_rate_observations(results, run_id, extra_cols=False, today=None):
    """Explode wide result dicts into tidy public.rate_observations rows.

    Where a value came from, and how far it can be trusted, is decided by
    rate_integrity.resolve() from the per-product provenance the crawler
    RECORDED when it assigned the value (result["provenance"][product]); it is
    never reconstructed from the free-text note or from the bank's general
    source_url (that is how a money-market value read by AI vision from a
    screenshot was once saved as site/high with the CD page as its source).
    A result that predates per-value provenance (an old same-day cache entry)
    falls back to rate_integrity.legacy_entry(), which uses only the product's
    OWN source URL and a whole-word "<product> from search" note, and gives
    "no source" (low) rather than borrowing another URL.

    A row with _edited=True (set by the verify UI's manual-save path — see
    rate_radar.py's manual_save()) means a person confirmed that value
    against the bank's own source, not that the crawler happened to read it
    off the bank's site — so it's forced to extraction_method='site' /
    confidence='high' / manually_verified=True here instead of running
    through the automated resolution, which only recognizes "site" when the
    value was read from a page and would otherwise misclassify a hand-typed
    confirmation (e.g. "confirmed by phone") as low-confidence data.

    extra_cols=True adds review_flags (text[]) and tier_note (text); pass it
    only when public.rate_observations has those columns (see
    push_rate_observations(), which checks).
    """
    now_iso = datetime.now().isoformat()
    rows = []
    # the state most pages of this run state (when they state one): a page naming another is flagged
    from collections import Counter
    _states = Counter()
    for r in results:
        for _st in {state_abbr((m or {}).get("scope")) for m in (r.get("rate_meta") or {}).values()} - {None}:
            _states[_st] += 1
    run_majority = _states.most_common(1)[0][0] if len(_states) > 1 else None
    for r in results:
        bank_name = r.get("bank_name")
        if not bank_name:
            continue
        edited     = bool(r.get("_edited"))
        note       = r.get("note")
        source_url = r.get("source_url")
        rate_tags  = r.get("rate_tags") or {}   # Phase 5: {"savings": ["promo"], ...} from tag_rate_context()
        prov       = r.get("provenance") or {}
        tier_notes = r.get("tier_notes") or {}
        rate_meta  = r.get("rate_meta") or {}   # {product: what its page said about its scope and as-of date}
        identity   = r.get("identity")          # "ok" | "mismatch" | "unknown" | None (not checked)
        not_pub    = set(r.get("not_published") or [])
        bank_url   = r.get("bank_url")
        hys_apy    = r.get("high_yield_savings_apy")
        products = [
            ("checking",     r.get("checking_apy"),    r.get("source_url_checking"), None),
            ("savings",      r.get("savings_apy"),      r.get("source_url_savings"),  None),
            ("high_yield_savings", hys_apy,             r.get("source_url_high_yield_savings"), None),
            ("money_market", r.get("money_market_apy"), r.get("source_url_money_market"), None),
        ]
        # Phase 5 (part 3): a full CD ladder means one row per distinct term
        # instead of only the single highest rate — the ladder always includes
        # that same best entry, so use it INSTEAD of cd_apy/cd_term, never
        # both (that would double-count the same rate under one term).
        cd_ladder = r.get("cd_ladder")
        if cd_ladder:
            for entry in cd_ladder:
                products.append(("cd", entry.get("apy"), r.get("source_url_cd"), entry.get("term")))
        else:
            products.append(("cd", r.get("cd_apy"), r.get("source_url_cd"), r.get("cd_term")))
        for product_type, apy, specific_url, cd_term in products:
            if apy in ("", None):
                continue
            # Wide-table backward compat put the HYS rate into savings_apy when
            # that was the only savings-type product found (see crawl_bank's
            # return dict) — that's the same rate as the high_yield_savings row
            # below, not a genuinely separate plain-savings observation.
            if product_type == "savings" and hys_apy is not None and apy == hys_apy:
                continue
            try:
                apy_val = float(str(apy).replace("%", "").replace(",", ""))
            except (ValueError, TypeError):
                continue
            entry = prov.get(product_type) or legacy_entry(product_type, specific_url, note)
            meta = rate_meta.get(product_type)
            method, conf, url, flags = resolve(entry, bank_url=bank_url, identity=identity, edited=edited,
                                               stale=bool(is_stale(meta, today)) and method_is_page_read(entry))
            if r.get("breaker_skipped") and not edited:
                flags = list(flags) + ["circuit_breaker_search_only"]
            if edited:
                url = specific_url or source_url
            # the bank says it does not publish this product's rate: only a value read from
            # its own page may stand; a search / last-known value is dropped, not saved
            if not edited and method in (SEARCH, LAST_KNOWN_GOOD) and (
                    "all" in not_pub or product_type in not_pub
                    or (product_type == "high_yield_savings" and "savings" in not_pub)):
                continue
            tags = rate_tags.get(product_type, [])
            row = {
                "run_id":            run_id,
                "bank_name":         str(bank_name),
                "rssdid":            str(r["RSSDID"]) if r.get("RSSDID") not in ("", None) else None,
                "product_type":      product_type,
                "term_months":       _parse_term_months(cd_term) if product_type == "cd" else None,
                "apy":               apy_val,
                "min_balance":       str(r["min_balance"]) if r.get("min_balance") not in ("", None) else None,
                "source_url":        str(url) if url else None,
                "extraction_method": method,
                "confidence":        conf,
                "is_conditional":    "conditional" in tags or "tiered" in tags,
                "is_promo":          "promo" in tags,
                "observed_at":       now_iso,
                "manually_verified": edited,
                "verified_by":       "rate-radar.html (manual save)" if edited else None,
                "verified_at":       now_iso if edited else None,
            }
            if extra_cols:
                row["review_flags"] = list(flags) + (["tiered"] if "tiered" in tags else [])
                _note = " | ".join(x for x in ((tier_notes.get(product_type) if product_type != "cd" else None), meta_note(meta)) if x)
                row["tier_note"]    = _note or None
                for _f in scope_flags(meta, home_state_of(r), run_majority):
                    row["review_flags"].append(_f)
                # first-class market fields: the ZIP the page was read for and the state it states
                _z = str((meta or {}).get("zip") or r.get("market_zip_used") or "")
                row["market_zip"] = _z if re.fullmatch(r"\d{5}", _z) else None
                row["scope_state"] = state_abbr((meta or {}).get("scope"))
                if meta and meta.get("as_of_raw") and not meta.get("as_of"):
                    row["review_flags"].append("as_of_unparsed")
            rows.append(row)
    return rows


_RATE_OBS_EXTRA = "review_flags,tier_note,market_zip,scope_state"


async def _rate_obs_has_extra_columns():
    """True when public.rate_observations has the optional review_flags / tier_note
    columns (an additive migration). Checked once per process; any failure means
    "no", so the write keeps working before or after the migration."""
    cached = crawl_state.get("_ro_extra_cols")
    if cached is not None:
        return cached
    ok = False
    if SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        try:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.get(f"{SUPABASE_URL}/rest/v1/rate_observations?select={_RATE_OBS_EXTRA}&limit=0",
                                 headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)) as resp:
                    ok = resp.status == 200
        except Exception:
            ok = False
    crawl_state["_ro_extra_cols"] = ok
    return ok


async def push_rate_observations(results, run_id):
    """Dual-write companion to push_to_supabase — same run, tidy shape.
    Must run after push_run_summary (rate_observations.run_id is a foreign
    key into rate_radar_runs). Never raises."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return False
    rows = build_rate_observations(results, run_id, extra_cols=await _rate_obs_has_extra_columns())
    if not rows:
        return False
    url = f"{SUPABASE_URL}/rest/v1/rate_observations"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(url, headers=headers, data=json.dumps(rows),
                              timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201, 204):
                    crawl_state["log"].append(
                        f"Supabase: ✓ pushed {len(rows)} tidy rows to rate_observations "
                        f"(run {run_id})")
                    return True
                body = (await resp.text())[:300]
                crawl_state["log"].append(
                    f"rate_observations push failed ({resp.status}): {body}")
                return False
    except Exception as e:
        crawl_state["log"].append(f"rate_observations push error: {type(e).__name__}: {e}")
        return False


# ── "Not published by the bank" outcome (public.rate_not_published) ───────────
# A bank whose own pages say "inquire for rates" has no number to store, and
# public.rate_observations.apy is NOT NULL, so the outcome gets its own small
# additive table (one row per run, bank and product; product_type may be "all"):
# the Hub can show "this bank does not publish its checking rate" next to the
# rates, and a search value is never saved in its place.

NOT_PUBLISHED_TABLE = "rate_not_published"
NOT_PUBLISHED_PRODUCTS = ("checking", "savings", "high_yield_savings", "money_market", "cd", "all")


def build_not_published_rows(results, run_id):
    """One row per (bank, product) a bank's own pages said it does not publish a
    rate for. A value a person confirmed in the verify UI (_edited) outranks the
    statement, so those results give no row."""
    now_iso = datetime.now().isoformat()
    rows = []
    for r in results:
        bank_name = r.get("bank_name")
        if not bank_name or r.get("_edited"):
            continue
        evidence = r.get("not_published_evidence") or {}
        for product in sorted(set(r.get("not_published") or [])):
            if product not in NOT_PUBLISHED_PRODUCTS:
                continue
            ev = evidence.get(product) or {}
            rows.append({
                "run_id":       run_id,
                "bank_name":    str(bank_name),
                "rssdid":       str(r["RSSDID"]) if r.get("RSSDID") not in ("", None) else None,
                "product_type": product,
                "evidence":     (ev.get("text") or None),
                "source_url":   (ev.get("url") or None),
                "observed_at":  now_iso,
            })
    return rows


async def _not_published_table_exists():
    """True when public.rate_not_published exists (an additive migration).
    Checked once per process; any failure means "no" so nothing breaks before it."""
    cached = crawl_state.get("_np_table")
    if cached is not None:
        return cached
    ok = False
    if SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        try:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.get(f"{SUPABASE_URL}/rest/v1/{NOT_PUBLISHED_TABLE}?select=id&limit=0",
                                 headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)) as resp:
                    ok = resp.status == 200
        except Exception:
            ok = False
    crawl_state["_np_table"] = ok
    return ok


async def push_not_published(results, run_id):
    """Write the not-published outcomes of this run. Same ordering rule as
    push_rate_observations (after push_run_summary: run_id is a foreign key).
    Never raises."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return False
    rows = build_not_published_rows(results, run_id)
    if not rows:
        return False
    if not await _not_published_table_exists():
        crawl_state["log"].append(
            f"Not-published outcomes for {len(rows)} product(s) not saved: public.{NOT_PUBLISHED_TABLE} does not exist yet")
        return False
    url = f"{SUPABASE_URL}/rest/v1/{NOT_PUBLISHED_TABLE}?on_conflict=run_id,bank_name,product_type"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(url, headers=headers, data=json.dumps(rows),
                              timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201, 204):
                    crawl_state["log"].append(
                        f"Supabase: ✓ pushed {len(rows)} not-published outcome(s) to {NOT_PUBLISHED_TABLE} (run {run_id})")
                    return True
                body = (await resp.text())[:300]
                crawl_state["log"].append(f"{NOT_PUBLISHED_TABLE} push failed ({resp.status}): {body}")
                return False
    except Exception as e:
        crawl_state["log"].append(f"{NOT_PUBLISHED_TABLE} push error: {type(e).__name__}: {e}")
        return False


async def load_bank_url_config():
    """Phase 2: load generic + bank-specific URL patterns from
    public.url_pattern_registry, and each bank's learned known-good URLs from
    public.bank_registry, merged with the hardcoded RATE_PATHS/BANK_EXTRA_URLS
    lists rather than replacing them — so a Supabase outage or a bank not yet
    in the registry never leaves the crawler with fewer candidate URLs than
    before this existed, and a registry row's last_successful_url gets tried
    first instead of just appended."""
    generic = list(RATE_PATHS)
    bank_urls = {k: list(v) for k, v in BANK_EXTRA_URLS.items()}
    health = {}   # Phase 3: bank_key -> {"consecutive_failures": int, "needs_review": bool}
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return generic, bank_urls, health
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.get(
                f"{SUPABASE_URL}/rest/v1/url_pattern_registry?active=eq.true&select=pattern,category,bank_key",
                headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as r:
                if r.status == 200:
                    for row in await r.json():
                        if row["category"] == "generic":
                            if row["pattern"] not in generic:
                                generic.append(row["pattern"])
                        elif row.get("bank_key"):
                            lst = bank_urls.setdefault(row["bank_key"], [])
                            if row["pattern"] not in lst:
                                lst.append(row["pattern"])
            async with s.get(
                f"{SUPABASE_URL}/rest/v1/bank_registry?select=bank_key,known_rate_urls,"
                f"last_successful_url,consecutive_failures,needs_review,zip_gate_zip",
                headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as r:
                if r.status == 200:
                    for row in await r.json():
                        lst = bank_urls.setdefault(row["bank_key"], [])
                        for u in (row.get("known_rate_urls") or []):
                            if u not in lst:
                                lst.append(u)
                        last_good = row.get("last_successful_url")
                        if last_good:
                            if last_good in lst:
                                lst.remove(last_good)
                            lst.insert(0, last_good)
                        health[row["bank_key"]] = {
                            "consecutive_failures": row.get("consecutive_failures") or 0,
                            "needs_review": bool(row.get("needs_review")),
                            "zip_gate_zip": row.get("zip_gate_zip"),
                        }
            crawl_state["log"].append(
                f"  ✓ Loaded bank/URL config: {len(generic)} generic paths, "
                f"{len(bank_urls)} banks with known URLs, "
                f"{sum(1 for h in health.values() if h['needs_review'])} flagged needs_review")
    except Exception as e:
        crawl_state["log"].append(
            f"  Config load warning: {type(e).__name__}: {e} (using built-in defaults)")
    return generic, bank_urls, health


def _bank_health(bank):
    raw_url = (bank.get("bank_url") or "").strip()
    if not raw_url:
        return None
    bank_key = extract_domain(raw_url if raw_url.startswith("http") else "https://" + raw_url).lower()
    return (crawl_state.get("_bank_health_cfg") or {}).get(bank_key)


def _circuit_open(bank):
    health = _bank_health(bank)
    if not health:
        return False
    failures = health.get("consecutive_failures", 0)
    if failures < CIRCUIT_BREAKER_THRESHOLD:
        return False
    return failures % CIRCUIT_BREAKER_PROBE_EVERY != 0


# Phase 4: extraction_cache — skip a redundant LLM vision call when the exact
# same (bank, url, screenshot) content was already extracted before. Keyed on
# content, not a calendar TTL: a bank whose rates page hasn't visually changed
# keeps hitting cache indefinitely; the moment the page content actually
# differs, the hash changes and it's a real cache miss, so this can never
# serve stale data disguised as fresh — it only ever skips work that would
# have produced the identical answer anyway.
def _content_hash(bank_name, url, *byte_blobs):
    h = hashlib.sha256()
    h.update((bank_name or "").encode("utf-8"))
    h.update(b"|")
    h.update((url or "").encode("utf-8"))
    for blob in byte_blobs:
        h.update(b"|")
        h.update(blob if isinstance(blob, bytes) else str(blob).encode("utf-8"))
    return h.hexdigest()


async def _extraction_cache_get(content_hash):
    """Never raises — a cache-read failure just falls through to a real LLM call."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.get(
                f"{SUPABASE_URL}/rest/v1/extraction_cache?content_hash=eq.{content_hash}"
                f"&select=extracted_json",
                headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as r:
                if r.status == 200:
                    rows = await r.json()
                    if rows:
                        return rows[0]["extracted_json"]
    except Exception:
        pass
    return None


async def _extraction_cache_put(content_hash, bank_name, page_url, extracted, model_used):
    """Best-effort write; never raises, never blocks the crawl on failure."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return
    headers = {
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    row = {"content_hash": content_hash, "bank_name": bank_name, "page_url": page_url,
           "extracted_json": extracted, "model_used": model_used}
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(
                f"{SUPABASE_URL}/rest/v1/extraction_cache?on_conflict=content_hash",
                headers=headers, data=json.dumps(row),
                timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as resp:
                await resp.read()
    except Exception:
        pass


async def update_bank_registry(bank, result):
    """After a crawl attempt, record what worked (or didn't) so the next run
    can try the known-good URL first instead of re-scanning every path.
    Success is judged by whether any product's rate actually came from the
    bank's own site or AI vision (not a search-engine guess) — that's the
    signal worth remembering, regardless of the run's overall Found/Partial
    status. Never raises; a registry-write failure must not affect the crawl."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return
    raw_url = (bank.get("bank_url") or "").strip()
    if not raw_url:
        return
    bank_key = extract_domain(raw_url if raw_url.startswith("http") else "https://" + raw_url).lower()
    if not bank_key:
        return

    def _clean_url(u):
        # Strip the "[AI] "/"[Search] " display prefixes used throughout this
        # file's source_url fields so a genuinely-verified URL is navigable,
        # not a tagged display string.
        if not u:
            return None
        u = u.strip()
        for prefix in ("[AI] ", "[Search] "):
            if u.startswith(prefix):
                u = u[len(prefix):].strip()
        return u if u.startswith("http") else None

    note = result.get("note")
    general_url = result.get("source_url")
    verified_url = None
    for product, specific in [("checking", result.get("source_url_checking")),
                               ("savings",  result.get("source_url_savings")),
                               ("cd",       result.get("source_url_cd"))]:
        if result.get(f"{product}_apy") is None:
            continue
        # the provenance recorded at assignment; an old result without one falls back to the
        # conservative classification from the product's OWN source (rate_integrity.legacy_entry)
        entry = (result.get("provenance") or {}).get(product) or legacy_entry(product, specific, note)
        if entry and entry.get("method") in ("site", "ai_vision"):
            # a page this value was read from; an AI-vision hit may only carry the general source_url
            cleaned = _clean_url(entry.get("url")) or _clean_url(specific) or _clean_url(general_url)
            if cleaned:
                verified_url = cleaned
                break
    headers = {
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.get(
                f"{SUPABASE_URL}/rest/v1/bank_registry?bank_key=eq.{bank_key}"
                f"&select=known_rate_urls,consecutive_failures",
                headers=headers, timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as r:
                existing = (await r.json()) if r.status == 200 else []
            prior = existing[0] if existing else {}
            row = {"bank_key": bank_key, "bank_name": bank.get("bank_name"), "bank_url": raw_url,
                   "updated_at": datetime.now().isoformat()}
            # ZIP-gate learning: only ever write a NEW value when this crawl actually
            # recovered a rate through the gate — never overwrite a previously-learned
            # working ZIP just because this run didn't happen to need/re-hit the gate.
            zip_gate_zip = result.get("_zip_gate_zip")
            if zip_gate_zip:
                row["zip_gate_zip"] = zip_gate_zip
            if verified_url:
                known = set(prior.get("known_rate_urls") or [])
                known.add(verified_url)
                row.update({
                    "last_successful_url": verified_url,
                    "known_rate_urls": sorted(known),
                    "consecutive_failures": 0,
                    "needs_review": False,
                })
            else:
                failures = (prior.get("consecutive_failures") or 0) + 1
                row.update({"consecutive_failures": failures,
                            "needs_review": failures >= CIRCUIT_BREAKER_THRESHOLD})
            upsert_headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            async with s.post(
                f"{SUPABASE_URL}/rest/v1/bank_registry?on_conflict=bank_key",
                headers=upsert_headers, data=json.dumps(row),
                timeout=aiohttp.ClientTimeout(total=SUPABASE_CALL_TIMEOUT_S)
            ) as resp:
                if resp.status not in (200, 201, 204):
                    body = (await resp.text())[:300]
                    crawl_state["log"].append(f"bank_registry write-back failed ({resp.status}): {body}")
                    return None
                # Report back whether this write just crossed the circuit-breaker
                # threshold for the first time, so the caller can surface an alert.
                return None if verified_url else row["consecutive_failures"]
    except Exception as e:
        crawl_state["log"].append(f"bank_registry write-back error: {type(e).__name__}: {e}")
        return None


async def push_run_summary(run_id, run_date, started_at, banks_found, banks_partial,
                            banks_error, ai_calls, chat_calls,
                            llm_input_tokens=0, llm_output_tokens=0, llm_web_searches=0,
                            notes=None):
    """One row per crawl run in public.rate_radar_runs — Phase 0's run-level
    observability. Must complete before push_rate_observations (FK parent).
    Never blocks the crawl."""
    if not (SUPABASE_URL and SUPABASE_KEY and AIOHTTP_OK):
        return False
    row = {
        "run_id":             run_id,
        "run_date":           run_date,
        "started_at":         started_at.isoformat(),
        "finished_at":        datetime.now().isoformat(),
        "banks_attempted":    banks_found + banks_partial + banks_error,
        "banks_found":        banks_found,
        "banks_partial":      banks_partial,
        "banks_error":        banks_error,
        "ai_vision_calls":    ai_calls,
        "chat_calls":         chat_calls,
        "llm_input_tokens":   llm_input_tokens,
        "llm_output_tokens":  llm_output_tokens,
        "llm_web_searches":   llm_web_searches,
        "estimated_cost_usd": _estimate_cost_usd(llm_input_tokens, llm_output_tokens, llm_web_searches),
    }
    if notes:
        row["notes"] = notes
    url = f"{SUPABASE_URL}/rest/v1/rate_radar_runs?on_conflict=run_id"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(url, headers=headers, data=json.dumps(row),
                              timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (200, 201, 204):
                    crawl_state["log"].append(f"Supabase: ✓ logged run summary (run {run_id})")
                    return True
                body = (await resp.text())[:300]
                crawl_state["log"].append(f"Run summary push failed ({resp.status}): {body}")
                return False
    except Exception as e:
        crawl_state["log"].append(f"Run summary push error: {type(e).__name__}: {e}")
        return False


def auto_save(results):
    try:
        EXPORTS.mkdir(exist_ok=True)
        now    = datetime.now()
        run_id = now.strftime("%Y%m%d_%H%M%S")
        path   = EXPORTS / f"rate_radar_{run_id}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in results:
                row = dict(r)
                row["run_id"]   = run_id
                row["run_date"] = now.strftime("%Y-%m-%d")
                w.writerow(row)
        crawl_state["log"].append(f"Saved: RateRadar_Exports/{path.name}")
        # v3.2: persist the run log too — a run should never be a black box
        try:
            log_path = EXPORTS / f"rate_radar_{run_id}.log"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(crawl_state["log"]))
            crawl_state["log"].append(f"Saved: RateRadar_Exports/{log_path.name}")
        except Exception as le:
            crawl_state["log"].append(f"Log save failed: {le}")
        return run_id, now.strftime("%Y-%m-%d")
    except Exception as e:
        crawl_state["log"].append(f"Save failed: {e}")
        return None, None
