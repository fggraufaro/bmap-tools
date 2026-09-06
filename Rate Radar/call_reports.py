# ── Call Report loader ────────────────────────────────────────────────────────
# FFIEC quarterly Call Report (RI / RCE / RCK) ingestion — implied-APY fields
# used to sanity-check scraped rates against regulatory filings. Genuinely
# standalone: reads from a local CallReports/ folder or directly from
# Supabase, and returns plain dicts. No dependency on the crawl/scrape flow.

import json
import os
import re
from datetime import datetime
from pathlib import Path
import urllib.request as urlreq
import urllib.error as urlerr

try:
    import pandas as pd
    import numpy as np
    PANDAS_OK = True
except ImportError:
    PANDAS_OK = False

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY", "")
                or os.environ.get("SUPABASE_KEY", "")).strip()

CALL_REPORTS_DIR = Path(__file__).parent / "CallReports"


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
        print(f"  ✓ Loaded {len(cr_data)} banks from {folder.name}")
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
            req = urlreq.Request(url, headers=headers)
            try:
                with urlreq.urlopen(req, timeout=30) as resp:
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
        print(f"  ✓ Loaded {len(cr_data)} banks from Supabase ({dt.strftime('%b %Y')})")
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
