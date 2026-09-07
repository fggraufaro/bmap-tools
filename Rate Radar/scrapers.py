# ── Scrapers: Playwright-driven crawling + Anthropic-assisted extraction ─────
# Everything that actually visits a bank's website and turns page content into
# rate data: the standard crawl_bank() path-guessing crawler, the AI-agent
# fallback, the chat-widget fallback, ZIP-gate handling, search-engine/site-
# search rescues, and the run_crawler() orchestrator that ties them together
# and drives the Supabase/local-export storage layer at the end of a run.

import asyncio
import base64
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
try:
    import anthropic
    ANTHROPIC_OK = bool(ANTHROPIC_API_KEY)
except ImportError:
    ANTHROPIC_OK = False

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

from extraction import (
    extract_rates, extract_domain, check_rate_staleness, detect_rebrand,
    parse_json_object, _clean_cd_ladder, extract_rates_from_json,
    JSON_RATE_KEY_PAT,
)
from shared import (
    crawl_state, RATE_PATHS, BANK_EXTRA_URLS,
    CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_PROBE_EVERY, _estimate_cost_usd,
)
from storage import (
    fetch_last_known_good, push_to_supabase, push_rate_observations,
    load_bank_url_config, _bank_health, _circuit_open,
    _content_hash, _extraction_cache_get, _extraction_cache_put,
    update_bank_registry, push_run_summary, auto_save,
)

# ── Same-day result cache ────────────────────────────────────────────────────
# Rates don't reprice multiple times a day — but the AI search fallback is
# non-deterministic, so re-running the same CSV within one calendar day was
# producing different rates/statuses per bank. Once a bank is resolved for
# today's date, every run for the rest of that day reuses the same result
# instead of re-crawling/re-searching it. A new calendar day starts a fresh
# cache file, so real week-over-week rate changes still show up normally.
RATE_CACHE_DIR = Path(__file__).parent / "RateCache"


def _bank_cache_key(bank):
    """Stable identity for a bank across runs: domain if we have a URL, else name."""
    url = (bank.get("bank_url") or "").strip()
    if url:
        dom = extract_domain(url)
        if dom:
            return dom.lower()
    return (bank.get("bank_name") or "").strip().lower()


def _today_cache_path():
    RATE_CACHE_DIR.mkdir(exist_ok=True)
    return RATE_CACHE_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"


