"""
Verlocity Intelligence Hub — Rate Radar
===============
Run: python rate_radar.py
Opens browser at http://localhost:7331

v3.6 performance:
  — 404/4xx pages are skipped outright: no waits, no accordion expansion, no
    deep dig. Most candidate paths don't exist, and rate-keyword 404s were
    getting the full JS treatment — this is where most of the run time went.
  — The blanket 2s wait on rate-keyword pages is now a wait *up to* 2s for
    '% APY' content, returning instantly once rates render.
  — AI vision skips 4xx targets instead of screenshotting error pages.
  Expected effect: roughly a third off total run time, no coverage change.

v3.5 Supabase dataset:
  — Every run upserts into raw.raw_rate_radar (PostgREST, keyed run_id +
    bank_name) so rates accumulate into a queryable time series. Views:
    public.vw_rate_radar_latest (newest per bank) and
    public.vw_rate_radar_history (run-over-run rate changes — the trigger
    signal). Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env to enable;
    runs still save CSV+log locally either way.
  — Vision JSON guard: model outputs with preamble no longer error out.

v3.4 ZIP-gate handler:
  — Big banks (TD, Fifth Third, 1st Source) publish rates per market behind a
    ZIP prompt. The branch_address column (populated by the Hub's v27 Rate
    Radar widget with the highest-vulnerability BMAP target branch) supplies
    the ZIP; the crawler detects the gate, fills it, submits, and sniffs the
    rates XHR (JSON) as well as the rendered DOM. Falls back to DEFAULT_ZIP
    from .env when no branch_address is present. Max 2 gate fills per bank;
    provenance recorded in the note as "ZIP-gated: rates for <zip>".

v3.3 reliability fix:
  — Final API-search pass: any bank ending the crawl with ≤1 core rate gets
    one retried Anthropic web search. Fixes run-over-run variance where a
    single non-deterministic search attempt silently dropped rates (9 values
    lost between the 06-11 12:06 and 20:28 runs).

v3.2 connectivity fixes (root cause of the 06-11 zero-browser-results run):
  — All aiohttp sessions use trust_env=True and Chromium launches with the
    proxy from HTTPS_PROXY/HTTP_PROXY, so machines behind a corporate
    proxy/VPN can crawl. Add HTTPS_PROXY=http://your-proxy:port to .env.
  — Connectivity self-check at crawl start: fails loudly with a remediation
    hint instead of silently producing an empty run.
  — The run log is saved to RateRadar_Exports/rate_radar_<runid>.log next
    to the CSV, and the log opens with a version banner.

v3.1 coverage fixes:
  Fix 1 — Min balance found but no rate: a detected min balance is treated as
          a strong "rates are here" signal → scroll, wait for JS, expand
          accordions, harvest hidden panel text, and follow "View Rates" /
          "See Details" links on that page before giving up.
  Fix 2 — Search-engine rescue: when the full path list yields 0/3 rates,
          search `site:<bankdomain> rates APY` (Google → Bing → DuckDuckGo)
          and crawl the top on-domain results before marking "Not public".
  Fix 3 — Rate-keyword URLs (cd / certificate / savings / rates in path) get
          an explicit JS wait + full-page scroll + accordion/tab expansion
          before extraction, in both the standard and AI-agent crawlers.

Setup (one time):
  pip install flask playwright pandas aiohttp python-dateutil
  playwright install chromium

Call Reports folder (sits next to this script):
  CallReports/
      12-31-2025/
          FFIEC CDR Call Schedule RI 12312025
          FFIEC CDR Call Schedule RCE 12312025
          FFIEC CDR Call Schedule RCK 12312025
      09-30-2025/
          ...
  Folder name format : MM-DD-YYYY
  File name format   : FFIEC CDR Call Schedule {RI|RCE|RCK} MMDDYYYY
  Files are tab-delimited, no extension.
"""

import asyncio
import csv
import io
import json
import re
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, quote
import urllib.request as urlreq

# Load .env automatically
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
import base64

# aiohttp for fast non-browser HTTP (DA fetches, preflight checks)
try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

# dateutil for staleness detection
try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_OK = True
except ImportError:
    DATEUTIL_OK = False

try:
    from flask import Flask, request, jsonify, Response, render_template_string
except ImportError:
    print("Run: pip install flask playwright pandas anthropic && playwright install chromium")
    raise

# Anthropic client for AI vision fallback
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# v3.5: Supabase push — set these in the same .env file (additive, optional)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY", "")
                or os.environ.get("SUPABASE_KEY", "")).strip()
try:
    import anthropic
    ANTHROPIC_OK = bool(ANTHROPIC_API_KEY)
except ImportError:
    ANTHROPIC_OK = False

try:
    import pandas as pd
    import numpy as np
    PANDAS_OK = True
except ImportError:
    PANDAS_OK = False

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

app = Flask(__name__)

# ── Call Report folder (next to this script) ──────────────────────────────────
CALL_REPORTS_DIR = Path(__file__).parent / "CallReports"

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


crawl_state = {
    "running":        False,
    "banks":          [],
    "results":        [],
    "log":            [],
    "done":           False,
    "ai_calls":       0,      # track AI vision calls this run
    "chat_calls":     0,      # track chat interactions this run
    "crawl_mode":     "auto",   # auto: standard → ai_agent fallback
    "cr_data":        {},     # {rssdid (int): {...implied APY fields...}} — current quarter
    "cr_period":      None,   # e.g. "Dec 2025"
    "prev_cr_data":   {},     # prior quarter data
    "cr_prev_period": None,   # e.g. "Sep 2025"
    "cr_quarters":    [],     # all available quarter labels
}

RATE_PATHS = [
    "", "/rates", "/personal/rates", "/personal-banking/rates",
    "/home/current-rates", "/savings", "/personal-banking/savings",
    "/checking", "/personal-banking/checking", "/cds",
    "/certificates-of-deposit", "/personal-banking/cds",
    "/commercial-banking", "/business-banking", "/deposits",
    "/personal-banking/money-market", "/money-market",
    # expanded — community bank common patterns
    "/personal/deposit-rates", "/products/deposits", "/banking/rates",
    "/accounts/rates", "/personal-banking/deposit-accounts",
    "/consumer/rates", "/rates/deposit", "/deposit-rates",
    "/personal/savings-accounts", "/personal/checking-accounts",
    "/personal/cds", "/current-rates", "/rate-sheet", "/interest-rates",
    "/about/rates", "/resources/rates", "/banking/deposit-rates",
    "/personal/money-market", "/products/rates", "/retail/rates",
    "/personal-banking/certificates", "/certificates",
    "/savings-accounts", "/checking-accounts", "/deposit-accounts",
    "/rates/personal", "/rates/consumer", "/rates/deposits",
    "/personal/deposit-accounts", "/banking/savings",
    "/banking/checking", "/banking/cds", "/banking/money-market",
    "/accounts/savings", "/accounts/checking", "/accounts/cds",
    "/products/savings", "/products/checking", "/products/cds",
    "/services/rates", "/services/deposits",
]

BANK_EXTRA_URLS = {
    "asb.com":             [
        "https://www.asb.com/personal/rates",
        "https://www.asb.com/personal/checking",
        "https://www.asb.com/personal/savings",
        "https://www.asb.com/",
    ],
    "jpmorganchase.com":   ["https://www.chase.com/personal/savings"],
    "chase.com":           [
        "https://www.chase.com/personal/savings",
        "https://www.chase.com/personal/checking",
        "https://www.chase.com/personal/cds",
    ],
    "texascapitalbank.com": [
        "https://www.texascapitalbank.com/personal/deposits",
        "https://www.texascapitalbank.com/personal/checking",
        "https://www.texascapitalbank.com/rates",
    ],
    "stearnsbank.com":     [
        "https://www.stearnsbank.com/personal/rates",
        "https://www.stearnsbank.com/rates",
    ],
    "gulfbank.com":        [
        "https://www.gulfbank.com/personal/rates",
        "https://www.gulfbank.com/rates",
    ],
    "bankwithfidelity.com": [
        "https://www.bankwithfidelity.com/rates",
        "https://www.bankwithfidelity.com/personal/rates",
    ],
    "nexbank.com":         ["https://nexbankpersonal.com/"],
    "tbkbank.com":         ["https://www.tbkbank.com/rates/"],
    "maplemarkbank.com":   ["https://go.maplemarkbank.com/"],
    "hibernia.bank":       [
        "https://www.hibernia.bank/personal-solutions/checking",
        "https://www.hibernia.bank/personal-solutions/savings",
        "https://www.hibernia.bank/personal-solutions/cds",
        "https://www.hibernia.bank/rates",
        "https://www.hibernia.bank/",
    ],
    "bayfirstfinancial.com": [
        "https://www.bayfirstfinancial.com/personal/rates/",
        "https://www.bayfirstfinancial.com/rates/",
        "https://www.bayfirstfinancial.com/personal-banking/savings/",
        "https://www.bayfirstfinancial.com/personal-banking/checking/",
    ],
}

APY_PAT      = re.compile(r'(\d+\.\d+)\s*%\s*(?:APY|Annual\s+Percentage\s+Yield)', re.I)

# Fix 3: two-pass savings — inline first (with CD cross-contamination guard), then next-line
SAVINGS_PAT        = re.compile(
    r'(?:savings|high.yield\s+savings)'
    r'(?!.*?(?:cd|certificate|loan|mortgage).*?\d+\.\d+\s*%\s*APY)'
    r'[^\n]{0,60}?(\d+\.\d+)\s*%\s*APY', re.I)
SAVINGS_PAT_NEXTLN = re.compile(
    r'(?:savings|high.yield\s+savings)[^\n]*\n\s*(\d+\.\d+)\s*%\s*APY', re.I)
# Wider gap — handles 2-3 blank/short lines between label and rate value
SAVINGS_PAT_WIDEGAP = re.compile(
    r'(?:savings|high.yield\s+savings)[^\n]*\n(?:[^\n]{0,30}\n){0,3}\s*(\d+\.\d+)\s*%\s*APY', re.I)

# Fix 3: checking — catches iChecking, eChecking, Rewards Checking, Interest Checking
CHECKING_PAT        = re.compile(
    r'(?:i?e?-?\s*checking|interest\s+checking|reward(?:s)?\s+checking)'
    r'(?!.*?(?:cd|certificate|loan|mortgage).*?\d+\.\d+\s*%\s*APY)'
    r'[^\n]{0,60}?(\d+\.\d+)\s*%\s*APY', re.I)
CHECKING_PAT_NEXTLN = re.compile(
    r'(?:i?e?-?\s*checking|interest\s+checking|reward(?:s)?\s+checking)'
    r'[^\n]*\n\s*(\d+\.\d+)\s*%\s*APY', re.I)
# Plain "checking" — broader catch for "Personal Checking 2.00% APY", still CD-guarded
CHECKING_PAT_PLAIN  = re.compile(
    r'(?:checking)'
    r'(?!.*?(?:cd|certificate|loan|mortgage).*?\d+\.\d+\s*%\s*APY)'
    r'[^\n]{0,80}?(\d+\.\d+)\s*%\s*APY', re.I)
# Hero/homepage pattern — catches "earn up to 4.00% APY" on homepage banners
HERO_CHECKING_PAT   = re.compile(
    r'(?:earn(?:ing)?|up\s+to|as\s+high\s+as)'
    r'[^\n]{0,40}?(\d+\.\d+)\s*%\s*APY'
    r'(?![^\n]{0,60}(?:cd|certificate|savings|money\s+market))', re.I)

# Fix 3: money market — same two-pass
MM_PAT        = re.compile(
    r'(?:money\s+market)'
    r'(?!.*?(?:cd|certificate|loan|mortgage).*?\d+\.\d+\s*%\s*APY)'
    r'[^\n]{0,60}?(\d+\.\d+)\s*%\s*APY', re.I)
MM_PAT_NEXTLN = re.compile(
    r'(?:money\s+market)[^\n]*\n\s*(\d+\.\d+)\s*%\s*APY', re.I)

MIN_BAL_PAT  = re.compile(r'\$\s*([1-9][\d,]*)\s*(?:minimum|min).*?(?:balance|deposit)', re.I)

