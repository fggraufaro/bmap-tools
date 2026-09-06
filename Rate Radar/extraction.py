# ── Rate extraction ───────────────────────────────────────────────────────────
# Pure text/JSON → rate-dict parsing logic: the regex patterns that recognize
# APY figures in scraped page text, and the functions that turn them into the
# {"checking": ..., "savings": ..., "cd_ladder": [...], ...} result shape used
# everywhere else. No Playwright, no crawl_state, no network calls — every
# function here takes plain text/dicts in and returns plain dicts out, so it
# can be unit-tested and reasoned about in isolation from the scraping flow.

import json
import re
from datetime import datetime

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_OK = True
except ImportError:
    DATEUTIL_OK = False

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
# Phase 5: SAVINGS_PAT's alternation already matches both plain "savings" and
# "high yield savings" labels (picking whichever has the higher rate) — this
# classifies which one a given match actually was, so the two can be reported
# as separate products instead of collapsed into one. A match always starts
# exactly at the label alternative that matched, so checking its own leading
# text is precise (no separate label-search window needed).
# No leading ^ anchor: Pattern.match(text, pos) already only tries to match
# starting exactly at pos — a literal "^" would (surprisingly) still require
# the true start of the whole string, not pos, and silently never match here.
HYS_LABEL_PAT = re.compile(r'high[\s-]?yield', re.I)

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

# JSON-payload rate sniffing (ZIP-gated rates XHRs) — shared with
# make_json_sniffer()'s Playwright response listener in rate_radar.py.
JSON_RATE_KEY_PAT = re.compile(r'apy|rate', re.I)

JSON_PRODUCT_HINTS = [
    ("cd",           re.compile(r'\bcds?\b|cds?[\d_\-]|[\d_\-]cds?\b|certificate|term\s*deposit', re.I)),
    ("money_market", re.compile(r'money.?market|\bmm[ad]?\b', re.I)),
    ("savings",      re.compile(r'sav', re.I)),
    ("checking",     re.compile(r'check|chk|interest\s*checking', re.I)),
]


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


def _clean_cd_ladder(raw):
    """Phase 5 (part 3): validate an LLM-returned cd_ladder array into a clean
    [{"term": str_or_None, "apy": float}, ...] list, tolerating malformed or
    missing entries rather than dropping the whole ladder. Returns None if raw
    isn't a usable list — never raises."""
    if not isinstance(raw, list) or not raw:
        return None
    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            apy = float(entry.get("apy"))
        except (TypeError, ValueError):
            continue
        cleaned.append({"term": entry.get("term"), "apy": apy})
    return cleaned or None


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


def _parse_term_months(cd_term):
    """'12-month' -> 12, '1-year' -> 12, 'best found' / None -> None."""
    if not cd_term or cd_term == "best found":
        return None
    m = re.search(r"(\d+)\s*[- ]?\s*(month|mo|year|yr)", cd_term, re.I)
    if m:
        n = int(m.group(1))
        return n * 12 if m.group(2).lower().startswith("y") else n
    m = re.search(r"\d+", cd_term)
    return int(m.group()) if m else None