def _load_today_cache():
    path = _today_cache_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_today_cache(cache):
    try:
        with open(_today_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        crawl_state["log"].append(f"  Cache save error: {e}")


# crawl_state moved to shared.py (imported above; mutable, shared with storage.py)


# Phase 3: fixed status enum. "Skipped" is new — the circuit breaker uses it
# to mean "we deliberately didn't attempt a full crawl this run", distinct
# from "Not public" ("we looked and found nothing"). Mirrored by a CHECK
# constraint on raw.raw_rate_radar.status so a typo'd literal can't silently
# create a 5th value the rest of the system doesn't know how to handle.
STATUS_FOUND     = "Found"
STATUS_PARTIAL   = "Partial"
STATUS_NOT_PUBLIC = "Not public"
STATUS_ERROR     = "Error"
STATUS_SKIPPED   = "Skipped"
STATUS_VALUES    = (STATUS_FOUND, STATUS_PARTIAL, STATUS_NOT_PUBLIC, STATUS_ERROR, STATUS_SKIPPED)

def _classify_status(core_rate_count, of=3):
    """Found/Partial/Not public from how many of the core rates (checking,
    savings, cd) came back — the same rule applied at every re-evaluation
    point (preflight, search-retry, chat fallback, history fallback)."""
    if core_rate_count >= of:
        return STATUS_FOUND
    if core_rate_count > 0:
        return STATUS_PARTIAL
    return STATUS_NOT_PUBLIC


# ── Phase 4: named tuning config (was scattered magic numbers) ──────────────
# Collected here so retuning any of these is a one-line change instead of a
# file-wide hunt. Values are unchanged from what the code already ran with —
# this is the infrastructure for tuning, not a claim that these numbers are
# now optimal; actually retuning them should follow observed run behavior
# (timeouts, rate-limit errors, wall-clock time), not a guess.
MAX_CONCURRENT_BANKS         = 5   # banks crawled in parallel during the browser phase
MAX_CONCURRENT_SEARCH_RETRIES = 2  # parallel final-pass API-search retries (Haiku TPM safety)
PREFLIGHT_BATCH_SIZE         = 3   # banks per preflight search batch
PREFLIGHT_BATCH_GAP_S        = 2   # pause between preflight batches
PAGE_LOAD_TIMEOUT_MS         = 12000  # standard crawl_bank() page.goto timeout
AGENT_PAGE_LOAD_TIMEOUT_MS   = 15000  # ai_agent_crawl/DA-scraper page.goto timeout
PAGE_BUDGET_PER_BANK         = 12  # max pages visited per bank in crawl_bank()
PAGE_BUDGET_PRIORITY_BONUS   = 2   # extra slots for links found via a priority page
BROWSER_LAUNCH_TIMEOUT_S     = 45
GOTO_RETRY_COUNT             = 1   # extra attempts on a transient page.goto failure
GOTO_RETRY_BACKOFF_S         = 0.6
# SUPABASE_CALL_TIMEOUT_S moved to shared.py (only used by storage.py)


def _track_llm_usage(resp):
    """Accumulate Anthropic token/web-search usage from a messages.create() response
    into crawl_state. Never raises — usage tracking must not break the crawl."""
    try:
        usage = resp.usage
        crawl_state["llm_input_tokens"]  += getattr(usage, "input_tokens", 0) or 0
        crawl_state["llm_output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use is not None:
            crawl_state["llm_web_searches"] += getattr(server_tool_use, "web_search_requests", 0) or 0
    except Exception:
        pass

# _estimate_cost_usd moved to shared.py (imported above)

# RATE_PATHS moved to shared.py (imported above)

# BANK_EXTRA_URLS moved to shared.py (imported above)

# Rate-extraction regex patterns + tag_rate_context/check_rate_staleness/detect_rebrand/extract_domain moved to extraction.py


async def _goto_with_retry(page, url, timeout=PAGE_LOAD_TIMEOUT_MS,
                            retries=GOTO_RETRY_COUNT, backoff=GOTO_RETRY_BACKOFF_S):
    """Phase 3: structured retry/backoff. A transient network hiccup (DNS
    blip, connection reset, a slow TLS handshake) shouldn't permanently give
    up on a URL the same way a real 404 should. Retries only on exceptions —
    a normal response, even a 4xx/5xx, is returned as-is and never retried,
    since that's a real answer, not a failure."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(backoff * (attempt + 1))
    raise last_exc


# ── v3.2: proxy support ───────────────────────────────────────────────────────
# Root cause of the 06-11 run: every Playwright/aiohttp request died with DNS
# failures (ERR_NAME_NOT_RESOLVED / getaddrinfo failed) while the Anthropic
# SDK worked — the machine routes traffic through a proxy. httpx (Anthropic
# SDK) honors HTTPS_PROXY automatically; aiohttp and Chromium must be told.
# Set HTTPS_PROXY (or HTTP_PROXY) in the .env file or system env and both the
# browser and all aiohttp sessions will use it.

# parse_json_object / _clean_cd_ladder moved to extraction.py


def get_proxy_url():
    """Return the proxy URL from standard env vars, or None."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return None


PROXY_URL = get_proxy_url()


# ── v3.1 deep-dig helpers (Fixes 1–3: dig before declaring "Not public") ─────

# Fix 3: URLs whose path mentions rates/CDs/savings get the full JS treatment
RATE_URL_KEYWORD_PAT = re.compile(r'(?:\bcds?\b|certificate|savings|rates?\b)', re.I)

# Fix 1: anchor text/hrefs that lead from a product page to its rates
VIEW_RATES_LINK_PAT = re.compile(
    r'(?:view|see|check|compare|our|current|today\'?s?)[\s\w\-]{0,20}(?:rates?|apys?|yields?)\b|'
    r'\brates?\s*(?:&(?:amp;)?|and)\s*(?:fees|terms)|rate[\s\-]?sheet|'
    r'see\s+details|view\s+details|view\s+disclosures?|account\s+details|'
    r'/rates?(?:\b|$)', re.I)

# Search-engine result links that are never the bank's own pages
SEARCH_ENGINE_DOMAINS = ("google.", "bing.", "duckduckgo.", "gstatic.",
                         "microsoft.", "youtube.", "googleadservices.")


async def expand_page_content(page):
    """
    Scroll the full page (triggers lazy-loaded JS rate tables) and click open
    accordions / <details> / collapsed tabs so their content lands in the DOM
    text. Skips anything inside nav/header/menu so we don't open hamburger
    menus or search overlays. Returns the number of elements expanded.
    """
    expanded = 0
    # Progressive scroll — many bank sites lazy-render rate tables on scroll
    try:
        await page.evaluate("""async () => {
            const step = Math.max(400, window.innerHeight * 0.8);
            const max  = Math.min(document.body.scrollHeight, 15000);
            for (let y = 0; y <= max; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 120));
            }
            window.scrollTo(0, document.body.scrollHeight);
        }""")
        await page.wait_for_timeout(800)
    except Exception:
        pass

    selectors = [
        "details:not([open]) > summary",
        "[data-toggle='collapse']", "[data-bs-toggle='collapse']",
        "button[class*='accordion' i]", "[class*='accordion' i] button",
        "[class*='accordion-header' i]", "[class*='accordion-title' i]",
        "button[class*='expand' i]", "button[class*='toggle' i]",
        "[role='tab'][aria-selected='false']",
        "[aria-expanded='false']",
    ]
    for sel in selectors:
        if expanded >= 25:
            break
        try:
            els = await page.query_selector_all(sel)
        except Exception:
            continue
        for el in els[:12]:
            if expanded >= 25:
                break
            try:
                if not await el.is_visible():
                    continue
                # Never click nav/menu/search togglers — they open overlays
                in_nav = await el.evaluate(
                    "el => !!el.closest(\"nav, header, footer, "
                    "[role='navigation'], [class*='nav' i], [class*='menu' i], "
                    "[class*='search' i], [id*='menu' i]\")")
                if in_nav:
                    continue
                await el.click(timeout=800)
                expanded += 1
                await page.wait_for_timeout(120)
            except Exception:
                continue
    if expanded:
        await page.wait_for_timeout(700)
    try:
        await page.keyboard.press("Escape")          # close any stray overlay
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    return expanded


async def click_rate_cta(page):
    """
    "Check rates" style CTAs — some large banks (Regions confirmed) gate
    rates behind a link/button with no accordion class or ARIA state and
    href="#" (a JS click handler opens a rate modal, often itself ZIP-
    gated). These have a real href-based counterpart handled by
    collect_rate_detail_links() for normal navigable links, but href="#"
    ones go nowhere to navigate to — they only ever reveal content if
    clicked in place, exactly like an accordion.

    Deliberately NOT folded into expand_page_content(): that runs
    unconditionally on every rate-flagged page before extraction is even
    attempted, and scanning every a/button on the page for this pattern adds
    real per-page latency that showed up as extra bank timeouts when it ran
    on every page regardless of need. This is only worth that cost once
    normal extraction has already come up empty, so callers should only
    reach for it from that fallback path — never unconditionally.

    Only the first match is clicked: a modal opened this way typically shows
    rates for every product tier at once, and clicking several such triggers
    in a row risks stacking modals instead of revealing more. Returns True
    if something was clicked.
    """
    try:
        cta_els = await page.query_selector_all("a, button")
    except Exception:
        return False
    for el in cta_els[:200]:
        try:
            href = (await el.get_attribute("href")) or ""
            if href and not href.startswith(("#", "javascript:")):
                continue   # real destination — collect_rate_detail_links() handles it
            txt = ((await el.inner_text()) or "").strip()
            if not txt or not VIEW_RATES_LINK_PAT.search(txt):
                continue
            if not await el.is_visible():
                continue
            in_nav = await el.evaluate(
                "el => !!el.closest(\"nav, header, footer, "
                "[role='navigation'], [class*='nav' i], [class*='menu' i], "
                "[class*='search' i], [id*='menu' i]\")")
            if in_nav:
                continue
            await el.click(timeout=800)
            await page.wait_for_timeout(300)
            return True
        except Exception:
            continue
    return False


async def harvest_hidden_text(page):
    """
    Pull text that page.inner_text() misses: collapsed accordion panels,
    hidden tab panes, and rate-table markup (textContent includes hidden
    nodes; innerText does not). Returns a newline-joined blob, '' on failure.
    """
    try:
        blob = await page.evaluate("""() => {
            const sels = [
                'table', '[class*="rate" i]', '[class*="apy" i]',
                '[id*="rate" i]', '[class*="accordion" i]',
                '[class*="collapse" i]', '[class*="panel" i]',
                '[role="tabpanel"]', 'details', '[class*="tier" i]'
            ];
            const seen = new Set(); const out = [];
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const t = (el.textContent || '')
                        .replace(/[ \\t]+/g, ' ')
                        .replace(/\\n{2,}/g, '\\n').trim();
                    if (t.length > 20 && t.length < 20000) out.push(t);
                }
            }
            return out.join('\\n---\\n');
        }""")
        return blob or ""
    except Exception:
        return ""


async def collect_rate_detail_links(page, domain_str, limit=4):
    """
    Fix 1: from the current page, gather same-domain links whose href or
    anchor text looks like "View Rates" / "See Details" / a rates path.
    Returns deduped absolute URLs, best-guess first.
    """
    out = []
    try:
        links = await page.query_selector_all("a[href]")
    except Exception:
        return out
    for link in links[:150]:
        try:
            href = await link.get_attribute("href") or ""
            txt  = (await link.inner_text()).strip()
        except Exception:
            continue
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if not VIEW_RATES_LINK_PAT.search(f"{href} {txt}"):
            continue
        full = (href if href.startswith("http")
                else domain_str + href if href.startswith("/") else None)
        if full and full.startswith(domain_str) and full not in out:
            out.append(full)
        if len(out) >= limit:
            break
    return out


async def find_rates_via_search_engine(page, domain):
    """
    Fix 2: before marking a bank "Not public", search the open web for
    `site:<domain> rates APY` and return up to 3 candidate URLs on the
    bank's own domain. Tries Google first, then Bing, then DuckDuckGo HTML
    (Google frequently captchas headless browsers; the others rarely do).
    """
    if not domain:
        return []
    q = f"site%3A{domain}+rates+APY"
    engines = [
        ("Google",     f"https://www.google.com/search?q={q}&num=10"),
        ("Bing",       f"https://www.bing.com/search?q={q}"),
        ("DuckDuckGo", f"https://duckduckgo.com/html/?q={q}"),
    ]
    for engine, surl in engines:
        try:
            await page.goto(surl, timeout=12000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
        except Exception:
            continue
        candidates = []
        for h in hrefs:
            if not isinstance(h, str):
                continue
            # Unwrap redirect-style results: google /url?q=… , ddg uddg=…
            for param in ("q", "uddg", "u"):
                m = re.search(r'[?&]' + param + r'=(https?[^&]+)', h)
                if m:
                    h = unquote(m.group(1))
                    break
            if not h.startswith("http"):
                continue
            h_dom = extract_domain(h)
            if any(se in h_dom for se in SEARCH_ENGINE_DOMAINS):
                continue
            if domain not in h_dom:
                continue
            if h not in candidates:
                candidates.append(h)
            if len(candidates) >= 3:
                break
        if candidates:
            crawl_state["log"].append(
                f"    [SE-rescue] {engine}: {len(candidates)} on-domain result(s), "
                f"top: {candidates[0]}")
            return candidates
    crawl_state["log"].append(f"    [SE-rescue] no on-domain results for site:{domain}")
    return []


# A search icon/button that must be clicked to reveal the actual input —
# common on bank sites where the search box is hidden until opened.
SITE_SEARCH_TRIGGER_SELECTORS = [
    'button[aria-label*="search" i]', 'a[aria-label*="search" i]',
    'button[class*="search" i]', 'a[class*="search" i]',
    '[class*="search-icon" i]', '[class*="search-toggle" i]',
    '[data-testid*="search" i]',
]
SITE_SEARCH_INPUT_SELECTORS = [
    'input[type="search"]', 'input[name*="search" i]',
    'input[placeholder*="search" i]', 'input[aria-label*="search" i]',
]

async def find_rates_via_site_search(page, base_url, query="rates"):
    """
    Many bank sites don't link their rates page from the nav at all, but
    their own on-site search reliably finds it when asked directly — often
    more precise than an external search engine, since it queries the
    site's own indexed content instead of relying on how well an external
    engine has crawled/ranked a small community bank (the technique behind
    this: Modern Bank's real CD-rates page was found this way after every
    guessed path and the external search-engine rescue both came up empty).
    Returns up to 3 candidate URLs on the bank's own domain, or []."""
    domain = extract_domain(base_url)
    try:
        await page.goto(base_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception:
        return []

    for sel in SITE_SEARCH_TRIGGER_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(400)
                break
        except Exception:
            continue

    inp = None
    for sel in SITE_SEARCH_INPUT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                inp = el
                break
        except Exception:
            continue
    if not inp:
        return []

    try:
        await inp.click()
        await inp.fill(query)
        await inp.press("Enter")
        await page.wait_for_timeout(1500)
    except Exception:
        return []

    try:
        hrefs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    except Exception:
        return []

    candidates = []
    for h in hrefs:
        if not isinstance(h, str) or not h.startswith("http"):
            continue
        if domain not in extract_domain(h):
            continue
        if not re.search(r"rate|apy|cd\b|savings|checking|deposit|certificate", h, re.I):
            continue
        if h not in candidates:
            candidates.append(h)
        if len(candidates) >= 3:
            break
    return candidates


# ── v3.4 ZIP-gate helpers ────────────────────────────────────────────────────
# Big banks (TD, Fifth Third, 1st Source) publish rates per market, behind a
# "enter your ZIP" prompt. The branch_address column in the input CSV carries
# the highest-vulnerability target branch from BMAP — its ZIP unlocks the
# rates for exactly the market where the client competes with that bank.

ZIP_FROM_ADDRESS_PAT = re.compile(r'\b(\d{5})(?:-\d{4})?\b')

ZIP_INPUT_SELECTOR = (
    "input[autocomplete='postal-code'], input[name*='zip' i], "
    "input[id*='zip' i], input[placeholder*='zip' i], "
    "input[aria-label*='zip' i], input[name*='postal' i], "
    "input[id*='postal' i], input[placeholder*='postal' i]")

ZIP_SUBMIT_TEXT_PAT = re.compile(
    r'\b(go|submit|search|view|update|apply|continue|see|find|get|show)\b', re.I)

# JSON_RATE_KEY_PAT / JSON_PRODUCT_HINTS moved to extraction.py (JSON_RATE_KEY_PAT imported above for make_json_sniffer)


def get_bank_zip(bank):
    """ZIP for the gate: last 5-digit group in branch_address, else DEFAULT_ZIP env."""
    addr = (bank.get("branch_address") or "").strip()
    zips = ZIP_FROM_ADDRESS_PAT.findall(addr)
    if zips:
        return zips[-1]
    return os.environ.get("DEFAULT_ZIP", "").strip() or None


async def find_zip_gate(page):
    """Return a visible ZIP/postal input on the page, or None."""
    try:
        els = await page.query_selector_all(ZIP_INPUT_SELECTOR)
    except Exception:
        els = []
    for el in els[:6]:
        try:
            if await el.is_visible():
                return el
        except Exception:
            continue
    # Fallback: some gates (Regions' rate-check modal confirmed) render a
    # plain <input> with no identifying name/id/placeholder/aria-label at
    # all — the only "this is a ZIP field" signal is an associated <label>
    # (via for="id" or wrapping the input), which a CSS attribute selector
    # can't express. Only tried when the attribute-based pass above finds
    # nothing, so it can't change behavior on sites that already work.
    try:
        handle = await page.evaluate_handle("""() => {
            const isZip = t => /\\bzip\\b|\\bpostal\\b/i.test(t || '');
            const inputs = document.querySelectorAll(
                'input[type="text"], input:not([type])');
            for (const el of inputs) {
                if (el.offsetParent === null) continue;
                let labelText = '';
                if (el.id) {
                    const lbl = document.querySelector(
                        `label[for="${CSS.escape(el.id)}"]`);
                    if (lbl) labelText = lbl.innerText;
                }
                if (!labelText) {
                    const wrap = el.closest('label');
                    if (wrap) labelText = wrap.innerText;
                }
                if (isZip(labelText)) return el;
            }
            return null;
        }""")
        el = handle.as_element()
        if el and await el.is_visible():
            return el
    except Exception:
        pass
    return None


async def fill_zip_gate(page, zip_el, zip_code):
    """
    Fill a detected ZIP input and submit it. Tries Enter first (most gates
    submit on Enter); if the page text doesn't change, clicks the submit
    control in the same form, then any visible button with submit-ish text.
    Returns True if a submission was attempted.
    """
    try:
        before = await page.evaluate("() => document.body.innerText.length")
    except Exception:
        before = -1
    try:
        await zip_el.click(timeout=1500)
        await zip_el.fill("")
        await zip_el.type(zip_code, delay=60)
        await page.wait_for_timeout(300)
        await zip_el.press("Enter")
        await page.wait_for_timeout(2200)
    except Exception:
        return False
    try:
        after = await page.evaluate("() => document.body.innerText.length")
    except Exception:
        after = before
    if before >= 0 and abs(after - before) < 40:
        clicked = False
        # Prefer the submit control inside the same form as the input
        try:
            handle = await zip_el.evaluate_handle(
                "el => el.closest('form') ? el.closest('form').querySelector("
                "'button, input[type=submit], [role=button]') : null")
            btn = handle.as_element() if handle else None
            if btn and await btn.is_visible():
                await btn.click(timeout=1500)
                clicked = True
        except Exception:
            pass
        if not clicked:
            try:
                for b in (await page.query_selector_all(
                        "button, input[type=submit], [role=button]"))[:30]:
                    try:
                        if not await b.is_visible():
                            continue
                        txt = ((await b.inner_text()) or "") + " " + \
                              ((await b.get_attribute("value")) or "")
                        if ZIP_SUBMIT_TEXT_PAT.search(txt):
                            await b.click(timeout=1500)
                            clicked = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        if clicked:
            await page.wait_for_timeout(2500)
    return True


# extract_rates_from_json moved to extraction.py


def make_json_sniffer(bucket):
    """Playwright response listener: collect JSON XHR bodies that mention rates."""
    async def on_response(resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct:
                return
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            body = await resp.text()
            if len(body) > 400_000:
                return
            if not JSON_RATE_KEY_PAT.search(body):
                return
            bucket.append(body)
        except Exception:
            pass
    return on_response


# ── v3: Input validation (URL redirect + RSSDID check) ───────────────────────

async def validate_banks_preflight(banks):
    """
    Fast pre-flight pass over all banks before the crawl starts.
    Checks for:
      - Dead / redirected URLs (possible rebrands)
      - Missing RSSDIDs (no Call Report enrichment possible)
    Returns list of warning strings surfaced in the log.
    """
    warnings = []
    if not AIOHTTP_OK:
        return warnings

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    timeout = aiohttp.ClientTimeout(total=8)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout,
                                     trust_env=True) as session:
        async def check_bank(bank):
            url = bank.get("bank_url", "").strip()
            name = bank["bank_name"]
            if not url or not url.startswith("http"):
                warnings.append(f"⚠️  {name}: no valid URL in CSV")
                return
            try:
                async with session.head(url, allow_redirects=True, ssl=False) as r:
                    final = str(r.url)
                    in_domain  = extract_domain(url)
                    out_domain = extract_domain(final)
                    if in_domain and out_domain and in_domain != out_domain:
                        warnings.append(
                            f"⚠️  {name}: URL redirects {in_domain} → {out_domain} "
                            f"(possible rebrand — verify bank name)"
                        )
                        bank["_redirect_domain"] = out_domain
            except Exception:
                pass  # unreachable URL — crawl will handle it gracefully
            if not bank.get("RSSDID"):
                warnings.append(f"ℹ️  {name}: no RSSDID — Call Report enrichment unavailable")

        await asyncio.gather(*[check_bank(b) for b in banks])

    return warnings


# ── v3: Preflight search for clean rate hits ─────────────────────────────────

async def preflight_search(bank_name, bank_url, session, focus=None):
    """
    Use Anthropic API with web_search tool to find current rates without
    spinning up a browser. Runs concurrently before the browser crawl.

    focus: optional list of missing product names (e.g. ["savings","checking"])
    to narrow the query on a retry instead of repeating the identical broad
    search that already came up empty once.

    Returns partial rate dict if snippets contain clear APY data, else None.
    Also returns a confidence tag and source note.
    """
    if not ANTHROPIC_OK:
        return None

    domain = extract_domain(bank_url) if bank_url else ""
    FOCUS_TERMS = {
        "checking":     "checking account interest rate APY",
        "savings":      "savings account interest rate APY",
        "cd":           "CD certificate of deposit rate APY",
        "money_market": "money market account rate APY",
    }
    if focus:
        term_str = " ".join(FOCUS_TERMS.get(f, f) for f in focus)
        query = f'"{bank_name}" {term_str} 2026'
    else:
        query = f'"{bank_name}" savings CD checking APY rates 2026'
    if domain:
        query += f' site:{domain}'

    focus_note = (f"\nThis is a targeted retry — the broad search already ran and missed "
                  f"{', '.join(focus)}. Focus specifically on finding {', '.join(focus)} "
                  f"for this bank; try phrasing/pages a general search might have skipped."
                  if focus else "")

    prompt = f"""Search for current deposit rates for {bank_name}.
Query: {query}{focus_note}

From the search results, extract deposit rates. Return ONLY valid JSON:
{{
  "checking": <float APY% or null>,
  "savings": <REGULAR savings float APY% or null, not high-yield>,
  "high_yield_savings": <a SEPARATE high-yield/premium savings product's float APY% or null,
   only if distinct from regular savings>,
  "cd": <float APY% or null>,
  "cd_term": <"12-month" or null>,
  "money_market": <float APY% or null>,
  "confidence": <"high" if rates found in official bank source, "low" if aggregator only>,
  "source_note": <short string like "rollstonebank.com rates page" or null>,
  "rebrand_hint": <string if bank appears to have rebranded, else null>
}}
Rules:
- Only extract rates from official bank websites or well-known aggregators (Bankrate, DA, NerdWallet)
- DO NOT cross-assign rates between product types
- If only one savings-type product exists, put it in whichever field describes it, leave the other null
- If no rates found return all nulls with confidence "low"
- Return ONLY the JSON"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        _track_llm_usage(resp)
        # Extract text content blocks
        txt = ""
        for block in resp.content:
            if hasattr(block, "text"):
                txt += block.text
        if not txt.strip():
            return None
        txt = txt.replace("```json", "").replace("```", "").strip()
        # Find JSON object
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        if not m:
            return None
        result = json.loads(m.group(0))
        cleaned = {
            "checking":          float(result["checking"])          if result.get("checking")          else None,
            "savings":           float(result["savings"])            if result.get("savings")           else None,
            "high_yield_savings": float(result["high_yield_savings"]) if result.get("high_yield_savings") else None,
            "cd":                float(result["cd"])                 if result.get("cd")                else None,
            "cd_term":           result.get("cd_term"),
            "money_market":      float(result["money_market"])       if result.get("money_market")      else None,
            "_confidence":       result.get("confidence", "low"),
            "_source_note":      result.get("source_note", ""),
            "_rebrand_hint":     result.get("rebrand_hint"),
        }
        # Only return if at least one rate found
        if any(cleaned.get(k) for k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]):
            return cleaned
    except Exception as e:
        crawl_state["log"].append(f"    [Search] preflight error: {e}")
    return None


# ── v3: aiohttp-based DepositAccounts fetch (no browser) ─────────────────────

def da_slugs(name):
    """Generate candidate DA slugs in order of likelihood."""
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    yield base
    words = base.split('-')
    filler = {'for', 'the', 'of', 'and', 'a', 'an'}
    no_filler = '-'.join(w for w in words if w not in filler)
    if no_filler != base:
        yield no_filler
    descriptors = {'national', 'federal', 'community', 'state', 'first', 'american'}
    no_desc = '-'.join(w for w in words if w not in descriptors)
    if no_desc != base and no_desc != no_filler:
        yield no_desc
    core_drops = {'bank', 'savings', 'financial', 'trust', 'na', 'fsb', 'ssb'}
    core = '-'.join(w for w in words if w not in core_drops)
    if core and core != base:
        yield core + '-bank'
        yield core + '-savings-bank'


async def fetch_da_fast(bank_name, session):
    """
    Fetch DepositAccounts.com via aiohttp (no browser).
    ~200ms vs ~4s with Playwright. Returns extract_rates dict or None.
    Also checks for rate staleness and flags old data.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for slug in da_slugs(bank_name):
        url = f"https://www.depositaccounts.com/banks/{slug}.html"
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=8),
                                   ssl=False, allow_redirects=True) as resp:
                if resp.status != 200:
                    continue
                final_url = str(resp.url)
                if "search" in final_url or "404" in final_url:
                    continue
                html = await resp.text(errors="replace")
                # Strip HTML tags for text extraction
                text = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.I)
                text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.I)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text)

                if "APY" not in text and "%" not in text:
                    continue

                r = extract_rates(text)
                found = sum(1 for k in ["checking", "savings", "cd"] if r.get(k))
                if found == 0:
                    continue

                # Staleness check
                date_str, age_days = check_rate_staleness(text)
                stale_flag = ""
                if age_days is not None and age_days > 60:
                    stale_flag = f" ⚠️ DA data {age_days}d old"
                elif age_days is not None and age_days > 30:
                    stale_flag = f" (DA: {age_days}d ago)"

                crawl_state["log"].append(
                    f"    [DA-fast] ✓ {slug}: sav={r.get('savings')} "
                    f"cd={r.get('cd')} chk={r.get('checking')}{stale_flag}"
                )
                r["_source"] = url
                r["_stale_flag"] = stale_flag
                return r
        except Exception as e:
            crawl_state["log"].append(f"    [DA-fast] {slug}: {e}")
            continue
    return None