# Fix 5: TABLE_PAT requires APY label — prevents matching loan/fee tables
TABLE_PAT    = re.compile(r'(\d{1,3})\s*[-]?\s*(month|mo|year|day)s?\b[^\n]{0,80}?(\d+\.\d+)\s*%\s*APY', re.I)
TERM_APY_PAT = re.compile(r'(\d+\.\d+)\s*%\s*APY[^\n]{0,60}?(\d+)\s*[-]?\s*(month|mo|day|year)', re.I)
CD_PAT       = re.compile(r'(?:CD|Certificate)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
# v3.4: CD label and APY on separate lines (rate tiles / table cells render
# this way via inner_text). Savings/checking/MM had _NEXTLN variants; CD
# didn't — the gap behind "CD page found but rates not extracted".
# The lookahead stops the match from leaking into a different product's line.
CD_PAT_NEXTLN = re.compile(
    r'(?:CDs?\b|Certificate)[^\n]{0,80}\n'
    r'(?![^\n]*(?:saving|checking|money\s*market))'
    r'[^\n]{0,40}?(\d+\.\d+)\s*%\s*APY', re.I)
TABLE_PAT_NEXTLN = re.compile(
    r'(\d{1,3})\s*[-]?\s*(month|mo|year|day)s?\b[^\n]{0,60}\n'
    r'(?![^\n]*(?:saving|checking|money\s*market))'
    r'[^\n]{0,40}?(\d+\.\d+)\s*%\s*APY', re.I)

# Fix 4: detect jumbo/promo context to deprioritise those CD rates
JUMBO_PAT    = re.compile(r'jumbo|special\s+rate|limited\s+time|promo|\$\s*(?:100|150|200|250)\s*[,k]|\$\s*\d{3},\d{3}', re.I)
# Reject loan/mortgage/APR matches bleeding into CD patterns
LOAN_PAT     = re.compile(r'(?:loan|mortgage|auto|home\s+equity|heloc|apr\b)', re.I)

# ── New v3 accuracy patterns ───────────────────────────────────────────────────

# Conditional rate detection — Kasasa, qualification-based, min-balance gated
CONDITIONAL_PAT = re.compile(
    r'(?:when\s+qualif|if\s+qualif|must\s+(?:make|have|maintain)|'
    r'requires?\s+(?:direct\s+deposit|debit\s+card|minimum\s+balance)|'
    r'kasasa|reward(?:s)?\s+checking\s+qualif|'
    r'monthly\s+qualif|per\s+qualif\s+cycle|'
    r'to\s+earn\s+(?:the\s+)?(?:rate|apy|reward)|'
    r'enrollment\s+required|qualifying\s+(?:activities|transactions))',
    re.I
)

# Promo / new-money rate detection
PROMO_PAT = re.compile(
    r'(?:new\s+money|new\s+(?:customers?|accounts?|funds?)|'
    r'limited\s+time|promotional|introductory|special\s+offer|'
    r'minimum\s+(?:opening|new)\s+(?:deposit\s+of\s+)?\$[\d,]{4,}|'
    r'not\s+available\s+for\s+(?:existing|current)|'
    r'offer\s+(?:may\s+be\s+)?discontinued)',
    re.I
)

# Rebrand signal — "now known as", "now operating as", "rebranded as"
REBRAND_PAT = re.compile(
    r'(?:now\s+(?:known\s+as|called|operating\s+as|part\s+of|branded\s+as)|'
    r'rebranded?\s+(?:as|to)|formerly\s+(?:known\s+as\s+)?|'
    r'has\s+(?:merged|joined|become))',
    re.I
)

# Rate staleness date — "rates as of MM/DD/YYYY", "effective 01/23/2026", etc.
RATE_DATE_PAT = re.compile(
    r'(?:rates?\s+(?:as\s+of|updated|effective|current\s+as\s+of)|'
    r'effective\s+(?:date\s+)?|last\s+updated\s*:?\s*|'
    r'accurate\s+as\s+of)\s*'
    r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+\.?\s+\d{1,2},?\s*\d{4})',
    re.I
)



# ── v3 helper utilities ───────────────────────────────────────────────────────

def tag_rate_context(text, match_pos):
    """
    Look at text surrounding a rate match and return any relevant tags.
    Returns list of strings like ['conditional', 'promo'] or [].
    """
    window = text[max(0, match_pos - 300): match_pos + 300]
    tags = []
    if CONDITIONAL_PAT.search(window):
        tags.append("conditional")
    if PROMO_PAT.search(window):
        tags.append("promo")
    return tags


def check_rate_staleness(text):
    """
    Extract the 'rates as of' date from page text.
    Returns (date_str, age_days) or (None, None) if not found.
    """
    m = RATE_DATE_PAT.search(text)
    if not m:
        return None, None
    raw = m.group(1).strip()
    if not DATEUTIL_OK:
        return raw, None
    try:
        dt = dateutil_parser.parse(raw, fuzzy=True)
        age = (datetime.now() - dt).days
        return raw, age
    except Exception:
        return raw, None


def detect_rebrand(text, bank_name):
    """
    Check if a page signals that the bank has rebranded.
    Returns the new name hint if detected, else None.
    """
    m = REBRAND_PAT.search(text[:3000])
    if not m:
        return None
    # Grab the 60 chars after the trigger phrase as the new name hint
    end = m.end()
    hint = text[end:end+60].strip().split('\n')[0].strip(' .,')
    # Only flag if the hint is meaningfully different from the input name
    if hint and bank_name.lower()[:6] not in hint.lower():
        return hint
    return None


def extract_domain(url):
    m = re.match(r'https?://([^/]+)', url)
    return m.group(1).replace('www.', '') if m else ''


# ── v3.2: proxy support ───────────────────────────────────────────────────────
# Root cause of the 06-11 run: every Playwright/aiohttp request died with DNS
# failures (ERR_NAME_NOT_RESOLVED / getaddrinfo failed) while the Anthropic
# SDK worked — the machine routes traffic through a proxy. httpx (Anthropic
# SDK) honors HTTPS_PROXY automatically; aiohttp and Chromium must be told.
# Set HTTPS_PROXY (or HTTP_PROXY) in the .env file or system env and both the
# browser and all aiohttp sessions will use it.

def parse_json_object(txt):
    """Parse a JSON object from model output; tolerates preamble/fences.
    Returns dict or raises ValueError."""
    txt = (txt or "").replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise ValueError("no JSON object in model output")


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

JSON_RATE_KEY_PAT = re.compile(r'apy|rate', re.I)

JSON_PRODUCT_HINTS = [
    ("cd",           re.compile(r'\bcds?\b|cds?[\d_\-]|[\d_\-]cds?\b|certificate|term\s*deposit', re.I)),
    ("money_market", re.compile(r'money.?market|\bmm[ad]?\b', re.I)),
    ("savings",      re.compile(r'sav', re.I)),
    ("checking",     re.compile(r'check|chk|interest\s*checking', re.I)),
]


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
        return None
    for el in els[:6]:
        try:
            if await el.is_visible():
                return el
        except Exception:
            continue
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


def extract_rates_from_json(data):
    """
    Walk a JSON payload sniffed from a ZIP-gated rates XHR and pull
    {product: rate}. A value counts as a rate when its key mentions apy/rate,
    it parses to 0.01–9.99, and (unless the key says 'apy') it has a decimal
    point — that last rule kills IDs and counts. Product is classified from
    the surrounding object's keys+scalar values plus inherited parent keys,
    so {"type":"cd12","apy":"3.50"} and {"cds":[{"apy":3.5}]} both classify.
    Keeps the highest rate per product (consistent with the crawler's `best`).
    """
    found = {}

    def visit_node(o, inherited=""):
        if isinstance(o, dict):
            scalars = {k: v for k, v in o.items()
                       if not isinstance(v, (dict, list))}
            blob = (inherited + " " +
                    " ".join(f"{k}={v}" for k, v in scalars.items()))[:600]
            for k, v in scalars.items():
                if not JSON_RATE_KEY_PAT.search(str(k)):
                    continue
                sv = str(v).replace("%", "").strip()
                if "apy" not in str(k).lower() and "." not in sv:
                    continue
                try:
                    val = float(sv)
                except (ValueError, TypeError):
                    continue
                if not (0.01 <= val <= 9.99):
                    continue
                for prod, pat in JSON_PRODUCT_HINTS:
                    if pat.search(blob):
                        if prod not in found or val > found[prod]:
                            found[prod] = val
                        break
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    visit_node(v, (inherited + " " + str(k))[-300:])
        elif isinstance(o, list):
            for v in o:
                visit_node(v, inherited)

    try:
        visit_node(data)
    except Exception:
        pass
    return found


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
  "savings": <float APY% or null>,
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
            "checking":     float(result["checking"])     if result.get("checking")     else None,
            "savings":      float(result["savings"])      if result.get("savings")      else None,
            "cd":           float(result["cd"])           if result.get("cd")           else None,
            "cd_term":      result.get("cd_term"),
            "money_market": float(result["money_market"]) if result.get("money_market") else None,
            "_confidence":  result.get("confidence", "low"),
            "_source_note": result.get("source_note", ""),
            "_rebrand_hint": result.get("rebrand_hint"),
        }
        # Only return if at least one rate found
        if any(cleaned.get(k) for k in ["checking", "savings", "cd", "money_market"]):
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

def _parse_quarter_folder(name):
    """MM-DD-YYYY → datetime, or None."""
    try:
        return datetime.strptime(name, "%m-%d-%Y")
    except ValueError:
        return None


def _find_schedule_file(folder, schedule):
    """
    Find RI / RCE / RCK file inside a quarter folder.
    Works with any filename format downloaded directly from FFIEC —
    spaces or underscores, with or without extension.
    Matches any file whose name contains the schedule keyword (RI, RCE, RCK)
    surrounded by spaces or underscores, case-insensitive.
    e.g. 'FFIEC CDR Call Schedule RI 12312025'  (no extension, spaces)
    """
    # Build patterns that won't accidentally match RCK when looking for RCE etc.
    # We check that the schedule code appears as a whole word (bounded by space/underscore/end)
    pat = re.compile(
        r'(?:[\s_])' + re.escape(schedule) + r'(?:[\s_\d]|$)',
        re.IGNORECASE
    )
    for f in folder.iterdir():
        if f.is_file() and pat.search(f.name):
            return f
    return None


def _load_quarter(dt, folder):
    """
    Load a single quarter folder into a cr_data dict.
    Returns the dict, or None if files are missing / unreadable.
    """
    if not PANDAS_OK:
        return None
    ri_file  = _find_schedule_file(folder, "RI")
    rce_file = _find_schedule_file(folder, "RCE")
    rck_file = _find_schedule_file(folder, "RCK")
    print(f"  Checking {folder.name}:")
    print(f"    RI  -> {ri_file.name if ri_file  else 'NOT FOUND'}")
    print(f"    RCE -> {rce_file.name if rce_file else 'NOT FOUND'}")
    print(f"    RCK -> {rck_file.name if rck_file else 'NOT FOUND'}")
    if not (ri_file and rce_file and rck_file):
        return None
    try:
        ri  = pd.read_csv(ri_file,  sep="\t", low_memory=False)
        rce = pd.read_csv(rce_file, sep="\t", low_memory=False)
        ri.columns  = [c.strip() for c in ri.columns]
        rce.columns = [c.strip() for c in rce.columns]

        ri_need  = ["IDRSSD","RIAD0093","RIAD4508","RIAD4073","RIADHK03","RIADHK04"]
        rce_need = ["IDRSSD","RCON2203","RCON2210","RCON2215","RCON6810","RCON6648"]
        if any(c not in ri.columns  for c in ri_need):  return None
        if any(c not in rce.columns for c in rce_need): return None

        merged = ri[ri_need].merge(rce[rce_need], on="IDRSSD", how="inner")
        for col in ri_need[1:] + rce_need[1:]:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

        ann = {3: 4.0, 6: 2.0, 9: 4/3, 12: 1.0}.get(dt.month, 1.0)

        def safe_apy(num, den):
            r = (num * ann) / den.replace(0, np.nan) * 100
            return r.where(r <= 20).round(2)

        merged["cr_savings_apy"]      = safe_apy(merged["RIAD0093"], merged["RCON6810"])
        merged["cr_checking_apy"]     = safe_apy(merged["RIAD4508"], merged["RCON2203"])
        merged["cr_cd_apy"]           = safe_apy(merged["RIADHK03"] + merged["RIADHK04"], merged["RCON6648"])
        merged["cr_cost_of_deposits"] = safe_apy(merged["RIAD4073"], merged["RCON2210"])
        merged["cr_total_deposits_m"] = (merged["RCON2215"] / 1000).round(1)

        def v(x):
            return None if pd.isna(x) else float(x)

        cr_data = {}
        for _, row in merged.iterrows():
            try:
                rssdid = int(row["IDRSSD"])
            except (ValueError, TypeError):
                continue
            cr_data[rssdid] = {
                "cr_savings_apy":      v(row["cr_savings_apy"]),
                "cr_checking_apy":     v(row["cr_checking_apy"]),
                "cr_cd_apy":           v(row["cr_cd_apy"]),
                "cr_cost_of_deposits": v(row["cr_cost_of_deposits"]),
                "cr_total_deposits_m": v(row["cr_total_deposits_m"]),
                "cr_period":           dt.strftime("%b %Y"),
            }
        print(f"  \u2713 Loaded {len(cr_data)} banks from {folder.name}")
        return cr_data
    except Exception as e:
        print(f"  Call Report load error ({folder.name}): {e}")
        return None


# Quarters loaded straight from Supabase (raw_schedule_RI / RCE) rather than
# requiring a local CallReports/MM-DD-YYYY folder. Add a new datetime here
# as soon as a quarter's FFIEC schedules have been uploaded and refreshed
# in Supabase — no local file drop needed for these.
SUPABASE_CALL_REPORT_QUARTERS = [
    datetime(2026, 3, 31),   # Mar 2026 — refreshed directly in Supabase
]


