# ── Rate integrity: provenance, confidence, tiers, identity, "not published" ───
# Pure functions (no network, no Playwright, no crawl_state) that decide HOW a
# rate value was obtained, how far it can be trusted, which tier of a tiered
# product it is, whether the page belongs to the bank, and whether the bank
# publishes the rate at all. Everything here takes plain text/dicts in and
# returns plain values out, so each rule has a unit test and a mutation check
# (tests/integrity).
#
# Why this module exists (run 20261117_103944, 4 banks, every saved value
# compared with the bank's own page):
#   * a money-market value read by AI vision from a screenshot was saved as
#     extraction_method=site / confidence=high with the CD page as its source,
#     because money_market had no source URL of its own and any http URL meant
#     "site" (storage._provenance);
#   * values filled by the web-search preflight carried no source, so they
#     inherited the first page the crawler happened to read;
#   * "savings from search" matched inside "high_yield_savings from search";
#   * no rule said which tier of a tiered rate is THE rate;
#   * a page that never names the bank was accepted, and a bank that says
#     "inquire for rates" was overwritten by a search value.

import os
import re
from urllib.parse import urlparse

# extraction_method values allowed by the CHECK on public.rate_observations
SITE, AI_VISION, AGGREGATOR, SEARCH, LAST_KNOWN_GOOD, MANUAL = (
    "site", "ai_vision", "aggregator", "search", "last_known_good", "manual")

HIGH, MEDIUM, LOW = "high", "medium", "low"

# Standing rule: NerdWallet rate data is proprietary and is NEVER collected by
# automation. Belt and braces on top of the search prompt's own refusal.
BLOCKED_SOURCE_DOMAINS = ("nerdwallet.com",)

# ── THE tier rule: one setting ────────────────────────────────────────────────
# A product that pays different rates by balance tier needs ONE stated rule for
# which tier is "the rate". It is a product decision, so it is ONE setting
# (environment variable RATE_RADAR_TIER_RULE), read at call time by the page
# parser and quoted to the AI readers in their prompts:
#   "highest" (default; decided by Francisco 17 Nov 2026) the highest rate
#             advertised for the product (its "up to" rate);
#   "entry"   the rate that applies at the product's minimum opening balance;
#             with no stated minimum, the lowest-balance tier.
# Either way the other tiers are listed in tier_note and the value carries the
# 'tiered' flag. Anything else in the variable falls back to the default.
TIER_RULES = {
    "entry": ("When a product pays different rates by balance tier, report the rate that applies at the "
              "product's minimum opening balance; if no opening minimum is stated, report the lowest-balance "
              "tier. Mention the other tiers in a note; never report the top tier as the product's rate."),
    "highest": ("When a product pays different rates by balance tier, report the HIGHEST advertised rate "
                "for that product (its 'up to' rate), and note the balance band and the minimum to open that "
                "rate needs; mention the other tiers in the note."),
}
DEFAULT_TIER_RULE = "highest"
TIER_RULE_ENV = "RATE_RADAR_TIER_RULE"


def tier_mode():
    """The active tier rule name: 'entry' (default) or 'highest'."""
    v = (os.environ.get(TIER_RULE_ENV) or "").strip().lower()
    return v if v in TIER_RULES else DEFAULT_TIER_RULE


def tier_rule():
    """The active tier rule as a sentence, for the AI prompts."""
    return TIER_RULES[tier_mode()]


def make_source(method, url=None, verified=False, label=None):
    """One provenance entry for one product's value, created at the moment the
    value is assigned: where it came from (method), the page it was read from
    (url, None when there is none), whether the number was actually found in
    that page's text (verified), and a human label for non-URL sources."""
    return {"method": method, "url": url or None, "verified": bool(verified), "label": label or None}