# ── Call Report loader ────────────────────────────────────────────────────────
# Moved to call_reports.py (load_call_reports, CALL_REPORTS_DIR imported above).

# ── Rate extraction ───────────────────────────────────────────────────────────

# extract_rates moved to extraction.py



# ── AI Vision fallback ────────────────────────────────────────────────────────

async def capture_carousel_screenshots(page, max_slides=5):
    """
    A single screenshot only captures whichever carousel slide happens to be
    showing at that instant — a CD promo sitting on slide 3 of a rotating
    hero banner is invisible to a one-shot capture. This detects common
    carousel/slider indicator patterns (Slick, Swiper, Bootstrap, Owl, and
    generic dot/pagination UIs), clicks through each slide, and returns one
    screenshot per slide. Falls back to a single normal screenshot if no
    carousel is detected or anything goes wrong — never worse than before.
    """
    CAROUSEL_SELECTORS = [
        ".slick-dots li button", ".slick-dots li",
        ".swiper-pagination-bullet",
        ".carousel-indicators [data-bs-slide-to]", ".carousel-indicators li",
        ".owl-dots .owl-dot",
        "[class*='carousel'] [class*='dot']",
        "[class*='slider'] [class*='dot']",
        "[class*='banner'] [class*='dot']",
        "[class*='pagination'] button",
        "[role='tablist'][class*='carousel'] [role='tab']",
    ]
    try:
        dots = None
        for sel in CAROUSEL_SELECTORS:
            candidates = page.locator(sel)
            count = await candidates.count()
            if 2 <= count <= 8:   # sane range — avoids false positives on unrelated repeated UI
                dots = candidates
                break
        if not dots:
            return [await page.screenshot(full_page=True, type="png")]

        count = await dots.count()
        shots = []
        for i in range(min(count, max_slides)):
            try:
                await dots.nth(i).click(timeout=2000)
                await page.wait_for_timeout(600)   # let the slide transition finish
            except Exception:
                pass   # if a click fails, still grab whatever's showing
            shots.append(await page.screenshot(full_page=True, type="png"))
        return shots if shots else [await page.screenshot(full_page=True, type="png")]
    except Exception:
        try:
            return [await page.screenshot(full_page=True, type="png")]
        except Exception:
            return []


