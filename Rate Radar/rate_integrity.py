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
#   "entry"   (default) the rate that applies at the product's minimum opening
#             balance; with no stated minimum, the lowest-balance tier;
#   "highest" the highest rate advertised for the product (the top tier).
# Either way the other tiers are listed in tier_note and the value carries the
# 'tiered' flag. Anything else in the variable falls back to the default.
TIER_RULES = {
    "entry": ("When a product pays different rates by balance tier, report the rate that applies at the "
              "product's minimum opening balance; if no opening minimum is stated, report the lowest-balance "
              "tier. Mention the other tiers in a note; never report the top tier as the product's rate."),
    "highest": ("When a product pays different rates by balance tier, report the HIGHEST advertised rate "
                "for that product (its top tier). Mention the other tiers in a note."),
}
DEFAULT_TIER_RULE = "entry"
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

def resolve(entry, bank_url=None, identity=None, edited=False):
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
        return f"{t['apy']:.2f}% over {_fmt_money(t['lo'])}"
    if t["lo"] == 0:
        return f"{t['apy']:.2f}% up to {_fmt_money(t['hi'])}"
    return f"{t['apy']:.2f}% {_fmt_money(t['lo'])} to {_fmt_money(t['hi'])}"


def extract_tiered_products(text, window=700, cluster_gap=60, mode=None):
    """Find balance-tiered rate blocks and apply the one tier rule (tier_mode(),
    or `mode` when given).

    Returns {product_type: {"apy", "min_open", "name", "tiers", "note"}} with at
    most one entry per product type (the first block on the page). A block is a
    run of tier lines (each '<balance bracket> -- N.NN% APY', or a table row
    '(<bracket>) <rate> <APY>') whose product is the nearest product keyword
    within `window` characters before it."""
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
    result = {}
    for cl in clusters:
        start = cl[0][0]
        before = text[max(0, start - window):start]
        best = None   # (position in `before`, product_type, phrase)
        for ptype, pat in _PRODUCT_KW:
            for m in pat.finditer(before):
                if best is None or m.start() > best[0] or (m.start() == best[0] and m.end() > best[3]):
                    best = (m.start(), ptype, m.group(0), m.end())
        if best is None:
            continue
        ptype, phrase = best[1], best[2]
        if ptype in result:
            continue
        if ptype == "savings" and re.search(r"high[\s-]?yield", before[max(0, best[0] - 15):best[0] + 20], re.I):
            ptype = "high_yield_savings"
            if ptype in result:
                continue
        mo = None
        for m in _MIN_OPEN.finditer(before):
            mo = _money(m.group(1))
        tiers = [{"lo": c[2], "hi": c[3], "apy": c[4]} for c in cl]
        use = mode or tier_mode()
        pick = pick_tier(tiers, mo, use)
        others = [_tier_label(t) for t in sorted(tiers, key=lambda t: t["lo"]) if t is not pick]
        why = ("highest advertised rate" if use == "highest" else
               ("rate that applies at the " + _fmt_money(mo) + " opening minimum" if mo is not None
                else "rate that applies at the lowest balance tier"))
        note = (f"tiered: {_tier_label(pick)} ({why})"
                + (f"; other tiers: {'; '.join(others)}" if others else ""))
        result[ptype] = {"apy": pick["apy"], "min_open": mo, "name": phrase.strip(), "tiers": tiers, "note": note}
    return result


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