def domain_of(url):
    """Bare host of a URL (no scheme, no www.), '' when there is none."""
    if not url or not str(url).lower().startswith("http"):
        return ""
    host = urlparse(str(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url, bank_url):
    """True when url is on the bank's own domain (either contains the other,
    so www.example.com and example.com/rates both match example.com)."""
    d, b = domain_of(url), domain_of(bank_url)
    return bool(d and b and (d == b or d.endswith("." + b) or b.endswith("." + d)))


def is_blocked_source(domain_or_url):
    s = (domain_or_url or "").lower()
    return any(b in s for b in BLOCKED_SOURCE_DOMAINS)


# ── confidence ────────────────────────────────────────────────────────────────

def _resolve_core(entry, bank_url=None, identity=None, edited=False):
    """Decide (extraction_method, confidence, source_url, flags) for one value.

    'high' is reserved for a value READ BY THE DOM EXTRACTOR from the
    product's own page on the bank's own domain, on a page that names the bank.
    An AI-vision value is at most 'medium', and only when the number is present
    in that page's text. A search value is always 'low'. A value without a
    recorded source is 'low' and says so; it never borrows another product's or
    the bank's general URL. A value a person confirmed in the verify UI keeps
    its existing treatment (site / high / manually verified)."""
    if edited:
        return SITE, HIGH, (entry or {}).get("url"), []
    if not entry:
        return SEARCH, LOW, None, ["source_unknown"]
    method, url = entry.get("method"), entry.get("url")
    flags = []
    if method == SEARCH:
        return SEARCH, LOW, entry.get("label") or None, ["search_only"]
    if method == LAST_KNOWN_GOOD:
        return LAST_KNOWN_GOOD, LOW, None, ["last_known_good"]
    if method == AGGREGATOR:
        return AGGREGATOR, MEDIUM, url, []
    if method == AI_VISION:
        if entry.get("verified"):
            conf = MEDIUM
        else:
            conf, flags = LOW, ["not_in_page_text"]
        if identity == "mismatch":
            conf, flags = LOW, flags + ["identity_mismatch"]
        return AI_VISION, conf, url, flags
    if method == SITE:
        if not url:
            return SEARCH, LOW, None, ["source_unknown"]
        conf = HIGH
        if not entry.get("verified"):
            conf, flags = MEDIUM, ["not_in_page_text"]
        if bank_url and not same_site(url, bank_url):
            conf, flags = MEDIUM, flags + ["off_domain"]
        if identity == "mismatch":
            conf, flags = LOW, flags + ["identity_mismatch"]
        return SITE, conf, url, flags
    return SEARCH, LOW, None, ["source_unknown"]


def resolve(entry, bank_url=None, identity=None, edited=False, stale=False):
    """_resolve_core() plus the page-date rule: a value read from a page whose own as-of date is
    older than the staleness limit is KEPT but never better than 'low', with the flag
    'stale_page_date' (a person confirmed value, edited=True, is left as it is)."""
    method, conf, url, flags = _resolve_core(entry, bank_url, identity, edited)
    if stale and not edited:
        conf, flags = LOW, list(flags) + ["stale_page_date"]
    return method, conf, url, flags


def legacy_entry(product_type, specific_url, note):
    """Provenance entry for a result that predates per-value provenance (an old
    cached result, a manual save): conservative, derived only from the product's
    OWN source URL and from a whole-word 'from search' note for that product."""
    note = (note or "").lower()
    su = (specific_url or "").strip()
    if "no fresh data found today" in note:
        return make_source(LAST_KNOWN_GOOD)
    # whole-token match: 'savings from search' must not match 'high_yield_savings from search'
    if re.search(r"(?<![a-z_])" + re.escape(product_type) + r" from search", note):
        return make_source(SEARCH, label=su if su.startswith("[Search]") else None)
    if su.startswith("[Search]"):
        return make_source(SEARCH, label=su)
    if su.startswith("[AI]"):
        return make_source(AI_VISION, url=su[4:].strip(), verified=False)
    if "depositaccounts" in su.lower():
        return make_source(AGGREGATOR, url=su)
    if su.lower().startswith("http"):
        return make_source(SITE, url=su, verified=True)
    return None


# ── is the number really on the page? ─────────────────────────────────────────

def value_in_text(value, text):
    """True when the APY value appears as a percentage in the page text
    (e.g. 4.75 -> '4.75%' / '4.750%'). A number a vision model reported that is
    nowhere in the page's text is a guess, not a reading."""
    if value is None or not text:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    forms = {f"{v:.2f}", f"{v:.3f}".rstrip("0"), f"{v:g}"}
    for f in forms:
        if re.search(r"(?<![\d.])" + re.escape(f) + r"0*\s*%", text):
            return True
    return False


def page_has_rates(text, minimum=3):
    """True when the text shows at least `minimum` percentage values."""
    return len(re.findall(r"\d+\.\d+\s*%", text or "")) >= minimum


def verify_ai_values(ai_result, page_text):
    """Check each product value a vision read returned against the page text.

    Returns (kept, dropped): `kept` is a copy of ai_result where every value is
    either present in the page text (verified) or the page has no readable
    rates at all (image-only: kept, but unverified); `dropped` lists
    (product, value) pairs removed because the page text shows rates and this
    number is not among them. Second element of `verified` maps product ->
    True/False for the values that were kept."""
    kept = dict(ai_result or {})
    dropped, verified = [], {}
    rich = page_has_rates(page_text)
    for k in ("checking", "savings", "high_yield_savings", "money_market", "cd"):
        v = kept.get(k)
        if v is None:
            continue
        if value_in_text(v, page_text):
            verified[k] = True
        elif rich:
            dropped.append((k, v))
            kept[k] = None
        else:
            verified[k] = False
    if kept.get("cd") is None:
        kept["cd_term"] = None
    ladder = kept.get("cd_ladder")
    if ladder:
        good = [e for e in ladder if value_in_text(e.get("apy"), page_text)] if rich else list(ladder)
        if len(good) != len(ladder):
            dropped.append(("cd_ladder", len(ladder) - len(good)))
        kept["cd_ladder"] = good or None
    return kept, dropped, verified


# ── tiered rates ──────────────────────────────────────────────────────────────

_NUM = r"\$\s*([\d,]+(?:\.\d+)?)"
_SEP = r"\s*(?:--|—|–|:|-)?\s*"
_RATE = r"(\d+\.\d+)\s*%\s*(?:APY)?"
_TIER_PATS = [
    # "$10,000 or less -- 0.05% APY" / "$2,499 or under"
    ("upto", re.compile(_NUM + r"\s*(?:or\s+)?(?:less|under|and\s+under)" + _SEP + _RATE, re.I)),
    # "up to $50,000 -- 0.62% APY" / "under $5,000: 0.10%"
    ("upto", re.compile(r"(?:up\s+to|under|less\s+than)\s*" + _NUM + _SEP + _RATE, re.I)),
    # "$10,000.01 to $25,000 -- 0.62% APY" / "$50,001 - $250,000: 3.17%"
    ("range", re.compile(_NUM + r"\s*(?:to|-|–|—)\s*" + _NUM + _SEP + _RATE, re.I)),
    # "balances over $25,000.01 -- 0.10% APY" / "over $75,000 0.62%"
    ("over", re.compile(r"(?:balances?\s+)?(?:over|above|greater\s+than)\s*" + _NUM + _SEP + _RATE, re.I)),
    # "$250,001 or over -- 3.56% APY" / "$2,500 or more 0.42%"
    ("over", re.compile(_NUM + r"\s*(?:(?:or|and)\s+)?(?:more|over|above)" + _SEP + _RATE, re.I)),
]
# table row: "Savings Accounts ($2,499 or less) 0.12% 0.12%" (interest rate, then APY)
_TABLE_PATS = [
    ("upto", re.compile(r"\(\s*" + _NUM + r"\s*or\s+(?:less|under)\s*\)\s*(\d+\.\d+)\s*%\s*(\d+\.\d+)\s*%", re.I)),
    ("over", re.compile(r"\(\s*" + _NUM + r"\s*or\s+(?:more|over|above)\s*\)\s*(\d+\.\d+)\s*%\s*(\d+\.\d+)\s*%", re.I)),
]
_PRODUCT_KW = [
    ("money_market", re.compile(r"money\s+market", re.I)),
    ("high_yield_savings", re.compile(r"high[\s-]?yield\s+savings", re.I)),
    ("savings", re.compile(r"savings\s+accounts?|savings", re.I)),
    ("checking", re.compile(r"checking", re.I)),
    ("cd", re.compile(r"certificates?(?:\s+of\s+deposit)?|\bCDs?\b", re.I)),
]
_MIN_OPEN = re.compile(_NUM + r"\s*minimum\s+(?:to\s+open|opening\s+deposit|balance\s+to\s+open)", re.I)


def _money(s):
    return float(s.replace(",", ""))


def _tier_matches(text):
    """All tier matches in the text as (start, end, lo, hi, apy), sorted."""
    out = []
    for kind, pat in _TIER_PATS:
        for m in pat.finditer(text):
            g = m.groups()
            if kind == "upto":
                lo, hi, apy = 0.0, _money(g[0]), float(g[1])
            elif kind == "range":
                lo, hi, apy = _money(g[0]), _money(g[1]), float(g[2])
            else:
                lo, hi, apy = _money(g[0]), None, float(g[1])
            out.append((m.start(), m.end(), lo, hi, apy))
    for kind, pat in _TABLE_PATS:
        for m in pat.finditer(text):
            n, _rate, apy = _money(m.group(1)), m.group(2), float(m.group(3))
            out.append((m.start(), m.end(), 0.0 if kind == "upto" else n, n if kind == "upto" else None, apy))
    out.sort()
    # drop overlapping duplicates (one tier matched by two patterns): keep the first, longest
    keep = []
    for t in out:
        if keep and t[0] < keep[-1][1]:
            if t[1] - t[0] > keep[-1][1] - keep[-1][0]:
                keep[-1] = t
            continue
        keep.append(t)
    return keep


def pick_entry_tier(tiers, min_open=None):
    """The tier that applies at the opening minimum (or the lowest-balance tier
    when no minimum is stated). `tiers` is a list of dicts with lo, hi, apy."""
    if not tiers:
        return None
    ordered = sorted(tiers, key=lambda t: t["lo"])
    if min_open is not None:
        for t in ordered:
            if t["lo"] <= min_open and (t["hi"] is None or min_open <= t["hi"]):
                return t
    return ordered[0]


def pick_highest_tier(tiers):
    """The tier advertising the highest rate (ties: the lowest balance tier)."""
    if not tiers:
        return None
    return sorted(tiers, key=lambda t: (-t["apy"], t["lo"]))[0]


def pick_tier(tiers, min_open=None, mode=None):
    """Apply the active tier rule (see tier_mode()): the entry tier by default,
    or the highest advertised tier when the setting says so."""
    return pick_highest_tier(tiers) if (mode or tier_mode()) == "highest" else pick_entry_tier(tiers, min_open)


def _fmt_money(v):
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def _tier_label(t):
    if t["hi"] is None:
        return f"{t['apy']:.2f}% from {_fmt_money(t['lo'])}"
    if t["lo"] == 0:
        return f"{t['apy']:.2f}% up to {_fmt_money(t['hi'])}"
    return f"{t['apy']:.2f}% {_fmt_money(t['lo'])} to {_fmt_money(t['hi'])}"


_PREMIUM_WORDS = re.compile(r"premium|elite|platinum|preferred|high[\s-]?yield|\bedge\b", re.I)


def _block_note(use, pick, tiers, mo, name):
    """The note stored with a tiered value: which tier was reported and why, the
    condition it needs (minimum to open, balance band) and the other tiers."""
    others = [_tier_label(t) for t in sorted(tiers, key=lambda t: t["lo"]) if t is not pick]
    if use == "highest":
        why = "highest advertised rate" + (f"; {_fmt_money(mo)} minimum to open" if mo is not None else "")
    elif mo is not None:
        why = f"rate that applies at the {_fmt_money(mo)} opening minimum"
    else:
        why = "rate that applies at the lowest balance tier"
    label = f"{name}: " if name else ""
    return (f"tiered: {label}{_tier_label(pick)} ({why})" + (f"; other tiers: {'; '.join(others)}" if others else ""))


def extract_tiered_products(text, window=700, cluster_gap=60, mode=None):
    """Find balance-tiered rate blocks and apply the one tier rule (tier_mode(),
    or `mode` when given).

    Returns {product_type: {"apy", "min_open", "name", "tiers", "note"}} with one
    entry per product type. A block is a run of tier lines (each '<balance
    bracket> -- N.NN% APY', or a table row '(<bracket>) <rate> <APY>') whose
    product is the nearest product keyword within `window` characters before it.
    The note carries the conditions of the reported rate (the balance band, the
    minimum to open) and the other tiers.

    A page that lists a plain money-market block AND a separate block labelled
    premium / elite / edge ... (its own minimum to open, its own tiers) has two
    products: the plain one is money_market, the premium one is
    high_yield_savings. With several premium blocks the highest-rate one is
    reported under the 'highest' rule, the first under 'entry'."""
    text = text or ""
    matches = _tier_matches(text)
    if not matches:
        return {}
    clusters, cur = [], [matches[0]]
    for t in matches[1:]:
        if t[0] - cur[-1][1] <= cluster_gap:
            cur.append(t)
        else:
            clusters.append(cur)
            cur = [t]
    clusters.append(cur)
    use = mode or tier_mode()
    blocks = []   # (product_type, premium, pick, tiers, min_open, name)
    seen = set()
    for cl in clusters:
        start = cl[0][0]
        before = text[max(0, start - window):start]
        best = None   # (position in `before`, product_type, phrase, end)
        for ptype, pat in _PRODUCT_KW:
            for m in pat.finditer(before):
                if best is None or m.start() > best[0] or (m.start() == best[0] and m.end() > best[3]):
                    best = (m.start(), ptype, m.group(0), m.end())
        if best is None:
            continue
        ptype, phrase = best[1], best[2]
        head = before[max(0, best[0] - 40):best[3] + 80]
        premium = bool(_PREMIUM_WORDS.search(head))
        if ptype == "savings" and re.search(r"high[\s-]?yield", before[max(0, best[0] - 15):best[0] + 20], re.I):
            ptype = "high_yield_savings"
        mo = None
        for m in _MIN_OPEN.finditer(before[best[0]:]):   # only the minimum stated AFTER this block's own heading
            mo = _money(m.group(1))
        tiers = [{"lo": c[2], "hi": c[3], "apy": c[4]} for c in cl]
        key = (ptype, premium, start)
        if key in seen:
            continue
        seen.add(key)
        blocks.append((ptype, premium, pick_tier(tiers, mo, use), tiers, mo, phrase.strip()))

    def choose(cands):
        if not cands:
            return None
        return max(cands, key=lambda b: b[2]["apy"]) if use == "highest" else cands[0]

    result = {}
    for ptype in dict.fromkeys(b[0] for b in blocks):
        same = [b for b in blocks if b[0] == ptype]
        if ptype == "money_market":
            plain = [b for b in same if not b[1]]
            prem = [b for b in same if b[1]]
            if plain:
                result["money_market"] = choose(plain[:1] if use != "highest" else plain)
                hy = choose(prem)
                if hy is not None and "high_yield_savings" not in result:
                    result["high_yield_savings"] = hy
            else:
                result["money_market"] = choose(prem)
        else:
            result.setdefault(ptype, choose(same[:1]) if use != "highest" else choose(same))
    out = {}
    for ptype, b in result.items():
        _p, _prem, pick, tiers, mo, name = b
        out[ptype] = {"apy": pick["apy"], "min_open": mo, "name": name, "tiers": tiers,
                      "note": _block_note(use, pick, tiers, mo, name)}
    return out


# ── multi-product rate tables ─────────────────────────────────────────────────
# A real, common shape: ONE page, a section per product family (Checking,
# Savings, Money Market, CDs), each a table "Product | Balance Tiers to Obtain
# APY | Interest Rate | APY" whose product name appears on the first row of its
# tiers only, and whose APY label is in the header, not on the rows. innerText
# renders it as tab-separated cells, one row per line.

_CELL_SPLIT = re.compile(r"\t+| {3,}")
_PCT_ALL = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_SECTION_HEADS = [
    ("money_market", re.compile(r"^\W*(?:personal\s+)?money\s+market(?:\s+(?:accounts?|rates?))?\W*$", re.I)),
    ("high_yield_savings", re.compile(r"^\W*high[\s-]?yield\s+savings(?:\s+(?:accounts?|rates?))?\W*$", re.I)),
    ("savings", re.compile(r"^\W*(?:personal\s+)?savings(?:\s+(?:accounts?|rates?))?\W*$", re.I)),
    ("checking", re.compile(r"^\W*(?:personal\s+)?checking(?:\s+(?:accounts?|rates?))?\W*$", re.I)),
    ("cd", re.compile(r"^\W*(?:cds?|certificates?\s+of\s+deposit(?:\s+rates?)?|cd\s+rates?)\W*$", re.I)),
]
_TERM_CELL = re.compile(r"^\s*(\d{1,3})\s*[- ]?\s*(month|mo|year|yr)s?\b", re.I)
_MONEY = r"\$\s*([\d,]+(?:\.\d+)?)"


def _tier_from_cell(cell):
    """(lo, hi) balance band from a table cell ('$0.01 and above', 'up to $99,999.99',
    '$2,000 - $9,999.99', '$100,000+'), or None when the cell is not a balance band."""
    c = cell.strip()
    m = re.search(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY, c, re.I)
    if m:
        return _money(m.group(1)), _money(m.group(2))
    m = re.search(r"(?:up\s+to|under|less\s+than|below)\s*" + _MONEY, c, re.I) or re.search(_MONEY + r"\s*(?:or\s+less|and\s+(?:under|below))", c, re.I)
    if m:
        return 0.0, _money(m.group(1))
    m = re.search(_MONEY + r"\s*(?:\+|(?:(?:or|and)\s+)?(?:above|over|more|up))", c, re.I) or re.search(r"(?:over|above|from|at\s+least|greater\s+than)\s*" + _MONEY, c, re.I)
    if m:
        return _money(m.group(1)), None
    m = re.fullmatch(r"\s*" + _MONEY + r"\s*", c)
    if m:
        return _money(m.group(1)), None
    return None


def _parse_table_rows(text):
    """Rows of every recognised product table: dicts with section, name, lo, hi, apy and,
    for a CD row, term (e.g. '6-month')."""
    rows, section, last_name = [], None, None
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        head = next((p for p, pat in _SECTION_HEADS if len(s) <= 40 and pat.match(s)), None)
        if head:
            section, last_name = head, None
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(s) if c.strip()]
        if section is None or len(cells) < 2:
            continue
        pcts = [(i, float(m.group(1))) for i, c in enumerate(cells) for m in [_PCT_ALL.search(c)] if m]
        if not pcts:
            continue
        apy = pcts[-1][1]
        term_cell = next((c for c in cells if _TERM_CELL.match(c)), None)
        if section == "cd":
            if term_cell:
                m = _TERM_CELL.match(term_cell)
                n = int(m.group(1))
                rows.append({"section": "cd", "name": None, "lo": 0.0, "hi": None, "apy": apy,
                             "term": f"{n * 12 if m.group(2).lower().startswith('y') else n}-month"})
            continue
        band = None
        for c in cells:
            if "%" in c:
                continue
            band = _tier_from_cell(c)
            if band:
                break
        first = cells[0]
        if "%" not in first and "$" not in first and re.search(r"[A-Za-z]{3}", first) and not _tier_from_cell(first):
            last_name = first
        if band is None and "$" in s:
            continue   # a row with dollar amounts we cannot read as a band: not a rate row
        lo, hi = band if band else (0.0, None)
        rows.append({"section": section, "name": last_name, "lo": lo, "hi": hi, "apy": apy, "term": None})
    return rows