async def ai_vision_extract(page, url, bank_name):
    """
    Screenshot the current page and send to Claude Haiku for rate extraction.
    Returns same dict as extract_rates(), or None on failure.
    Falls back to HTML text extraction if screenshot fails.
    """
    if not ANTHROPIC_OK:
        return None
    try:
        # Multiple slides if this page has a rotating carousel/banner —
        # a single screenshot would miss a promo rate on any slide other
        # than whichever one happened to be showing.
        screenshots = await capture_carousel_screenshots(page)
        if not screenshots:
            return None

        # Phase 4: extraction_cache — same bank+url+pixel content as a prior
        # crawl means an identical extraction would result, so skip the LLM call.
        content_hash = _content_hash(bank_name, url, *screenshots)
        cached = await _extraction_cache_get(content_hash)
        if cached is not None:
            crawl_state["log"].append("    [AI] cache hit — page unchanged since last extraction, skipped LLM call")
            return cached

        if len(screenshots) > 1:
            crawl_state["log"].append(
                f"    [AI] carousel detected — captured {len(screenshots)} slides")
        img_blocks = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": base64.standard_b64encode(shot).decode("utf-8")}}
            for shot in screenshots
        ]

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        slide_note = (f"\nThese are {len(screenshots)} images captured from a rotating "
                      f"promotional banner/carousel on this page (one per slide) — "
                      f"check ALL of them for rate information, not just the first."
                      if len(screenshots) > 1 else "")
        prompt = f"""You are analyzing a bank rates page for {bank_name}.{slide_note}
Extract the STANDARD deposit rates. Return ONLY JSON, no explanation:
{{
  "checking": <standard checking APY% float or null>,
  "savings": <REGULAR/basic savings APY% float or null — NOT the high-yield product>,
  "high_yield_savings": <a SEPARATE high-yield/premium/elite savings product's APY% float or null, if this page shows one distinct from regular savings>,
  "cd": <highest CD APY% float or null>,
  "cd_term": <term for that CD like "12-month" or null>,
  "cd_ladder": <array of EVERY distinct CD term/rate shown, e.g.
   [{{"term": "6-month", "apy": 3.5}}, {{"term": "12-month", "apy": 4.1}}] — include the
   highest-APY one too (it should match "cd"/"cd_term" above); empty array if only one term shown>,
  "money_market": <money market APY% float or null>,
  "min_balance": <minimum balance string like "$1,000" or null>
}}
Critical rules:
- DO NOT cross-assign rates between product types (a CD rate is never savings)
- Label each savings-type product by what it actually is: a plain/basic/statement
  savings account goes in "savings"; a product explicitly branded high-yield,
  premium, or elite savings goes in "high_yield_savings". If the page shows only
  one of the two, populate that field and leave the other null — don't force a
  single high-yield product into "savings" just because there's no second one
- For checking/savings/money_market: use the HIGHEST advertised APY for that product type
- For CD: use the HIGHEST APY shown, record its term — AND list every term you see in cd_ladder
- If a rate says "up to X%" use X
- If you only see a promo banner with no rate table, return null for that field
- Return ONLY the JSON object"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": img_blocks + [{"type": "text", "text": prompt}]
            }]
        )
        _track_llm_usage(response)

        import json
        txt = response.content[0].text.strip()
        result = parse_json_object(txt)   # tolerates preamble / fences

        # Validate and clean
        cleaned = {
            "checking":          float(result["checking"])          if result.get("checking")          else None,
            "savings":           float(result["savings"])            if result.get("savings")           else None,
            "high_yield_savings": float(result["high_yield_savings"]) if result.get("high_yield_savings") else None,
            "cd":                float(result["cd"])                 if result.get("cd")                else None,
            "cd_term":           result.get("cd_term"),
            "cd_ladder":         _clean_cd_ladder(result.get("cd_ladder")),
            "money_market":      float(result["money_market"])       if result.get("money_market")      else None,
            "min_balance":       result.get("min_balance"),
        }
        await _extraction_cache_put(content_hash, bank_name, url, cleaned, "claude-haiku-4-5-20251001")
        return cleaned

    except Exception as e:
        crawl_state["log"].append(f"    [AI] vision error: {e}")
        return None


# ── DepositAccounts.com scraper (browser fallback when aiohttp not available) ──

async def scrape_deposit_accounts(page, bank_name):
    """
    Browser-based DA fallback — only used when aiohttp session isn't available.
    Prefer fetch_da_fast() which is ~20x faster.
    """
    for slug in da_slugs(bank_name):
        url = f"https://www.depositaccounts.com/banks/{slug}.html"
        try:
            crawl_state["log"].append(f"    [DA] → depositaccounts.com/banks/{slug}")
            await _goto_with_retry(page, url, timeout=AGENT_PAGE_LOAD_TIMEOUT_MS)
            await page.wait_for_timeout(1500)
            current_url = page.url
            if "search" in current_url or "404" in current_url:
                continue
            text = await page.inner_text("body")
            if "APY" not in text and "%" not in text:
                continue
            r = extract_rates(text)
            found = sum(1 for k in ["checking", "savings", "cd"] if r.get(k))
            if found > 0:
                # Staleness check
                date_str, age_days = check_rate_staleness(text)
                if age_days is not None and age_days > 60:
                    r["_stale_flag"] = f" ⚠️ DA data {age_days}d old"
                crawl_state["log"].append(
                    f"    [DA] ✓ {found}/3 sav={r.get('savings')} cd={r.get('cd')} chk={r.get('checking')}"
                    + r.get("_stale_flag", "")
                )
                r["_source"] = url
                return r
        except Exception as e:
            crawl_state["log"].append(f"    [DA] Error ({slug}): {e}")
            continue
    return None


# ── Web crawler ───────────────────────────────────────────────────────────────

async def crawl_bank(page, bank, timeout=12000, http_session=None):
    base = bank["bank_url"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    best = {"checking": None, "savings": None, "high_yield_savings": None,
            "cd": None, "cd_term": None, "cd_ladder": None, "money_market": None, "min_balance": None}
    found_on    = None
    source_urls = {}
    visited     = set()
    rate_tags   = {}   # accumulated tags across all pages
    rebrand_hint = bank.get("_rebrand_hint")   # pre-populated from preflight if found
    bank_urls_cfg = crawl_state.get("_bank_urls_cfg") or BANK_EXTRA_URLS
    extra    = next((urls for k, urls in bank_urls_cfg.items() if k in base.replace('www.','').lower()), [])
    # v3.4: ZIP-gate state — max 2 fill attempts per bank, remembers where it fired.
    # Prefer a ZIP this bank is already known (from bank_registry) to pass the
    # gate with — cheaper and more reliable than re-deriving from
    # branch_address/DEFAULT_ZIP every run, which is only a fallback now.
    _health = _bank_health(bank) or {}
    bank_zip = _health.get("zip_gate_zip") or get_bank_zip(bank)
    zip_gate = {"fills": 0, "used_on": None, "recovered": False}

    async def visit(url, priority=False):
        nonlocal found_on, rebrand_hint
        if url in visited: return
        # Early exit — all 4 tracked rates already found (checking/savings/cd/MM)
        if all(best[k] is not None for k in ["checking", "savings", "cd", "money_market"]):
            return
        # Page budget — don't crawl more than 12 pages per bank
        # (priority pages — "View Rates"/"See Details" links found on a page
        #  that already showed a min balance — get 2 bonus slots)
        if len(visited) >= (PAGE_BUDGET_PER_BANK + PAGE_BUDGET_PRIORITY_BONUS if priority else PAGE_BUDGET_PER_BANK):
            return
        visited.add(url)
        try:
            domain = re.match(r"https?://[^/]+", base)
            domain = domain.group(0) if domain else base

            # Fix 3: rate-keyword URLs (cd / certificate / savings / rates)
            # get explicit JS wait + scroll + accordion expansion up front
            path_part   = url.replace(domain, "", 1)
            is_rate_url = bool(RATE_URL_KEYWORD_PAT.search(path_part))
            did_expand  = False

            # domcontentloaded is much faster than networkidle on JS-heavy bank sites
            resp = await _goto_with_retry(page, url, timeout=PAGE_LOAD_TIMEOUT_MS)
            # v3.6: dead page — don't wait on it, don't expand it, don't dig it.
            # Most of the ~50 candidate paths 404, and rate-keyword 404s were
            # getting the full deep treatment (2s wait + scroll + accordions).
            if resp is not None and resp.status >= 400:
                return
            # Wait only for rate-relevant content, not a blanket delay
            try:
                await page.wait_for_selector(
                    "table, [class*='rate'], [class*='apy'], [class*='Rate'], "
                    "[id*='rate'], text=APY, text=Annual Percentage Yield",
                    timeout=2000)
            except: pass

            if is_rate_url:
                # v3.6: wait *up to* 2s for JS-rendered APY content instead of
                # always sleeping 2s — returns instantly once rates are present
                try:
                    await page.wait_for_selector("text=% APY", timeout=2000)
                except Exception:
                    pass
                n = await expand_page_content(page)
                did_expand = True
                if n:
                    crawl_state["log"].append(
                        f"    [Deep] {path_part or '/'}: expanded {n} element(s)")

            # v3.4: ZIP gate — market-specific rates behind a ZIP prompt.
            # Fill with the BMAP target branch's ZIP and sniff the XHR that
            # usually delivers the rates as JSON before the DOM renders them.
            sniffed_json_rates = {}
            if bank_zip and zip_gate["fills"] < 2:
                gate_el = await find_zip_gate(page)
                if gate_el:
                    zip_gate["fills"] += 1
                    crawl_state["log"].append(
                        f"    [ZIP-gate] {path_part or '/'}: ZIP prompt found — filling {bank_zip}")
                    sniff_bucket = []
                    handler = make_json_sniffer(sniff_bucket)
                    try:
                        page.on("response", handler)
                    except Exception:
                        handler = None
                    submitted = await fill_zip_gate(page, gate_el, bank_zip)
                    if handler:
                        try:
                            page.remove_listener("response", handler)
                        except Exception:
                            pass
                    if submitted:
                        zip_gate["used_on"] = url
                        for body in sniff_bucket[:10]:
                            try:
                                jr = extract_rates_from_json(json.loads(body))
                            except Exception:
                                continue
                            for k, v in jr.items():
                                if k not in sniffed_json_rates or v > sniffed_json_rates[k]:
                                    sniffed_json_rates[k] = v
                        if sniffed_json_rates:
                            crawl_state["log"].append(
                                f"    [ZIP-gate] XHR sniff: "
                                + ", ".join(f"{k}={v}" for k, v in sniffed_json_rates.items()))
                        # gated content just rendered — give it a scroll/expand
                        if not did_expand:
                            await expand_page_content(page)
                            did_expand = True
                        await page.wait_for_timeout(800)

            text = await page.inner_text("body")

            # Rebrand detection on page text
            if not rebrand_hint:
                hint = detect_rebrand(text, bank["bank_name"])
                if hint:
                    rebrand_hint = hint
                    crawl_state["log"].append(f"    [⚠️ REBRAND] {bank['bank_name']} → {hint}")

            r = extract_rates(text)

            # Merge rate_tags from this page
            for k, tags in r.get("rate_tags", {}).items():
                if k not in rate_tags:
                    rate_tags[k] = tags

            core_keys = ("checking", "savings", "cd", "money_market")

            def merge_into_r(r2):
                """Fill r's gaps from a second extraction pass."""
                for k in list(r.keys()):
                    if k != "rate_tags" and r[k] is None and r2.get(k) is not None:
                        r[k] = r2[k]
                for k, tags in r2.get("rate_tags", {}).items():
                    if k not in rate_tags:
                        rate_tags[k] = tags

            # v3.4: rates sniffed from the ZIP-gate XHR fill gaps in this page's
            # extraction (DOM extraction wins where both found a value)
            if sniffed_json_rates:
                merge_into_r(sniffed_json_rates)

            # Did the gate we just filled on THIS page actually pay off? Judge
            # by the page's own extraction (r), not only the XHR sniff — the
            # gated content usually re-renders into the DOM too, so a gate that
            # worked but wasn't JSON-sniffable would otherwise never register
            # as "recovered" and bank_zip would never get learned.
            if zip_gate["used_on"] == url and any(r.get(k) for k in core_keys):
                zip_gate["recovered"] = True

            # Fix: also try DOM table extraction when inner_text misses structure
            if not any(r.get(k) for k in core_keys):
                try:
                    table_text = await page.evaluate("""() => {
                        return Array.from(document.querySelectorAll(
                            'table, [class*="rate"], [class*="product"], [class*="apy"]'))
                            .map(el => el.innerText).join('\\n---\\n');
                    }""")
                    if table_text.strip():
                        merge_into_r(extract_rates(table_text))
                except: pass

            # Fix 1: a min balance with no rate means we're ON the right page
            # but the rates haven't surfaced yet — dig before walking away.
            if (r.get("min_balance") or is_rate_url) and not any(r.get(k) for k in core_keys):
                crawl_state["log"].append(
                    f"    [Deep] {path_part or '/'}: "
                    + ("min balance found, no APY — digging deeper..."
                       if r.get("min_balance") else "rate-keyword page, no APY — digging..."))
                # Pass 1: scroll + expand accordions (if not already done), re-read
                if not did_expand:
                    await expand_page_content(page)
                    did_expand = True
                await page.wait_for_timeout(1200)
                try:
                    merge_into_r(extract_rates(await page.inner_text("body")))
                except Exception:
                    pass
                # Pass 1.5: click a "Check rates" style CTA (only reached here,
                # once accordion-expand + re-read already came up empty — see
                # click_rate_cta()'s docstring for why it isn't unconditional).
                # A modal opened this way is often itself ZIP-gated, so give
                # find_zip_gate/fill_zip_gate one more shot at it too.
                if not any(r.get(k) for k in core_keys):
                    if await click_rate_cta(page):
                        if bank_zip and zip_gate["fills"] < 2:
                            gate_el = await find_zip_gate(page)
                            if gate_el:
                                zip_gate["fills"] += 1
                                crawl_state["log"].append(
                                    f"    [ZIP-gate] {path_part or '/'}: "
                                    f"found after CTA click — filling {bank_zip}")
                                if await fill_zip_gate(page, gate_el, bank_zip):
                                    zip_gate["used_on"] = url
                                    await page.wait_for_timeout(800)
                        try:
                            merge_into_r(extract_rates(await page.inner_text("body")))
                        except Exception:
                            pass
                        if zip_gate["used_on"] == url and any(r.get(k) for k in core_keys):
                            zip_gate["recovered"] = True
                # Pass 2: harvest hidden text (collapsed panels via textContent)
                if not any(r.get(k) for k in core_keys):
                    hidden = await harvest_hidden_text(page)
                    if hidden:
                        merge_into_r(extract_rates(hidden))
                # Pass 3: follow "View Rates" / "See Details" links on THIS page
                if not any(r.get(k) for k in core_keys):
                    detail_links = await collect_rate_detail_links(page, domain)
                    detail_links = [u for u in detail_links if u not in visited]
                    if detail_links:
                        crawl_state["log"].append(
                            f"    [Deep] following {len(detail_links[:3])} detail link(s)")
                    # NOTE: recursing navigates `page` away — finish reading the
                    # current page (links below) BEFORE recursing, so collect
                    # general rate links now and recurse after.
                    pending_detail = detail_links[:3]
                else:
                    pending_detail = []
            else:
                pending_detail = []

            for k in best:
                if k == "rate_tags": continue
                if r.get(k) is not None:
                    if best[k] is None or (isinstance(r[k], float) and r[k] > best[k]):
                        best[k] = r[k]
                        if k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]:
                            source_urls[k] = url
            if r.get("cd_term") and not best["cd_term"]:
                best["cd_term"] = r["cd_term"]
            if any(r.get(k) for k in ["checking", "savings", "cd", "money_market"]):
                found_on = url
            links = await page.query_selector_all("a[href]")
            rate_links = []
            for link in links[:80]:
                try:
                    href = await link.get_attribute("href") or ""
                    txt  = (await link.inner_text()).strip()
                    full = href if href.startswith("http") else domain + href if href.startswith("/") else None
                    if not full or not full.startswith(domain) or full in visited: continue
                    if re.search(r"rate|apy|cd|savings|checking|deposit|certificate|offer|interest|account", href+txt, re.I):
                        rate_links.append(full)
                except: continue
            # Fix 1: detail links jump the queue with priority budget
            for u in pending_detail:
                await visit(u, priority=True)
            for u in rate_links[:6]:
                await visit(u)
        except: pass

    for url in extra:
        await visit(url)

    # Quick first pass — top priority paths only
    PRIORITY_PATHS = [
        "/rates", "/personal/rates", "/personal-banking/rates",
        "/current-rates", "/deposit-rates", "/interest-rates",
        "/personal/deposit-rates", "/rate-sheet", "/banking/rates",
        "/tools/rates.html",
    ]
    for path in PRIORITY_PATHS:
        await visit(base + path)

    # Early DA fallback — if still missing 2+ core rates after priority paths, go to DA now
    # Uses fast aiohttp fetch (~200ms) rather than browser (~4s)
    count = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    if count < 2:
        da = (await fetch_da_fast(bank["bank_name"], http_session)
              if http_session and AIOHTTP_OK
              else await scrape_deposit_accounts(page, bank["bank_name"]))
        if da:
            stale_flag = da.get("_stale_flag", "")
            for k in ["checking", "savings", "high_yield_savings", "cd", "cd_term", "cd_ladder", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]:
                        source_urls[k] = da.get("_source", "depositaccounts.com") + stale_flag

    # Full path scan — only if still incomplete after priority + early DA
    count = sum(1 for k in ["checking", "savings", "cd", "money_market"] if best[k] is not None)
    if count < 4:
        rate_paths_cfg = crawl_state.get("_rate_paths_cfg") or RATE_PATHS
        remaining = [p for p in rate_paths_cfg if p not in PRIORITY_PATHS]
        for path in remaining:
            await visit(base + path)

    # ── Site-search rescue ────────────────────────────────────────────────────
    # Path list exhausted and we have NOTHING — before asking an external
    # search engine, try the bank's OWN on-site search for "rates" first.
    # Often more precise than an external engine, since it queries the site's
    # own indexed content instead of relying on how well it's ranked
    # externally (this is how Modern Bank's real CD-rates page was found,
    # after every guessed path and the external rescue both came up empty).
    count = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    if count == 0:
        crawl_state["log"].append(
            f"    [Site-search] path list exhausted with 0/3 — trying {base}'s own search for \"rates\"")
        try:
            site_search_candidates = await find_rates_via_site_search(page, base)
            if site_search_candidates:
                crawl_state["log"].append(
                    f"    [Site-search] {len(site_search_candidates)} result(s), "
                    f"top: {site_search_candidates[0]}")
                for u in site_search_candidates:
                    await visit(u, priority=True)
            else:
                crawl_state["log"].append("    [Site-search] no usable search box or results found")
        except Exception as e:
            crawl_state["log"].append(f"    [Site-search] error: {e}")

    # ── Fix 2: search-engine rescue ──────────────────────────────────────────
    # Still nothing — ask an external search engine for `site:<domain> rates
    # APY` and crawl the top on-domain results (Fifth Third, Citizens,
    # Santander etc. publish rates on paths no static list will ever guess).
    search_candidates = []
    count = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    if count == 0:
        bank_domain = extract_domain(base)
        if not bank_domain:
            crawl_state["log"].append(
                f"    [SE-rescue] skipped — no valid bank_url in CSV for {bank['bank_name']}")
        else:
            crawl_state["log"].append(
                f"    [SE-rescue] path list exhausted with 0/3 — searching site:{bank_domain} rates APY")
            try:
                search_candidates = await find_rates_via_search_engine(page, bank_domain)
                for u in search_candidates:
                    await visit(u, priority=True)
            except Exception as e:
                crawl_state["log"].append(f"    [SE-rescue] error: {e}")

    # ── Final DepositAccounts.com fallback ──────────────────────────────────
    count = sum(1 for k in ["checking", "savings", "cd", "money_market"] if best[k] is not None)
    if count < 4:
        da = (await fetch_da_fast(bank["bank_name"], http_session)
              if http_session and AIOHTTP_OK
              else await scrape_deposit_accounts(page, bank["bank_name"]))
        if da:
            stale_flag = da.get("_stale_flag", "")
            for k in ["checking", "savings", "high_yield_savings", "cd", "cd_term", "cd_ladder", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]:
                        source_urls[k] = da.get("_source", "depositaccounts.com") + stale_flag

    # ── AI Vision fallback for empty/partial results ────────────────────────
    # Also trigger if the only CD result is low-confidence "best found"
    count = sum(1 for k in ["checking", "savings", "cd", "money_market"] if best[k] is not None)
    best_found_only = (count == 1 and best.get("cd_term") == "best found")
    if (count < 4 or best_found_only) and ANTHROPIC_OK and bank.get("bank_url",""):
        # If cd came from "best found" low-confidence path, clear it before AI confirms
        if best_found_only:
            best["cd"] = None
            best["cd_term"] = None
        try:
            # Pick the single best candidate URL — prefer found_on, then extra, then /rates
            domain = re.match(r"https?://[^/]+", base)
            domain_str = domain.group(0) if domain else base
            if found_on:
                vision_targets = [found_on]
            elif search_candidates:
                # Fix 2: search-engine hits are the best vision candidates —
                # they're confirmed on-domain rate pages we couldn't regex
                vision_targets = list(search_candidates)[:2]
            elif extra:
                vision_targets = list(extra)[:2]
            else:
                vision_targets = [domain_str + "/rates", domain_str + "/personal/rates", base]

            crawl_state["log"].append(f"    [AI] regex got {count}/3 — trying vision on up to {len(vision_targets)} page(s)...")

            for v_url in vision_targets[:3]:  # max 3 vision attempts per bank
                try:
                    v_resp = await page.goto(v_url, timeout=12000, wait_until="domcontentloaded")
                    # v3.6: don't screenshot a 404 — wastes an API call
                    if v_resp is not None and v_resp.status >= 400:
                        crawl_state["log"].append(f"    [AI] skip {v_url}: HTTP {v_resp.status}")
                        continue
                    await page.wait_for_timeout(1500)
                    # Fix 3: expand accordions before the screenshot so the
                    # vision model sees rates hidden in collapsed panels
                    try:
                        await expand_page_content(page)
                    except Exception:
                        pass
                    # v3.4: if a ZIP gate blocks this page, fill it so the
                    # screenshot shows market rates instead of the prompt
                    if bank_zip and zip_gate["fills"] < 2:
                        try:
                            gate_el = await find_zip_gate(page)
                            if gate_el:
                                zip_gate["fills"] += 1
                                crawl_state["log"].append(
                                    f"    [ZIP-gate] vision page: filling {bank_zip}")
                                if await fill_zip_gate(page, gate_el, bank_zip):
                                    zip_gate["used_on"] = v_url
                        except Exception:
                            pass
                    ai_result = await ai_vision_extract(page, v_url, bank["bank_name"])
                    if ai_result:
                        filled = 0
                        for k in ["checking", "savings", "high_yield_savings", "cd", "cd_term", "cd_ladder", "money_market", "min_balance"]:
                            if ai_result.get(k) is not None and best.get(k) is None:
                                best[k] = ai_result[k]
                                filled += 1
                                if k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]:
                                    source_urls[k] = f"[AI] {v_url}"
                        if zip_gate["used_on"] == v_url and any(
                                ai_result.get(k) for k in ("checking", "savings", "cd", "money_market")):
                            zip_gate["recovered"] = True
                        crawl_state["ai_calls"] = crawl_state.get("ai_calls", 0) + 1
                        crawl_state["log"].append(f"    [AI] ✓ {v_url.split('/')[-1] or 'home'}: sav={ai_result.get('savings')} cd={ai_result.get('cd')} chk={ai_result.get('checking')}")
                        # Stop if we have all 4 tracked rates
                        new_count = sum(1 for k in ["checking", "savings", "cd", "money_market"] if best[k] is not None)
                        if new_count >= 4:
                            break
                except Exception as ve:
                    crawl_state["log"].append(f"    [AI] skip {v_url}: {ve}")
                    continue
        except Exception as e:
            crawl_state["log"].append(f"    [AI] fallback error: {e}")

    count  = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    status = "Found" if count == 3 else "Partial" if count > 0 else "Not public"

    # Build rate_tags note suffixes  e.g. "Checking 3.30% [conditional]"
    def rate_label(product, key):
        val = best.get(key)
        if not val: return None
        label = f"{product} {val:.2f}%"
        tags = rate_tags.get(key, [])
        if tags:
            label += f" [{'/'.join(tags)}]"
        return label

    parts  = []
    if rebrand_hint:
        parts.append(f"⚠️ REBRAND DETECTED: now '{rebrand_hint}' — rates may be stale")
        status = "Partial"   # downgrade confidence on rebrand
    if zip_gate["used_on"]:
        parts.append(f"ZIP-gated: rates for {bank_zip} "
                     f"({(bank.get('branch_address') or '').strip()[:60]})".rstrip(" ("))
    rate_parts = []
    lbl = rate_label("CD", "cd")
    if lbl: rate_parts.append(lbl + (f" ({best['cd_term']})" if best.get('cd_term') else ''))
    lbl = rate_label("Savings", "savings")
    if lbl: rate_parts.append(lbl)
    lbl = rate_label("High-Yield Savings", "high_yield_savings")
    if lbl: rate_parts.append(lbl)
    lbl = rate_label("Checking", "checking")
    if lbl: rate_parts.append(lbl)
    lbl = rate_label("Money Mkt", "money_market")
    if lbl: rate_parts.append(lbl)
    if rate_parts: parts.append(", ".join(rate_parts))
    if best["min_balance"]: parts.append(f"Min: {best['min_balance']}")
    if not parts: parts.append("Rates not publicly listed")
    unique_sources = list(dict.fromkeys(source_urls.values()))
    source_note = (" | Source: " + unique_sources[0]) if unique_sources else ""

    # Merge Call Report data by RSSDID
    rssdid = bank.get("RSSDID") or bank.get("rssdid") or ""
    cr = {}
    prev_cr = {}
    if rssdid:
        try:
            rid = int(str(rssdid).strip())
            cr      = crawl_state["cr_data"].get(rid, {})
            prev_cr = crawl_state["prev_cr_data"].get(rid, {})
        except (ValueError, TypeError):
            cr = {}
            prev_cr = {}

    def delta(curr_key):
        """Return QoQ difference (current - prev), or None if either is missing."""
        c = cr.get(curr_key)
        p = prev_cr.get(curr_key)
        if c is None or p is None:
            return None
        return round(c - p, 2)

    def vulnerability_flag():
        """
        Flag a competitor as vulnerable when their implied APY (what they actually pay)
        meaningfully exceeds their best advertised rate (what they tell new customers).
        Gap > 0.50% = Vulnerable  |  0.25–0.50% = Watch  |  else = Normal
        Uses the highest implied signal available: savings > CD > cost_of_deposits.
        """
        implied  = cr.get("cr_savings_apy") or cr.get("cr_cd_apy") or cr.get("cr_cost_of_deposits")
        scraped  = best.get("savings") or best.get("cd") or best.get("checking")
        if implied is None or scraped is None:
            return ""
        gap = round(implied - scraped, 2)
        if gap >= 0.50:
            return f"Vulnerable (gap +{gap:.2f}%)"
        if gap >= 0.25:
            return f"Watch (gap +{gap:.2f}%)"
        return "Normal"

    return {
        **bank,
        "checking_apy":              best["checking"],
        # Wide-table backward compat: falls back to the HYS rate when that's
        # the only savings-type product found, same as pre-split behavior —
        # the genuine split lives in high_yield_savings_apy for the tidy table.
        "savings_apy":               best["savings"] if best["savings"] is not None else best["high_yield_savings"],
        "high_yield_savings_apy":    best["high_yield_savings"],
        "cd_apy":                    best["cd"],
        "cd_term":                   best["cd_term"],
        "cd_ladder":                 best["cd_ladder"],
        "money_market_apy":          best["money_market"],
        "min_balance":               best["min_balance"],
        "status":                    status,
        "note":                      " | ".join(parts) + source_note,
        "source_url":                unique_sources[0] if unique_sources else "",
        "source_url_checking":       source_urls.get("checking", ""),
        "source_url_high_yield_savings": source_urls.get("high_yield_savings", ""),
        "source_url_savings":        source_urls.get("savings") or source_urls.get("high_yield_savings", ""),
        "source_url_cd":             source_urls.get("cd", ""),
        "crawled_at":                datetime.now().strftime("%Y-%m-%d %H:%M"),
        # Phase 5: structured conditional/promo tags per product, from
        # tag_rate_context() — survives into build_rate_observations() instead
        # of only being rendered into the free-text note.
        "rate_tags":                 dict(rate_tags),
        # Enrichment fields from CSV
        "bank_type":                 bank.get("bank_type", ""),
        "branch_address":            bank.get("branch_address", ""),
        # Vulnerability signal
        "vulnerability_flag":        vulnerability_flag(),
        # Current quarter CR fields
        "cr_savings_apy":            cr.get("cr_savings_apy"),
        "cr_checking_apy":           cr.get("cr_checking_apy"),
        "cr_cd_apy":                 cr.get("cr_cd_apy"),
        "cr_cost_of_deposits":       cr.get("cr_cost_of_deposits"),
        "cr_total_deposits_m":       cr.get("cr_total_deposits_m"),
        "cr_period":                 cr.get("cr_period", crawl_state.get("cr_period") or ""),
        # Previous quarter CR fields
        "prev_cr_savings_apy":       prev_cr.get("cr_savings_apy"),
        "prev_cr_checking_apy":      prev_cr.get("cr_checking_apy"),
        "prev_cr_cd_apy":            prev_cr.get("cr_cd_apy"),
        "prev_cr_cost_of_deposits":  prev_cr.get("cr_cost_of_deposits"),
        "prev_cr_total_deposits_m":  prev_cr.get("cr_total_deposits_m"),
        "cr_prev_period":            prev_cr.get("cr_period", crawl_state.get("cr_prev_period") or ""),
        # QoQ deltas
        "delta_savings_apy":         delta("cr_savings_apy"),
        "delta_checking_apy":        delta("cr_checking_apy"),
        "delta_cd_apy":              delta("cr_cd_apy"),
        "delta_cost_of_deposits":    delta("cr_cost_of_deposits"),
        # Not a Supabase column — read by update_bank_registry() to learn a
        # working ZIP for this bank's gate, only set when it actually recovered
        # a rate (never written when the gate was tried but yielded nothing).
        "_zip_gate_zip":             bank_zip if zip_gate["recovered"] else None,
    }