def _load_quarter_from_supabase(dt):
    """
    Load a single quarter directly from Supabase's raw_schedule_RI / RCE
    tables (PostgREST), computing the same implied-APY fields as
    _load_quarter(). Returns a cr_data dict, or None on failure.
    """
    if not (SUPABASE_URL and SUPABASE_KEY) or not PANDAS_OK:
        return None

    import urllib.request as _urlreq
    import urllib.error as _urlerr

    period_str = dt.strftime("%Y-%m-%d")
    # raw_schedule_RI / raw_schedule_RCE live in the 'raw' schema (confirmed
    # in Supabase Table Editor) — PostgREST needs Accept-Profile to read
    # from a non-public schema, or every request 404s.
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept-Profile": "raw",
    }

    def fetch_all(table, select, page=1000):
        rows, offset = [], 0
        while True:
            url = (f"{SUPABASE_URL}/rest/v1/{table}"
                   f"?period=eq.{period_str}&select={select}"
                   f"&limit={page}&offset={offset}")
            req = _urlreq.Request(url, headers=headers)
            try:
                with _urlreq.urlopen(req, timeout=30) as resp:
                    batch = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                print(f"  Supabase fetch error ({table}, {period_str}): {e}")
                return None
            if not isinstance(batch, list):
                print(f"  Supabase fetch error ({table}, {period_str}): {batch}")
                return None
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return rows

    print(f"  Checking Supabase for {dt.strftime('%b %Y')} (period {period_str}):")
    ri_rows  = fetch_all("raw_schedule_RI",  "IDRSSD,RIAD0093,RIAD4508,RIAD4073,RIADHK03,RIADHK04")
    rce_rows = fetch_all("raw_schedule_RCE", "IDRSSD,RCON2203,RCON2210,RCON2215,RCON6810,RCON6648")
    print(f"    RI  -> {len(ri_rows)  if ri_rows  else 0} rows")
    print(f"    RCE -> {len(rce_rows) if rce_rows else 0} rows")
    if not ri_rows or not rce_rows:
        return None

    try:
        ri  = pd.DataFrame(ri_rows)
        rce = pd.DataFrame(rce_rows)

        ri_need  = ["IDRSSD","RIAD0093","RIAD4508","RIAD4073","RIADHK03","RIADHK04"]
        rce_need = ["IDRSSD","RCON2203","RCON2210","RCON2215","RCON6810","RCON6648"]
        if any(c not in ri.columns  for c in ri_need):  return None
        if any(c not in rce.columns for c in rce_need): return None

        merged = ri[ri_need].merge(rce[rce_need], on="IDRSSD", how="inner")
        for col in ri_need[1:] + rce_need[1:]:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

        ann = {3: 4.0, 6: 2.0, 9: 4/3, 12: 1.0}.get(dt.month, 1.0)

        def safe_apy(num, den):
            r = (num * ann) / den.replace(0, np.nan) * 100
            return r.where(r <= 20).round(2)

        merged["cr_savings_apy"]      = safe_apy(merged["RIAD0093"], merged["RCON6810"])
        merged["cr_checking_apy"]     = safe_apy(merged["RIAD4508"], merged["RCON2203"])
        merged["cr_cd_apy"]           = safe_apy(merged["RIADHK03"] + merged["RIADHK04"], merged["RCON6648"])
        merged["cr_cost_of_deposits"] = safe_apy(merged["RIAD4073"], merged["RCON2210"])
        merged["cr_total_deposits_m"] = (merged["RCON2215"] / 1000).round(1)

        def v(x):
            return None if pd.isna(x) else float(x)

        cr_data = {}
        for _, row in merged.iterrows():
            try:
                rssdid = int(row["IDRSSD"])
            except (ValueError, TypeError):
                continue
            cr_data[rssdid] = {
                "cr_savings_apy":      v(row["cr_savings_apy"]),
                "cr_checking_apy":     v(row["cr_checking_apy"]),
                "cr_cd_apy":           v(row["cr_cd_apy"]),
                "cr_cost_of_deposits": v(row["cr_cost_of_deposits"]),
                "cr_total_deposits_m": v(row["cr_total_deposits_m"]),
                "cr_period":           dt.strftime("%b %Y"),
            }
        print(f"  \u2713 Loaded {len(cr_data)} banks from Supabase ({dt.strftime('%b %Y')})")
        return cr_data
    except Exception as e:
        print(f"  Call Report load error (Supabase, {period_str}): {e}")
        return None


def load_call_reports():
    """
    Scan CallReports/ for MM-DD-YYYY quarter folders, and also consider
    quarters in SUPABASE_CALL_REPORT_QUARTERS that are refreshed directly
    in Supabase and don't need a local file drop. Loads the two most
    recent quarters (by date, across both sources) that have complete data.
    Returns (cr_data, period_label, prev_cr_data, prev_period_label, all_quarter_labels).
    """
    if not PANDAS_OK:
        return {}, None, {}, None, []

    local_quarters = []
    if CALL_REPORTS_DIR.exists():
        local_quarters = [
            (dt, "local", f) for f in CALL_REPORTS_DIR.iterdir()
            if f.is_dir() and (dt := _parse_quarter_folder(f.name))
        ]
    local_dates = {dt for dt, _, _ in local_quarters}

    supabase_quarters = [
        (dt, "supabase", None) for dt in SUPABASE_CALL_REPORT_QUARTERS
        if dt not in local_dates
    ]

    candidates = sorted(local_quarters + supabase_quarters, key=lambda x: x[0], reverse=True)
    quarter_labels = [dt.strftime("%b %Y") for dt, _, _ in candidates]

    loaded = []  # list of (dt, cr_data)
    for dt, source, folder in candidates:
        if len(loaded) >= 2:
            break
        data = _load_quarter(dt, folder) if source == "local" else _load_quarter_from_supabase(dt)
        if data is not None:
            loaded.append((dt, data))

    if not loaded:
        return {}, None, {}, None, quarter_labels

    curr_dt,  curr_data  = loaded[0]
    prev_dt,  prev_data  = loaded[1] if len(loaded) > 1 else (None, {})

    return (
        curr_data,
        curr_dt.strftime("%b %Y"),
        prev_data,
        prev_dt.strftime("%b %Y") if prev_dt else None,
        quarter_labels,
    )


# ── Rate extraction ───────────────────────────────────────────────────────────

def extract_rates(text):
    r = {"checking": None, "savings": None, "cd": None, "cd_term": None,
         "money_market": None, "min_balance": None,
         "rate_tags": {}}   # e.g. {"checking": ["conditional"], "savings": ["promo"]}

    # Savings — collect ALL matches with positions, take the highest
    sav_matches = [(float(m.group(1)), m.start()) for m in SAVINGS_PAT.finditer(text)
                   if 0.05 <= float(m.group(1)) <= 15]
    sav_matches += [(float(m.group(1)), m.start()) for m in SAVINGS_PAT_NEXTLN.finditer(text)
                    if 0.05 <= float(m.group(1)) <= 15]
    sav_matches += [(float(m.group(1)), m.start()) for m in SAVINGS_PAT_WIDEGAP.finditer(text)
                    if 0.05 <= float(m.group(1)) <= 15]
    if sav_matches:
        best_sav = max(sav_matches, key=lambda x: x[0])
        r["savings"] = best_sav[0]
        tags = tag_rate_context(text, best_sav[1])
        if tags:
            r["rate_tags"]["savings"] = tags

    # Checking — collect ALL matches with positions, take highest
    chk_matches = [(float(m.group(1)), m.start()) for m in CHECKING_PAT.finditer(text)
                   if 0.05 <= float(m.group(1)) <= 15]
    chk_matches += [(float(m.group(1)), m.start()) for m in CHECKING_PAT_NEXTLN.finditer(text)
                    if 0.05 <= float(m.group(1)) <= 15]
    chk_matches += [(float(m.group(1)), m.start()) for m in CHECKING_PAT_PLAIN.finditer(text)
                    if 0.05 <= float(m.group(1)) <= 15]
    if not chk_matches:
        m = HERO_CHECKING_PAT.search(text)
        if m:
            chk_matches.append((float(m.group(1)), m.start()))
    if chk_matches:
        best_chk = max(chk_matches, key=lambda x: x[0])
        r["checking"] = best_chk[0]
        tags = tag_rate_context(text, best_chk[1])
        if tags:
            r["rate_tags"]["checking"] = tags

    # Money market — collect ALL matches, take highest
    mm_matches = [(float(m.group(1)), m.start()) for m in MM_PAT.finditer(text)
                  if 0.05 <= float(m.group(1)) <= 15]
    mm_matches += [(float(m.group(1)), m.start()) for m in MM_PAT_NEXTLN.finditer(text)
                   if 0.05 <= float(m.group(1)) <= 15]
    if mm_matches:
        best_mm = max(mm_matches, key=lambda x: x[0])
        r["money_market"] = best_mm[0]
        tags = tag_rate_context(text, best_mm[1])
        if tags:
            r["rate_tags"]["money_market"] = tags

    # CD candidates — require APY label, reject loans, deprioritise jumbos
    cd_candidates = []
    for m in TABLE_PAT.finditer(text):
        val = float(m.group(3))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            unit = m.group(2).lower()
            term = f"{int(m.group(1))*12}-month" if "year" in unit else f"{m.group(1)}-month"
            cd_candidates.append((val, term, ctx, m.start()))
    for m in TERM_APY_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, f"{m.group(2)}-{m.group(3)}", ctx, m.start()))
    for m in CD_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, None, ctx, m.start()))
    # v3.4: label-on-one-line / APY-on-the-next (rate tiles, table cells)
    for m in TABLE_PAT_NEXTLN.finditer(text):
        val = float(m.group(3))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            unit = m.group(2).lower()
            term = f"{int(m.group(1))*12}-month" if "year" in unit else f"{m.group(1)}-month"
            cd_candidates.append((val, term, ctx, m.start()))
    for m in CD_PAT_NEXTLN.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, None, ctx, m.start()))
    if cd_candidates:
        def cd_score(c): return c[0] - (2.0 if JUMBO_PAT.search(c[2] or "") else 0.0)
        best_cd = max(cd_candidates, key=cd_score)
        r["cd"], r["cd_term"] = best_cd[0], best_cd[1]
        tags = tag_rate_context(text, best_cd[3])
        if tags:
            r["rate_tags"]["cd"] = tags

    if not any([r["checking"], r["savings"], r["cd"], r["money_market"]]):
        apys = [float(v) for v in APY_PAT.findall(text) if 0.05 <= float(v) <= 15]
        if apys:
            r["cd"] = max(apys)
            r["cd_term"] = "best found"  # low-confidence — flagged for AI confirmation

    m = MIN_BAL_PAT.search(text)
    if m:
        val = float(m.group(1).replace(",", ""))
        if val > 0:
            r["min_balance"] = f"${int(val):,}"
    return r



# ── AI Vision fallback ────────────────────────────────────────────────────────

async def ai_vision_extract(page, url, bank_name):
    """
    Screenshot the current page and send to Claude Haiku for rate extraction.
    Returns same dict as extract_rates(), or None on failure.
    Falls back to HTML text extraction if screenshot fails.
    """
    if not ANTHROPIC_OK:
        return None
    try:
        # Take full-page screenshot
        screenshot_bytes = await page.screenshot(full_page=True, type="png")
        img_b64 = base64.standard_b64encode(screenshot_bytes).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are analyzing a bank rates page for {bank_name}.