def extract_rate_tables(text, mode=None):
    """Parse multi-product rate tables and apply the one tier rule (tier_mode(), or `mode`).

    Returns {product_type: {"apy", "min_open", "name", "tiers", "note"}} for
    checking / savings / high_yield_savings / money_market. A product's tiers
    are its rows; the product reports its tier by the rule. Across the products
    of one type the highest-rate product is reported under 'highest', the first
    listed under 'entry'. The note names the product and its band and lists the
    other tiers and the other products. CD rows are returned by
    extract_table_cd_rows()."""
    use = mode or tier_mode()
    groups = {}   # (section, name) -> tiers
    order = []
    for r in _parse_table_rows(text):
        if r["section"] == "cd" or r["apy"] <= 0:   # a 0.00% tier is not a rate
            continue
        key = (r["section"], r["name"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({"lo": r["lo"], "hi": r["hi"], "apy": r["apy"]})
    out = {}
    for ptype in dict.fromkeys(k[0] for k in order):
        prods = []
        for key in [k for k in order if k[0] == ptype]:
            tiers = groups[key]
            prods.append((key[1], tiers, pick_tier(tiers, None, use)))
        chosen = max(prods, key=lambda x: x[2]["apy"]) if use == "highest" else prods[0]
        name, tiers, pick = chosen
        note = _block_note(use, pick, tiers, None, name or "")
        others = [f"{n or ptype}: {_tier_label(p)}" for n, _t, p in prods if (n, _t, p) != chosen]
        if others:
            note += "; other products: " + "; ".join(others[:6])
        out[ptype] = {"apy": pick["apy"], "min_open": None, "name": name or "", "tiers": tiers, "note": note}
    return out


def extract_table_cd_rows(text):
    """[(term like '6-month', apy)] from the CD rows of a product table."""
    return [(r["term"], r["apy"]) for r in _parse_table_rows(text) if r["section"] == "cd" and r["term"]]


# ── a link to the page that has the rates ─────────────────────────────────────

RATES_PAGE_LINK_PAT = re.compile(
    r"(?:\b(?:view|see|check|compare|current|today'?s?|our)\s+(?:[\w\-]+\s+){0,2}(?:rates?|apys?)\b)"
    r"|(?:/(?:[\w\-]*-)?rates?(?:[/?#.\s]|$))|(?:rate[\s\-]?sheet)",
    re.I)


_LOAN_LINK_PAT = re.compile(r"mortgage|loan|heloc|\bauto\b|credit[\s-]?card|home[\s-]?equity|student|borrow|refinanc", re.I)


def is_rates_page_link(href, text):
    """True when a link's address or anchor text points at a page of DEPOSIT rates ('see rates',
    '/personal/checking-savings/interest-rates', '/rates'); a loan or mortgage rates link is not."""
    s = f"{href or ''} {text or ''}"
    return bool(RATES_PAGE_LINK_PAT.search(s)) and not _LOAN_LINK_PAT.search(s)


# ── what the page says about itself: geographic scope and as-of date ──────────
# "Interest rates and Annual Percentage Yield (APY) shown below are for the state of
# South Carolina. Interest rates are current as of 03/01/2026." A rate that is
# regional and dated is not "the bank's rate today": the scope and the date are
# captured with the raw text, and a page older than the limit is flagged stale.

import datetime as _dt

STALE_DAYS_ENV = "RATE_RADAR_STALE_DAYS"
DEFAULT_STALE_DAYS = 60
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_NAME = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_ANY = (r"(?P<num>\d{1,2}[/\-.]\d{1,2}[/\-.](?:\d{4}|\d{2}))"
             r"|(?P<mdy>" + _MONTH_NAME + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
             r"|(?P<dmy>\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_NAME + r"\.?,?\s+\d{4})")
_AS_OF_PAT = re.compile(
    r"(?:current\s+as\s+of|accurate\s+as\s+of|as\s+of|effective(?:\s+date)?|rates?\s+(?:were\s+)?updated(?:\s+on)?|last\s+updated(?:\s+on)?)"
    r"\s*:?\s*(?:on\s+)?(?:" + _DATE_ANY + r")", re.I)
_SCOPE_PAT = re.compile(
    r"(?i:(?:rates?|apys?|annual\s+percentage\s+yield)[^.\n]{0,100}?\b(?:are|apply|applies)\s+(?:for|to|in)\s+(?:the\s+)?"
    r"(?:state\s+of\s+|commonwealth\s+of\s+|region\s+of\s+)?)(?P<s>[A-Z][A-Za-z]+(?:\s+(?:of\s+)?[A-Z][A-Za-z]+){0,2})(?![A-Za-z])")


_ZIP_PAT = re.compile(r"(?i:showing\s+(?:information|rates|rate\s+information)\s+for\s+zip(?:\s+code)?)\s*:?\s*(?P<z>\d{5})(?:-\d{4})?")
_SCOPE_PAT2 = re.compile(r"(?i:(?:showing|viewing|displaying)\s+(?:information|rates)\s+for\s+(?:the\s+)?(?:state\s+of\s+))(?P<s>[A-Z][A-Za-z]+(?:\s+(?:of\s+)?[A-Z][A-Za-z]+){0,2})(?![A-Za-z])")

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_STATE_ABBRS = set(STATE_NAMES.values())


def state_abbr(value):
    """Two-letter US state code from a state name or code ('South Carolina' / 'sc' -> 'SC'), else None."""
    s = re.sub(r"\s+", " ", (value or "").strip().lower().rstrip("."))
    if s in STATE_NAMES:
        return STATE_NAMES[s]
    return s.upper() if s.upper() in _STATE_ABBRS and len(s) == 2 else None


def stale_days():
    """The staleness limit in days (environment variable RATE_RADAR_STALE_DAYS, default 60)."""
    try:
        v = int((os.environ.get(STALE_DAYS_ENV) or "").strip())
        return v if v > 0 else DEFAULT_STALE_DAYS
    except ValueError:
        return DEFAULT_STALE_DAYS


def parse_as_of(raw):
    """A page's as-of date from its raw text, or None. STRICT about order: a numeric date is
    read month/day/year (the order US banks write); it is NEVER reinterpreted day-first,
    so '25/03/2026' (month 25) and impossible dates give None, and the caller keeps the raw
    text. Named months ('March 1, 2026', '1 March 2026') are unambiguous. Two-digit years
    mean 20YY."""
    s = (raw or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})", s)
    try:
        if m:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _dt.date(y + 2000 if y < 100 else y, mo, d)
        m = re.fullmatch(r"(" + _MONTH_NAME + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s, re.I)
        if m:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(1).lower()[:4].rstrip("t") if m.group(1).lower().startswith("sept") else m.group(1).lower()[:3]], int(m.group(2)))
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_NAME + r")\.?,?\s+(\d{4})", s, re.I)
        if m:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
    except (ValueError, KeyError):
        return None
    return None


