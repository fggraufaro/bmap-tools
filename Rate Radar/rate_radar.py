"""
BMAP Rate Radar
===============
Run: python rate_radar.py
Opens browser at http://localhost:7331

Setup (one time):
  pip install flask playwright requests python-dotenv
  playwright install chromium

Supabase setup:
  Create a .env file in this folder with:
  SUPABASE_SERVICE_KEY=your_service_role_key_here
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

# Load .env file automatically if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — fall back to environment variables

try:
    from flask import Flask, request, jsonify, Response, render_template_string
except ImportError:
    print("Run: pip install flask playwright && playwright install chromium")
    raise

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

app = Flask(__name__)

crawl_state = {
    "running": False,
    "banks": [],
    "results": [],
    "log": [],
    "done": False,
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
    "comerica.com": ["https://www.comerica.com/offer/deposits/cd-hymmia.html"],
    "nexbank.com":  ["https://nexbankpersonal.com/"],
    "tbkbank.com":  ["https://www.tbkbank.com/rates/"],
    "maplemarkbank.com": ["https://go.maplemarkbank.com/"],
}

APY_PAT      = re.compile(r'(\d+\.\d+)\s*%\s*(?:APY|Annual\s+Percentage\s+Yield)', re.I)
SAVINGS_PAT  = re.compile(r'(?:savings|high.yield)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
CHECKING_PAT = re.compile(r'(?:checking)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
MM_PAT       = re.compile(r'(?:money\s+market)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)
MIN_BAL_PAT  = re.compile(r'\$\s*([1-9][\d,]*)\s*(?:minimum|min).*?(?:balance|deposit)', re.I)
TABLE_PAT    = re.compile(r'(\d+)\s*[-]?\s*(month|mo|year|day)s?\b[^\n]{0,80}?(\d+\.\d+)\s*%', re.I)
TERM_APY_PAT = re.compile(r'(\d+\.\d+)\s*%\s*APY[^\n]{0,60}?(\d+)\s*[-]?\s*(month|mo|day|year)', re.I)
CD_PAT       = re.compile(r'(?:CD|Certificate)[^\n]{0,120}?(\d+\.\d+)\s*%\s*APY', re.I)


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
            term = f"{int(m.group(1))*12}-month" if 'year' in unit else f"{m.group(1)}-month"
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


async def crawl_bank(page, bank, timeout=12000):
    base = bank["bank_url"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    best = {"checking": None, "savings": None, "cd": None, "cd_term": None,
            "money_market": None, "min_balance": None}
    found_on = None
    visited = set()
    extra = next((urls for k, urls in BANK_EXTRA_URLS.items() if k in base), [])

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
            if r.get("cd_term") and not best["cd_term"]:
                best["cd_term"] = r["cd_term"]
            if any(r.get(k) for k in ["checking","savings","cd","money_market"]):
                found_on = url
            domain = re.match(r'https?://[^/]+', base)
            domain = domain.group(0) if domain else base
            links = await page.query_selector_all("a[href]")
            rate_links = []
            for link in links[:40]:
                try:
                    href = await link.get_attribute("href") or ""
                    txt  = (await link.inner_text()).strip()
                    full = href if href.startswith("http") else domain + href if href.startswith("/") else None
                    if not full or not full.startswith(domain) or full in visited: continue
                    if re.search(r'rate|apy|cd|savings|checking|deposit|certificate|offer', href+txt, re.I):
                        rate_links.append(full)
                except: continue
            for u in rate_links[:5]:
                await visit(u)
        except: pass

    for url in extra:
        await visit(url)
    for path in RATE_PATHS:
        await visit(base + path)

    count = sum(1 for k in ["checking","savings","cd"] if best[k] is not None)
    status = "Found" if count == 3 else "Partial" if count > 0 else "Not public"
    parts = []
    if found_on:
        rate_parts = []
        if best["cd"]:           rate_parts.append(f"CD {best['cd']:.2f}%{' ('+best['cd_term']+')' if best['cd_term'] else ''}")
        if best["savings"]:      rate_parts.append(f"Savings {best['savings']:.2f}%")
        if best["checking"]:     rate_parts.append(f"Checking {best['checking']:.2f}%")
        if best["money_market"]: rate_parts.append(f"Money Mkt {best['money_market']:.2f}%")
        if rate_parts: parts.append(", ".join(rate_parts))
    if best["min_balance"]: parts.append(f"Min: {best['min_balance']}")
    if not parts: parts.append("Rates not publicly listed")
    return {
        **bank,
        "checking_apy":     best["checking"],
        "savings_apy":      best["savings"],
        "cd_apy":           best["cd"],
        "cd_term":          best["cd_term"],
        "money_market_apy": best["money_market"],
        "min_balance":      best["min_balance"],
        "status":           status,
        "note":             " | ".join(parts),
        "crawled_at":       datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


async def run_crawler(banks):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        for i, bank in enumerate(banks):
            if not crawl_state["running"]: break
            crawl_state["log"].append(f"[{i+1}/{len(banks)}] {bank['bank_name']}...")
            page = await ctx.new_page()
            try:
                result = await crawl_bank(page, bank)
                icon = "✓" if result["status"] == "Found" else "~" if result["status"] == "Partial" else "○"
                chk = f"{result['checking_apy']:.2f}%" if result["checking_apy"] else "—"
                sav = f"{result['savings_apy']:.2f}%"  if result["savings_apy"]  else "—"
                cd  = f"{result['cd_apy']:.2f}%"       if result["cd_apy"]       else "—"
                crawl_state["log"].append(f"  {icon} Chk:{chk} Sav:{sav} CD:{cd}")
                crawl_state["results"].append(result)
            except Exception as e:
                crawl_state["log"].append(f"  x Error: {e}")
                crawl_state["results"].append({**bank,"checking_apy":None,"savings_apy":None,"cd_apy":None,"cd_term":None,"money_market_apy":None,"min_balance":None,"status":"Error","note":str(e),"crawled_at":datetime.now().strftime("%Y-%m-%d %H:%M")})
            finally:
                await page.close()
        await browser.close()
    crawl_state["running"] = False
    crawl_state["done"] = True
    found   = sum(1 for r in crawl_state["results"] if r["status"]=="Found")
    partial = sum(1 for r in crawl_state["results"] if r["status"]=="Partial")
    crawl_state["log"].append(f"Done - {found} full, {partial} partial, {len(crawl_state['results'])-found-partial} not public")
    auto_save(crawl_state["results"])


def start_crawl_thread(banks):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_crawler(banks))
        loop.close()
    threading.Thread(target=run, daemon=True).start()


EXPORTS = Path(__file__).parent / "RateRadar_Exports"
FIELDS  = ["run_id","run_date","crawled_at","bank_name","bank_url","checking_apy","savings_apy","cd_apy","cd_term","money_market_apy","min_balance","status","note"]

def auto_save(results):
    # ── Save to CSV ───────────────────────────────────────────────
    try:
        EXPORTS.mkdir(exist_ok=True)
        now = datetime.now()
        run_id = now.strftime("%Y%m%d_%H%M%S")
        path = EXPORTS / f"rate_radar_{run_id}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in results:
                row = dict(r); row["run_id"] = run_id; row["run_date"] = now.strftime("%Y-%m-%d")
                w.writerow(row)
        crawl_state["log"].append(f"Saved: RateRadar_Exports/{path.name}")
    except Exception as e:
        crawl_state["log"].append(f"CSV save failed: {e}")

    # ── Save to Supabase ──────────────────────────────────────────
    # Requires: pip install requests
    # Set SUPABASE_SERVICE_KEY in environment or replace below
    import os
    SUPABASE_URL = "https://tuiiywphoynbmkxpoyps.supabase.co"
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_KEY:
        crawl_state["log"].append("Supabase: set SUPABASE_SERVICE_KEY env variable to save results")
        return
    try:
        import requests as req
        rows = [{
            "bank_name":         r.get("bank_name"),
            "bank_url":          r.get("bank_url"),
            "state":             r.get("state"),
            "cd_apy":            r.get("cd_apy"),
            "cd_term":           r.get("cd_term"),
            "savings_apy":       r.get("savings_apy"),
            "checking_apy":      r.get("checking_apy"),
            "money_market_apy":  r.get("money_market_apy"),
            "min_balance":       r.get("min_balance"),
            "status":            r.get("status"),
            "note":              r.get("note"),
        } for r in results]
        resp = req.post(
            f"{SUPABASE_URL}/rest/v1/raw_rate_radar",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json=rows,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            crawl_state["log"].append(f"Supabase: {len(rows)} results saved to raw_rate_radar")
        else:
            crawl_state["log"].append(f"Supabase error: {resp.status_code} — {resp.text[:120]}")
    except Exception as e:
        crawl_state["log"].append(f"Supabase save failed: {e}")


@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv")
    if not f: return jsonify({"error": "No file received"}), 400
    text = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    banks = []
    for row in reader:
        norm = {k.strip().lower(): v.strip() for k, v in row.items()}
        name_key = next((k for k in norm if "bank_name" in k or k=="name"), None)
        url_key  = next((k for k in norm if "url" in k or "website" in k), None)
        if not name_key: continue
        banks.append({"bank_name": norm[name_key], "bank_url": norm.get(url_key,"") if url_key else ""})
    if not banks: return jsonify({"error": "No banks found - check column names (need bank_name)"}), 400
    crawl_state["banks"] = banks
    return jsonify({"count": len(banks), "banks": banks})

@app.route("/start", methods=["POST"])
def start():
    if crawl_state["running"]: return jsonify({"error": "Already running"}), 400
    if not crawl_state["banks"]: return jsonify({"error": "Upload a CSV first"}), 400
    if not PLAYWRIGHT_OK: return jsonify({"error": "Run: pip install playwright && playwright install chromium"}), 500
    crawl_state.update({"running":True,"results":[],"log":[],"done":False})
    start_crawl_thread(crawl_state["banks"])
    return jsonify({"ok": True})

@app.route("/status")
def status():
    total = len(crawl_state["banks"])
    done  = len(crawl_state["results"])
    return jsonify({
        "running":  crawl_state["running"],
        "done":     crawl_state["done"],
        "total":    total,
        "progress": done,
        "pct":      round(done/total*100 if total else 0, 1),
        "log":      crawl_state["log"][-50:],
        "results":  crawl_state["results"],
    })

@app.route("/export")
def export():
    if not crawl_state["results"]: return "No results", 400
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    now = datetime.now()
    for r in crawl_state["results"]:
        row = dict(r); row["run_id"] = now.strftime("%Y%m%d_%H%M%S"); row["run_date"] = now.strftime("%Y-%m-%d")
        w.writerow(row)
    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=rate_radar_{now.strftime('%Y%m%d_%H%M')}.csv"})


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
main { max-width: 1200px; margin: 0 auto; padding: 24px; }
.card { background: white; border: 1px solid #dce4ee; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
.upload-area { text-align: center; padding: 20px 0; }
.upload-area h2 { margin: 0 0 8px; }
.upload-area p { margin: 0 0 16px; color: #6b82a0; font-size: 13px; }
input[type=file] { font-size: 14px; padding: 8px; border: 2px solid #1B3A5C; border-radius: 6px; background: white; cursor: pointer; margin-right: 10px; }
.btn { padding: 9px 22px; border: none; border-radius: 6px; font-size: 14px; font-weight: bold; cursor: pointer; }
.btn-navy  { background: #1B3A5C; color: white; }
.btn-navy:hover  { background: #122840; }
.btn-navy:disabled  { opacity: 0.4; cursor: not-allowed; }
.btn-amber { background: #F5A623; color: #1B3A5C; }
.btn-amber:hover { background: #c47d0e; color: white; }
.btn-amber:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-gray  { background: #eef2f7; color: #1B3A5C; }
.metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
.metric { background: white; border: 1px solid #dce4ee; border-radius: 8px; padding: 14px 16px; border-top: 3px solid #F5A623; }
.metric-label { font-size: 11px; color: #6b82a0; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
.metric-value { font-size: 24px; font-weight: bold; color: #1B3A5C; }
.progress-bar { background: #dce4ee; border-radius: 99px; height: 8px; overflow: hidden; flex:1; min-width:100px; }
.progress-fill { height: 100%; background: #F5A623; border-radius: 99px; width: 0%; transition: width 0.4s; }
.controls-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #f0f4f8; padding: 10px 12px; text-align: left; font-size: 11px; color: #6b82a0; text-transform: uppercase; border-bottom: 2px solid #dce4ee; cursor: pointer; white-space: nowrap; }
th:hover { color: #1B3A5C; }
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
    <p>Bank Market Assessment Platform &middot; Deposit Rate Crawler</p>
  </div>
</header>

<main>

  <!-- Step 1: Upload -->
  <div class="card" id="upload-card">
    <div class="upload-area">
      <h2>Step 1 &mdash; Select your bank CSV file</h2>
      <p>Requires columns: bank_name, bank_url &nbsp;&middot;&nbsp; Additional columns (state, city) are preserved</p>
      <input type="file" id="csv-input" accept=".csv">
      <button class="btn btn-navy" onclick="doUpload()">Load Banks</button>
      <div id="upload-msg" class="success-msg" style="display:none;"></div>
    </div>
  </div>

  <!-- Step 2: Crawl (hidden until upload) -->
  <div id="crawl-section" style="display:none;">

    <div class="card">
      <div class="controls-row">
        <button class="btn btn-amber" id="start-btn" onclick="startCrawl()">&#9654; Start Crawl</button>
        <button class="btn btn-navy" id="export-btn" onclick="exportCSV()" disabled>&#8595; Export CSV</button>
        <button class="btn btn-gray" onclick="resetAll()">&#10005; Change File</button>
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
      <div class="metric"><div class="metric-label">Best savings APY</div><div class="metric-value" id="m-sav">-</div></div>
    </div>

    <div class="card" style="padding:0;overflow:hidden;">
      <div style="padding:12px 16px;background:#f0f4f8;border-bottom:2px solid #dce4ee;display:flex;align-items:center;gap:10px;">
        <strong>Results</strong>
        <span id="result-count" style="font-size:12px;color:#6b82a0;"></span>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th onclick="sortBy('bank_name')" style="width:20%">Bank</th>
              <th onclick="sortBy('checking_apy')" style="width:10%">Checking APY</th>
              <th onclick="sortBy('savings_apy')" style="width:10%">Savings APY</th>
              <th onclick="sortBy('money_market_apy')" style="width:10%">Money Mkt APY</th>
              <th onclick="sortBy('cd_apy')" style="width:10%">CD APY</th>
              <th style="width:8%">Min Balance</th>
              <th style="width:8%">Status</th>
              <th style="width:24%">Note</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="8" style="text-align:center;padding:40px;color:#6b82a0;">Upload a CSV to get started</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div>
</main>

<script>
let allResults = [], polling = null, sortField = null, sortDir = -1;

function doUpload() {
  const input = document.getElementById('csv-input');
  if (!input.files || input.files.length === 0) {
    alert('Please select a CSV file first using the file picker above.');
    return;
  }
  const file = input.files[0];
  const fd = new FormData();
  fd.append('csv', file);
  fetch('/upload', {method:'POST', body:fd})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert('Error: ' + data.error); return; }
      document.getElementById('upload-msg').textContent = data.count + ' banks loaded successfully!';
      document.getElementById('upload-msg').style.display = 'block';
      document.getElementById('crawl-section').style.display = 'block';
      populateQueued(data.banks);
    })
    .catch(function(e) { alert('Upload failed: ' + e); });
}

function populateQueued(banks) {
  document.getElementById('tbody').innerHTML = banks.map(function(b) {
    return '<tr><td><div class="bank-name">' + b.bank_name + '</div><div class="bank-url"><a href="' + b.bank_url + '" target="_blank">' + b.bank_url + '</a></div></td><td><span class="dash">-</span></td><td><span class="dash">-</span></td><td><span class="dash">-</span></td><td><span class="dash">-</span></td><td><span class="dash">-</span></td><td><span class="badge b-queued">Queued</span></td><td></td></tr>';
  }).join('');
  document.getElementById('result-count').textContent = banks.length + ' banks';
}

function startCrawl() {
  fetch('/start', {method:'POST'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); return; }
      document.getElementById('start-btn').disabled = true;
      document.getElementById('start-btn').textContent = 'Running...';
      document.getElementById('metrics').style.display = 'grid';
      document.getElementById('log-box').style.display = 'block';
      polling = setInterval(pollStatus, 1500);
    });
}

function pollStatus() {
  fetch('/status').then(function(r) { return r.json(); }).then(function(data) {
    document.getElementById('prog-fill').style.width = data.pct + '%';
    document.getElementById('prog-label').textContent = data.progress + ' / ' + data.total;
    var lb = document.getElementById('log-box');
    lb.innerHTML = data.log.map(function(l) {
      var cls = l.indexOf('✓') >= 0 ? 'log-hit' : (l.indexOf('○') >= 0 || l.indexOf('~') >= 0) ? 'log-miss' : '';
      return '<div class="' + cls + '">' + l + '</div>';
    }).join('');
    lb.scrollTop = lb.scrollHeight;
    allResults = data.results;
    renderTable();
    updateMetrics();
    if (data.done) {
      clearInterval(polling);
      document.getElementById('start-btn').disabled = false;
      document.getElementById('start-btn').textContent = '▶ Re-crawl';
      document.getElementById('export-btn').disabled = false;
      document.getElementById('status-msg').textContent = 'Complete - ' + new Date().toLocaleTimeString();
    }
  });
}

function rateCell(val, sub) {
  if (!val) return '<span class="dash">-</span>';
  var n = parseFloat(val);
  var cls = n >= 4.0 ? 'rate-high' : n >= 1.5 ? 'rate-mid' : 'rate-low';
  var termStr = (sub && sub !== 'best found') ? '<br><small style="color:#6b82a0">' + sub + '</small>' : '';
  return '<span class="' + cls + '">' + n.toFixed(2) + '%</span>' + termStr;
}

function badge(s) {
  if (s === 'Found')   return '<span class="badge b-found">Found</span>';
  if (s === 'Partial') return '<span class="badge b-partial">Partial</span>';
  return '<span class="badge b-np">Not public</span>';
}

function renderTable() {
  var rows = allResults.slice();
  if (sortField) {
    rows.sort(function(a,b) {
      if (sortField === 'bank_name') return a.bank_name.localeCompare(b.bank_name) * sortDir;
      return ((parseFloat(b[sortField]) || -1) - (parseFloat(a[sortField]) || -1)) * sortDir;
    });
  }
  document.getElementById('tbody').innerHTML = rows.map(function(r) {
    return '<tr><td><div class="bank-name">' + r.bank_name + '</div><div class="bank-url"><a href="' + (r.bank_url||'') + '" target="_blank">' + (r.bank_url||'') + '</a></div></td><td>' + rateCell(r.checking_apy, null) + '</td><td>' + rateCell(r.savings_apy, null) + '</td><td>' + rateCell(r.money_market_apy, null) + '</td><td>' + rateCell(r.cd_apy, r.cd_term) + '</td><td><span style="font-size:12px">' + (r.min_balance || '<span class="dash">-</span>') + '</span></td><td>' + badge(r.status) + '</td><td><div class="note-cell">' + (r.note||'') + '</div></td></tr>';
  }).join('');
  document.getElementById('result-count').textContent = rows.length + ' banks';
}

function updateMetrics() {
  var total = allResults.length;
  var found = allResults.filter(function(r) { return r.status !== 'Not public'; }).length;
  var cds   = allResults.filter(function(r) { return r.cd_apy; }).map(function(r) { return r.cd_apy; });
  var savs  = allResults.filter(function(r) { return r.savings_apy; }).map(function(r) { return r.savings_apy; });
  document.getElementById('m-total').textContent = total;
  document.getElementById('m-found').textContent = found + '/' + total;
  document.getElementById('m-cd').textContent  = cds.length  ? Math.max.apply(null,cds).toFixed(2)+'%'  : '-';
  document.getElementById('m-sav').textContent = savs.length ? Math.max.apply(null,savs).toFixed(2)+'%' : '-';
}

function sortBy(field) {
  if (sortField === field) sortDir *= -1; else { sortField = field; sortDir = -1; }
  renderTable();
}

function exportCSV() { window.location = '/export'; }

function resetAll() {
  if (polling) clearInterval(polling);
  allResults = [];
  document.getElementById('crawl-section').style.display = 'none';
  document.getElementById('upload-msg').style.display = 'none';
  document.getElementById('csv-input').value = '';
  document.getElementById('start-btn').disabled = false;
  document.getElementById('start-btn').textContent = '▶ Start Crawl';
  document.getElementById('export-btn').disabled = true;
  document.getElementById('metrics').style.display = 'none';
  document.getElementById('log-box').style.display = 'none';
  document.getElementById('status-msg').textContent = '';
  document.getElementById('prog-fill').style.width = '0';
  document.getElementById('prog-label').textContent = '';
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("  BMAP Rate Radar - Opening http://localhost:7331")
    if not PLAYWRIGHT_OK:
        print("  Setup: pip install flask playwright && playwright install chromium")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:7331")).start()
    app.run(port=7331, debug=False)
