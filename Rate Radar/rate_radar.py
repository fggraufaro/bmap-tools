"""
BMAP Rate Radar
===============
Run: python rate_radar.py
Opens browser at http://localhost:7331

Setup (one time):
  pip install flask playwright pandas
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

# Load .env automatically
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
import base64

try:
    from flask import Flask, request, jsonify, Response, render_template_string
except ImportError:
    print("Run: pip install flask playwright pandas anthropic && playwright install chromium")
    raise

# Anthropic client for AI vision fallback
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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

crawl_state = {
    "running":        False,
    "banks":          [],
    "results":        [],
    "log":            [],
    "done":           False,
    "ai_calls":       0,      # track AI vision calls this run
    "chat_calls":     0,      # track chat interactions this run
    "crawl_mode":     "standard",  # standard | ai_agent | chat
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
]

BANK_EXTRA_URLS = {
    "comerica.com":        ["https://www.comerica.com/offer/deposits/cd-hymmia.html"],
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
SAVINGS_PAT  = re.compile(r'(?:savings|high.yield)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
CHECKING_PAT = re.compile(r'(?:checking)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
MM_PAT       = re.compile(r'(?:money\s+market)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
MIN_BAL_PAT  = re.compile(r'\$\s*([1-9][\d,]*)\s*(?:minimum|min).*?(?:balance|deposit)', re.I)
TABLE_PAT    = re.compile(r'(\d+)\s*[-]?\s*(month|mo|year|day)s?\b[^\n]{0,80}?(\d+\.\d+)\s*%', re.I)
TERM_APY_PAT = re.compile(r'(\d+\.\d+)\s*%\s*APY[^\n]{0,60}?(\d+)\s*[-]?\s*(month|mo|day|year)', re.I)
CD_PAT       = re.compile(r'(?:CD|Certificate)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)


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


def load_call_reports():
    """
    Scan CallReports/ for MM-DD-YYYY quarter folders.
    Loads the two most recent quarters that have all three schedules.
    Returns (cr_data, period_label, prev_cr_data, prev_period_label, all_quarter_labels).
    """
    if not PANDAS_OK or not CALL_REPORTS_DIR.exists():
        return {}, None, {}, None, []

    quarters = sorted(
        [(dt, f) for f in CALL_REPORTS_DIR.iterdir()
         if f.is_dir() and (dt := _parse_quarter_folder(f.name))],
        key=lambda x: x[0], reverse=True
    )
    quarter_labels = [dt.strftime("%b %Y") for dt, _ in quarters]

    loaded = []  # list of (dt, cr_data)
    for dt, folder in quarters:
        if len(loaded) >= 2:
            break
        data = _load_quarter(dt, folder)
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
         "money_market": None, "min_balance": None}
    m = SAVINGS_PAT.search(text)
    if m: r["savings"] = float(m.group(1))
    m = CHECKING_PAT.search(text)
    if m: r["checking"] = float(m.group(1))
    m = MM_PAT.search(text)
    if m: r["money_market"] = float(m.group(1))
    cd_candidates = []
    for m in TABLE_PAT.finditer(text):
        val = float(m.group(3))
        if 0.05 <= val <= 15:
            unit = m.group(2).lower()
            term = f"{int(m.group(1))*12}-month" if "year" in unit else f"{m.group(1)}-month"
            cd_candidates.append((val, term))
    for m in TERM_APY_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            cd_candidates.append((val, f"{m.group(2)}-{m.group(3)}"))
    for m in CD_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            cd_candidates.append((val, None))
    if cd_candidates:
        best = max(cd_candidates, key=lambda x: x[0])
        r["cd"], r["cd_term"] = best
    if not any([r["checking"], r["savings"], r["cd"], r["money_market"]]):
        apys = [float(v) for v in APY_PAT.findall(text) if 0.05 <= float(v) <= 15]
        if apys:
            r["cd"] = max(apys)
            r["cd_term"] = "best found"
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
Extract ALL deposit rates visible in this image.
Return ONLY a JSON object with these exact keys (use null if not found):
{{
  "checking": <float APY% or null>,
  "savings": <float APY% or null>,
  "cd": <float APY% — use the BEST/HIGHEST rate shown or null>,
  "cd_term": <string like "12-month" or null>,
  "money_market": <float APY% or null>,
  "min_balance": <string like "$1,000" or null>
}}
Rules:
- Extract the HIGHEST APY shown for each product type
- CD term should match the CD with the highest rate
- If a rate says "up to X%" use X
- Return ONLY the JSON, no explanation"""

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
        # Strip markdown code fences if present
        txt = txt.replace("```json", "").replace("```", "").strip()
        result = json.loads(txt)

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