def find_rate_page_meta(text):
    """What a rate page says about its own scope and date, or None when it says neither:
    {"scope": "South Carolina" | None, "scope_raw": the sentence, "as_of": "2026-03-01" | None,
    "as_of_raw": "03/01/2026" | None, "as_of_order": "month/day/year" | "named month" | None}.
    The raw text is always kept, whether or not it could be parsed."""
    text = text or ""
    scope = scope_raw = as_of = as_of_raw = order = None
    zipc = None
    mz = _ZIP_PAT.search(text)
    if mz:
        zipc = mz.group("z")
    m = _SCOPE_PAT.search(text) or _SCOPE_PAT2.search(text)
    if m:
        scope = m.group("s").strip()
        a = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        b = text.find(".", m.end())
        scope_raw = re.sub(r"\s+", " ", text[a:(b + 1 if b != -1 else m.end())]).strip()
    m = _AS_OF_PAT.search(text)
    if m:
        as_of_raw = next(g for g in (m.group("num"), m.group("mdy"), m.group("dmy")) if g)
        d = parse_as_of(as_of_raw)
        as_of = d.isoformat() if d else None
        order = ("month/day/year" if m.group("num") else "named month") if d else None
    if scope is None and as_of_raw is None and zipc is None:
        return None
    return {"scope": scope, "scope_raw": scope_raw, "zip": zipc, "as_of": as_of, "as_of_raw": as_of_raw, "as_of_order": order}