# ── AI Agent Crawler ──────────────────────────────────────────────────────────

async def ai_agent_crawl(page, bank):
    """
    AI-first crawler. Claude navigates the bank website like a human:
    1. Go to homepage → screenshot → ask Claude where rates are
    2. Navigate to rates page → screenshot → extract all rates
    3. If incomplete → follow sub-links Claude identifies → repeat
    Returns same result dict as crawl_bank().
    """
    base = bank["bank_url"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base

    best = {"checking": None, "savings": None, "high_yield_savings": None,
            "cd": None, "cd_term": None, "cd_ladder": None, "money_market": None, "min_balance": None}
    source_urls = {}
    visited = set()
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    async def screenshot_b64():
        try:
            data = await page.screenshot(full_page=True, type="png")
            return base64.standard_b64encode(data).decode("utf-8")
        except:
            data = await page.screenshot(type="png")
            return base64.standard_b64encode(data).decode("utf-8")

    async def ask_claude(img_b64, question):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": question}
                ]}]
            )
            _track_llm_usage(resp)
            return resp.content[0].text.strip()
        except Exception as e:
            return f"ERROR: {e}"

    async def extract_rates_ai(img_b64, url):
        """Ask Claude to extract all rates from a screenshot."""
        prompt = f"""Extract ALL deposit rates from this bank rates page for {bank['bank_name']}.
Return ONLY valid JSON, no explanation:
{{"checking": <float or null>, "savings": <REGULAR savings float or null, not high-yield>,
  "high_yield_savings": <a SEPARATE high-yield/premium savings product's float or null, only if
   distinct from regular savings>, "cd": <float or null>,
  "cd_term": <"12-month" or null>,
  "cd_ladder": <array of EVERY distinct CD term/rate shown, e.g.
   [{{"term": "6-month", "apy": 3.5}}, {{"term": "12-month", "apy": 4.1}}], empty array if only one term>,
  "money_market": <float or null>, "min_balance": <"$1,000" or null>}}
Rules: use highest APY shown for each type. If only one savings-type product exists, put it in
whichever field actually describes it (savings vs high_yield_savings), leave the other null.
List every CD term you see in cd_ladder, not just the highest. If no rates visible return all nulls."""
        content_hash = _content_hash(bank["bank_name"], url, img_b64)
        cached = await _extraction_cache_get(content_hash)
        if cached is not None:
            crawl_state["log"].append("    [Agent] cache hit — page unchanged since last extraction, skipped LLM call")
            return cached
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt}
                ]}]
            )
            _track_llm_usage(resp)
            txt = resp.content[0].text.strip()
            result = parse_json_object(txt)   # v3.5: tolerate non-JSON preamble
            cleaned = {
                "checking":          float(result["checking"])          if result.get("checking")          else None,
                "savings":           float(result["savings"])            if result.get("savings")           else None,
                "high_yield_savings": float(result["high_yield_savings"]) if result.get("high_yield_savings") else None,
                "cd":                float(result["cd"])                 if result.get("cd")                else None,
                "cd_term":           result.get("cd_term"),
                "cd_ladder":         _clean_cd_ladder(result.get("cd_ladder")),
                "money_market":      float(result["money_market"])       if result.get("money_market")      else None,
                "min_balance":       result.get("min_balance"),
            }
            await _extraction_cache_put(content_hash, bank["bank_name"], url, cleaned, "claude-haiku-4-5-20251001")
            return cleaned
        except:
            return None

    async def navigate_and_extract(url, depth=0):
        """Navigate to URL, extract rates, follow Claude-identified links if needed."""
        if url in visited or depth > 3:
            return
        visited.add(url)
        current_count = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
        if current_count >= 3:
            return
        try:
            crawl_state["log"].append(f"    [Agent] → {url.replace(base,'') or '/'}")
            await _goto_with_retry(page, url, timeout=AGENT_PAGE_LOAD_TIMEOUT_MS)
            await page.wait_for_timeout(2500)
            # Wait for dynamic content
            try: await page.wait_for_selector("text=APY", timeout=3000)
            except: pass

            # Fix 3: on rate-keyword URLs, scroll + expand accordions so the
            # screenshot shows JS-rendered tables and collapsed panel content
            if RATE_URL_KEYWORD_PAT.search(url.replace(base, "")):
                try:
                    await expand_page_content(page)
                except Exception:
                    pass

            img_b64 = await screenshot_b64()
            crawl_state["ai_calls"] = crawl_state.get("ai_calls", 0) + 1

            # Extract rates from this page
            rates = await extract_rates_ai(img_b64, url)
            if rates:
                filled = False
                for k in ["checking","savings","high_yield_savings","cd","cd_term", "cd_ladder","money_market","min_balance"]:
                    if rates.get(k) is not None and best.get(k) is None:
                        best[k] = rates[k]
                        filled  = True
                        if k in ["checking","savings","high_yield_savings","cd","money_market"]:
                            source_urls[k] = url
                if filled:
                    c = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
                    crawl_state["log"].append(
                        f"    [Agent] ✓ sav={rates.get('savings')} cd={rates.get('cd')} chk={rates.get('checking')} ({c}/3)")

            # If still incomplete, ask Claude which links to follow
            c = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
            if c < 3 and depth < 2:
                link_q = """Look at this bank webpage. List up to 4 URLs or link texts that would lead to deposit rate information (savings, checking, CD rates). 
Return ONLY a JSON array of strings (href values or link text). Example: ["/rates", "/personal/savings", "View Rates"]
If no relevant links found return []."""
                links_txt = await ask_claude(img_b64, link_q)
                try:
                    links_txt = links_txt.replace("```json","").replace("```","").strip()
                    suggested = json.loads(links_txt)
                    domain    = re.match(r"https?://[^/]+", base)
                    domain_s  = domain.group(0) if domain else base
                    for lnk in suggested[:4]:
                        if not lnk or not isinstance(lnk, str): continue
                        if lnk.startswith("http"):
                            full = lnk if lnk.startswith(domain_s) else None
                        elif lnk.startswith("/"):
                            full = domain_s + lnk
                        else:
                            # Try to find matching link on page
                            try:
                                el = await page.query_selector(f'a:has-text("{lnk}")')
                                full = await el.get_attribute("href") if el else None
                                if full and not full.startswith("http"):
                                    full = domain_s + full
                            except:
                                full = None
                        if full and full not in visited and domain_s in full:
                            await navigate_and_extract(full, depth + 1)
                except:
                    pass
        except Exception as e:
            crawl_state["log"].append(f"    [Agent] ✗ {e}")

    # Step 1: Check the config-driven bank/URL registry first
    bank_urls_cfg = crawl_state.get("_bank_urls_cfg") or BANK_EXTRA_URLS
    extra = next((urls for k, urls in bank_urls_cfg.items() if k in base.replace("www.","").lower()), [])
    for url in extra:
        await navigate_and_extract(url)

    # Step 2: Start from homepage if still incomplete
    c = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
    if c < 3:
        await navigate_and_extract(base)

    # Step 3: DepositAccounts.com fallback if still incomplete
    c = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
    if c < 3:
        da = await scrape_deposit_accounts(page, bank["bank_name"])
        if da:
            for k in ["checking", "savings", "high_yield_savings", "cd", "cd_term", "cd_ladder", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "high_yield_savings", "cd", "money_market"]:
                        source_urls[k] = da.get("_source", "depositaccounts.com")

    # Build result
    count  = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
    status = _classify_status(count)
    parts  = []
    if any(best[k] for k in ["checking","savings","cd","money_market"]):
        rate_parts = []
        if best["cd"]:           rate_parts.append(f"CD {best['cd']:.2f}%{' ('+best['cd_term']+')' if best['cd_term'] else ''}")
        if best["savings"]:      rate_parts.append(f"Savings {best['savings']:.2f}%")
        if best["high_yield_savings"]: rate_parts.append(f"High-Yield Savings {best['high_yield_savings']:.2f}%")
        if best["checking"]:     rate_parts.append(f"Checking {best['checking']:.2f}%")
        if best["money_market"]: rate_parts.append(f"Money Mkt {best['money_market']:.2f}%")
        if rate_parts: parts.append(", ".join(rate_parts))
    if best["min_balance"]: parts.append(f"Min: {best['min_balance']}")
    if not parts: parts.append("Rates not publicly listed")

    unique_sources = list(dict.fromkeys(source_urls.values()))
    source_note    = (" | Source: " + unique_sources[0]) if unique_sources else ""

    # Merge Call Report data
    rssdid  = bank.get("RSSDID") or bank.get("rssdid") or ""
    cr      = {}
    prev_cr = {}
    if rssdid:
        try:
            rid     = int(str(rssdid).strip())
            cr      = crawl_state["cr_data"].get(rid, {})
            prev_cr = crawl_state["prev_cr_data"].get(rid, {})
        except: pass

    def delta(k):
        c, p = cr.get(k), prev_cr.get(k)
        return round(c - p, 2) if c is not None and p is not None else None

    def vuln():
        implied = cr.get("cr_savings_apy") or cr.get("cr_cd_apy") or cr.get("cr_cost_of_deposits")
        scraped = best.get("savings") or best.get("cd") or best.get("checking")
        if not implied or not scraped: return ""
        gap = round(implied - scraped, 2)
        if gap >= 0.50: return f"Vulnerable (gap +{gap:.2f}%)"
        if gap >= 0.25: return f"Watch (gap +{gap:.2f}%)"
        return "Normal"

    return {
        **bank,
        "checking_apy":             best["checking"],
        # Wide-table backward compat: falls back to the HYS rate when that's
        # the only savings-type product found — see crawl_bank's return dict.
        "savings_apy":              best["savings"] if best["savings"] is not None else best["high_yield_savings"],
        "high_yield_savings_apy":   best["high_yield_savings"],
        "cd_apy":                   best["cd"],
        "cd_term":                  best["cd_term"],
        "cd_ladder":                best["cd_ladder"],
        "money_market_apy":         best["money_market"],
        "min_balance":              best["min_balance"],
        "status":                   status,
        "note":                     " | ".join(parts) + source_note,
        "source_url":               unique_sources[0] if unique_sources else "",
        "source_url_checking":      source_urls.get("checking",""),
        "source_url_savings":       source_urls.get("savings") or source_urls.get("high_yield_savings",""),
        "source_url_high_yield_savings": source_urls.get("high_yield_savings",""),
        "source_url_cd":            source_urls.get("cd",""),
        "crawled_at":               datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bank_type":                bank.get("bank_type",""),
        "branch_address":           bank.get("branch_address",""),
        "vulnerability_flag":       vuln(),
        "cr_savings_apy":           cr.get("cr_savings_apy"),
        "cr_checking_apy":          cr.get("cr_checking_apy"),
        "cr_cd_apy":                cr.get("cr_cd_apy"),
        "cr_cost_of_deposits":      cr.get("cr_cost_of_deposits"),
        "cr_total_deposits_m":      cr.get("cr_total_deposits_m"),
        "cr_period":                cr.get("cr_period", crawl_state.get("cr_period") or ""),
        "prev_cr_savings_apy":      prev_cr.get("cr_savings_apy"),
        "prev_cr_checking_apy":     prev_cr.get("cr_checking_apy"),
        "prev_cr_cd_apy":           prev_cr.get("cr_cd_apy"),
        "prev_cr_cost_of_deposits": prev_cr.get("cr_cost_of_deposits"),
        "prev_cr_total_deposits_m": prev_cr.get("cr_total_deposits_m"),
        "cr_prev_period":           prev_cr.get("cr_period", crawl_state.get("cr_prev_period") or ""),
        "delta_savings_apy":        delta("cr_savings_apy"),
        "delta_checking_apy":       delta("cr_checking_apy"),
        "delta_cd_apy":             delta("cr_cd_apy"),
        "delta_cost_of_deposits":   delta("cr_cost_of_deposits"),
    }