Extract the STANDARD deposit rates. Return ONLY JSON, no explanation:
{{
  "checking": <standard checking APY% float or null>,
  "savings": <standard savings or high-yield savings APY% float or null>,
  "cd": <highest CD APY% float or null>,
  "cd_term": <term for that CD like "12-month" or null>,
  "money_market": <money market APY% float or null>,
  "min_balance": <minimum balance string like "$1,000" or null>
}}
Critical rules:
- DO NOT cross-assign rates between product types (a CD rate is never savings)
- For checking/savings/money_market: use the HIGHEST advertised APY for that product type
- For CD: use the HIGHEST APY shown, record its term
- If a rate says "up to X%" use X
- If you only see a promo banner with no rate table, return null for that field
- Return ONLY the JSON object"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        import json
        txt = response.content[0].text.strip()
        result = parse_json_object(txt)   # tolerates preamble / fences

        # Validate and clean
        cleaned = {
            "checking":     float(result["checking"])     if result.get("checking")     else None,
            "savings":      float(result["savings"])      if result.get("savings")      else None,
            "cd":           float(result["cd"])           if result.get("cd")           else None,
            "cd_term":      result.get("cd_term"),
            "money_market": float(result["money_market"]) if result.get("money_market") else None,
            "min_balance":  result.get("min_balance"),
        }
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
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
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
    best = {"checking": None, "savings": None, "cd": None, "cd_term": None,
            "money_market": None, "min_balance": None}
    found_on    = None
    source_urls = {}
    visited     = set()
    rate_tags   = {}   # accumulated tags across all pages
    rebrand_hint = bank.get("_rebrand_hint")   # pre-populated from preflight if found
    extra    = next((urls for k, urls in BANK_EXTRA_URLS.items() if k in base.replace('www.','')), [])
    # v3.4: ZIP-gate state — max 2 fill attempts per bank, remembers where it fired
    bank_zip = get_bank_zip(bank)
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
        if len(visited) >= (14 if priority else 12):
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
            resp = await page.goto(url, timeout=12000, wait_until="domcontentloaded")
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
                if any(r.get(k) for k in core_keys):
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
                        if k in ["checking", "savings", "cd", "money_market"]:
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
            for k in ["checking", "savings", "cd", "cd_term", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "cd", "money_market"]:
                        source_urls[k] = da.get("_source", "depositaccounts.com") + stale_flag

    # Full path scan — only if still incomplete after priority + early DA
    count = sum(1 for k in ["checking", "savings", "cd", "money_market"] if best[k] is not None)
    if count < 4:
        remaining = [p for p in RATE_PATHS if p not in PRIORITY_PATHS]
        for path in remaining:
            await visit(base + path)

    # ── Fix 2: search-engine rescue ──────────────────────────────────────────
    # Path list exhausted and we have NOTHING — before marking "Not public",
    # ask a search engine for `site:<domain> rates APY` and crawl the top
    # on-domain results (Fifth Third, Citizens, Santander etc. publish rates
    # on paths no static list will ever guess).
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
            for k in ["checking", "savings", "cd", "cd_term", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "cd", "money_market"]:
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
                        for k in ["checking", "savings", "cd", "cd_term", "money_market", "min_balance"]:
                            if ai_result.get(k) is not None and best.get(k) is None:
                                best[k] = ai_result[k]
                                filled += 1
                                if k in ["checking", "savings", "cd", "money_market"]:
                                    source_urls[k] = f"[AI] {v_url}"
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
        "savings_apy":               best["savings"],
        "cd_apy":                    best["cd"],
        "cd_term":                   best["cd_term"],
        "money_market_apy":          best["money_market"],
        "min_balance":               best["min_balance"],
        "status":                    status,
        "note":                      " | ".join(parts) + source_note,
        "source_url":                unique_sources[0] if unique_sources else "",
        "source_url_checking":       source_urls.get("checking", ""),
        "source_url_savings":        source_urls.get("savings", ""),
        "source_url_cd":             source_urls.get("cd", ""),
        "crawled_at":                datetime.now().strftime("%Y-%m-%d %H:%M"),
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

    best = {"checking": None, "savings": None, "cd": None, "cd_term": None,
            "money_market": None, "min_balance": None}
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
            return resp.content[0].text.strip()
        except Exception as e:
            return f"ERROR: {e}"

    async def extract_rates_ai(img_b64, url):
        """Ask Claude to extract all rates from a screenshot."""
        prompt = f"""Extract ALL deposit rates from this bank rates page for {bank['bank_name']}.
Return ONLY valid JSON, no explanation:
{{"checking": <float or null>, "savings": <float or null>, "cd": <float or null>,
  "cd_term": <"12-month" or null>, "money_market": <float or null>, "min_balance": <"$1,000" or null>}}
Rules: use highest APY shown for each type. If no rates visible return all nulls."""
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt}
                ]}]
            )
            txt = resp.content[0].text.strip()
            result = parse_json_object(txt)   # v3.5: tolerate non-JSON preamble
            cleaned = {
                "checking":     float(result["checking"])     if result.get("checking")     else None,
                "savings":      float(result["savings"])      if result.get("savings")      else None,
                "cd":           float(result["cd"])           if result.get("cd")           else None,
                "cd_term":      result.get("cd_term"),
                "money_market": float(result["money_market"]) if result.get("money_market") else None,
                "min_balance":  result.get("min_balance"),
            }
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
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
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
                for k in ["checking","savings","cd","cd_term","money_market","min_balance"]:
                    if rates.get(k) is not None and best.get(k) is None:
                        best[k] = rates[k]
                        filled  = True
                        if k in ["checking","savings","cd","money_market"]:
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

    # Step 1: Check BANK_EXTRA_URLS first
    extra = next((urls for k, urls in BANK_EXTRA_URLS.items() if k in base.replace("www.","")), [])
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
            for k in ["checking", "savings", "cd", "cd_term", "money_market", "min_balance"]:
                if da.get(k) is not None and best.get(k) is None:
                    best[k] = da[k]
                    if k in ["checking", "savings", "cd", "money_market"]:
                        source_urls[k] = da.get("_source", "depositaccounts.com")

    # Build result
    count  = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
    status = "Found" if count == 3 else "Partial" if count > 0 else "Not public"
    parts  = []
    if any(best[k] for k in ["checking","savings","cd","money_market"]):
        rate_parts = []
        if best["cd"]:           rate_parts.append(f"CD {best['cd']:.2f}%{' ('+best['cd_term']+')' if best['cd_term'] else ''}")
        if best["savings"]:      rate_parts.append(f"Savings {best['savings']:.2f}%")
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
        "savings_apy":              best["savings"],
        "cd_apy":                   best["cd"],
        "cd_term":                  best["cd_term"],
        "money_market_apy":         best["money_market"],
        "min_balance":              best["min_balance"],
        "status":                   status,
        "note":                     " | ".join(parts) + source_note,
        "source_url":               unique_sources[0] if unique_sources else "",
        "source_url_checking":      source_urls.get("checking",""),
        "source_url_savings":       source_urls.get("savings",""),
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
  "savings": <float APY% or null>,
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
            txt = resp.content[0].text.strip()
            result = parse_json_object(txt)

            extracted = {
                "checking":     float(result["checking"])     if result.get("checking")     else None,
                "savings":      float(result["savings"])      if result.get("savings")      else None,
                "cd":           float(result["cd"])           if result.get("cd")           else None,
                "cd_term":      result.get("cd_term"),
                "money_market": float(result["money_market"]) if result.get("money_market") else None,
                "min_balance":  result.get("min_balance"),
                "promo_note":   result.get("promo_note"),
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
    # ── Version banner + environment ────────────────────────────────────────
    crawl_state["log"].append("Rate Radar v3.6 — Supabase push + 404-skip fast crawl")
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

            # Batch size 3, 2s between batches — ~4K tokens each → stays well under 50K TPM
            BATCH_SIZE = 3
            for i in range(0, len(banks_to_preflight), BATCH_SIZE):
                batch = banks_to_preflight[i:i + BATCH_SIZE]
                await asyncio.gather(*[do_preflight(b) for b in batch])
                if i + BATCH_SIZE < len(banks_to_preflight):
                    await asyncio.sleep(2)  # breathe between batches

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
            browser = await asyncio.wait_for(p.chromium.launch(**launch_kwargs), timeout=45)
            crawl_state["log"].append("  ✓ Browser launched")
        except asyncio.TimeoutError:
            crawl_state["log"].append(
                "  ✗ Browser launch timed out after 45s — likely a sandbox/permissions "
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

        semaphore = asyncio.Semaphore(5)
        # v3.3: cap concurrent final-pass search calls (Haiku TPM safety)
        final_search_sem = asyncio.Semaphore(2)
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
                pre = preflight_results.get(bank["bank_name"])
                if pre and pre.get("_confidence") == "high":
                    pre_count = sum(1 for k in ["checking","savings","cd"] if pre.get(k))
                    if pre_count == 3:
                        crawl_state["log"].append(
                            f"  ✓ Preflight 3/3 ({pre.get('_source_note','search')}) — skipping browser"
                        )
                        # Build minimal result from preflight data
                        result = _build_result_from_preflight(bank, pre)
                        crawl_state["results"].append(result)
                        if result.get("status") in ("Found","Partial","Not public"):
                            async with cache_lock:
                                today_cache[cache_key] = result
                                _save_today_cache(today_cache)
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
                                                   ("cd","cd_apy"),("cd_term","cd_term"),
                                                   ("money_market","money_market_apy"),("min_balance","min_balance")]:
                                        if chat_result.get(k) is not None and not result.get(rk):
                                            result[rk] = chat_result[k]
                                    if chat_result.get("promo_note"):
                                        result["note"] = (result.get("note","") + " | 💬 Chat: " + chat_result["promo_note"]).strip(" |")
                                    new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                                    result["status"] = "Found" if new_count==3 else "Partial" if new_count>0 else "Not public"
                                    result["source_url"] = result.get("source_url","") or "Live chat"
                            finally:
                                await page2.close()
                    else:
                        result = await crawl_bank(page, bank, http_session=http_session)

                    # Merge any preflight data into gaps left by browser crawl
                    if pre:
                        for k, rk in [("checking","checking_apy"),("savings","savings_apy"),
                                       ("cd","cd_apy"),("cd_term","cd_term"),
                                       ("money_market","money_market_apy")]:
                            if pre.get(k) is not None and not result.get(rk):
                                result[rk] = pre[k]
                                result["note"] = (result.get("note","") +
                                    f" | {k} from search: {pre[k]}").strip(" |")
                        # Re-evaluate status
                        new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                        result["status"] = "Found" if new_count==3 else "Partial" if new_count>0 else "Not public"

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
                            result["status"] = ("Found" if new_count==3
                                                else "Partial" if new_count>0 else "Not public")

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
                                result["status"] = "Partial"
                                crawl_state["log"].append(
                                    f"    [History] \u21bb recovered {', '.join(filled)} "
                                    f"from {as_of} (today's search found nothing)")

                    icon = "✓" if result["status"] == "Found" else "~" if result["status"] == "Partial" else "○"
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
                except Exception as e:
                    crawl_state["log"].append(f"  x Error: {e}")
                    if bank["bank_name"].lower() not in crawl_state.get("removed", set()):
                        crawl_state["results"].append({
                        **bank,
                        "checking_apy": None, "savings_apy": None, "cd_apy": None,
                        "cd_term": None, "money_market_apy": None, "min_balance": None,
                        "status": "Error", "note": str(e),
                        "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "cr_savings_apy": None, "cr_checking_apy": None,
                        "cr_cd_apy": None, "cr_cost_of_deposits": None,
                        "cr_total_deposits_m": None, "cr_period": "",
                        "prev_cr_savings_apy": None, "prev_cr_checking_apy": None,
                        "prev_cr_cd_apy": None, "prev_cr_cost_of_deposits": None,
                        "prev_cr_total_deposits_m": None, "cr_prev_period": "",
                        "delta_savings_apy": None, "delta_checking_apy": None,
                        "delta_cd_apy": None, "delta_cost_of_deposits": None,
                        })
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
    ai_note    = f" · {ai_calls} AI vision" if ai_calls else ""
    chat_note  = f" · {chat_calls} chat" if chat_calls else ""
    crawl_state["log"].append(
        f"Done — {found} full, {partial} partial, "
        f"{len(crawl_state['results'])-found-partial} not public{ai_note}{chat_note}"
    )
    saved = auto_save(crawl_state["results"])
    if saved and saved[0]:
        await push_to_supabase(crawl_state["results"], saved[0], saved[1])


def _build_result_from_preflight(bank, pre):
    """Build a minimal result dict from preflight search data (no browser needed)."""
    count  = sum(1 for k in ["checking","savings","cd"] if pre.get(k))
    status = "Found" if count == 3 else "Partial" if count > 0 else "Not public"
    parts  = []
    if pre.get("rebrand_hint"):
        parts.append(f"⚠️ REBRAND: {pre['rebrand_hint']}")
        status = "Partial"
    rate_parts = []
    if pre.get("cd"):      rate_parts.append(f"CD {pre['cd']:.2f}%{' ('+pre['cd_term']+')' if pre.get('cd_term') else ''}")
    if pre.get("savings"): rate_parts.append(f"Savings {pre['savings']:.2f}%")
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
        "savings_apy":               pre.get("savings"),
        "cd_apy":                    pre.get("cd"),
        "cd_term":                   pre.get("cd_term"),
        "money_market_apy":          pre.get("money_market"),
        "min_balance":               None,
        "status":                    status,
        "note":                      " | ".join(parts) + f" | Source: [Search] {src}",
        "source_url":                f"[Search] {src}",
        "source_url_checking":       f"[Search] {src}" if pre.get("checking") else "",
        "source_url_savings":        f"[Search] {src}" if pre.get("savings") else "",
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


# ── Export / auto-save ────────────────────────────────────────────────────────

EXPORTS = Path(__file__).parent / "RateRadar_Exports"
FIELDS  = [
    "run_id", "run_date", "crawled_at",
    "bank_name", "bank_url", "RSSDID", "bank_type", "branch_address",
    "checking_apy", "savings_apy", "cd_apy", "cd_term",
    "money_market_apy", "min_balance", "status", "note",
    "source_url", "source_url_checking", "source_url_savings", "source_url_cd",
    "vulnerability_flag",
    "cr_period", "cr_total_deposits_m",
    "cr_savings_apy", "cr_checking_apy", "cr_cd_apy", "cr_cost_of_deposits",
    "cr_prev_period", "prev_cr_total_deposits_m",
    "prev_cr_savings_apy", "prev_cr_checking_apy", "prev_cr_cd_apy", "prev_cr_cost_of_deposits",
    "delta_savings_apy", "delta_checking_apy", "delta_cd_apy", "delta_cost_of_deposits",
]

# ── v3.5: Supabase push ──────────────────────────────────────────────────────
# Every run lands in raw.raw_rate_radar so rates accumulate into a time
# series (vw_rate_radar_history) instead of dying in local CSVs.

SB_NUMERIC_FIELDS = {
    "checking_apy", "savings_apy", "cd_apy", "money_market_apy",
    "cr_total_deposits_m", "cr_savings_apy", "cr_checking_apy", "cr_cd_apy",
    "cr_cost_of_deposits", "prev_cr_total_deposits_m", "prev_cr_savings_apy",
    "prev_cr_checking_apy", "prev_cr_cd_apy", "prev_cr_cost_of_deposits",
    "delta_savings_apy", "delta_checking_apy", "delta_cd_apy",
    "delta_cost_of_deposits",
}


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


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/search-banks")
def search_banks():
    """Typeahead against public.vw_bank_directory (dim_institutions x bank_website),
    used by the '+ Add bank' box so a bank can be added without a CSV re-upload."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    if not (SUPABASE_URL and SUPABASE_KEY):
        return jsonify({"error": "Supabase not configured"}), 500
    url = (f"{SUPABASE_URL}/rest/v1/vw_bank_directory"
           f"?bank_name=ilike.*{quote(q)}*"
           f"&select=rssdid,bank_name,bank_url,city_hq,state_hq&limit=8")
    req = urlreq.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urlreq.urlopen(req, timeout=8) as resp:
            return jsonify(json.loads(resp.read().decode("utf-8")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/remove-bank", methods=["POST"])
def remove_bank():
    """Remove one bank from the current queue (and any existing crawl result) by name."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("bank_name") or "").strip()
    if not name:
        return jsonify({"error": "bank_name required"}), 400
    before = len(crawl_state.get("banks", []))
    crawl_state["banks"] = [b for b in crawl_state.get("banks", [])
                            if b.get("bank_name","").lower() != name.lower()]
    crawl_state["results"] = [r for r in crawl_state.get("results", [])
                              if r.get("bank_name","").lower() != name.lower()]
    crawl_state.setdefault("removed", set()).add(name.lower())
    removed = before - len(crawl_state["banks"])
    if removed:
        crawl_state["log"].append(f"− Removed: {name}")
    return jsonify({"banks": crawl_state["banks"], "removed": bool(removed)})


@app.route("/add-bank", methods=["POST"])
def add_bank():
    """Append one manually-picked bank to the current queue without re-uploading a CSV."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("bank_name") or "").strip()
    if not name:
        return jsonify({"error": "bank_name required"}), 400
    bank = {
        "bank_name": name,
        "bank_url":  (body.get("bank_url") or "").strip(),
        "RSSDID":    str(body.get("RSSDID") or "").strip(),
        "bank_type": (body.get("bank_type") or "").strip(),
        "branch_address": (body.get("branch_address") or "").strip(),
    }
    crawl_state.setdefault("banks", [])
    if any(b.get("bank_name","").lower() == name.lower() for b in crawl_state["banks"]):
        return jsonify({"error": f"{name} is already in the list", "banks": crawl_state["banks"]}), 409
    crawl_state["banks"].append(bank)
    crawl_state["log"].append(f"+ Manually added: {name}")
    return jsonify({"banks": crawl_state["banks"], "count": len(crawl_state["banks"])})

@app.route("/manual-save", methods=["POST"])
def manual_save():
    """Persist the on-screen table (crawled + any hand-edited cells) to Supabase
    as its own run, timestamped, so manual overrides are tracked over time
    alongside crawled runs rather than silently overwriting them."""
    body = request.get_json(force=True, silent=True) or {}
    rows_in = body.get("results") or []
    if not rows_in:
        return jsonify({"error": "No rows to save"}), 400
    if not (SUPABASE_URL and SUPABASE_KEY):
        return jsonify({"error": "Supabase not configured on server"}), 500

    run_id   = f"manual-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_date = datetime.now().strftime("%Y-%m-%d")
    for r in rows_in:
        if r.get("_edited"):
            note = (r.get("note") or "").strip()
            r["note"] = (note + " | Manually verified/edited").strip(" |")
    rows = build_supabase_rows(rows_in, run_id, run_date)
    if not rows:
        return jsonify({"error": "Nothing valid to save"}), 400

    url = f"{SUPABASE_URL}/rest/v1/raw_rate_radar?on_conflict=run_id,bank_name"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Content-Profile": "raw",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    req = urlreq.Request(url, data=json.dumps(rows).encode("utf-8"),
                         headers=headers, method="POST")
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            crawl_state["log"].append(
                f"Manual save: ✓ pushed {len(rows)} rows to raw.raw_rate_radar (run {run_id})")
            return jsonify({"saved": len(rows), "run_id": run_id})
    except Exception as e:
        crawl_state["log"].append(f"Manual save failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/current-state")
def current_state():
    """Read-only peek at whatever's already loaded server-side, without
    changing anything. Used on page load so a reload or a re-clicked
    autoload link can't silently discard an in-progress session (deletions,
    manual edits, a trimmed queue) that only exists in server memory."""
    return jsonify({
        "banks":   crawl_state.get("banks", []),
        "results": crawl_state.get("results", []),
        "running": crawl_state.get("running", False),
    })

@app.route("/reset", methods=["POST"])
def reset_state():
    """Explicit server-side clear — 'Change File' calling this makes the
    reset real instead of just cosmetic on the frontend."""
    if crawl_state.get("running"):
        return jsonify({"error": "Can't reset while a crawl is running"}), 400
    crawl_state["banks"]   = []
    crawl_state["results"] = []
    crawl_state["log"].append("— Session reset —")
    return jsonify({"ok": True})

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv")
    if not f:
        return jsonify({"error": "No file received"}), 400
    text   = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    banks  = []
    for row in reader:
        norm     = {k.strip().lower(): v.strip() for k, v in row.items()}
        name_key = next((k for k in norm if "bank_name" in k or k == "name"), None)
        url_key  = next((k for k in norm if "url" in k or "website" in k), None)
        rssd_key    = next((k for k in norm if "rssd" in k), None)
        type_key    = next((k for k in norm if "bank_type" in k or k == "type"), None)
        address_key = next((k for k in norm if "branch_address" in k or "address" in k or "trade_area" in k), None)
        if not name_key:
            continue
        banks.append({
            "bank_name":      norm[name_key],
            "bank_url":       norm.get(url_key, "") if url_key else "",
            "RSSDID":         norm.get(rssd_key, "") if rssd_key else "",
            "bank_type":      norm.get(type_key, "") if type_key else "",
            "branch_address": norm.get(address_key, "") if address_key else "",
        })
    if not banks:
        return jsonify({"error": "No banks found — check column names (need bank_name)"}), 400
    crawl_state["banks"] = banks
    return jsonify({"count": len(banks), "banks": banks})

@app.route("/cr_status")
def cr_status():
    resp = jsonify({
        "loaded":       bool(crawl_state["cr_data"]),
        "period":       crawl_state["cr_period"],
        "prev_period":  crawl_state["cr_prev_period"],
        "quarters":     crawl_state["cr_quarters"],
        "count":        len(crawl_state["cr_data"]),
        "pandas_ok":    PANDAS_OK,
        "ai_vision_ok": ANTHROPIC_OK,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/debug")
def debug():
    """Diagnostic page — shows exactly what Rate Radar sees on disk."""
    lines = []
    lines.append(f"<h2>Rate Radar — File Diagnostics</h2>")
    lines.append(f"<b>Script location:</b> {Path(__file__).resolve()}<br>")
    lines.append(f"<b>CallReports folder expected at:</b> {CALL_REPORTS_DIR.resolve()}<br>")
    lines.append(f"<b>CallReports folder exists:</b> {CALL_REPORTS_DIR.exists()}<br>")
    lines.append(f"<b>pandas installed:</b> {PANDAS_OK}<br><br>")

    if CALL_REPORTS_DIR.exists():
        lines.append("<b>Contents of CallReports/:</b><ul>")
        for item in sorted(CALL_REPORTS_DIR.iterdir()):
            dt = _parse_quarter_folder(item.name)
            lines.append(f"<li>{'📁' if item.is_dir() else '📄'} <b>{item.name}</b>"
                         f" — parsed as: {dt.strftime('%b %Y') if dt else '❌ NOT a valid MM-DD-YYYY folder'}")
            if item.is_dir():
                lines.append("<ul>")
                for f in sorted(item.iterdir()):
                    ri  = "✓ RI"  if _find_schedule_file(item, "RI")  and _find_schedule_file(item, "RI")  == f else ""
                    rce = "✓ RCE" if _find_schedule_file(item, "RCE") and _find_schedule_file(item, "RCE") == f else ""
                    rck = "✓ RCK" if _find_schedule_file(item, "RCK") and _find_schedule_file(item, "RCK") == f else ""
                    tag = " ".join(filter(None, [ri, rce, rck]))
                    lines.append(f"<li>{f.name} &nbsp; <span style='color:green'>{tag}</span></li>")
                ri_f  = _find_schedule_file(item, "RI")
                rce_f = _find_schedule_file(item, "RCE")
                rck_f = _find_schedule_file(item, "RCK")
                missing = [s for s, f in [("RI", ri_f), ("RCE", rce_f), ("RCK", rck_f)] if not f]
                if missing:
                    lines.append(f"<li style='color:red'>❌ Missing: {', '.join(missing)}</li>")
                else:
                    lines.append(f"<li style='color:green'>✓ All three schedules found</li>")
                lines.append("</ul>")
            lines.append("</li>")
        lines.append("</ul>")
    else:
        lines.append("<p style='color:red'>❌ CallReports folder not found at the expected location above.<br>"
                     "Make sure the CallReports folder is in the <b>same folder as rate_radar.py</b>.</p>")

    return "<html><body style='font-family:Arial;padding:24px;'>" + "\n".join(lines) + "</body></html>"

@app.route("/start", methods=["POST"])
def start():
    if crawl_state["running"]:
        return jsonify({"error": "Already running"}), 400
    if not crawl_state["banks"]:
        return jsonify({"error": "Upload a CSV first"}), 400
    if not PLAYWRIGHT_OK:
        return jsonify({"error": "Run: pip install playwright && playwright install chromium"}), 500
    mode = request.json.get("mode", "standard") if request.is_json else request.form.get("mode", "standard")
    force_refresh = (request.json.get("force_refresh", False) if request.is_json
                      else request.form.get("force_refresh") in ("true", "1", "on"))
    crawl_state.update({"running": True, "results": [], "log": [], "done": False,
                        "ai_calls": 0, "crawl_mode": mode, "force_refresh": force_refresh,
                        "preflight_total": 0, "preflight_done": 0, "phase": "starting",
                        "removed": set()})
    crawl_state["log"].append(f"Mode: {mode.upper()}" + (" · AI Vision ON" if ANTHROPIC_OK else " · No API key"))
    start_crawl_thread(crawl_state["banks"])
    return jsonify({"ok": True, "mode": mode})

@app.route("/status")
def status():
    total = len(crawl_state["banks"])
    done  = len(crawl_state["results"])
    return jsonify({
        "running":         crawl_state["running"],
        "done":            crawl_state["done"],
        "total":           total,
        "progress":        done,
        "pct":             round(done / total * 100 if total else 0, 1),
        "log":             crawl_state["log"][-50:],
        "results":         crawl_state["results"],
        "cr_period":       crawl_state["cr_period"],
        "cr_prev_period":  crawl_state["cr_prev_period"],
        "phase":           crawl_state.get("phase", ""),
        "preflight_total": crawl_state.get("preflight_total", 0),
        "preflight_done":  crawl_state.get("preflight_done", 0),
        "force_refresh":   crawl_state.get("force_refresh", False),
    })

@app.route("/export")
def export():
    if not crawl_state["results"]:
        return "No results", 400
    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    now = datetime.now()
    for r in crawl_state["results"]:
        row = dict(r)
        row["run_id"]   = now.strftime("%Y%m%d_%H%M%S")
        row["run_date"] = now.strftime("%Y-%m-%d")
        w.writerow(row)
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=rate_radar_{now.strftime('%Y%m%d_%H%M')}.csv"}
    )
@app.route("/export_prompt")
def export_prompt():
    """
    Generate a plain-text BMAP prompt block for each competitor —
    formatted to paste directly into the v2 persona enrichment prompt.
    Only includes banks that have Call Report data (RSSDID matched).
    """
    if not crawl_state["results"]:
        return "No results", 400

    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    period  = crawl_state.get("cr_period") or "Unknown period"
    p_period = crawl_state.get("cr_prev_period") or ""
    lines   = []

    lines.append("## RATE RADAR COMPETITOR SIGNAL BLOCKS")
    lines.append(f"## Generated: {now}  |  Call Report: {period}" + (f" vs {p_period}" if p_period else ""))
    lines.append("## Paste these blocks into the BMAP Persona Enrichment prompt (Block A per competitor)")
    lines.append("")

    for r in crawl_state["results"]:
        # Only emit blocks where we have at least some CR data
        has_cr = any(r.get(k) for k in ["cr_savings_apy","cr_cd_apy","cr_cost_of_deposits"])
        if not has_cr:
            continue

        name    = r.get("bank_name", "Unknown")
        btype   = r.get("bank_type", "")
        address = r.get("branch_address", "")
        vuln    = r.get("vulnerability_flag", "")

        def fmt(v, suffix="%"):
            return f"{float(v):.2f}{suffix}" if v not in (None, "", "None") else "—"

        def fmt_delta(v):
            if v in (None, "", "None"): return "—"
            f = float(v)
            sign = "+" if f > 0 else ""
            arrow = " ▲" if f > 0.005 else " ▼" if f < -0.005 else ""
            return f"{sign}{f:.2f}%{arrow}"

        lines.append(f"### COMPETITOR: {name}" + (f" [{btype}]" if btype else ""))
        if address:
            lines.append(f"Branch / trade area: {address}")
        lines.append(f"Website: {r.get('bank_url','')}")
        lines.append(f"Total deposits: {fmt(r.get('cr_total_deposits_m'), 'M')}")
        lines.append("")
        lines.append(f"Implied APY — Savings:          {fmt(r.get('cr_savings_apy'))}  (QoQ: {fmt_delta(r.get('delta_savings_apy'))})")
        lines.append(f"Implied APY — Checking:         {fmt(r.get('cr_checking_apy'))}  (QoQ: {fmt_delta(r.get('delta_checking_apy'))})")
        lines.append(f"Implied APY — CD:               {fmt(r.get('cr_cd_apy'))}  (QoQ: {fmt_delta(r.get('delta_cd_apy'))})")
        lines.append(f"Cost of deposits:               {fmt(r.get('cr_cost_of_deposits'))}  (QoQ: {fmt_delta(r.get('delta_cost_of_deposits'))})")
        lines.append("")
        lines.append(f"Advertised — Savings:           {fmt(r.get('savings_apy'))}")
        lines.append(f"Advertised — CD:                {fmt(r.get('cd_apy'))}" + (f"  ({r.get('cd_term','')})" if r.get('cd_term') else ""))
        lines.append(f"Advertised — Checking:          {fmt(r.get('checking_apy'))}")
        lines.append("")
        lines.append(f"Vulnerability flag:             {vuln if vuln else '—'}")
        lines.append(f"Call Report period:             {r.get('cr_period','')}" + (f" vs {r.get('cr_prev_period','')}" if r.get('cr_prev_period') else ""))
        lines.append("-" * 60)
        lines.append("")

    if not any("COMPETITOR:" in l for l in lines):
        return Response(
            "No competitors with Call Report data found.\n"
            "Make sure your CSV includes RSSDID and Call Reports are loaded.",
            mimetype="text/plain"
        )

    return Response(
        "\n".join(lines),
        mimetype="text/plain",
        headers={"Content-Disposition":
                 f"attachment; filename=rate_radar_prompt_blocks_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"}
    )



HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Verlocity - Rate Radar</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --navy: #083D5F;         /* Primary Dark Blue */
  --navy-hover: #062F49;
  --teal: #02A7C2;         /* Primary Light Blue */
  --teal-hover: #028199;
  --jet: #213141;          /* Jet Black */
  --emerald: #66CC99;
  --emerald-dark: #2E8B57;
  --emerald-bg: #EAF8F0;
  --lemon: #CDD61A;
  --lemon-dark: #8A8F12;
  --lemon-bg: #FAFBDE;
  --red: #A32D2D;
  --red-bg: #FCEBEB;
  --muted: #5C7285;
  --muted-light: #93A8BC;
  --border: #DCE4EE;
  --bg: #F2F6F9;
  --white: #FFFFFF;
}
body { font-family: 'Inter', system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--navy); }
header { background: var(--navy); color: white; padding: 14px 24px; border-bottom: 3px solid var(--teal); display:flex; align-items:center; gap:16px; }
header h1 { margin:0; font-size:20px; font-weight:700; }
header h1 span { color:var(--teal); font-weight:500; }
header p { margin:0; font-size:12px; opacity:0.65; }
main { max-width: 1400px; margin: 0 auto; padding: 24px; }
.card { background: var(--white); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
.search-dd-item { padding:9px 14px; cursor:pointer; border-bottom:1px solid #f0f2f5; font-size:13px; }
.search-dd-item:hover { background:#f7f9fc; }
.upload-area { text-align: center; padding: 20px 0; }
.upload-area h2 { margin: 0 0 8px; font-weight:700; }
.upload-area p  { margin: 0 0 16px; color: var(--muted); font-size: 13px; }
input[type=file] { font-size: 14px; padding: 8px; border: 2px solid var(--navy); border-radius: 6px; background: white; cursor: pointer; margin-right: 10px; font-family:'Inter',sans-serif; }
.btn { padding: 9px 22px; border: none; border-radius: 6px; font-size: 14px; font-weight: 700; cursor: pointer; font-family:'Inter',sans-serif; }
.btn-navy  { background: var(--navy); color: white; }
.btn-navy:hover  { background: var(--navy-hover); }
.btn-navy:disabled  { opacity: 0.4; cursor: not-allowed; }
.btn-amber { background: var(--teal); color: white; }
.btn-amber:hover { background: var(--teal-hover); color: white; }
.btn-amber:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-gray  { background: #E7EEF3; color: var(--navy); }
.metrics { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
.metric { background: white; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; border-top: 3px solid var(--teal); }
.metric.cr { border-top-color: var(--navy); }
.metric-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
.metric-value { font-size: 22px; font-weight: 800; color: var(--navy); }
.progress-bar { background: var(--border); border-radius: 99px; height: 8px; overflow: hidden; flex:1; min-width:100px; }
.progress-fill { height: 100%; background: var(--teal); border-radius: 99px; width: 0%; transition: width 0.4s; }
.controls-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.cr-pill { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:99px; font-size:12px; font-weight:bold; }
.cr-pill.ok   { background:var(--emerald-bg); color:var(--emerald-dark); }
.cr-pill.warn { background:var(--lemon-bg); color:var(--lemon-dark); }
.cr-pill.none { background:var(--bg); color:var(--muted); }
.toggle-bar { display:flex; border:2px solid var(--navy); border-radius:6px; overflow:hidden; }
.toggle-bar button { padding:6px 16px; font-size:12px; font-weight:bold; border:none; cursor:pointer; background:var(--bg); color:var(--navy); font-family:'Inter',sans-serif; }
.toggle-bar button.active { background:var(--navy); color:white; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: var(--bg); padding: 10px 12px; text-align: left; font-size: 11px; color: var(--muted); text-transform: uppercase; border-bottom: 2px solid var(--border); cursor: pointer; white-space: nowrap; }
th:hover { color: var(--navy); }
th.cr-col { background: #E4EDF3; color: var(--navy); }
td { padding: 10px 12px; border-bottom: 1px solid #EEF3F7; vertical-align: top; }
tr:hover td { background: #F7FAFC; }
.bank-name { font-weight: bold; color: var(--navy); }
.bank-url a { color: var(--teal); font-size: 11px; text-decoration: none; }
.rate-high { color: var(--emerald-dark); font-weight: bold; font-size: 14px; }
.rate-mid  { color: var(--lemon-dark); font-weight: bold; font-size: 14px; }
.rate-low  { color: var(--muted); font-size: 14px; }
.dash { color: #ccc; }
.badge { display:inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: bold; }
.b-found   { background: var(--emerald-bg); color: var(--emerald-dark); }
.b-partial { background: var(--lemon-bg); color: var(--lemon-dark); }
.b-np      { background: var(--bg); color: var(--muted); }
.b-queued  { background: #E7EEF3; color: var(--muted-light); }
.note-cell { font-size: 11px; color: var(--muted); line-height: 1.5; }
.delta-pos { color: var(--red); font-size: 11px; font-weight: bold; }
.delta-neg { color: var(--emerald-dark); font-size: 11px; font-weight: bold; }
.delta-zero { color: var(--muted); font-size: 11px; }
.prev-val { color: var(--muted-light); font-size: 11px; display:block; }
.vuln-high { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:bold; background:var(--red-bg); color:var(--red); }
.vuln-watch { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:bold; background:var(--lemon-bg); color:var(--lemon-dark); }
.vuln-normal { color:var(--muted-light); font-size:11px; }
.log-box { background: var(--navy-hover); color: #7FD4E3; font-family: monospace; font-size: 11px; padding: 12px 16px; border-radius: 6px; max-height: 140px; overflow-y: auto; line-height: 1.8; display: none; margin-top: 12px; }
.log-hit  { color: var(--emerald); }
.log-miss { color: var(--teal); }
.success-msg { color: var(--emerald-dark); font-size: 13px; font-weight: bold; margin-top: 10px; }
.filter-select { font-family:'Inter',sans-serif; font-size:12px; padding:5px 10px; border:1.5px solid var(--border); border-radius:6px; background:var(--white); color:var(--navy); cursor:pointer; }
.filter-select:hover { border-color:var(--teal); }
.filter-select:focus { outline:none; border-color:var(--teal); }
.filter-check { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); cursor:pointer; white-space:nowrap; }
.filter-check input { cursor:pointer; accent-color:var(--teal); }
</style>
</head>
<body>

<header>
  <img src="data:image/png;base64,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" alt="Verlocity" style="height:40px;width:auto;object-fit:contain;border-radius:8px;margin:6px 14px 6px 4px;">
  <div>
    <h1>Verlocity <span>-</span> Rate Radar</h1>
    <p>Deposit Rate Crawler &middot; Call Report Intelligence</p>
  </div>
  <div style="margin-left:auto;" id="cr-header-pill"></div>
</header>

<main>

  <div class="card" id="upload-card">
    <div class="upload-area">
      <h2>Step 1 &mdash; Select your bank CSV file</h2>
      <p>Required columns: <strong>bank_name</strong>, <strong>bank_url</strong> &nbsp;&middot;&nbsp;
         Optional: <strong>RSSDID</strong> (Call Report APY) &nbsp;&middot;&nbsp;
         <strong>bank_type</strong> (e.g. Regional / Community) &nbsp;&middot;&nbsp;
         <strong>branch_address</strong> (trade area for Resonate)</p>
      <input type="file" id="csv-input" accept=".csv">
      <button class="btn btn-navy" onclick="doUpload()">Load Banks</button>
      <div id="upload-msg" class="success-msg" style="display:none;"></div>
    </div>
  </div>

  <div id="crawl-section" style="display:none;">

    <div class="card" style="position:relative">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <label style="font-size:12px;font-weight:700;color:var(--navy);white-space:nowrap">+ Add bank manually:</label>
        <input id="add-bank-input" type="text" autocomplete="off" placeholder="Start typing a bank name…"
               style="flex:1;min-width:220px;padding:8px 10px;border:1px solid #d5dbe3;border-radius:6px;font-size:13px;font-family:Inter,system-ui,sans-serif">
      </div>
      <div id="add-bank-dd" style="display:none;position:absolute;left:16px;right:16px;top:56px;background:#fff;
           border:1px solid #d5dbe3;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12);z-index:50;max-height:280px;overflow-y:auto"></div>
    </div>

    <div class="card">
      <div style="display:flex;gap:8px">
        <button class="btn btn-navy" id="save-btn" onclick="manualSave()" disabled
                title="Save the current table (including any manual edits) to Supabase">💾 Save to Supabase</button>
      </div>
      <div id="save-msg" style="display:none;font-size:12px;margin-top:8px"></div>
    </div>

    <div class="card">
      <div class="controls-row">
        <button class="btn btn-amber" id="start-btn" onclick="startCrawl()">&#9654; Start Crawl</button>
        <button class="btn btn-navy"  id="export-btn" onclick="exportCSV()" disabled>&#8595; Export CSV</button>
        <button class="btn btn-navy"  id="prompt-btn" onclick="exportPrompt()" disabled title="Download competitor blocks pre-formatted for the BMAP persona enrichment prompt">&#128203; Export Prompt Blocks</button>
        <button class="btn btn-gray"  onclick="resetAll()">&#10005; Change File</button>
        <label class="filter-check" title="By default, a bank already resolved earlier today is reused as-is for consistency. Check this to ignore that and re-crawl everything.">
          <input type="checkbox" id="force-refresh">
          Force refresh (ignore today's cache)
        </label>
        <div class="toggle-bar" id="view-toggle" style="display:none;">
          <button class="active" id="btn-scraped" onclick="setView('scraped')">&#127758; Scraped Rates</button>
          <button id="btn-cr" onclick="setView('callreport')">&#128196; Call Report</button>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
        <span id="prog-label" style="font-size:12px;color:var(--muted);white-space:nowrap;"></span>
        <span id="status-msg" style="font-size:13px;color:var(--muted);"></span>
      </div>
      <div class="log-box" id="log-box"></div>
    </div>

    <div class="metrics" id="metrics" style="display:none;">
      <div class="metric"><div class="metric-label">Banks scanned</div><div class="metric-value" id="m-total">-</div></div>
      <div class="metric"><div class="metric-label">Rates found</div><div class="metric-value" id="m-found">-</div></div>
      <div class="metric"><div class="metric-label">Best CD APY</div><div class="metric-value" id="m-cd">-</div></div>
      <div class="metric"><div class="metric-label">Best Savings APY</div><div class="metric-value" id="m-sav">-</div></div>
      <div class="metric cr"><div class="metric-label">Avg Cost of Deposits</div><div class="metric-value" id="m-cod">-</div></div>
    </div>

    <div class="card" style="padding:0;overflow:hidden;">
      <div style="padding:12px 16px;background:var(--bg);border-bottom:2px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <strong>Results</strong>
        <span id="result-count" style="font-size:12px;color:var(--muted);"></span>
        <span id="cr-period-label" style="font-size:12px;color:var(--navy);font-weight:bold;display:none;"></span>
        <div style="margin-left:auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <label class="filter-check">
            <input type="checkbox" id="hide-no-rate" onchange="applyFilters()">
            Hide banks with no rate
          </label>
          <select id="status-filter" class="filter-select" onchange="applyFilters()">
            <option value="">All statuses</option>
            <option value="Found">Found</option>
            <option value="Partial">Partial</option>
            <option value="Not public">Not public</option>
          </select>
          <select id="sort-select" class="filter-select" onchange="applySortSelect()">
            <option value="">Sort: upload order</option>
            <option value="cd_apy">Sort: CD APY, high &rarr; low</option>
            <option value="savings_apy">Sort: Savings APY, high &rarr; low</option>
            <option value="checking_apy">Sort: Checking APY, high &rarr; low</option>
            <option value="money_market_apy">Sort: Money Mkt APY, high &rarr; low</option>
            <option value="bank_name">Sort: Bank name, A &rarr; Z</option>
          </select>
        </div>
      </div>
      <div style="overflow-x:auto;">

        <table id="tbl-scraped">
          <thead><tr>
            <th onclick="sortBy('bank_name')" style="width:20%">Bank</th>
            <th onclick="sortBy('checking_apy')">Checking APY</th>
            <th onclick="sortBy('savings_apy')">Savings APY</th>
            <th onclick="sortBy('money_market_apy')">Money Mkt APY</th>
            <th onclick="sortBy('cd_apy')">CD APY</th>
            <th>Min Balance</th>
            <th>Status</th>
            <th>Note</th>
          </tr></thead>
          <tbody id="tbody-scraped">
            <tr><td colspan="8" style="text-align:center;padding:40px;color:var(--muted);">Upload a CSV to get started</td></tr>
          </tbody>
        </table>

        <table id="tbl-cr" style="display:none;">
          <thead><tr>
            <th onclick="sortBy('bank_name')" style="width:18%">Bank</th>
            <th class="cr-col" onclick="sortBy('cr_total_deposits_m')">Total Deposits ($M)</th>
            <th class="cr-col" onclick="sortBy('cr_savings_apy')">Savings Implied APY</th>
            <th class="cr-col" onclick="sortBy('delta_savings_apy')" id="th-delta-sav" style="display:none">Δ Savings</th>
            <th class="cr-col" onclick="sortBy('cr_checking_apy')">Checking Implied APY</th>
            <th class="cr-col" onclick="sortBy('delta_checking_apy')" id="th-delta-chk" style="display:none">Δ Checking</th>
            <th class="cr-col" onclick="sortBy('cr_cd_apy')">CD Implied APY</th>
            <th class="cr-col" onclick="sortBy('delta_cd_apy')" id="th-delta-cd" style="display:none">Δ CD</th>
            <th class="cr-col" onclick="sortBy('cr_cost_of_deposits')">Cost of Deposits</th>
            <th class="cr-col" onclick="sortBy('delta_cost_of_deposits')" id="th-delta-cod" style="display:none">Δ CoD</th>
            <th class="cr-col" onclick="sortBy('vulnerability_flag')">Vulnerability</th>
            <th onclick="sortBy('savings_apy')">Scraped Savings</th>
            <th onclick="sortBy('cd_apy')">Scraped CD</th>
          </tr></thead>
          <tbody id="tbody-cr">
            <tr><td colspan="13" style="text-align:center;padding:40px;color:var(--muted);">No Call Report data — add RSSDID column to your CSV</td></tr>
          </tbody>
        </table>

      </div>
    </div>
  </div>
</main>

<script>
let allResults = [], polling = null, sortField = null, sortDir = 1;
let statusFilter = '', hideNoRate = false;
let currentView = 'scraped', crPeriod = '', crPrevPeriod = '';


function checkCRStatus() {
  fetch('/cr_status?t=' + Date.now()).then(r => r.json()).then(data => {
    var pill = document.getElementById('cr-header-pill');
    if (!data.pandas_ok) {
      pill.innerHTML = '<span class="cr-pill warn">&#9888; Install pandas for Call Report data</span>';
    } else if (data.loaded) {
      crPeriod     = data.period;
      crPrevPeriod = data.prev_period || '';
      var prevLabel = crPrevPeriod ? ' &nbsp;vs&nbsp; ' + crPrevPeriod : '';
      var aiLabel = data.ai_vision_ok ? ' &nbsp;&#128065; AI Vision ON' : '';
      pill.innerHTML = '<span class="cr-pill ok">&#128196; Call Reports loaded &middot; ' + data.period + prevLabel + ' &middot; ' + data.count + ' banks' + aiLabel + '</span>';
    } else if (data.quarters && data.quarters.length > 0) {
      pill.innerHTML = '<span class="cr-pill warn">&#9888; CallReports folder found — missing RI/RCE/RCK files</span>';
    } else {
      var aiStatus = data.ai_vision_ok ? ' &nbsp;&#128065; AI Vision ON' : ' &nbsp;&#9888; Set ANTHROPIC_API_KEY for AI vision';
      pill.innerHTML = '<span class="cr-pill none">No CallReports folder — place files in CallReports/MM-DD-YYYY/' + aiStatus + '</span>';
    }
  }).catch(() => {});
}
checkCRStatus();

// Auto-load a CSV passed in via ?autoload=<base64 CSV>&bank=<name> — lets the
// Hub open Rate Radar pre-populated instead of the user downloading then
// re-uploading a file by hand. Same-origin fetch to /upload, no CORS needed.
function showResumedState(state) {
  document.getElementById('crawl-section').style.display = 'block';
  if (state.results && state.results.length > 0) {
    allResults = state.results;
    renderTable();
    document.getElementById('save-btn').disabled = false;
    var anyDone = !state.running;
    document.getElementById('export-btn').disabled = !anyDone;
    document.getElementById('prompt-btn').disabled = !anyDone;
    if (anyDone) document.getElementById('start-btn').textContent = '\u25B6 Re-crawl';
    if (crPeriod && state.results.some(function(r){ return r.RSSDID && r.RSSDID !== ''; })) {
      document.getElementById('view-toggle').style.display = 'flex';
    }
  } else if (state.banks && state.banks.length > 0) {
    populateQueued(state.banks);
  }
  document.getElementById('upload-msg').textContent =
    (state.results.length || state.banks.length) + ' banks resumed from your current session';
  document.getElementById('upload-msg').style.display = 'block';
}

function doAutoload(b64, bankParam) {
  let csvText;
  try {
    csvText = decodeURIComponent(escape(atob(decodeURIComponent(b64))));
  } catch (e) {
    console.error('autoload decode failed', e);
    return;
  }
  const blob = new Blob([csvText], {type: 'text/csv'});
  const fd = new FormData();
  fd.append('csv', blob, (bankParam || 'bank') + '.csv');
  fetch('/upload', {method:'POST', body:fd})
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert('Autoload error: ' + data.error); return; }
      document.getElementById('upload-msg').textContent = data.count + ' banks loaded from Intelligence Hub';
      document.getElementById('upload-msg').style.display = 'block';
      document.getElementById('crawl-section').style.display = 'block';
      populateQueued(data.banks);
      if (crPeriod && data.banks.some(b => b.RSSDID && b.RSSDID !== '')) {
        document.getElementById('view-toggle').style.display = 'flex';
      }
    })
    .catch(e => alert('Autoload failed: ' + e));
}

function resumeOrAutoload() {
  const params = new URLSearchParams(window.location.search);
  const b64 = params.get('autoload');
  const bankParam = params.get('bank');
  // Always strip the (potentially very long) autoload param from the visible
  // URL up front, regardless of what we decide to do with it.
  const clean = window.location.origin + window.location.pathname;
  window.history.replaceState({}, document.title, clean);

  fetch('/current-state').then(r => r.json()).then(state => {
    const hasExisting = (state.banks && state.banks.length > 0) ||
                        (state.results && state.results.length > 0);
    if (!b64) {
      // Plain page load/reload, no incoming autoload — just resume whatever
      // is already sitting server-side, if anything. Never destructive.
      if (hasExisting) showResumedState(state);
      return;
    }
    if (hasExisting) {
      const count = state.results.length || state.banks.length;
      const proceed = confirm(
        'You already have ' + count + ' bank(s) loaded in this session ' +
        '(from a previous upload, edits, or deletions). Loading from the ' +
        'Hub now will replace that list. Continue and replace it?'
      );
      if (!proceed) { showResumedState(state); return; }
    }
    doAutoload(b64, bankParam);
  }).catch(function(){
    // If the state check itself fails, fall back to the old direct-autoload
    // behavior rather than silently doing nothing.
    if (b64) doAutoload(b64, bankParam);
  });
}
resumeOrAutoload();

function doUpload() {
  const input = document.getElementById('csv-input');
  if (!input.files || input.files.length === 0) { alert('Please select a CSV file first.'); return; }
  const fd = new FormData();
  fd.append('csv', input.files[0]);
  fetch('/upload', {method:'POST', body:fd})
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert('Error: ' + data.error); return; }
      document.getElementById('upload-msg').textContent = data.count + ' banks loaded successfully!';
      document.getElementById('upload-msg').style.display = 'block';
      document.getElementById('crawl-section').style.display = 'block';
      populateQueued(data.banks);
      if (crPeriod && data.banks.some(b => b.RSSDID && b.RSSDID !== '')) {
        document.getElementById('view-toggle').style.display = 'flex';
      }
    })
    .catch(e => alert('Upload failed: ' + e));
}

function populateQueued(banks) {
  allResults = banks.map(function(b){
    return {
      bank_name: b.bank_name, bank_url: b.bank_url || '', RSSDID: b.RSSDID || '',
      checking_apy: null, savings_apy: null, money_market_apy: null,
      cd_apy: null, cd_term: null, min_balance: null,
      status: 'Queued', note: '', source_url: '', _edited: false
    };
  });
  renderTable();
  document.getElementById('save-btn').disabled = allResults.length === 0;
}

// ── Manual bank add (typeahead against ref.dim_institutions x bank_website) ──
var addBankTimer = null, addBankItems = [], addBankIdx = -1;
document.getElementById('add-bank-input').addEventListener('input', function(e){
  clearTimeout(addBankTimer);
  var v = e.target.value.trim();
  if (v.length < 2) { closeAddBankDd(); return; }
  addBankTimer = setTimeout(function(){ addBankSearch(v); }, 250);
});
document.getElementById('add-bank-input').addEventListener('blur', function(){ setTimeout(closeAddBankDd, 150); });

function closeAddBankDd(){
  document.getElementById('add-bank-dd').style.display = 'none';
  addBankIdx = -1;
}

function addBankSearch(q){
  var dd = document.getElementById('add-bank-dd');
  dd.style.display = 'block';
  dd.innerHTML = '<div style="padding:10px 14px;font-size:12px;color:var(--muted)">Searching…</div>';
  fetch('/search-banks?q=' + encodeURIComponent(q)).then(r => r.json()).then(function(data){
    if (data.error) { dd.innerHTML = '<div style="padding:10px 14px;font-size:12px;color:#b23">' + data.error + '</div>'; return; }
    addBankItems = data || [];
    if (!addBankItems.length) { dd.innerHTML = '<div style="padding:10px 14px;font-size:12px;color:var(--muted)">No matches</div>'; return; }
    dd.innerHTML = addBankItems.map(function(b, i){
      return '<div class="search-dd-item" onmousedown="pickAddBank(' + i + ')">' +
             '<strong>' + b.bank_name + '</strong> ' +
             '<span style="color:var(--muted);font-size:11px">' + (b.bank_url||'') + (b.city_hq ? ' · ' + b.city_hq + ', ' + (b.state_hq||'') : '') + '</span></div>';
    }).join('');
  }).catch(function(e){ dd.innerHTML = '<div style="padding:10px 14px;font-size:12px;color:#b23">' + e + '</div>'; });
}

function pickAddBank(i){
  var b = addBankItems[i];
  if (!b) return;
  fetch('/add-bank', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bank_name: b.bank_name, bank_url: b.bank_url, RSSDID: b.rssdid})
  }).then(r => r.json()).then(function(data){
    if (data.error) { alert(data.error); return; }
    document.getElementById('add-bank-input').value = '';
    closeAddBankDd();
    document.getElementById('crawl-section').style.display = 'block';
    populateQueued(data.banks);
    document.getElementById('save-btn').disabled = false;
  }).catch(function(e){ alert('Add failed: ' + e); });
}

function startCrawl() {
  var force = document.getElementById('force-refresh').checked;
  fetch('/start', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: 'auto', force_refresh: force})
  }).then(r => r.json()).then(data => {
    if (data.error) { alert(data.error); return; }
    document.getElementById('start-btn').disabled = true;
    document.getElementById('start-btn').textContent = 'Running...';
    document.getElementById('metrics').style.display = 'grid';
    document.getElementById('log-box').style.display = 'block';
    polling = setInterval(pollStatus, 1500);
  });
}

function pollStatus() {
  fetch('/status').then(r => r.json()).then(data => {
    if (data.phase === 'preflight' && data.preflight_total > 0) {
      var pfPct = Math.round(data.preflight_done / data.preflight_total * 100);
      document.getElementById('prog-fill').style.width = pfPct + '%';
      document.getElementById('prog-label').textContent =
        'Preflight search ' + data.preflight_done + ' / ' + data.preflight_total +
        ' (browser crawl starts after)';
    } else {
      document.getElementById('prog-fill').style.width = data.pct + '%';
      document.getElementById('prog-label').textContent = data.progress + ' / ' + data.total;
    }
    var lb = document.getElementById('log-box');
    lb.innerHTML = data.log.map(l => {
      var cls = l.indexOf('✓') >= 0 ? 'log-hit' : (l.indexOf('○') >= 0 || l.indexOf('~') >= 0) ? 'log-miss' : '';
      return '<div class="' + cls + '">' + l + '</div>';
    }).join('');
    lb.scrollTop = lb.scrollHeight;
    allResults = data.results;
    if (data.cr_period) {
      crPeriod = data.cr_period;
      crPrevPeriod = data.cr_prev_period || '';
      document.getElementById('view-toggle').style.display = 'flex';
      var lbl = document.getElementById('cr-period-label');
      var prevLabel = crPrevPeriod ? ' vs ' + crPrevPeriod : '';
      lbl.textContent = '📄 Call Report: ' + crPeriod + prevLabel;
      lbl.style.display = 'inline';
    }
    renderTable();
    updateMetrics();
    if (data.done) {
      clearInterval(polling);
      document.getElementById('start-btn').disabled = false;
      document.getElementById('start-btn').textContent = '\u25B6 Re-crawl';
      document.getElementById('export-btn').disabled = false;
      document.getElementById('prompt-btn').disabled = false;
      document.getElementById('save-btn').disabled = allResults.length === 0;
      document.getElementById('status-msg').textContent = 'Complete \u2014 ' + new Date().toLocaleTimeString() +
        (data.force_refresh ? ' \u2014 cache bypassed (fresh crawl)' : ' \u2014 same-day cache reused where available');
    }
  });
}

function setView(v) {
  currentView = v;
  document.getElementById('tbl-scraped').style.display  = v === 'scraped'    ? '' : 'none';
  document.getElementById('tbl-cr').style.display       = v === 'callreport' ? '' : 'none';
  document.getElementById('btn-scraped').className = v === 'scraped'    ? 'active' : '';
  document.getElementById('btn-cr').className      = v === 'callreport' ? 'active' : '';
  renderTable();
}

function verifyLink(sourceUrl) {
  if (!sourceUrl) return '';
  var linkStyle = 'font-size:10px;color:var(--teal);text-decoration:none';
  if (/^https?:\/\//i.test(sourceUrl)) {
    return ' <a href="' + sourceUrl + '" target="_blank" style="' + linkStyle + '" title="Verify source">\u2197 verify</a>';
  }
  // Search-based results carry a text description, not a real URL — route to
  // a Google search instead of rendering a link that would 404.
  var text = sourceUrl.replace(/^\[Search\]\s*/, '');
  var q = encodeURIComponent(text);
  return ' <a href="https://www.google.com/search?q=' + q + '" target="_blank" style="' + linkStyle + '" title="Search for this source">\u2197 verify (search)</a>';
}

function rateCell(val, sub) {
  if (val === null || val === undefined || val === '') return '<span class="dash">-</span>';
  var n = parseFloat(val);
  if (isNaN(n)) return '<span class="dash">-</span>';
  var cls = n >= 4.0 ? 'rate-high' : n >= 1.5 ? 'rate-mid' : 'rate-low';
  var termStr = (sub && sub !== 'best found') ? '<br><small style="color:var(--muted)">' + sub + '</small>' : '';
  return '<span class="' + cls + '">' + n.toFixed(2) + '%</span>' + termStr;
}

function editCell(bankName, field, val, isText) {
  var v = (val === null || val === undefined) ? '' : val;
  var type = isText ? 'text' : 'number';
  var step = isText ? '' : ' step="0.01"';
  return '<input type="' + type + '"' + step + ' value="' + String(v).replace(/"/g,'&quot;') +
    '" data-bank="' + bankName.replace(/"/g,'&quot;') + '" data-field="' + field + '" ' +
    'onchange="onRateEdit(this)" ' +
    'style="width:' + (isText ? '70px' : '58px') + ';font-size:12px;padding:3px 5px;border:1px solid #d5dbe3;border-radius:4px;font-family:Inter,system-ui,sans-serif">';
}

function removeBank(el) {
  var bankName = el.getAttribute('data-bank');
  fetch('/remove-bank', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bank_name: bankName})
  }).then(r => r.json()).then(function(){
    allResults = allResults.filter(function(r){ return r.bank_name !== bankName; });
    renderTable();
    document.getElementById('save-btn').disabled = allResults.length === 0;
  }).catch(function(e){ alert('Remove failed: ' + e); });
}

function onRateEdit(input) {
  var bankName = input.getAttribute('data-bank');
  var field    = input.getAttribute('data-field');
  var raw      = input.value.trim();
  var isNumeric = field !== 'min_balance';
  var val = raw === '' ? null : (isNumeric ? parseFloat(raw) : raw);
  var r = allResults.find(function(x){ return x.bank_name === bankName; });
  if (!r) return;
  r[field] = (isNumeric && isNaN(val)) ? null : val;
  r._edited = true;
  input.style.borderColor = 'var(--amber, #d99b23)';
  input.style.background  = '#fff8ec';
  document.getElementById('save-btn').disabled = false;
}

function deltaCell(val, prevVal) {
  /* val = current quarter value, prevVal = prior quarter value */
  if (val === null || val === undefined || prevVal === null || prevVal === undefined) {
    return '<span class="dash">-</span>';
  }
  var d = parseFloat(val) - parseFloat(prevVal);
  if (isNaN(d)) return '<span class="dash">-</span>';
  var sign  = d > 0 ? '+' : '';
  /* For deposit costs, up is bad (red); for rates paid to depositors same convention */
  var cls   = Math.abs(d) < 0.005 ? 'delta-zero' : d > 0 ? 'delta-pos' : 'delta-neg';
  var arrow = d > 0.005 ? ' ▲' : d < -0.005 ? ' ▼' : '';
  return '<span class="' + cls + '">' + sign + d.toFixed(2) + '%' + arrow + '</span>' +
         '<span class="prev-val">prev: ' + parseFloat(prevVal).toFixed(2) + '%</span>';
}

function codCell(val) {
  if (val === null || val === undefined || val === '') return '<span class="dash">-</span>';
  var n = parseFloat(val);
  if (isNaN(n)) return '<span class="dash">-</span>';
  var col = n >= 4.0 ? 'var(--emerald-dark)' : n >= 2.0 ? 'var(--lemon-dark)' : 'var(--muted)';
  return '<strong style="color:' + col + '">' + n.toFixed(2) + '%</strong>';
}

function depCell(val) {
  if (val === null || val === undefined || val === '') return '<span class="dash">-</span>';
  return '$' + parseFloat(val).toFixed(0) + 'M';
}

function badge(s, note) {
  var chatTag = (note && note.indexOf('💬') >= 0) ? ' <span style="font-size:10px">💬</span>' : '';
  if (s === 'Found')   return '<span class="badge b-found">Found</span>' + chatTag;
  if (s === 'Partial') return '<span class="badge b-partial">Partial</span>' + chatTag;
  if (s === 'Queued')  return '<span class="badge b-queued">Queued</span>';
  return '<span class="badge b-np">Not public</span>';
}

function hasAnyRate(r) {
  return [r.checking_apy, r.savings_apy, r.money_market_apy, r.cd_apy]
    .some(function(v) { return v !== null && v !== undefined && v !== ''; });
}

function renderTable() {
  var rows = allResults.filter(function(r) {
    if (statusFilter && r.status !== statusFilter) return false;
    if (hideNoRate && !hasAnyRate(r)) return false;
    return true;
  });
  if (sortField) {
    rows.sort(function(a, b) {
      if (sortField === 'bank_name') return a.bank_name.localeCompare(b.bank_name) * sortDir;
      var av = parseFloat(a[sortField]); if (isNaN(av)) av = -Infinity;
      var bv = parseFloat(b[sortField]); if (isNaN(bv)) bv = -Infinity;
      return (bv - av) * sortDir;
    });
  }
  if (currentView === 'scraped') {
    document.getElementById('tbody-scraped').innerHTML = rows.map(r =>
      '<tr><td><div style="display:flex;align-items:flex-start;gap:6px">' +
      '<span onclick="removeBank(this)" data-bank="' + r.bank_name.replace(/"/g,'&quot;') + '" title="Remove from list" ' +
      'style="cursor:pointer;color:#b23;font-size:14px;line-height:1.4;flex-shrink:0">✕</span>' +
      '<div><div class="bank-name">' + r.bank_name + '</div>' +
      '<div class="bank-url"><a href="' + (r.bank_url||'') + '" target="_blank">' + (r.bank_url||'') + '</a></div></div></div></td>' +
      '<td>' + editCell(r.bank_name, 'checking_apy', r.checking_apy) + '</td>' +
      '<td>' + editCell(r.bank_name, 'savings_apy', r.savings_apy) + '</td>' +
      '<td>' + editCell(r.bank_name, 'money_market_apy', r.money_market_apy) + '</td>' +
      '<td>' + editCell(r.bank_name, 'cd_apy', r.cd_apy) + (r.cd_term && r.cd_term !== 'best found' ? '<br><small style="color:var(--muted)">' + r.cd_term + '</small>' : '') + '</td>' +
      '<td>' + editCell(r.bank_name, 'min_balance', r.min_balance, true) + '</td>' +
      '<td>' + badge(r.status, r.note) + (r._edited ? ' <span style="font-size:10px;color:var(--amber,#d99b23)">edited</span>' : '') + '</td>' +
      '<td><div class="note-cell">' + (r.note||'') + verifyLink(r.source_url) + '</div></td></tr>'
    ).join('');
  } else {
    var hasPrev = crPrevPeriod && rows.some(r => r.prev_cr_savings_apy !== null && r.prev_cr_savings_apy !== undefined);
    // Show/hide delta columns based on whether prev data is available
    ['th-delta-sav','th-delta-chk','th-delta-cd','th-delta-cod'].forEach(function(id) {
      document.getElementById(id).style.display = hasPrev ? '' : 'none';
    });
    document.getElementById('tbody-cr').innerHTML = rows.map(r =>
      '<tr><td><div class="bank-name">' + r.bank_name + '</div>' +
      '<div class="bank-url"><a href="' + (r.bank_url||'') + '" target="_blank">' + (r.bank_url||'') + '</a></div></td>' +
      '<td>' + depCell(r.cr_total_deposits_m) + '</td>' +
      '<td>' + codCell(r.cr_savings_apy) + '</td>' +
      (hasPrev ? '<td>' + deltaCell(r.cr_savings_apy, r.prev_cr_savings_apy) + '</td>' : '') +
      '<td>' + codCell(r.cr_checking_apy) + '</td>' +
      (hasPrev ? '<td>' + deltaCell(r.cr_checking_apy, r.prev_cr_checking_apy) + '</td>' : '') +
      '<td>' + codCell(r.cr_cd_apy) + '</td>' +
      (hasPrev ? '<td>' + deltaCell(r.cr_cd_apy, r.prev_cr_cd_apy) + '</td>' : '') +
      '<td>' + codCell(r.cr_cost_of_deposits) + '</td>' +
      (hasPrev ? '<td>' + deltaCell(r.cr_cost_of_deposits, r.prev_cr_cost_of_deposits) + '</td>' : '') +
      '<td>' + vulnCell(r.vulnerability_flag) + '</td>' +
      '<td>' + rateCell(r.savings_apy, null) + '</td>' +
      '<td>' + rateCell(r.cd_apy, r.cd_term) + '</td></tr>'
    ).join('');
  }
  document.getElementById('result-count').textContent = rows.length === allResults.length
    ? rows.length + ' banks'
    : rows.length + ' of ' + allResults.length + ' banks';
}

function updateMetrics() {
  var total = allResults.length;
  var found = allResults.filter(r => r.status !== 'Not public').length;
  var cds   = allResults.filter(r => r.cd_apy).map(r => r.cd_apy);
  var savs  = allResults.filter(r => r.savings_apy).map(r => r.savings_apy);
  var cods  = allResults.filter(r => r.cr_cost_of_deposits).map(r => r.cr_cost_of_deposits);
  document.getElementById('m-total').textContent = total;
  document.getElementById('m-found').textContent = found + '/' + total;
  document.getElementById('m-cd').textContent    = cds.length  ? Math.max(...cds).toFixed(2)+'%'  : '-';
  document.getElementById('m-sav').textContent   = savs.length ? Math.max(...savs).toFixed(2)+'%' : '-';
  document.getElementById('m-cod').textContent   = cods.length
    ? (cods.reduce((a,b) => a+b, 0) / cods.length).toFixed(2) + '%' : '-';
}

function sortBy(field) {
  if (sortField === field) { sortDir *= -1; } else { sortField = field; sortDir = 1; }
  renderTable();
}

function applyFilters() {
  hideNoRate  = document.getElementById('hide-no-rate').checked;
  statusFilter = document.getElementById('status-filter').value;
  renderTable();
}

function applySortSelect() {
  var val = document.getElementById('sort-select').value;
  sortField = val || null;
  sortDir = 1; // fresh pick from the dropdown always starts at its natural order
  renderTable();
}

function vulnCell(val) {
  if (!val) return '<span class="vuln-normal">—</span>';
  if (val.startsWith('Vulnerable')) return '<span class="vuln-high">' + val + '</span>';
  if (val.startsWith('Watch'))      return '<span class="vuln-watch">' + val + '</span>';
  return '<span class="vuln-normal">' + val + '</span>';
}

function exportCSV()    { window.location = '/export'; }

function manualSave() {
  if (!allResults.length) { alert('Nothing to save yet — load or crawl some banks first.'); return; }
  var btn = document.getElementById('save-btn');
  var msg = document.getElementById('save-msg');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  fetch('/manual-save', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({results: allResults})
  }).then(r => r.json()).then(function(data){
    btn.textContent = '💾 Save to Supabase';
    btn.disabled = false;
    msg.style.display = 'block';
    if (data.error) {
      msg.style.color = '#b23';
      msg.textContent = 'Save failed: ' + data.error;
    } else {
      msg.style.color = 'var(--teal)';
      msg.textContent = '✓ Saved ' + data.saved + ' banks to Supabase (run ' + data.run_id + ', ' + new Date().toLocaleTimeString() + ')';
      allResults.forEach(function(r){ r._edited = false; });
      renderTable();
    }
  }).catch(function(e){
    btn.textContent = '💾 Save to Supabase';
    btn.disabled = false;
    msg.style.display = 'block';
    msg.style.color = '#b23';
    msg.textContent = 'Save failed: ' + e;
  });
}
function exportPrompt() { window.location = '/export_prompt'; }

function resetAll() {
  if (polling) clearInterval(polling);
  allResults = [];
  sortField = null; sortDir = 1; statusFilter = ''; hideNoRate = false;
  document.getElementById('hide-no-rate').checked   = false;
  document.getElementById('status-filter').value    = '';
  document.getElementById('sort-select').value      = '';
  document.getElementById('force-refresh').checked  = false;
  document.getElementById('crawl-section').style.display  = 'none';
  document.getElementById('upload-msg').style.display     = 'none';
  document.getElementById('csv-input').value              = '';
  document.getElementById('start-btn').disabled           = false;
  document.getElementById('start-btn').textContent        = '\u25B6 Start Crawl';
  document.getElementById('export-btn').disabled          = true;
  document.getElementById('prompt-btn').disabled           = true;
  document.getElementById('metrics').style.display        = 'none';
  document.getElementById('log-box').style.display        = 'none';
  document.getElementById('view-toggle').style.display    = 'none';
  document.getElementById('status-msg').textContent       = '';
  document.getElementById('prog-fill').style.width        = '0';
  document.getElementById('prog-label').textContent       = '';
  setView('scraped');
  fetch('/reset', {method:'POST'}).catch(function(){});
}
</script>
</body>
</html>
"""

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # PORT is injected by Railway; falls back to 7331 for local runs.
    PORT = int(os.environ.get("PORT", 7331))
    IS_LOCAL = os.environ.get("RAILWAY_ENVIRONMENT") is None

    print(f"  Verlocity Intelligence Hub (Rate Radar) - starting on port {PORT}")
    if not PLAYWRIGHT_OK:
        print("  Setup: pip install flask playwright pandas && playwright install chromium")
    if not PANDAS_OK:
        print("  Note:  pip install pandas numpy  (needed for Call Report data)")

    print("  Scanning for Call Reports...")
    cr_data, cr_period, prev_cr_data, cr_prev_period, cr_quarters = load_call_reports()
    crawl_state["cr_data"]        = cr_data
    crawl_state["cr_period"]      = cr_period
    crawl_state["prev_cr_data"]   = prev_cr_data
    crawl_state["cr_prev_period"] = cr_prev_period
    crawl_state["cr_quarters"]    = cr_quarters

    if cr_period:
        print(f"  \u2713 Call Reports loaded \u2014 {len(cr_data)} banks \u2014 period: {cr_period}")
        if cr_prev_period:
            print(f"  \u2713 Previous quarter loaded \u2014 {len(prev_cr_data)} banks \u2014 period: {cr_prev_period}")
        if cr_quarters:
            print(f"  Available quarters: {', '.join(cr_quarters)}")
    else:
        print("  \u25CB No Call Reports found \u2014 falling back to Supabase quarters if configured")

    if IS_LOCAL:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