def extract_rates(text):
    r = {"checking": None, "savings": None, "high_yield_savings": None,
         "cd": None, "cd_term": None, "cd_ladder": None,
         "money_market": None, "min_balance": None,
         "rate_tags": {}}   # e.g. {"checking": ["conditional"], "savings": ["promo"]}

    # Savings — collect ALL matches with positions, then split into plain
    # "savings" vs "high yield savings" by what the match's own label text
    # says (Phase 5). A page with both a low plain-savings rate and a
    # separate high-yield product reports both instead of only the higher one.
    all_sav_matches = [m for m in SAVINGS_PAT.finditer(text) if 0.05 <= float(m.group(1)) <= 15]
    all_sav_matches += [m for m in SAVINGS_PAT_NEXTLN.finditer(text) if 0.05 <= float(m.group(1)) <= 15]
    all_sav_matches += [m for m in SAVINGS_PAT_WIDEGAP.finditer(text) if 0.05 <= float(m.group(1)) <= 15]
    sav_matches, hys_matches = [], []
    for m in all_sav_matches:
        bucket = hys_matches if HYS_LABEL_PAT.match(text, m.start()) else sav_matches
        bucket.append((float(m.group(1)), m.start()))
    if sav_matches:
        best_sav = max(sav_matches, key=lambda x: x[0])
        r["savings"] = best_sav[0]
        tags = tag_rate_context(text, best_sav[1])
        if tags:
            r["rate_tags"]["savings"] = tags
    if hys_matches:
        best_hys = max(hys_matches, key=lambda x: x[0])
        r["high_yield_savings"] = best_hys[0]
        tags = tag_rate_context(text, best_hys[1])
        if tags:
            r["rate_tags"]["high_yield_savings"] = tags

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

    # CD candidates — require APY label, reject loans, deprioritise jumbos.
    # 5th field is a same-line-vs-next-line priority (0=safe, 1=risky) — see
    # the cd_ladder comment below for why this matters for the ladder grouping.
    cd_candidates = []
    for m in TABLE_PAT.finditer(text):
        val = float(m.group(3))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            unit = m.group(2).lower()
            term = f"{int(m.group(1))*12}-month" if "year" in unit else f"{m.group(1)}-month"
            cd_candidates.append((val, term, ctx, m.start(), 0))
    for m in TERM_APY_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, f"{m.group(2)}-{m.group(3)}", ctx, m.start(), 0))
    for m in CD_PAT.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, None, ctx, m.start(), 0))
    # v3.4: label-on-one-line / APY-on-the-next (rate tiles, table cells).
    # Risky (priority 1): when a page has several complete "label + rate"
    # lines back to back, these can cross-match a label on one line with the
    # NEXT line's rate instead of its own — harmless for the single-best pick
    # (the correctly-labeled same-line match usually ties or wins on value
    # anyway), but would corrupt a specific term's bucket in the ladder.
    for m in TABLE_PAT_NEXTLN.finditer(text):
        val = float(m.group(3))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            unit = m.group(2).lower()
            term = f"{int(m.group(1))*12}-month" if "year" in unit else f"{m.group(1)}-month"
            cd_candidates.append((val, term, ctx, m.start(), 1))
    for m in CD_PAT_NEXTLN.finditer(text):
        val = float(m.group(1))
        if 0.05 <= val <= 15:
            start = max(0, m.start() - 40); end = min(len(text), m.end() + 40)
            ctx = text[start:end]
            if LOAN_PAT.search(ctx): continue
            cd_candidates.append((val, None, ctx, m.start(), 1))
    if cd_candidates:
        def cd_score(c): return c[0] - (2.0 if JUMBO_PAT.search(c[2] or "") else 0.0)
        best_cd = max(cd_candidates, key=cd_score)
        r["cd"], r["cd_term"] = best_cd[0], best_cd[1]
        tags = tag_rate_context(text, best_cd[3])
        if tags:
            r["rate_tags"]["cd"] = tags

        # Phase 5 (part 3): full CD ladder — group candidates by their PARSED
        # term (so "12-month" and "1-year" land in the same bucket) and keep
        # the best per term, instead of only the single overall best. Bucket
        # winner is chosen by priority first (same-line matches beat risky
        # cross-line ones), THEN score — a wrongly cross-matched "risky"
        # candidate must never outrank a correctly-labeled "safe" one just
        # because it happens to have a higher number. The overall best above
        # is always one of these entries, so nothing is lost —
        # build_rate_observations() prefers the ladder over the singular
        # cd/cd_term when both are present.
        def bucket_key(c): return (-c[4], cd_score(c))   # c[4]=priority, 0=safe beats 1=risky
        by_term = {}
        for c in cd_candidates:
            term_key = _parse_term_months(c[1])
            if term_key is None:
                continue   # CD_PAT/CD_PAT_NEXTLN's untermed catch-all isn't a ladder "rung"
            if term_key not in by_term or bucket_key(c) > bucket_key(by_term[term_key]):
                by_term[term_key] = c
        r["cd_ladder"] = [{"term": c[1], "apy": c[0]} for c in by_term.values()] or None

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