# ── Chat Mode Crawler ─────────────────────────────────────────────────────────

# Common chat widget selectors across platforms
CHAT_SELECTORS = [
    # Open/trigger buttons
    "button[aria-label*='chat' i]", "button[aria-label*='help' i]",
    "button[class*='chat' i]", "button[class*='livechat' i]",
    "div[class*='chat-button' i]", "div[class*='chat-launcher' i]",
    "div[id*='chat-button' i]", "div[id*='livechat' i]",
    "#chat-widget-container button", ".intercom-launcher",
    ".drift-widget-controller", "[data-testid='live-chat-button']",
    "iframe[title*='chat' i]", "iframe[id*='chat' i]",
    # Direct input fields (chat already open)
    "input[placeholder*='message' i]", "input[placeholder*='type' i]",
    "textarea[placeholder*='message' i]", "textarea[placeholder*='type' i]",
    "div[contenteditable='true'][aria-label*='message' i]",
]

CHAT_INPUT_SELECTORS = [
    "input[placeholder*='message' i]", "input[placeholder*='type' i]",
    "input[placeholder*='ask' i]", "input[placeholder*='question' i]",
    "textarea[placeholder*='message' i]", "textarea[placeholder*='type' i]",
    "div[contenteditable='true']", "input[type='text'][class*='chat' i]",
    "#chat-input", ".chat-input input", ".message-input",
]

CHAT_SEND_SELECTORS = [
    "button[aria-label*='send' i]", "button[type='submit'][class*='chat' i]",
    "button[class*='send' i]", "button[id*='send' i]",
    "button[aria-label*='Send message' i]", ".send-button",
]

CHAT_OPENER_MESSAGE = (
    "Hi! I'm interested in opening a savings account or CD — "
    "around $25,000 to deposit. Could you tell me what rates you're "
    "currently offering? Any current promotions would be great to know about."
)

CHAT_FOLLOWUP = "Do you have any CD specials right now? What about savings account rates?"


async def chat_crawl(page, bank):
    """
    Finds and interacts with the bank's live chat widget.
    Extracts rates from the conversation using Claude.
    Returns partial result dict with chat-sourced rates, or None if no chat found.
    """
    if not ANTHROPIC_OK:
        return None

    base = bank["bank_url"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base

    crawl_state["log"].append(f"    [Chat] Looking for chat widget on {base}...")

    try:
        await page.goto(base, timeout=15000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # ── Step 1: Find and click chat launcher ─────────────────────────────
        chat_opened = False

        # Check for iframes first (many chat widgets load in iframes)
        frames = page.frames
        for frame in frames:
            if any(kw in (frame.url or "").lower() for kw in ["chat","intercom","drift","zendesk","freshchat","livechat"]):
                crawl_state["log"].append(f"    [Chat] Found chat iframe: {frame.url[:60]}")
                try:
                    btn = await frame.query_selector("button")
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        chat_opened = True
                        break
                except: pass

        # Try direct selectors on main page
        if not chat_opened:
            for sel in CHAT_SELECTORS[:8]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(2500)
                        crawl_state["log"].append(f"    [Chat] Opened via: {sel}")
                        chat_opened = True
                        break
                except: continue

        # ── Step 2: Find input field ──────────────────────────────────────────
        chat_input = None

        # Check all frames for input
        all_frames = [page] + list(page.frames)
        for frame in all_frames:
            for sel in CHAT_INPUT_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        chat_input = (frame, el)
                        crawl_state["log"].append(f"    [Chat] Found input field")
                        break
                except: continue
            if chat_input: break

        if not chat_input:
            crawl_state["log"].append(f"    [Chat] No chat input found — skipping")
            return None

        frame, inp = chat_input

        # ── Step 3: Send opening message ─────────────────────────────────────
        await inp.click()
        await inp.fill(CHAT_OPENER_MESSAGE)
        await page.wait_for_timeout(500)

        # Find and click send button
        sent = False
        for sel in CHAT_SEND_SELECTORS:
            try:
                btn = await frame.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    sent = True
                    break
            except: continue

        if not sent:
            # Try Enter key
            await inp.press("Enter")

        crawl_state["log"].append(f"    [Chat] Message sent — waiting for response...")
        crawl_state["chat_calls"] = crawl_state.get("chat_calls", 0) + 1

        # ── Step 4: Wait for response (up to 90 seconds) ─────────────────────
        response_text = ""
        for attempt in range(18):  # 18 x 5 seconds = 90 seconds max
            await page.wait_for_timeout(5000)

            # Capture full page text to look for rate-like responses
            try:
                page_text = await page.inner_text("body")
            except:
                page_text = ""

            # Check all frames too
            for frame in page.frames:
                try:
                    frame_text = await frame.inner_text("body")
                    page_text += " " + frame_text
                except: pass

            # Check if response contains rate-like content
            has_rate = bool(re.search(r'\d+\.\d+\s*%', page_text))
            has_response_words = any(w in page_text.lower() for w in [
                "apy", "rate", "savings", "certificate", "cd", "checking",
                "percent", "interest", "deposit", "currently", "offering",
                "promotion", "special", "annual"
            ])

            if has_rate or (has_response_words and attempt >= 2):
                response_text = page_text
                crawl_state["log"].append(f"    [Chat] Response received after {(attempt+1)*5}s")
                break

            # Send follow-up at 30 seconds if no response yet
            if attempt == 5:
                try:
                    inp2 = await frame.query_selector(CHAT_INPUT_SELECTORS[0])
                    if inp2:
                        await inp2.fill(CHAT_FOLLOWUP)
                        await inp2.press("Enter")
                        crawl_state["log"].append(f"    [Chat] Sent follow-up message")
                except: pass

        if not response_text:
            crawl_state["log"].append(f"    [Chat] No rate response received")
            return None

        # ── Step 5: Screenshot + Claude extraction ────────────────────────────
        try:
            screenshot = await page.screenshot(type="png")
            img_b64 = base64.standard_b64encode(screenshot).decode("utf-8")
        except:
            img_b64 = None

        # Extract rates using Claude
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Trim response text to relevant section
        response_trimmed = response_text[-3000:] if len(response_text) > 3000 else response_text

        prompt = f"""A bank chat conversation for {bank['bank_name']} contains this text:

{response_trimmed}

Extract any deposit rates mentioned. Return ONLY valid JSON:
{{
  "checking": <float APY% or null>,
  "savings": <REGULAR savings float APY% or null, not high-yield>,
  "high_yield_savings": <a SEPARATE high-yield/premium savings product's float APY% or null,
   only if distinct from regular savings>,
  "cd": <float APY% or null>,
  "cd_term": <"12-month" or null>,
  "money_market": <float APY% or null>,
  "min_balance": <"$1,000" or null>,
  "promo_note": <string — any special promotion mentioned, or null>
}}
Use null if not mentioned. Only extract rates clearly stated by the bank."""

        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        if img_b64:
            messages = [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt}
            ]}]

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=messages
            )
            _track_llm_usage(resp)
            txt = resp.content[0].text.strip()
            result = parse_json_object(txt)

            extracted = {
                "checking":          float(result["checking"])          if result.get("checking")          else None,
                "savings":           float(result["savings"])            if result.get("savings")           else None,
                "high_yield_savings": float(result["high_yield_savings"]) if result.get("high_yield_savings") else None,
                "cd":                float(result["cd"])                 if result.get("cd")                else None,
                "cd_term":           result.get("cd_term"),
                "money_market":      float(result["money_market"])       if result.get("money_market")      else None,
                "min_balance":       result.get("min_balance"),
                "promo_note":        result.get("promo_note"),
            }

            found = sum(1 for k in ["checking","savings","cd"] if extracted.get(k))
            crawl_state["log"].append(
                f"    [Chat] ✓ Extracted: sav={extracted.get('savings')} "
                f"cd={extracted.get('cd')} chk={extracted.get('checking')} ({found}/3)"
            )
            return extracted

        except Exception as e:
            crawl_state["log"].append(f"    [Chat] Extraction error: {e}")
            return None

    except Exception as e:
        crawl_state["log"].append(f"    [Chat] Error: {e}")
        return None