# ── Web crawler ───────────────────────────────────────────────────────────────

async def crawl_bank(page, bank, timeout=12000):
    base = bank["bank_url"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    best = {"checking": None, "savings": None, "cd": None, "cd_term": None,
            "money_market": None, "min_balance": None}
    found_on    = None
    source_urls = {}
    visited     = set()
    extra    = next((urls for k, urls in BANK_EXTRA_URLS.items() if k in base.replace('www.','')), [])

    async def visit(url):
        nonlocal found_on
        if url in visited: return
        visited.add(url)
        try:
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            try: await page.wait_for_selector("text=APY", timeout=2000)
            except: pass
            text = await page.inner_text("body")
            r = extract_rates(text)
            for k in best:
                if r.get(k) is not None:
                    if best[k] is None or (isinstance(r[k], float) and r[k] > best[k]):
                        best[k] = r[k]
                        if k in ["checking", "savings", "cd", "money_market"]:
                            source_urls[k] = url
            if r.get("cd_term") and not best["cd_term"]:
                best["cd_term"] = r["cd_term"]
            if any(r.get(k) for k in ["checking", "savings", "cd", "money_market"]):
                found_on = url
            domain = re.match(r"https?://[^/]+", base)
            domain = domain.group(0) if domain else base
            links = await page.query_selector_all("a[href]")
            rate_links = []
            for link in links[:40]:
                try:
                    href = await link.get_attribute("href") or ""
                    txt  = (await link.inner_text()).strip()
                    full = href if href.startswith("http") else domain + href if href.startswith("/") else None
                    if not full or not full.startswith(domain) or full in visited: continue
                    if re.search(r"rate|apy|cd|savings|checking|deposit|certificate|offer", href+txt, re.I):
                        rate_links.append(full)
                except: continue
            for u in rate_links[:5]:
                await visit(u)
        except: pass

    for url in extra:
        await visit(url)
    for path in RATE_PATHS:
        await visit(base + path)

    # ── AI Vision fallback for empty/partial results ────────────────────────
    count = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    if count < 3 and ANTHROPIC_OK and bank.get("bank_url",""):
        try:
            # Build list of pages to try vision on — prioritise extra URLs, then base
            vision_targets = list(extra) if extra else []
            if not vision_targets:
                vision_targets = [base]
            # Also try common rate paths directly
            domain = re.match(r"https?://[^/]+", base)
            domain_str = domain.group(0) if domain else base
            for path in ["/rates", "/personal/rates", "/personal-banking/rates",
                         "/personal-solutions/checking", "/personal-solutions/savings"]:
                vision_targets.append(domain_str + path)

            crawl_state["log"].append(f"    [AI] regex got {count}/3 — trying vision on {len(vision_targets)} pages...")

            for v_url in vision_targets[:5]:  # max 5 vision attempts per bank
                try:
                    await page.goto(v_url, timeout=12000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
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
                        # Stop if we have all 3
                        new_count = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
                        if new_count >= 3:
                            break
                except Exception as ve:
                    crawl_state["log"].append(f"    [AI] skip {v_url}: {ve}")
                    continue
        except Exception as e:
            crawl_state["log"].append(f"    [AI] fallback error: {e}")

    count  = sum(1 for k in ["checking", "savings", "cd"] if best[k] is not None)
    status = "Found" if count == 3 else "Partial" if count > 0 else "Not public"
    parts  = []
    if found_on:
        rate_parts = []
        if best["cd"]:           rate_parts.append(f"CD {best['cd']:.2f}%{' ('+best['cd_term']+')' if best['cd_term'] else ''}")
        if best["savings"]:      rate_parts.append(f"Savings {best['savings']:.2f}%")
        if best["checking"]:     rate_parts.append(f"Checking {best['checking']:.2f}%")
        if best["money_market"]: rate_parts.append(f"Money Mkt {best['money_market']:.2f}%")
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
            txt = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
            result = json.loads(txt)
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
            txt = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
            result = json.loads(txt)

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
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        for i, bank in enumerate(banks):
            if not crawl_state["running"]: break
            mode = crawl_state.get("crawl_mode", "standard")
            crawl_state["log"].append(f"[{i+1}/{len(banks)}] {bank['bank_name']}... [{mode}]")
            page = await ctx.new_page()
            try:
                if mode == "ai_agent" and ANTHROPIC_OK:
                    result = await ai_agent_crawl(page, bank)
                elif mode == "chat" and ANTHROPIC_OK:
                    # Chat mode: Standard first, then chat widget for missing rates
                    result = await crawl_bank(page, bank)
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
                                # Recalculate status
                                new_count = sum(1 for k in ["checking_apy","savings_apy","cd_apy"] if result.get(k))
                                result["status"] = "Found" if new_count==3 else "Partial" if new_count>0 else "Not public"
                                result["source_url"] = result.get("source_url","") or "Live chat"
                        finally:
                            await page2.close()
                else:
                    result = await crawl_bank(page, bank)
                icon = "✓" if result["status"] == "Found" else "~" if result["status"] == "Partial" else "○"
                chk  = f"{result['checking_apy']:.2f}%" if result["checking_apy"] else "—"
                sav  = f"{result['savings_apy']:.2f}%"  if result["savings_apy"]  else "—"
                cd   = f"{result['cd_apy']:.2f}%"       if result["cd_apy"]       else "—"
                cod  = f"  CoD:{result['cr_cost_of_deposits']:.2f}%" if result.get("cr_cost_of_deposits") else ""
                crawl_state["log"].append(f"  {icon} Chk:{chk} Sav:{sav} CD:{cd}{cod}")
                crawl_state["results"].append(result)
            except Exception as e:
                crawl_state["log"].append(f"  x Error: {e}")
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
                await page.close()
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
    auto_save(crawl_state["results"])


def start_crawl_thread(banks):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_crawler(banks))
        loop.close()
    threading.Thread(target=run, daemon=True).start()


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
    except Exception as e:
        crawl_state["log"].append(f"Save failed: {e}")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

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
    crawl_state.update({"running": True, "results": [], "log": [], "done": False,
                        "ai_calls": 0, "crawl_mode": mode})
    crawl_state["log"].append(f"Mode: {mode.upper()}" + (" · AI Vision ON" if ANTHROPIC_OK else " · No API key"))
    start_crawl_thread(crawl_state["banks"])
    return jsonify({"ok": True, "mode": mode})

@app.route("/status")
def status():
    total = len(crawl_state["banks"])
    done  = len(crawl_state["results"])
    return jsonify({
        "running":        crawl_state["running"],
        "done":           crawl_state["done"],
        "total":          total,
        "progress":       done,
        "pct":            round(done / total * 100 if total else 0, 1),
        "log":            crawl_state["log"][-50:],
        "results":        crawl_state["results"],
        "cr_period":      crawl_state["cr_period"],
        "cr_prev_period": crawl_state["cr_prev_period"],
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
<title>BMAP Rate Radar</title>
<style>
body { font-family: Arial, sans-serif; margin: 0; background: #f0f4f8; color: #1B3A5C; }
header { background: #1B3A5C; color: white; padding: 14px 24px; border-bottom: 3px solid #F5A623; display:flex; align-items:center; gap:16px; }
header h1 { margin:0; font-size:20px; }
header h1 span { color:#F5A623; }
header p { margin:0; font-size:12px; opacity:0.6; }
main { max-width: 1400px; margin: 0 auto; padding: 24px; }
.card { background: white; border: 1px solid #dce4ee; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
.upload-area { text-align: center; padding: 20px 0; }
.upload-area h2 { margin: 0 0 8px; }
.upload-area p  { margin: 0 0 16px; color: #6b82a0; font-size: 13px; }
input[type=file] { font-size: 14px; padding: 8px; border: 2px solid #1B3A5C; border-radius: 6px; background: white; cursor: pointer; margin-right: 10px; }
.btn { padding: 9px 22px; border: none; border-radius: 6px; font-size: 14px; font-weight: bold; cursor: pointer; }
.btn-navy  { background: #1B3A5C; color: white; }
.btn-navy:hover  { background: #122840; }
.btn-navy:disabled  { opacity: 0.4; cursor: not-allowed; }
.btn-amber { background: #F5A623; color: #1B3A5C; }
.btn-amber:hover { background: #c47d0e; color: white; }
.btn-amber:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-gray  { background: #eef2f7; color: #1B3A5C; }
.metrics { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
.metric { background: white; border: 1px solid #dce4ee; border-radius: 8px; padding: 14px 16px; border-top: 3px solid #F5A623; }
.metric.cr { border-top-color: #1B3A5C; }
.metric-label { font-size: 11px; color: #6b82a0; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
.metric-value { font-size: 22px; font-weight: bold; color: #1B3A5C; }
.progress-bar { background: #dce4ee; border-radius: 99px; height: 8px; overflow: hidden; flex:1; min-width:100px; }
.progress-fill { height: 100%; background: #F5A623; border-radius: 99px; width: 0%; transition: width 0.4s; }
.controls-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.cr-pill { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:99px; font-size:12px; font-weight:bold; }
.cr-pill.ok   { background:#eaf5ee; color:#1a7a3a; }
.cr-pill.warn { background:#fff8ec; color:#c47d0e; }
.cr-pill.none { background:#f0f4f8; color:#6b82a0; }
.toggle-bar { display:flex; border:2px solid #1B3A5C; border-radius:6px; overflow:hidden; }
.toggle-bar button { padding:6px 16px; font-size:12px; font-weight:bold; border:none; cursor:pointer; background:#f0f4f8; color:#1B3A5C; }
.toggle-bar button.active { background:#1B3A5C; color:white; }
.mode-bar { display:flex; border:2px solid #F5A623; border-radius:6px; overflow:hidden; }
.mode-btn { padding:6px 18px; font-size:12px; font-weight:bold; border:none; cursor:pointer; background:#fff8ec; color:#854F0B; }
.mode-btn.active { background:#F5A623; color:#1B3A5C; }
.mode-btn:hover { opacity:0.85; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #f0f4f8; padding: 10px 12px; text-align: left; font-size: 11px; color: #6b82a0; text-transform: uppercase; border-bottom: 2px solid #dce4ee; cursor: pointer; white-space: nowrap; }
th:hover { color: #1B3A5C; }
th.cr-col { background: #e8eef5; color: #1B3A5C; }
td { padding: 10px 12px; border-bottom: 1px solid #edf2f7; vertical-align: top; }
tr:hover td { background: #f7fafd; }
.bank-name { font-weight: bold; color: #1B3A5C; }
.bank-url a { color: #c47d0e; font-size: 11px; text-decoration: none; }
.rate-high { color: #1a7a3a; font-weight: bold; font-size: 14px; }
.rate-mid  { color: #c47d0e; font-weight: bold; font-size: 14px; }
.rate-low  { color: #6b82a0; font-size: 14px; }
.dash { color: #ccc; }
.badge { display:inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: bold; }
.b-found   { background: #eaf5ee; color: #1a7a3a; }
.b-partial { background: #fff8ec; color: #c47d0e; }
.b-np      { background: #f0f4f8; color: #6b82a0; }
.b-queued  { background: #eef2f7; color: #8aa0b8; }
.note-cell { font-size: 11px; color: #6b82a0; line-height: 1.5; }
.delta-pos { color: #d9534f; font-size: 11px; font-weight: bold; }
.delta-neg { color: #1a7a3a; font-size: 11px; font-weight: bold; }
.delta-zero { color: #6b82a0; font-size: 11px; }
.prev-val { color: #9aaccb; font-size: 11px; display:block; }
.vuln-high { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:bold; background:#fde8e8; color:#a32d2d; }
.vuln-watch { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:bold; background:#fff8ec; color:#854f0b; }
.vuln-normal { color:#9aaccb; font-size:11px; }
.log-box { background: #122840; color: #7eb8e0; font-family: monospace; font-size: 11px; padding: 12px 16px; border-radius: 6px; max-height: 140px; overflow-y: auto; line-height: 1.8; display: none; margin-top: 12px; }
.log-hit  { color: #6dd68a; }
.log-miss { color: #F5A623; }
.success-msg { color: #1a7a3a; font-size: 13px; font-weight: bold; margin-top: 10px; }
</style>
</head>
<body>

<header>
  <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
    <path d="M18 2C11 2 5 8 5 15c0 9 13 21 13 21S31 24 31 15C31 8 25 2 18 2z" fill="#1B3A5C" stroke="#F5A623" stroke-width="1.5"/>
    <circle cx="18" cy="15" r="8" fill="white"/>
    <rect x="11" y="17" width="2.5" height="3.5" fill="#1B3A5C" rx="0.3"/>
    <rect x="15" y="13" width="2.5" height="7.5" fill="#1B3A5C" rx="0.3"/>
    <rect x="19" y="10" width="2.5" height="10.5" fill="#1B3A5C" rx="0.3"/>
    <path d="M11 17 L19 10 L23 7" stroke="#F5A623" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M21 7 L23 7 L23 9" stroke="#F5A623" stroke-width="1.8" stroke-linecap="round"/>
  </svg>
  <div>
    <h1>BMAP <span>Rate Radar</span></h1>
    <p>Bank Market Assessment Platform &middot; Deposit Rate Crawler + Call Report Intelligence</p>
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

    <div class="card">
      <div class="controls-row">
        <div class="mode-bar">
          <button class="mode-btn active" id="mode-standard" onclick="setMode('standard')" title="Fast regex scraping ~30s/bank">⚡ Standard</button>
          <button class="mode-btn" id="mode-ai_agent" onclick="setMode('ai_agent')" title="Claude navigates site like a human ~2min/bank">🤖 AI Agent</button>
          <button class="mode-btn" id="mode-chat" onclick="setMode('chat')" title="Standard + live chat widget interaction for missing rates ~3min/bank">💬 Chat</button>
        </div>
        <span id="mode-hint" style="font-size:11px;color:#6b82a0;font-style:italic;">⚡ Standard — fast regex scraping, ~30s/bank, free</span>
        <button class="btn btn-amber" id="start-btn" onclick="startCrawl()">&#9654; Start Crawl</button>
        <button class="btn btn-navy"  id="export-btn" onclick="exportCSV()" disabled>&#8595; Export CSV</button>
        <button class="btn btn-navy"  id="prompt-btn" onclick="exportPrompt()" disabled title="Download competitor blocks pre-formatted for the BMAP persona enrichment prompt">&#128203; Export Prompt Blocks</button>
        <button class="btn btn-gray"  onclick="resetAll()">&#10005; Change File</button>
        <div class="toggle-bar" id="view-toggle" style="display:none;">
          <button class="active" id="btn-scraped" onclick="setView('scraped')">&#127758; Scraped Rates</button>
          <button id="btn-cr" onclick="setView('callreport')">&#128196; Call Report</button>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
        <span id="prog-label" style="font-size:12px;color:#6b82a0;white-space:nowrap;"></span>
        <span id="status-msg" style="font-size:13px;color:#6b82a0;"></span>
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
      <div style="padding:12px 16px;background:#f0f4f8;border-bottom:2px solid #dce4ee;display:flex;align-items:center;gap:10px;">
        <strong>Results</strong>
        <span id="result-count" style="font-size:12px;color:#6b82a0;"></span>
        <span id="cr-period-label" style="font-size:12px;color:#1B3A5C;font-weight:bold;display:none;"></span>
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
            <tr><td colspan="8" style="text-align:center;padding:40px;color:#6b82a0;">Upload a CSV to get started</td></tr>
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
            <tr><td colspan="13" style="text-align:center;padding:40px;color:#6b82a0;">No Call Report data — add RSSDID column to your CSV</td></tr>
          </tbody>
        </table>

      </div>
    </div>
  </div>
</main>

<script>
let allResults = [], polling = null, sortField = null, sortDir = -1;
let currentView = 'scraped', crPeriod = '', crPrevPeriod = '';
let crawlMode = 'standard';

function setMode(m) {
  crawlMode = m;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('mode-' + m).classList.add('active');
  var hints = {
    standard: '⚡ Standard — fast regex scraping, ~30s/bank, free',
    ai_agent: '🤖 AI Agent — Claude navigates the site, ~2min/bank, ~$0.02/bank',
    chat:     '💬 Chat — Standard + live chat widget for missing rates, ~3min/bank, free'
  };
  document.getElementById('mode-hint').textContent = hints[m] || '';
}

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
  document.getElementById('tbody-scraped').innerHTML = banks.map(b =>
    '<tr><td><div class="bank-name">' + b.bank_name + '</div>' +
    '<div class="bank-url"><a href="' + b.bank_url + '" target="_blank">' + b.bank_url + '</a></div></td>' +
    '<td><span class="dash">-</span></td><td><span class="dash">-</span></td>' +
    '<td><span class="dash">-</span></td><td><span class="dash">-</span></td>' +
    '<td><span class="dash">-</span></td><td><span class="badge b-queued">Queued</span></td><td></td></tr>'
  ).join('');
  document.getElementById('result-count').textContent = banks.length + ' banks';
}

function startCrawl() {
  fetch('/start', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: crawlMode})
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
    document.getElementById('prog-fill').style.width = data.pct + '%';
    document.getElementById('prog-label').textContent = data.progress + ' / ' + data.total;
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
      document.getElementById('status-msg').textContent = 'Complete \u2014 ' + new Date().toLocaleTimeString();
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

function rateCell(val, sub) {
  if (val === null || val === undefined || val === '') return '<span class="dash">-</span>';
  var n = parseFloat(val);
  if (isNaN(n)) return '<span class="dash">-</span>';
  var cls = n >= 4.0 ? 'rate-high' : n >= 1.5 ? 'rate-mid' : 'rate-low';
  var termStr = (sub && sub !== 'best found') ? '<br><small style="color:#6b82a0">' + sub + '</small>' : '';
  return '<span class="' + cls + '">' + n.toFixed(2) + '%</span>' + termStr;
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
  var col = n >= 4.0 ? '#1a7a3a' : n >= 2.0 ? '#c47d0e' : '#6b82a0';
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
  return '<span class="badge b-np">Not public</span>';
}

function renderTable() {
  var rows = allResults.slice();
  if (sortField) {
    rows.sort(function(a, b) {
      if (sortField === 'bank_name') return a.bank_name.localeCompare(b.bank_name) * sortDir;
      return ((parseFloat(b[sortField]) || -1) - (parseFloat(a[sortField]) || -1)) * sortDir;
    });
  }
  if (currentView === 'scraped') {
    document.getElementById('tbody-scraped').innerHTML = rows.map(r =>
      '<tr><td><div class="bank-name">' + r.bank_name + '</div>' +
      '<div class="bank-url"><a href="' + (r.bank_url||'') + '" target="_blank">' + (r.bank_url||'') + '</a></div></td>' +
      '<td>' + rateCell(r.checking_apy, null) + '</td>' +
      '<td>' + rateCell(r.savings_apy, null) + '</td>' +
      '<td>' + rateCell(r.money_market_apy, null) + '</td>' +
      '<td>' + rateCell(r.cd_apy, r.cd_term) + '</td>' +
      '<td><span style="font-size:12px">' + (r.min_balance || '<span class="dash">-</span>') + '</span></td>' +
      '<td>' + badge(r.status, r.note) + '</td>' +
      '<td><div class="note-cell">' + (r.note||'') + (r.source_url ? ' <a href="' + r.source_url + '" target="_blank" style="font-size:10px;color:#185FA5;text-decoration:none" title="Verify source">↗ verify</a>' : '') + '</div></td></tr>'
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
  document.getElementById('result-count').textContent = rows.length + ' banks';
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
  if (sortField === field) sortDir *= -1; else { sortField = field; sortDir = -1; }
  renderTable();
}

function vulnCell(val) {
  if (!val) return '<span class="vuln-normal">—</span>';
  if (val.startsWith('Vulnerable')) return '<span class="vuln-high">' + val + '</span>';
  if (val.startsWith('Watch'))      return '<span class="vuln-watch">' + val + '</span>';
  return '<span class="vuln-normal">' + val + '</span>';
}

function exportCSV()    { window.location = '/export'; }
function exportPrompt() { window.location = '/export_prompt'; }

function resetAll() {
  if (polling) clearInterval(polling);
  allResults = [];
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
}
</script>
</body>
</html>
"""

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("  BMAP Rate Radar - Opening http://localhost:7331")
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
        print("  \u25CB No Call Reports found \u2014 place files in CallReports/MM-DD-YYYY/ next to this script")

    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:7331")).start()
    app.run(port=7331, debug=False)