def page_age_days(meta, today=None):
    """Days between the page's as-of date and today (negative when it is in the future), or None."""
    if not meta or not meta.get("as_of"):
        return None
    return ((today or _dt.date.today()) - _dt.date.fromisoformat(meta["as_of"])).days


def is_stale(meta, today=None, days=None):
    """True when the page's own as-of date is MORE than `days` (default stale_days()) old."""
    age = page_age_days(meta, today)
    return age is not None and age > (days if days is not None else stale_days())


def home_state_of(result):
    """The bank's home state code from a result: its 'state' field, else the two letters before
    a ZIP in its branch address."""
    for key in ("state", "bank_state", "State"):
        s = state_abbr(result.get(key))
        if s:
            return s
    m = re.search(r",?\s+([A-Z]{2})\s+\d{5}", result.get("branch_address") or "")
    return state_abbr(m.group(1)) if m else None


def scope_flags(meta, home_state=None, run_majority=None):
    """Review flags for a page's scope: 'regional_scope' (it states one), 'scope_differs_from_home_state'
    (a state other than the bank's home state), 'scope_differs_from_run' (a state other than the
    one most pages of the run state). A page that picks its state from the visitor's location
    (the same URL showed two states on the same day) is why the scope is recorded and never
    assumed to be 'the bank's rate'."""
    flags = []
    if not meta or not meta.get("scope"):
        return flags
    flags.append("regional_scope")
    sc = state_abbr(meta["scope"])
    if sc and home_state and sc != home_state:
        flags.append("scope_differs_from_home_state")
    if sc and run_majority and sc != run_majority:
        flags.append("scope_differs_from_run")
    return flags