async def run_crawler(banks):
    run_started_at = datetime.now()
    # ── Version banner + environment ────────────────────────────────────────
    crawl_state["log"].append("Rate Radar v3.6 — Supabase push + 404-skip fast crawl")

    # ── Phase 2: config-driven bank/URL registry ────────────────────────────
    crawl_state["_rate_paths_cfg"], crawl_state["_bank_urls_cfg"], crawl_state["_bank_health_cfg"] = \
        await load_bank_url_config()
    if PROXY_URL:
        crawl_state["log"].append(f"  Proxy: {PROXY_URL} (browser + aiohttp will route through it)")
    else:
        crawl_state["log"].append("  Proxy: none configured (set HTTPS_PROXY in .env if behind a corporate proxy/VPN)")

    # ── Same-day consistency cache ──────────────────────────────────────────
    force_refresh = crawl_state.get("force_refresh", False)
    today_cache   = {} if force_refresh else _load_today_cache()
    cache_lock    = asyncio.Lock()
    if force_refresh:
        crawl_state["log"].append("Force refresh ON — ignoring any cached results from today")
    elif today_cache:
        crawl_state["log"].append(
            f"Same-day cache: {len(today_cache)} bank(s) already resolved today — "
            f"reusing those for consistency (delete RateCache/ or use Force refresh to override)")

    # ── Phase 0: Pre-flight validation ──────────────────────────────────────
    crawl_state["log"].append("── Pre-flight checks ──")
    warnings = await validate_banks_preflight(banks)
    for w in warnings:
        crawl_state["log"].append(w)
    if warnings:
        crawl_state["log"].append(f"  {len(warnings)} warning(s) above — review before acting on results")
    crawl_state["log"].append("── Starting crawl ──")

    # ── Phase 1: Preflight search (batched, no browser) ────────────────────
    # Fire in batches of 3 with a 2s gap — avoids blowing the 50K TPM Haiku limit
    # when running large lists. Each search call is ~2-4K tokens.
    preflight_results = {}   # bank_name → result dict or None
    banks_to_preflight = [b for b in banks if _bank_cache_key(b) not in today_cache]
    crawl_state["preflight_total"] = len(banks_to_preflight)
    crawl_state["preflight_done"] = 0
    if ANTHROPIC_OK and banks_to_preflight:
        crawl_state["phase"] = "preflight"
        crawl_state["log"].append("── Preflight search pass ──")
        conn = aiohttp.TCPConnector(limit=10, ssl=False) if AIOHTTP_OK else None
        async with aiohttp.ClientSession(connector=conn, trust_env=True) as search_session:
            async def do_preflight(bank):
                try:
                    r = await preflight_search(bank["bank_name"], bank.get("bank_url",""), search_session)
                    if r:
                        found = sum(1 for k in ["checking","savings","cd"] if r.get(k))
                        conf  = r.get("_confidence","?")
                        crawl_state["log"].append(
                            f"  [Search] {bank['bank_name']}: {found}/3 rates ({conf})"
                            + (f" ⚠️ rebrand: {r['_rebrand_hint']}" if r.get("_rebrand_hint") else "")
                        )
                        if r.get("_rebrand_hint"):
                            bank["_rebrand_hint"] = r["_rebrand_hint"]
                    preflight_results[bank["bank_name"]] = r
                except Exception as e:
                    crawl_state["log"].append(f"  [Search] {bank['bank_name']}: {e}")
                    preflight_results[bank["bank_name"]] = None
                finally:
                    crawl_state["preflight_done"] += 1

            # ~4K tokens per batched call → stays well under 50K TPM
            for i in range(0, len(banks_to_preflight), PREFLIGHT_BATCH_SIZE):
                batch = banks_to_preflight[i:i + PREFLIGHT_BATCH_SIZE]
                await asyncio.gather(*[do_preflight(b) for b in batch])
                if i + PREFLIGHT_BATCH_SIZE < len(banks_to_preflight):
                    await asyncio.sleep(PREFLIGHT_BATCH_GAP_S)  # breathe between batches

    crawl_state["phase"] = "crawling"
    crawl_state["log"].append("── Launching browser ──")
    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            # Railway (and most PaaS containers) don't grant Chromium's
            # sandbox the namespaces it wants by default — without these
            # flags the launch can hang or die silently with zero log output.
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}
        try:
            browser = await asyncio.wait_for(p.chromium.launch(**launch_kwargs), timeout=BROWSER_LAUNCH_TIMEOUT_S)
            crawl_state["log"].append("  ✓ Browser launched")
        except asyncio.TimeoutError:
            crawl_state["log"].append(
                f"  ✗ Browser launch timed out after {BROWSER_LAUNCH_TIMEOUT_S}s — likely a sandbox/permissions "
                "issue in this container. Crawl aborted.")
            crawl_state["running"] = False
            crawl_state["done"] = True
            return
        except Exception as e:
            crawl_state["log"].append(
                f"  ✗ Browser launch failed: {type(e).__name__}: {str(e)[:200]}")
            crawl_state["running"] = False
            crawl_state["done"] = True
            return

        # ── v3.2: connectivity self-check ──────────────────────────────────
        # The 06-11 run silently produced 0 browser results because every
        # request failed DNS. Fail loudly up front instead.
        net_ok = True
        check_page = await browser.new_page()
        try:
            await check_page.goto("https://example.com", timeout=10000,
                                  wait_until="domcontentloaded")
            crawl_state["log"].append("  ✓ Browser connectivity OK")
        except Exception as e:
            net_ok = False
            crawl_state["log"].append(
                "  ✗ BROWSER CANNOT REACH THE INTERNET — every site crawl will fail.")
            crawl_state["log"].append(f"    ({type(e).__name__}: {str(e)[:120]})")
            crawl_state["log"].append(
                "    Likely cause: corporate proxy or VPN. Add HTTPS_PROXY=http://your-proxy:port "
                "to the .env file next to this script and re-run.")
        finally:
            await check_page.close()
        if AIOHTTP_OK:
            try:
                async with aiohttp.ClientSession(trust_env=True) as s:
                    async with s.get("https://www.depositaccounts.com/",
                                     timeout=aiohttp.ClientTimeout(total=10),
                                     ssl=False) as resp:
                        crawl_state["log"].append(
                            f"  ✓ aiohttp connectivity OK (DA responded {resp.status})")
            except Exception as e:
                crawl_state["log"].append(
                    f"  ⚠️ aiohttp cannot reach depositaccounts.com ({type(e).__name__}) "
                    f"— DA fallback will be unavailable this run")
        if not net_ok:
            crawl_state["log"].append(
                "  Continuing with search-only data (Anthropic API preflight) — "
                "browser results will be empty until connectivity is fixed.")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_BANKS)
        # v3.3: cap concurrent final-pass search calls (Haiku TPM safety)
        final_search_sem = asyncio.Semaphore(MAX_CONCURRENT_SEARCH_RETRIES)
        total = len(banks)
        completed = [0]

        # Shared aiohttp session for fast DA fetches during browser crawl
        da_connector = aiohttp.TCPConnector(limit=20, ssl=False) if AIOHTTP_OK else None
        da_session_ctx = (aiohttp.ClientSession(connector=da_connector, trust_env=True)
                          if AIOHTTP_OK else None)

        async def process_bank(i, bank, http_session):
            async with semaphore:
                if not crawl_state["running"]:
                    return
                if bank["bank_name"].lower() in crawl_state.get("removed", set()):
                    crawl_state["log"].append(
                        f"[{i+1}/{total}] {bank['bank_name']} — removed, skipping")
                    completed[0] += 1
                    return
                mode = crawl_state.get("crawl_mode", "standard")
                crawl_state["log"].append(f"[{i+1}/{total}] {bank['bank_name']}... [{mode}]")

                cache_key = _bank_cache_key(bank)
                if cache_key in today_cache:
                    cached = dict(today_cache[cache_key])
                    if "[cached:" not in (cached.get("note") or ""):
                        cached["note"] = (cached.get("note","") +
                            " | [cached: same-day consistency]").strip(" |")
                    crawl_state["log"].append(
                        f"  \u21bb reusing today's result from {cached.get('crawled_at','earlier today')} "
                        f"(same-day consistency — use Force refresh to re-crawl)")
                    crawl_state["results"].append(cached)
                    completed[0] += 1
                    return

                # Merge preflight result — if high-confidence and 3/3, skip browser entirely
                # (but "Force refresh" means a real page check, not just a
                # cache bypass — never let it skip the browser via this path)
                pre = preflight_results.get(bank["bank_name"])
                if pre and pre.get("_confidence") == "high" and not force_refresh:
                    pre_count = sum(1 for k in ["checking","savings","cd"] if pre.get(k))
                    if pre_count == 3:
                        crawl_state["log"].append(
                            f"  ✓ Preflight 3/3 ({pre.get('_source_note','search')}) — skipping browser"
                        )
                        # Build minimal result from preflight data
                        result = _build_result_from_preflight(bank, pre)
                        crawl_state["results"].append(result)
                        if result.get("status") in (STATUS_FOUND, STATUS_PARTIAL, STATUS_NOT_PUBLIC):
                            async with cache_lock:
                                today_cache[cache_key] = result
                                _save_today_cache(today_cache)
                        completed[0] += 1
                        return

                # ── Phase 3: circuit breaker ─────────────────────────────────
                # A bank with enough consecutive site-crawl failures skips the
                # expensive browser step this run (but still gets whatever the
                # cheap preflight search found) — except every PROBE_EVERY-th
                # failure, where we still attempt a real crawl to catch recovery.
                if _circuit_open(bank):
                    failures = _bank_health(bank)["consecutive_failures"]
                    if pre:
                        result = _build_result_from_preflight(bank, pre)
                        result["note"] = (result.get("note","") +
                            f" | ⊘ Circuit breaker: {failures} consecutive site-crawl failures — "
                            f"skipped full browser crawl to save cost (needs manual review)").strip(" |")
                    else:
                        result = {
                            **bank,
                            "checking_apy": None, "savings_apy": None, "cd_apy": None,
                            "cd_term": None, "money_market_apy": None, "min_balance": None,
                            "status": STATUS_SKIPPED,
                            "note": f"⊘ Circuit breaker: {failures} consecutive failures — needs "
                                    f"manual review. Skipped full crawl (no preflight data either).",
                            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "cr_savings_apy": None, "cr_checking_apy": None,
                            "cr_cd_apy": None, "cr_cost_of_deposits": None,
                            "cr_total_deposits_m": None, "cr_period": "",
                            "prev_cr_savings_apy": None, "prev_cr_checking_apy": None,
                            "prev_cr_cd_apy": None, "prev_cr_cost_of_deposits": None,
                            "prev_cr_total_deposits_m": None, "cr_prev_period": "",
                            "delta_savings_apy": None, "delta_checking_apy": None,
                            "delta_cd_apy": None, "delta_cost_of_deposits": None,
                        }
                    next_probe = ((failures // CIRCUIT_BREAKER_PROBE_EVERY) + 1) * CIRCUIT_BREAKER_PROBE_EVERY
                    crawl_state["log"].append(
                        f"  ⊘ Circuit breaker open ({failures} consecutive failures) — "
                        f"{'using preflight data, ' if pre else ''}skipping full browser crawl "
                        f"(will probe again at {next_probe} failures)")
                    crawl_state["results"].append(result)
                    completed[0] += 1
                    return

                ctx  = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                )
                page = await ctx.new_page()
                try:
                    if mode == "ai_agent" and ANTHROPIC_OK:
                        result = await ai_agent_crawl(page, bank)
                    elif mode == "chat" and ANTHROPIC_OK:
                        result = await crawl_bank(page, bank, http_session=http_session)
                        count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                        if count < 3:
                            crawl_state["log"].append(f"    [Chat] Standard got {count}/3 — trying chat...")
                            page2 = await ctx.new_page()
                            try:
                                chat_result = await chat_crawl(page2, bank)
                                if chat_result:
                                    for k, rk in [("checking","checking_apy"),("savings","savings_apy"),
                                                   ("high_yield_savings","high_yield_savings_apy"),
                                                   ("cd","cd_apy"),("cd_term","cd_term"),
                                                   ("money_market","money_market_apy"),("min_balance","min_balance")]:
                                        if chat_result.get(k) is not None and not result.get(rk):
                                            result[rk] = chat_result[k]
                                    if chat_result.get("promo_note"):
                                        result["note"] = (result.get("note","") + " | 💬 Chat: " + chat_result["promo_note"]).strip(" |")
                                    new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                                    result["status"] = _classify_status(new_count)
                                    result["source_url"] = result.get("source_url","") or "Live chat"
                            finally:
                                await page2.close()
                    else:
                        result = await crawl_bank(page, bank, http_session=http_session)

                    # Merge any preflight data into gaps left by browser crawl
                    if pre:
                        for k, rk in [("checking","checking_apy"),("savings","savings_apy"),
                                       ("high_yield_savings","high_yield_savings_apy"),
                                       ("cd","cd_apy"),("cd_term","cd_term"),
                                       ("money_market","money_market_apy")]:
                            if pre.get(k) is not None and not result.get(rk):
                                result[rk] = pre[k]
                                result["note"] = (result.get("note","") +
                                    f" | {k} from search: {pre[k]}").strip(" |")
                        # Re-evaluate status
                        new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                        result["status"] = _classify_status(new_count)

                    # ── v3.3: final API-search pass ──────────────────────────
                    # The Haiku preflight search is non-deterministic — the
                    # 06-11 20:28 run lost 9 rate values that the 12:06 run
                    # had, purely because the single search attempt came back
                    # empty. If, after browser + preflight merge, this bank
                    # still has ≤1 core rate, retry the API search once.
                    # (API search demonstrably works on this network; the
                    # browser SE-rescue does not.)
                    core_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"]
                                     if result.get(k))
                    if core_count <= 1 and ANTHROPIC_OK and bank.get("bank_url",""):
                        missing = [name for name, rk in
                                   [("checking","checking_apy"),("savings","savings_apy"),
                                    ("cd","cd_apy"),("money_market","money_market_apy")]
                                   if not result.get(rk)]
                        async with final_search_sem:   # cap concurrency for TPM
                            crawl_state["log"].append(
                                f"    [Search-retry] {core_count}/3 after crawl — targeted "
                                f"retry on {', '.join(missing)}...")
                            try:
                                retry = await preflight_search(
                                    bank["bank_name"], bank.get("bank_url",""), None,
                                    focus=missing)
                            except Exception as e:
                                retry = None
                                crawl_state["log"].append(f"    [Search-retry] error: {e}")
                            await asyncio.sleep(1)     # breathe between calls
                        if retry:
                            filled = []
                            for k, rk in [("checking","checking_apy"),("savings","savings_apy"),
                                           ("high_yield_savings","high_yield_savings_apy"),
                                           ("cd","cd_apy"),("cd_term","cd_term"),
                                           ("money_market","money_market_apy")]:
                                if retry.get(k) is not None and not result.get(rk):
                                    result[rk] = retry[k]
                                    if rk != "cd_term":
                                        filled.append(f"{k}={retry[k]}")
                                        result["note"] = (result.get("note","") +
                                            f" | {k} from search retry: {retry[k]}").strip(" |")
                            if filled:
                                src = retry.get("_source_note","search")
                                crawl_state["log"].append(
                                    f"    [Search-retry] ✓ recovered {', '.join(filled)} ({src})")
                                if not result.get("source_url"):
                                    result["source_url"] = f"[Search] {src}"
                            else:
                                crawl_state["log"].append("    [Search-retry] no new rates found")
                            new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"]
                                            if result.get(k))
                            result["status"] = _classify_status(new_count)

                    # ── Last-resort: today found nothing — check history ────
                    # Doesn't touch any bank that found at least one rate today;
                    # only fires for the complete-blank case, and is always
                    # clearly labeled as historical, never presented as fresh.
                    core_count_final = sum(1 for k in ["checking_apy","savings_apy",
                                                        "cd_apy","money_market_apy"]
                                           if result.get(k))
                    if core_count_final == 0:
                        hist = await fetch_last_known_good(bank["bank_name"], http_session)
                        if hist:
                            filled = []
                            for k in ["checking_apy","savings_apy","cd_apy",
                                     "cd_term","money_market_apy","min_balance"]:
                                if hist.get(k) is not None:
                                    result[k] = hist[k]
                                    if k not in ("cd_term","min_balance"):
                                        filled.append(k)
                            if filled:
                                as_of = hist.get("run_date") or hist.get("crawled_at","earlier")
                                result["note"] = (
                                    f"\u23f1 No fresh data found today — showing last "
                                    f"confirmed rate from {as_of} | " +
                                    (result.get("note","") or "")
                                ).strip(" |")
                                result["status"] = STATUS_PARTIAL
                                crawl_state["log"].append(
                                    f"    [History] \u21bb recovered {', '.join(filled)} "
                                    f"from {as_of} (today's search found nothing)")

                    icon = "✓" if result["status"] == STATUS_FOUND else "~" if result["status"] == STATUS_PARTIAL else "⊘" if result["status"] == STATUS_SKIPPED else "○"
                    chk  = f"{result['checking_apy']:.2f}%" if result["checking_apy"] else "—"
                    sav  = f"{result['savings_apy']:.2f}%"  if result["savings_apy"]  else "—"
                    cd   = f"{result['cd_apy']:.2f}%"       if result["cd_apy"]       else "—"
                    cod  = f"  CoD:{result['cr_cost_of_deposits']:.2f}%" if result.get("cr_cost_of_deposits") else ""
                    crawl_state["log"].append(f"  {icon} Chk:{chk} Sav:{sav} CD:{cd}{cod}")
                    if bank["bank_name"].lower() in crawl_state.get("removed", set()):
                        crawl_state["log"].append(
                            f"  (removed mid-crawl — discarding result for {bank['bank_name']})")
                    else:
                        crawl_state["results"].append(result)
                        if result.get("status") in ("Found","Partial","Not public"):
                            async with cache_lock:
                                today_cache[cache_key] = result
                                _save_today_cache(today_cache)
                        failures = await update_bank_registry(bank, result)
                        if failures == CIRCUIT_BREAKER_THRESHOLD:
                            crawl_state["log"].append(
                                f"  ⚠️ ALERT: {bank['bank_name']} just crossed {failures} "
                                f"consecutive failures — flagged needs_review")
                            crawl_state.setdefault("_newly_flagged", []).append(bank["bank_name"])
                except Exception as e:
                    crawl_state["log"].append(f"  x Error: {e}")
                    if bank["bank_name"].lower() not in crawl_state.get("removed", set()):
                        error_result = {
                        **bank,
                        "checking_apy": None, "savings_apy": None, "cd_apy": None,
                        "cd_term": None, "money_market_apy": None, "min_balance": None,
                        "status": STATUS_ERROR, "note": str(e),
                        "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "cr_savings_apy": None, "cr_checking_apy": None,
                        "cr_cd_apy": None, "cr_cost_of_deposits": None,
                        "cr_total_deposits_m": None, "cr_period": "",
                        "prev_cr_savings_apy": None, "prev_cr_checking_apy": None,
                        "prev_cr_cd_apy": None, "prev_cr_cost_of_deposits": None,
                        "prev_cr_total_deposits_m": None, "cr_prev_period": "",
                        "delta_savings_apy": None, "delta_checking_apy": None,
                        "delta_cd_apy": None, "delta_cost_of_deposits": None,
                        }
                        crawl_state["results"].append(error_result)
                        failures = await update_bank_registry(bank, error_result)
                        if failures == CIRCUIT_BREAKER_THRESHOLD:
                            crawl_state["log"].append(
                                f"  ⚠️ ALERT: {bank['bank_name']} just crossed {failures} "
                                f"consecutive failures — flagged needs_review")
                            crawl_state.setdefault("_newly_flagged", []).append(bank["bank_name"])
                finally:
                    completed[0] += 1
                    await page.close()
                    await ctx.close()

        BANK_TIMEOUT_SECONDS = 150  # hard ceiling per bank — a stuck site can't hang the batch

        async def process_bank_guarded(i, bank, http_session):
            try:
                await asyncio.wait_for(process_bank(i, bank, http_session),
                                        timeout=BANK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                crawl_state["log"].append(
                    f"[{i+1}/{total}] {bank['bank_name']} — TIMED OUT after "
                    f"{BANK_TIMEOUT_SECONDS}s, skipping")
                # process_bank's own finally already closes page/ctx and bumps
                # completed[0] on cancellation — nothing further to do here.

        if da_session_ctx:
            async with da_session_ctx as http_session:
                await asyncio.gather(*[process_bank_guarded(i, bank, http_session)
                                       for i, bank in enumerate(banks)])
        else:
            await asyncio.gather(*[process_bank_guarded(i, bank, None)
                                   for i, bank in enumerate(banks)])

        await browser.close()

    crawl_state["running"] = False
    crawl_state["done"]    = True
    found   = sum(1 for r in crawl_state["results"] if r["status"] == "Found")
    partial = sum(1 for r in crawl_state["results"] if r["status"] == "Partial")
    ai_calls   = crawl_state.get("ai_calls", 0)
    chat_calls = crawl_state.get("chat_calls", 0)
    llm_input_tokens  = crawl_state.get("llm_input_tokens", 0)
    llm_output_tokens = crawl_state.get("llm_output_tokens", 0)
    llm_web_searches  = crawl_state.get("llm_web_searches", 0)
    ai_note    = f" · {ai_calls} AI vision" if ai_calls else ""
    chat_note  = f" · {chat_calls} chat" if chat_calls else ""
    cost_note  = f" · ${_estimate_cost_usd(llm_input_tokens, llm_output_tokens, llm_web_searches):.2f} LLM cost"
    crawl_state["log"].append(
        f"Done — {found} full, {partial} partial, "
        f"{len(crawl_state['results'])-found-partial} not public{ai_note}{chat_note}{cost_note}"
    )

    # ── Phase 3: run-completion alerting (surfaced in-app, no external channel) ──
    attempted = len(crawl_state["results"])
    errored   = sum(1 for r in crawl_state["results"] if r["status"] == STATUS_ERROR)
    skipped   = sum(1 for r in crawl_state["results"] if r["status"] == STATUS_SKIPPED)
    newly_flagged = crawl_state.get("_newly_flagged", [])
    alert_lines = []
    if attempted >= 5 and (found / attempted) < 0.2:
        alert_lines.append(
            f"Only {found}/{attempted} banks fully found this run ({found/attempted:.0%}) — "
            f"unusually low, worth a manual look.")
    if newly_flagged:
        alert_lines.append(
            f"{len(newly_flagged)} bank(s) newly flagged needs_review "
            f"({CIRCUIT_BREAKER_THRESHOLD} consecutive failures): {', '.join(newly_flagged)}.")
    if errored:
        alert_lines.append(f"{errored} bank(s) errored (crashed) rather than just finding no data.")
    if alert_lines:
        crawl_state["log"].append("⚠️ RUN ALERT — " + " ".join(alert_lines))
    run_notes = " ".join(alert_lines) if alert_lines else None

    saved = auto_save(crawl_state["results"])
    if saved and saved[0]:
        await push_to_supabase(crawl_state["results"], saved[0], saved[1])
        # Run row must land before observation rows — rate_observations.run_id
        # is a foreign key into rate_radar_runs.
        not_public = len(crawl_state["results"]) - found - partial
        await push_run_summary(saved[0], saved[1], run_started_at,
                                found, partial, not_public, ai_calls, chat_calls,
                                llm_input_tokens, llm_output_tokens, llm_web_searches,
                                notes=run_notes)
        await push_rate_observations(crawl_state["results"], saved[0])


def _build_result_from_preflight(bank, pre):
    """Build a minimal result dict from preflight search data (no browser needed)."""
    count  = sum(1 for k in ["checking","savings","cd"] if pre.get(k))
    status = _classify_status(count)
    parts  = []
    if pre.get("rebrand_hint"):
        parts.append(f"⚠️ REBRAND: {pre['rebrand_hint']}")
        status = STATUS_PARTIAL
    rate_parts = []
    if pre.get("cd"):      rate_parts.append(f"CD {pre['cd']:.2f}%{' ('+pre['cd_term']+')' if pre.get('cd_term') else ''}")
    if pre.get("savings"): rate_parts.append(f"Savings {pre['savings']:.2f}%")
    if pre.get("high_yield_savings"): rate_parts.append(f"High-Yield Savings {pre['high_yield_savings']:.2f}%")
    if pre.get("checking"):rate_parts.append(f"Checking {pre['checking']:.2f}%")
    if rate_parts: parts.append(", ".join(rate_parts))
    if not parts: parts.append("Rates not publicly listed")
    src = pre.get("_source_note","search")
    rssdid = bank.get("RSSDID") or ""
    cr, prev_cr = {}, {}
    if rssdid:
        try:
            rid = int(str(rssdid).strip())
            cr      = crawl_state["cr_data"].get(rid, {})
            prev_cr = crawl_state["prev_cr_data"].get(rid, {})
        except: pass
    return {
        **bank,
        "checking_apy":              pre.get("checking"),
        # Wide-table backward compat: falls back to the HYS rate when that's
        # the only savings-type product found.
        "savings_apy":               pre.get("savings") if pre.get("savings") is not None else pre.get("high_yield_savings"),
        "high_yield_savings_apy":    pre.get("high_yield_savings"),
        "cd_apy":                    pre.get("cd"),
        "cd_term":                   pre.get("cd_term"),
        "money_market_apy":          pre.get("money_market"),
        "min_balance":               None,
        "status":                    status,
        "note":                      " | ".join(parts) + f" | Source: [Search] {src}",
        "source_url":                f"[Search] {src}",
        "source_url_checking":       f"[Search] {src}" if pre.get("checking") else "",
        "source_url_savings":        f"[Search] {src}" if (pre.get("savings") or pre.get("high_yield_savings")) else "",
        "source_url_high_yield_savings": f"[Search] {src}" if pre.get("high_yield_savings") else "",
        "source_url_cd":             f"[Search] {src}" if pre.get("cd") else "",
        "crawled_at":                datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bank_type":                 bank.get("bank_type",""),
        "branch_address":            bank.get("branch_address",""),
        "vulnerability_flag":        "",
        "cr_savings_apy":            cr.get("cr_savings_apy"),
        "cr_checking_apy":           cr.get("cr_checking_apy"),
        "cr_cd_apy":                 cr.get("cr_cd_apy"),
        "cr_cost_of_deposits":       cr.get("cr_cost_of_deposits"),
        "cr_total_deposits_m":       cr.get("cr_total_deposits_m"),
        "cr_period":                 cr.get("cr_period",""),
        "prev_cr_savings_apy":       prev_cr.get("cr_savings_apy"),
        "prev_cr_checking_apy":      prev_cr.get("cr_checking_apy"),
        "prev_cr_cd_apy":            prev_cr.get("cr_cd_apy"),
        "prev_cr_cost_of_deposits":  prev_cr.get("cr_cost_of_deposits"),
        "prev_cr_total_deposits_m":  prev_cr.get("cr_total_deposits_m"),
        "cr_prev_period":            prev_cr.get("cr_period",""),
        "delta_savings_apy":         None,
        "delta_checking_apy":        None,
        "delta_cd_apy":              None,
        "delta_cost_of_deposits":    None,
    }


def start_crawl_thread(banks):
    MAX_CRAWL_SECONDS = 25 * 60  # hard ceiling for the whole batch, regardless of size
    run_token = crawl_state["run_token"] = crawl_state.get("run_token", 0) + 1

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_crawler(banks))
        loop.close()

    def watchdog():
        # If the crawl thread is still marked running past the ceiling AND
        # this is still the same run (not a newer one that already finished
        # and started again), force-clear the flag so /start isn't stuck
        # returning "Already running" forever.
        if crawl_state["running"] and crawl_state.get("run_token") == run_token:
            crawl_state["log"].append(
                f"\u26a0 Watchdog: crawl exceeded {MAX_CRAWL_SECONDS//60} min — "
                f"force-clearing running flag so a new crawl can start")
            crawl_state["running"] = False
            crawl_state["done"] = True

    threading.Thread(target=run, daemon=True).start()
    threading.Timer(MAX_CRAWL_SECONDS, watchdog).start()