def meta_note(meta):
    """The scope and date of a page as note text: 'scope: South Carolina; rates dated 03/01/2026 (read month/day/year)'."""
    if not meta:
        return ""
    parts = []
    if meta.get("scope"):
        parts.append(f"scope: {meta['scope']}")
    if meta.get("zip"):
        parts.append(f"shown for zip {meta['zip']}")
    if meta.get("as_of_raw"):
        how = f" (read {meta['as_of_order']})" if meta.get("as_of_order") else " (date not understood)"
        parts.append(f"rates dated {meta['as_of_raw']}{how}")
    return "; ".join(parts)


# ── identity: does the page belong to this bank? ──────────────────────────────

_NAME_GENERIC = {
    "bank", "banks", "national", "association", "na", "n.a", "fsb", "f.s.b", "fa", "f.a", "ssb", "savings",
    "federal", "financial", "trust", "company", "co", "the", "of", "and", "inc", "corp", "corporation",
    "bancorp", "bancshares", "group", "holdings", "state", "community", "usa", "us", "credit", "union", "loan",
    "thrift", "bk", "banking", "services",
}


def name_tokens(bank_name):
    """Distinctive, lower-case tokens of a bank's name ('Colonial Savings, F.A.' ->
    ['colonial']). When a name is nothing but generic words, every word is used."""
    words = [w for w in re.split(r"[^a-z0-9.]+", (bank_name or "").lower()) if w]
    cleaned = [w.strip(".") for w in words]
    toks = [w for w in cleaned if w and w not in _NAME_GENERIC and not w.isdigit()]
    return toks or [w for w in cleaned if w and w not in {"the", "of", "and"}]


def _squash(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def check_identity(bank_name, bank_url, page_title="", site_name="", page_text=""):
    """'ok' when the page, its title/site name or the bank's own domain names the
    bank; 'mismatch' when a page with text never names it (for example a site
    branded for a different bank than the one on file); 'unknown' when there
    is no text to judge by. Matching ignores spaces and punctuation, so
    'Southstate' and 'South State' agree."""
    toks = [_squash(t) for t in name_tokens(bank_name)]
    toks = [t for t in toks if len(t) >= 3]
    if not toks:
        return "unknown"
    hay = _squash(" ".join([page_title or "", site_name or "", page_text or ""]))
    dom = _squash(domain_of(bank_url))
    if any(t in hay or t in dom for t in toks):
        return "ok"
    if not hay:
        return "unknown"
    return "mismatch"


# ── does the bank publish this rate at all? ───────────────────────────────────

_NOT_PUBLISHED_PAT = re.compile(
    r"(?:inquire|inquiry|ask|call|contact\s+(?:us|a\s+banker|your\s+local\s+branch)|visit\s+(?:us|a\s+branch)|speak\s+with[^.\n]{0,30})"
    r"[^.\n]{0,25}\b(?:for|about)\s+(?:current\s+|our\s+)?rates?\b"
    r"|rates?\s+(?:are\s+)?(?:available\s+)?(?:upon|on)\s+request"
    r"|rates?\s+(?:are\s+)?(?:not\s+(?:published|posted|listed)|vary\s+by\s+(?:location|branch))"
    r"|please\s+(?:call|contact|inquire)[^.\n]{0,40}\brates?\b",
    re.I)
_PRODUCT_WORDS = [
    ("checking", re.compile(r"interest[\s-]bearing|checking", re.I)),
    ("money_market", re.compile(r"money\s+market", re.I)),
    ("savings", re.compile(r"savings", re.I)),
    ("cd", re.compile(r"certificates?(?:\s+of\s+deposit)?|\bCDs?\b", re.I)),
]


def find_not_published(text):
    """Like detect_not_published, with the evidence: {product_type or 'all': the
    sentence fragment that says so (first occurrence, collapsed whitespace; the window is
    bounded by the pattern, 60 characters before to 20 after)}. The evidence is stored with the outcome so a person
    can see why the bank was marked as not publishing."""
    out = {}
    text = text or ""
    for m in _NOT_PUBLISHED_PAT.finditer(text):
        before = text[max(0, m.start() - 80):m.end()]
        hit = None
        for ptype, pat in _PRODUCT_WORDS:
            for pm in pat.finditer(before):
                if hit is None or pm.start() > hit[0]:
                    hit = (pm.start(), ptype)
        key = hit[1] if hit else "all"
        if key not in out:
            out[key] = re.sub(r"\s+", " ", text[max(0, m.start() - 60):m.end() + 20]).strip()
    return out


def detect_not_published(text):
    """Products a page says the bank does not publish a rate for ('Interest
    Bearing: Inquire for Rates', 'rates available upon request'). Returns a set
    of product types; the string 'all' when the statement names no product."""
    return set(find_not_published(text))
