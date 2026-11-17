#!/usr/bin/env python3
"""Mutation check for the rate-integrity rules (python tests/integrity/run_mutations.py, from the "Rate Radar" folder).

For each mutation the Rate Radar folder is COPIED to a temp directory (the real files are never touched), one rule, threshold or wiring line is changed
in the copy, and the unittest suite in tests/integrity must FAIL. A mutation the suite does not catch is reported as MISS (the exit code is then 1).
Equivalent mutants, if any, are listed in EQUIVALENT with the reason; there must be none that are unexplained.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # the "Rate Radar" folder
PY = sys.executable

# (name, file, old, new)
M = [
    # ---- resolve(): confidence rules
    ("manual path no longer high", "rate_integrity.py", "        return SITE, HIGH, (entry or {}).get(\"url\"), []", "        return SITE, MEDIUM, (entry or {}).get(\"url\"), []"),
    ("no recorded source becomes site/high", "rate_integrity.py", "        return SEARCH, LOW, None, [\"source_unknown\"]\n    method, url", "        return SITE, HIGH, None, []\n    method, url"),
    ("search value no longer low", "rate_integrity.py", "        return SEARCH, LOW, entry.get(\"label\") or None, [\"search_only\"]", "        return SEARCH, MEDIUM, entry.get(\"label\") or None, [\"search_only\"]"),
    ("search value borrows the page url", "rate_integrity.py", "        return SEARCH, LOW, entry.get(\"label\") or None, [\"search_only\"]", "        return SEARCH, LOW, url or entry.get(\"label\"), [\"search_only\"]"),
    ("last-known-good no longer low", "rate_integrity.py", "        return LAST_KNOWN_GOOD, LOW, None, [\"last_known_good\"]", "        return LAST_KNOWN_GOOD, MEDIUM, None, [\"last_known_good\"]"),
    ("aggregator becomes high", "rate_integrity.py", "        return AGGREGATOR, MEDIUM, url, []", "        return AGGREGATOR, HIGH, url, []"),
    ("aggregator becomes low", "rate_integrity.py", "        return AGGREGATOR, MEDIUM, url, []", "        return AGGREGATOR, LOW, url, []"),
    ("ai value in the page text becomes high", "rate_integrity.py", "        if entry.get(\"verified\"):\n            conf = MEDIUM\n        else:\n            conf, flags = LOW, [\"not_in_page_text\"]",
     "        if entry.get(\"verified\"):\n            conf = HIGH\n        else:\n            conf, flags = LOW, [\"not_in_page_text\"]"),
    ("ai value not in the page text is medium", "rate_integrity.py", "            conf, flags = LOW, [\"not_in_page_text\"]\n        if identity == \"mismatch\":\n            conf, flags = LOW, flags + [\"identity_mismatch\"]\n        return AI_VISION",
     "            conf, flags = MEDIUM, [\"not_in_page_text\"]\n        if identity == \"mismatch\":\n            conf, flags = LOW, flags + [\"identity_mismatch\"]\n        return AI_VISION"),
    ("ai value ignores a identity mismatch", "rate_integrity.py", "        if identity == \"mismatch\":\n            conf, flags = LOW, flags + [\"identity_mismatch\"]\n        return AI_VISION", "        return AI_VISION"),
    ("site value without a url stays site", "rate_integrity.py", "        if not url:\n            return SEARCH, LOW, None, [\"source_unknown\"]\n        conf = HIGH", "        conf = HIGH"),
    ("site value not verified in text stays high", "rate_integrity.py", "        if not entry.get(\"verified\"):\n            conf, flags = MEDIUM, [\"not_in_page_text\"]\n        if bank_url", "        if bank_url"),
    ("off-domain site value stays high", "rate_integrity.py", "        if bank_url and not same_site(url, bank_url):\n            conf, flags = MEDIUM, flags + [\"off_domain\"]\n", ""),
    ("site value ignores an identity mismatch", "rate_integrity.py", "        if identity == \"mismatch\":\n            conf, flags = LOW, flags + [\"identity_mismatch\"]\n        return SITE, conf, url, flags", "        return SITE, conf, url, flags"),
    ("identity mismatch only medium", "rate_integrity.py", "            conf, flags = LOW, flags + [\"identity_mismatch\"]\n        return SITE, conf, url, flags", "            conf, flags = MEDIUM, flags + [\"identity_mismatch\"]\n        return SITE, conf, url, flags"),
    ("unknown method becomes high", "rate_integrity.py", "    return SEARCH, LOW, None, [\"source_unknown\"]\n\n\ndef legacy_entry", "    return SITE, HIGH, url, []\n\n\ndef legacy_entry"),
    # ---- legacy_entry()
    ("legacy note match is a plain substring", "rate_integrity.py", "    if re.search(r\"(?<![a-z_])\" + re.escape(product_type) + r\" from search\", note):", "    if (product_type + \" from search\") in note:"),
    ("legacy last-known-good ignored", "rate_integrity.py", "    if \"no fresh data found today\" in note:\n        return make_source(LAST_KNOWN_GOOD)\n", ""),
    ("legacy [AI] url treated as a site value", "rate_integrity.py", "        return make_source(AI_VISION, url=su[4:].strip(), verified=False)", "        return make_source(SITE, url=su[4:].strip(), verified=True)"),
    ("legacy aggregator url treated as a site value", "rate_integrity.py", "    if \"depositaccounts\" in su.lower():\n        return make_source(AGGREGATOR, url=su)\n", ""),
    ("legacy http url not verified", "rate_integrity.py", "        return make_source(SITE, url=su, verified=True)\n    return None", "        return make_source(SITE, url=su, verified=False)\n    return None"),
    ("legacy no source borrows a site entry", "rate_integrity.py", "        return make_source(SITE, url=su, verified=True)\n    return None", "        return make_source(SITE, url=su, verified=True)\n    return make_source(SEARCH)"),
    # ---- value_in_text / verify_ai_values
    ("value_in_text ignores the digit before the number", "rate_integrity.py", "        if re.search(r\"(?<![\\d.])\" + re.escape(f) + r\"0*\\s*%\", text):", "        if re.search(re.escape(f) + r\"0*\\s*%\", text):"),
    ("value_in_text needs no percent sign", "rate_integrity.py", "r\"0*\\s*%\", text):", "r\"0*\", text):"),
    ("value_in_text always true", "rate_integrity.py", "    if value is None or not text:\n        return False", "    if value is None:\n        return True"),
    ("a page with rates is no longer 'rich'", "rate_integrity.py", "def page_has_rates(text, minimum=3):\n    \"\"\"True when the text shows at least `minimum` percentage values.\"\"\"\n    return len(re.findall(r\"\\d+\\.\\d+\\s*%\", text or \"\")) >= minimum",
     "def page_has_rates(text, minimum=3):\n    \"\"\"True when the text shows at least `minimum` percentage values.\"\"\"\n    return False"),
    ("image-only pages drop every value", "rate_integrity.py", "        elif rich:\n            dropped.append((k, v))\n            kept[k] = None\n        else:\n            verified[k] = False", "        else:\n            dropped.append((k, v))\n            kept[k] = None"),
    ("unverified values on rich pages are kept", "rate_integrity.py", "        elif rich:\n            dropped.append((k, v))\n            kept[k] = None\n        else:", "        elif False:\n            dropped.append((k, v))\n            kept[k] = None\n        else:"),
    ("cd term kept when cd is dropped", "rate_integrity.py", "    if kept.get(\"cd\") is None:\n        kept[\"cd_term\"] = None\n", ""),
    ("ladder not filtered", "rate_integrity.py", "        good = [e for e in ladder if value_in_text(e.get(\"apy\"), page_text)] if rich else list(ladder)", "        good = list(ladder)"),
    ("verified flag lost for found values", "rate_integrity.py", "        if value_in_text(v, page_text):\n            verified[k] = True", "        if value_in_text(v, page_text):\n            verified[k] = False"),
    # ---- tiers
    ("tier rule picks the top tier", "rate_integrity.py", "    return ordered[0]\n\n\ndef pick_highest_tier", "    return ordered[-1]\n\n\ndef pick_highest_tier"),
    ("tier rule ignores the opening minimum", "rate_integrity.py", "    if min_open is not None:\n        for t in ordered:", "    if False:\n        for t in ordered:"),
    ("opening minimum at a tier ceiling falls out of it", "rate_integrity.py", "(t[\"hi\"] is None or min_open <= t[\"hi\"])", "(t[\"hi\"] is None or min_open < t[\"hi\"])"),
    ("opening minimum below the tier floor accepted", "rate_integrity.py", "        for t in ordered:\n            if t[\"lo\"] <= min_open and", "        for t in ordered:\n            if True and"),
    ("tiers not ordered before picking", "rate_integrity.py", "    ordered = sorted(tiers, key=lambda t: t[\"lo\"])", "    ordered = list(tiers)"),
    ("only the last tier block per product is used", "rate_integrity.py", "        if ptype in result:\n            continue\n        if ptype == \"savings\"", "        if False:\n            continue\n        if ptype == \"savings\""),
    ("opening minimum not read", "rate_integrity.py", "        mo = None\n        for m in _MIN_OPEN.finditer(before):\n            mo = _money(m.group(1))", "        mo = None"),
    ("tier lines far apart merged into one block", "rate_integrity.py", "def extract_tiered_products(text, window=700, cluster_gap=60, mode=None):", "def extract_tiered_products(text, window=700, cluster_gap=100000, mode=None):"),
    ("product keyword window too small", "rate_integrity.py", "def extract_tiered_products(text, window=700, cluster_gap=60, mode=None):", "def extract_tiered_products(text, window=20, cluster_gap=60, mode=None):"),
    ("tier lines with no product keyword are kept", "rate_integrity.py", "        if best is None:\n            continue\n        ptype, phrase", "        if best is None:\n            best = (0, \"savings\", \"savings\", 0)\n        ptype, phrase"),
    ("range tier pattern removed", "rate_integrity.py", "    (\"range\", re.compile(_NUM + r\"\\s*(?:to|-|–|—)\\s*\" + _NUM + _SEP + _RATE, re.I)),\n", ""),
    ("'or less' tier pattern removed", "rate_integrity.py", "    (\"upto\", re.compile(_NUM + r\"\\s*(?:or\\s+)?(?:less|under|and\\s+under)\" + _SEP + _RATE, re.I)),\n", ""),
    ("'over' tier pattern removed", "rate_integrity.py", "    (\"over\", re.compile(r\"(?:balances?\\s+)?(?:over|above|greater\\s+than)\\s*\" + _NUM + _SEP + _RATE, re.I)),\n", ""),
    ("'or more' / 'and over' tier pattern removed", "rate_integrity.py", "    (\"over\", re.compile(_NUM + r\"\\s*(?:(?:or|and)\\s+)?(?:more|over|above)\" + _SEP + _RATE, re.I)),\n", ""),
    ("'and over' no longer accepted", "rate_integrity.py", "r\"\\s*(?:(?:or|and)\\s+)?(?:more|over|above)\"", "r\"\\s*(?:or\\s+)?(?:more|over|above)\""),
    ("table row patterns removed", "rate_integrity.py", "_TABLE_PATS = [", "_TABLE_PATS = []\n_UNUSED_TABLE_PATS = ["),
    ("table row takes the interest rate not the APY", "rate_integrity.py", "            n, _rate, apy = _money(m.group(1)), m.group(2), float(m.group(3))", "            n, apy, _rate = _money(m.group(1)), float(m.group(2)), m.group(3)"),
    ("tier note does not list the other tiers", "rate_integrity.py", "        others = [_tier_label(t) for t in sorted(tiers, key=lambda t: t[\"lo\"]) if t is not pick]", "        others = []"),
    ("tier note does not say which rule applied", "rate_integrity.py", "else \"rate that applies at the lowest balance tier\"", "else \"x\""),
    # ---- identity
    ("identity: no token ever matches", "rate_integrity.py", "    if any(t in hay or t in dom for t in toks):\n        return \"ok\"", "    if False:\n        return \"ok\""),
    ("identity: a different bank's page is ok", "rate_integrity.py", "    if not hay:\n        return \"unknown\"\n    return \"mismatch\"", "    return \"ok\""),
    ("identity: no text is a mismatch", "rate_integrity.py", "    if not hay:\n        return \"unknown\"\n    return \"mismatch\"", "    return \"mismatch\""),
    ("identity: the bank's domain is ignored", "rate_integrity.py", "    if any(t in hay or t in dom for t in toks):", "    if any(t in hay for t in toks):"),
    ("identity: spacing matters", "rate_integrity.py", "def _squash(s):\n    return re.sub(r\"[^a-z0-9]+\", \"\", (s or \"\").lower())", "def _squash(s):\n    return (s or \"\").lower()"),
    ("identity: short tokens count", "rate_integrity.py", "    toks = [t for t in toks if len(t) >= 3]\n", ""),
    ("identity: generic words count as tokens", "rate_integrity.py", "    toks = [w for w in cleaned if w and w not in _NAME_GENERIC and not w.isdigit()]", "    toks = [w for w in cleaned if w]"),
    ("identity: nothing but generic words gives no tokens", "rate_integrity.py", "    return toks or [w for w in cleaned if w and w not in {\"the\", \"of\", \"and\"}]", "    return toks"),
    # ---- not published
    ("not-published detector off", "rate_integrity.py", "    for m in _NOT_PUBLISHED_PAT.finditer(text):", "    for m in []:"),
    ("not-published product never named", "rate_integrity.py", "        key = hit[1] if hit else \"all\"", "        key = \"all\""),
    ("not-published always names the product nearest after", "rate_integrity.py", "        before = text[max(0, m.start() - 80):m.end()]", "        before = text[m.start():m.end() + 80]"),
    ("'upon request' phrase removed", "rate_integrity.py", "    r\"|rates?\\s+(?:are\\s+)?(?:available\\s+)?(?:upon|on)\\s+request\"\n", "    r\"\"\n"),
    # ---- sources
    ("nerdwallet no longer blocked", "rate_integrity.py", "BLOCKED_SOURCE_DOMAINS = (\"nerdwallet.com\",)", "BLOCKED_SOURCE_DOMAINS = ()"),
    ("blocked-source check is case sensitive", "rate_integrity.py", "    s = (domain_or_url or \"\").lower()\n    return any(b in s for b in BLOCKED_SOURCE_DOMAINS)", "    s = (domain_or_url or \"\")\n    return any(b in s for b in BLOCKED_SOURCE_DOMAINS)"),
    ("same_site accepts a look-alike domain", "rate_integrity.py", "    return bool(d and b and (d == b or d.endswith(\".\" + b) or b.endswith(\".\" + d)))", "    return bool(d and b and (b in d or d in b))"),
    ("same_site always false", "rate_integrity.py", "    return bool(d and b and (d == b or d.endswith(\".\" + b) or b.endswith(\".\" + d)))", "    return False"),
    ("entry rule text loses the opening-minimum rule", "rate_integrity.py", "product's minimum opening balance; if no opening minimum is stated, report the lowest-balance \"", "product's top balance; if no opening minimum is stated, report the highest-balance \""),
    # ---- storage.build_rate_observations wiring
    ("provenance map ignored", "storage.py", "            entry = prov.get(product_type) or legacy_entry(product_type, specific_url, note)", "            entry = legacy_entry(product_type, specific_url, note)"),
    ("legacy fallback ignored", "storage.py", "            entry = prov.get(product_type) or legacy_entry(product_type, specific_url, note)", "            entry = prov.get(product_type)"),
    ("money market has no source field again", "storage.py", "(\"money_market\", r.get(\"money_market_apy\"), r.get(\"source_url_money_market\"), None),", "(\"money_market\", r.get(\"money_market_apy\"), None, None),"),
    ("identity not passed to resolve", "storage.py", "resolve(entry, bank_url=bank_url, identity=identity, edited=edited)", "resolve(entry, bank_url=bank_url, identity=None, edited=edited)"),
    ("bank url not passed to resolve", "storage.py", "resolve(entry, bank_url=bank_url, identity=identity, edited=edited)", "resolve(entry, bank_url=None, identity=identity, edited=edited)"),
    ("edited flag not passed to resolve", "storage.py", "resolve(entry, bank_url=bank_url, identity=identity, edited=edited)", "resolve(entry, bank_url=bank_url, identity=identity, edited=False)"),
    ("manual save keeps its own url no more", "storage.py", "            if edited:\n                url = specific_url or source_url\n", ""),
    ("not-published guard removed", "storage.py", "            if not edited and method in (SEARCH, LAST_KNOWN_GOOD) and (", "            if False and (method in (SEARCH, LAST_KNOWN_GOOD)) and ("),
    ("not-published guard also drops site values", "storage.py", "            if not edited and method in (SEARCH, LAST_KNOWN_GOOD) and (", "            if not edited and (\n"),
    ("not-published high-yield link to savings removed", "storage.py", "                    or (product_type == \"high_yield_savings\" and \"savings\" in not_pub)):", "                    ):"),
    ("extra columns always added", "storage.py", "            if extra_cols:\n                row[\"review_flags\"]", "            if True:\n                row[\"review_flags\"]"),
    ("extra columns never added", "storage.py", "            if extra_cols:\n                row[\"review_flags\"]", "            if False:\n                row[\"review_flags\"]"),
    ("tiered flag not added", "storage.py", "list(flags) + ([\"tiered\"] if \"tiered\" in tags else [])", "list(flags)"),
    ("tier note taken from the wrong product", "storage.py", "tier_notes.get(product_type) if product_type != \"cd\" else None", "tier_notes.get(\"money_market\")"),
    ("hys duplicate of savings no longer skipped", "storage.py", "            if product_type == \"savings\" and hys_apy is not None and apy == hys_apy:\n                continue", ""),
    ("push uses the columns without checking", "storage.py", "extra_cols=await _rate_obs_has_extra_columns()", "extra_cols=True"),
    ("column check cached wrongly", "storage.py", "    crawl_state[\"_ro_extra_cols\"] = ok\n    return ok", "    return True"),
    # ---- extraction wiring
    ("tier rule does not override the generic match", "extraction.py", "        r[ptype] = t[\"apy\"]\n        tags =", "        if r.get(ptype) is None:\n            r[ptype] = t[\"apy\"]\n        tags ="),
    ("tier parser not called", "extraction.py", "    for ptype, t in extract_tiered_products(text).items():", "    for ptype, t in {}.items():"),
    ("tier parser also overwrites cd", "extraction.py", "        if ptype not in (\"checking\", \"savings\", \"high_yield_savings\", \"money_market\"):\n            continue\n", ""),
    ("tier note not stored", "extraction.py", "        r[\"tier_notes\"][ptype] = t[\"note\"]", "        pass"),
    ("tiered tag not set", "extraction.py", "        tags = [x for x in r[\"rate_tags\"].get(ptype, []) if x != \"tiered\"] + [\"tiered\"]", "        tags = []"),
    # ---- scrapers wiring (string-level)
    ("vision prompt back to the highest rate", "scrapers.py", "- For checking/savings/money_market: {tier_rule()}", "- For checking/savings/money_market: use the HIGHEST advertised APY for that product type"),
    ("search prompt loses the tier rule", "scrapers.py", "- Tiered rates: {tier_rule()}\n", ""),
    ("vision prompt allows guessing", "scrapers.py", "never guess or estimate", "estimate if unsure"),
    ("nerdwallet guard removed from the preflight", "scrapers.py", "        if is_blocked_source(result.get(\"source_domain\")) or is_blocked_source(result.get(\"source_note\")):", "        if False:"),
    ("nerdwallet refusal removed from the search prompt", "scrapers.py", "never NerdWallet, its rate data is proprietary and off-limits", "NerdWallet is fine"),
    ("ai values not verified before use", "scrapers.py", "                        ai_result, _dropped, _ver = verify_ai_values(ai_result, _vtext)", "                        _dropped, _ver = [], {}"),
    ("ai provenance not recorded", "scrapers.py", "                                    prov[k] = make_source(AI_VISION, url=v_url, verified=_ver.get(k, False))", "                                    pass"),
    ("site provenance not recorded", "scrapers.py", "                            prov[k] = make_source(SITE, url=url, verified=True)", "                            pass"),
    ("aggregator provenance not recorded", "scrapers.py", "                        prov[k] = make_source(AGGREGATOR, url=da.get(\"_source\", \"depositaccounts.com\"))", "                        pass"),
    ("search fill ignores not-published in the preflight merge", "scrapers.py", "                                if _search_blocked(result, k):\n                                    continue\n                                result[rk] = pre[k]", "                                result[rk] = pre[k]"),
    ("search fill ignores not-published in the retry", "scrapers.py", "                                    if _search_blocked(result, k):\n                                        continue\n                                    result[rk] = retry[k]", "                                    result[rk] = retry[k]"),
    ("search fill ignores not-published in the chat merge", "scrapers.py", "if k not in (\"cd_term\", \"min_balance\") and _search_blocked(result, k):\n                                                continue", "if False:\n                                                continue"),
    ("last-known-good ignores not-published", "scrapers.py", "if k not in (\"cd_term\",\"min_balance\") and _search_blocked(result, k[:-4]):\n                                        continue", "if False:\n                                        continue"),
    ("hidden text only read when nothing was found", "scrapers.py", "            if is_rate_url and not all(r.get(k) for k in core_keys):\n                try:\n                    _hid", "            if False:\n                try:\n                    _hid"),
    ("result drops the provenance", "scrapers.py", "        \"provenance\":                {k: dict(v) for k, v in prov.items()},", "        \"provenance\":                {},"),
    ("result drops the money market source", "scrapers.py", "        \"source_url_money_market\":   source_urls.get(\"money_market\", \"\"),", "        \"source_url_money_market\":   \"\","),
    ("result drops the identity", "scrapers.py", "        \"identity\":                  identity,", "        \"identity\":                  None,"),
    ("result drops the not-published list", "scrapers.py", "        \"not_published\":             sorted(np_final),", "        \"not_published\":             [],"),
    ("result drops the tier notes", "scrapers.py", "        \"tier_notes\":                dict(tier_notes),", "        \"tier_notes\":                {},"),
    ("search provenance label dropped", "scrapers.py", "    result.setdefault(\"provenance\", {})[product] = make_source(SEARCH, label=f\"[Search] {src_label}\")", "    result.setdefault(\"provenance\", {})[product] = make_source(SEARCH)"),
    ("search never blocked", "scrapers.py", "    return \"all\" in np or product in np or (product == \"high_yield_savings\" and \"savings\" in np)", "    return False"),
    ("preflight result builder drops provenance", "scrapers.py", "        \"provenance\":                {p: make_source(SEARCH, label=f\"[Search] {src}\")\n                                      for p in (\"checking\", \"savings\", \"high_yield_savings\", \"cd\", \"money_market\") if pre.get(p)},", "        \"provenance\":                {},"),
    # ---- the tier rule setting
    ("tier setting ignored", "rate_integrity.py", "    return v if v in TIER_RULES else DEFAULT_TIER_RULE", "    return DEFAULT_TIER_RULE"),
    ("unknown tier setting accepted", "rate_integrity.py", "    return v if v in TIER_RULES else DEFAULT_TIER_RULE", "    return v or DEFAULT_TIER_RULE"),
    ("tier setting is case sensitive", "rate_integrity.py", "    v = (os.environ.get(TIER_RULE_ENV) or \"\").strip().lower()", "    v = (os.environ.get(TIER_RULE_ENV) or \"\")"),
    ("tier setting default becomes highest", "rate_integrity.py", "DEFAULT_TIER_RULE = \"entry\"", "DEFAULT_TIER_RULE = \"highest\""),
    ("tier setting variable renamed", "rate_integrity.py", "TIER_RULE_ENV = \"RATE_RADAR_TIER_RULE\"", "TIER_RULE_ENV = \"TIER_RULE\""),
    ("pick_tier ignores the mode", "rate_integrity.py", "    return pick_highest_tier(tiers) if (mode or tier_mode()) == \"highest\" else pick_entry_tier(tiers, min_open)", "    return pick_entry_tier(tiers, min_open)"),
    ("pick_tier ignores the setting", "rate_integrity.py", "    return pick_highest_tier(tiers) if (mode or tier_mode()) == \"highest\" else pick_entry_tier(tiers, min_open)", "    return pick_highest_tier(tiers) if mode == \"highest\" else pick_entry_tier(tiers, min_open)"),
    ("highest tier is the lowest rate", "rate_integrity.py", "    return sorted(tiers, key=lambda t: (-t[\"apy\"], t[\"lo\"]))[0]", "    return sorted(tiers, key=lambda t: (t[\"apy\"], t[\"lo\"]))[0]"),
    ("highest tier tie goes to the largest balances", "rate_integrity.py", "    return sorted(tiers, key=lambda t: (-t[\"apy\"], t[\"lo\"]))[0]", "    return sorted(tiers, key=lambda t: (-t[\"apy\"], -t[\"lo\"]))[0]"),
    ("parser ignores the mode argument", "rate_integrity.py", "        use = mode or tier_mode()", "        use = tier_mode()"),
    ("parser ignores the setting", "rate_integrity.py", "        use = mode or tier_mode()", "        use = mode or \"entry\""),
    ("highest note does not say so", "rate_integrity.py", "        why = (\"highest advertised rate\" if use == \"highest\" else", "        why = (\"entry\" if use == \"highest\" else"),
    ("highest rule text loses its wording", "rate_integrity.py", "report the HIGHEST advertised rate", "report a rate"),
    ("prompts use a fixed rule not the setting", "scrapers.py", "- Tiered rates: {tier_rule()}", "- Tiered rates: {tier_rule}"),
    # ---- not-published evidence and the outcome table
    ("evidence keeps newlines", "rate_integrity.py", "re.sub(r\"\\s+\", \" \", text[max(0, m.start() - 60):m.end() + 20]).strip()", "text[max(0, m.start() - 60):m.end() + 20]"),
    ("evidence keeps the last occurrence", "rate_integrity.py", "        if key not in out:\n", "        if True:\n"),
    ("detect returns the evidence values", "rate_integrity.py", "    return set(find_not_published(text))", "    return set(find_not_published(text).values())"),
    ("outcome rows ignore a manual confirmation", "storage.py", "        if not bank_name or r.get(\"_edited\"):\n            continue\n        evidence", "        if not bank_name:\n            continue\n        evidence"),
    ("outcome rows accept any product", "storage.py", "            if product not in NOT_PUBLISHED_PRODUCTS:\n                continue\n", ""),
    ("outcome rows drop the evidence", "storage.py", "                \"evidence\":     (ev.get(\"text\") or None),", "                \"evidence\":     None,"),
    ("outcome rows drop the page", "storage.py", "                \"source_url\":   (ev.get(\"url\") or None),", "                \"source_url\":   None,"),
    ("outcome rows lose the bank id", "storage.py", "                \"rssdid\":       str(r[\"RSSDID\"]) if r.get(\"RSSDID\") not in (\"\", None) else None,\n                \"product_type\": product,", "                \"rssdid\":       None,\n                \"product_type\": product,"),
    ("outcome rows not sorted", "storage.py", "for product in sorted(set(r.get(\"not_published\") or [])):", "for product in list(r.get(\"not_published\") or [])[::-1]:"),
    ("outcome table written without checking it exists", "storage.py", "    if not await _not_published_table_exists():", "    if False:"),
    ("outcome table check never cached", "storage.py", "    cached = crawl_state.get(\"_np_table\")\n    if cached is not None:\n        return cached", "    cached = None"),
    ("outcome table check always yes", "storage.py", "    crawl_state[\"_np_table\"] = ok\n    return ok", "    return True"),
    ("outcome upsert key changed", "storage.py", "?on_conflict=run_id,bank_name,product_type", "?on_conflict=run_id"),
    ("outcomes not written by the run", "scrapers.py", "        await push_not_published(crawl_state[\"results\"], saved[0])\n", ""),
    ("outcome evidence not recorded", "scrapers.py", "                    np_evidence.setdefault(_np, {\"text\": _ev, \"url\": url})", "                    pass"),
    ("result drops the outcome evidence", "scrapers.py", "        \"not_published_evidence\":    {p: np_evidence[p] for p in np_final if p in np_evidence},\n", ""),
    # ---- the RR01 SQL files
    ("RR01 apply: row security off", "sql/RR01_Apply.sql", "alter table public.rate_not_published enable row level security;\n", ""),
    ("RR01 apply: anon may read", "sql/RR01_Apply.sql", "revoke all on public.rate_not_published from public, anon, authenticated, service_role;\n", "grant select on public.rate_not_published to anon;\n"),
    ("RR01 apply: service_role cannot update", "sql/RR01_Apply.sql", "grant select, insert, update, delete on public.rate_not_published", "grant select, insert, delete on public.rate_not_published"),
    ("RR01 apply: product check loses 'all'", "sql/RR01_Apply.sql", "'cd', 'all')),", "'cd')),"),
    ("RR01 apply: no unique key", "sql/RR01_Apply.sql", "  unique (run_id, bank_name, product_type)\n", "  observed_at2 timestamptz\n"),
    ("RR01 apply: no foreign key", "sql/RR01_Apply.sql", " references public.rate_radar_runs (run_id)", ""),
    ("RR01 apply: no guard against a second apply", "sql/RR01_Apply.sql", "    raise exception 'rr01 is already applied (review_flags or tier_note exists): stop and tell Session 3';", "    raise exception 'x';"),
    ("RR01 apply: flags nullable", "sql/RR01_Apply.sql", "add column review_flags text[] not null default '{}'", "add column review_flags text[]"),
    ("RR01 apply: alters apy", "sql/RR01_Apply.sql", "alter table public.rate_observations add column tier_note text;", "alter table public.rate_observations add column tier_note text;\nalter table public.rate_observations alter column apy drop not null;"),
    ("RR01 apply: no commit", "sql/RR01_Apply.sql", "\ncommit;\n", "\n"),
    ("RR01 rollback: leaves the table", "sql/RR01_Rollback.sql", "drop table if exists public.rate_not_published;\n", ""),
    ("RR01 rollback: leaves a column", "sql/RR01_Rollback.sql", "alter table public.rate_observations drop column if exists tier_note;\n", ""),
    ("RR01 rollback: drops an extra column", "sql/RR01_Rollback.sql", "alter table public.rate_observations drop column if exists tier_note;\n", "alter table public.rate_observations drop column if exists tier_note;\nalter table public.rate_observations drop column if exists apy;\n"),
    ("RR01 verify: writes data", "sql/RR01_Verify.sql", "-- Query 5:", "delete from public.rate_not_published;\n-- Query 5:"),
    # ---- page scope, as-of date, staleness, zip, state flags
    ("numeric date read day-first", "rate_integrity.py", "            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))", "            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))"),
    ("two-digit year not 20YY", "rate_integrity.py", "y + 2000 if y < 100 else y", "y"),
    ("stale at exactly the limit", "rate_integrity.py", "    return age is not None and age > (days if days is not None else stale_days())", "    return age is not None and age >= (days if days is not None else stale_days())"),
    ("stale default 30", "rate_integrity.py", "DEFAULT_STALE_DAYS = 60", "DEFAULT_STALE_DAYS = 30"),
    ("stale setting ignored", "rate_integrity.py", "        return v if v > 0 else DEFAULT_STALE_DAYS", "        return DEFAULT_STALE_DAYS"),
    ("zero stale limit accepted", "rate_integrity.py", "        return v if v > 0 else DEFAULT_STALE_DAYS", "        return v"),
    ("stale value keeps its confidence", "rate_integrity.py", "        conf, flags = LOW, list(flags) + [\"stale_page_date\"]", "        flags = list(flags) + [\"stale_page_date\"]"),
    ("stale applies to a manual confirmation", "rate_integrity.py", "    if stale and not edited:", "    if stale:"),
    ("raw date text dropped", "rate_integrity.py", "\"as_of_raw\": as_of_raw,", "\"as_of_raw\": None,"),
    ("order claimed for an unparsed date", "rate_integrity.py", "        order = (\"month/day/year\" if m.group(\"num\") else \"named month\") if d else None", "        order = \"month/day/year\""),
    ("state-of scope form not read", "rate_integrity.py", "    m = _SCOPE_PAT.search(text) or _SCOPE_PAT2.search(text)", "    m = _SCOPE_PAT.search(text)"),
    ("zip not read", "rate_integrity.py", "    mz = _ZIP_PAT.search(text)", "    mz = None"),
    ("state names not mapped", "rate_integrity.py", "    if s in STATE_NAMES:\n        return STATE_NAMES[s]", "    if False:\n        return None"),
    ("home-state flag removed", "rate_integrity.py", "    if sc and home_state and sc != home_state:", "    if False:"),
    ("run flag removed", "rate_integrity.py", "    if sc and run_majority and sc != run_majority:", "    if False:"),
    ("regional flag removed", "rate_integrity.py", "    flags.append(\"regional_scope\")", "    pass"),
    ("home state never read from the result", "rate_integrity.py", "    for key in (\"state\", \"bank_state\", \"State\"):", "    for key in ():"),
    ("run majority never computed", "storage.py", "    run_majority = _states.most_common(1)[0][0] if len(_states) > 1 else None", "    run_majority = None"),
    ("stale also applies to search values", "storage.py", "stale=bool(is_stale(meta, today)) and method_is_page_read(entry))", "stale=bool(is_stale(meta, today)))"),
    ("stale never applied", "storage.py", "stale=bool(is_stale(meta, today)) and method_is_page_read(entry))", "stale=False)"),
    ("unparsed date not flagged", "storage.py", "                    row[\"review_flags\"].append(\"as_of_unparsed\")", "                    pass"),
    ("page meta not kept with the value", "scrapers.py", "                                rate_meta[k] = _meta", "                                pass"),
    # ---- multi-product tables
    ("zero-rate bands count as rates", "rate_integrity.py", "        if r[\"section\"] == \"cd\" or r[\"apy\"] <= 0:", "        if r[\"section\"] == \"cd\":"),
    ("highest product not chosen across products", "rate_integrity.py", "        chosen = max(prods, key=lambda x: x[2][\"apy\"]) if use == \"highest\" else prods[0]", "        chosen = prods[0]"),
    ("unreadable dollar rows are kept", "rate_integrity.py", "        if band is None and \"$\" in s:\n            continue   # a row with dollar amounts we cannot read as a band: not a rate row", "        pass"),
    ("CD rows not read", "rate_integrity.py", "        if section == \"cd\":\n            if term_cell:", "        if section == \"cd\":\n            if False:"),
    ("rows before a heading are read", "rate_integrity.py", "        if section is None or len(cells) < 2:", "        if len(cells) < 2:"),
    ("product name does not carry down", "rate_integrity.py", "            last_name = first\n", "            pass\n"),
    ("tables not used by extract_rates", "extraction.py", "    tiered.update(extract_rate_tables(text))", "    pass"),
    ("table CD rows not used", "extraction.py", "    for _term, _apy in extract_table_cd_rows(text):", "    for _term, _apy in []:"),
    # ---- links, robots, breaker, registry
    ("loan links count as rates links", "rate_integrity.py", "    return bool(RATES_PAGE_LINK_PAT.search(s)) and not _LOAN_LINK_PAT.search(s)", "    return bool(RATES_PAGE_LINK_PAT.search(s))"),
    ("rates links not domain-locked", "scrapers.py", "        if full and full.startswith(domain_str):\n            full = full.split(\"#\")[0]", "        if full:\n            full = full.split(\"#\")[0]"),
    ("rates link limit ignored", "scrapers.py", "        if len(out) >= limit:\n            break\n    return out\n\n\nasync def collect_rate_detail_links", "        if False:\n            break\n    return out\n\n\nasync def collect_rate_detail_links"),
    ("unreadable robots.txt allows the link", "scrapers.py", "    if rp is None:\n        return False\n    if rp is True:", "    if rp is None:\n        return True\n    if rp is True:"),
    ("missing robots.txt blocks the link", "scrapers.py", "    if status is not None and 400 <= status < 500:\n        return True\n    return None", "    return None"),
    ("robots rules ignored", "scrapers.py", "    return rp.can_fetch(\"*\", url)", "    return True"),
    ("robots fetched every time", "scrapers.py", "    if origin not in _ROBOTS_CACHE:", "    if True:"),
    ("robots not checked before following", "scrapers.py", "        if from_link and not await robots_allows(url, http_session):", "        if False:"),
    ("skipped bank not counted", "scrapers.py", "                    try:\n                        await update_bank_registry(bank, result)\n                    except Exception:\n                        pass\n", ""),
    ("skipped bank stays Found", "scrapers.py", "                    result[\"status\"] = STATUS_SKIPPED\n", ""),
    ("search value counts as a successful crawl", "storage.py", "        if entry and entry.get(\"method\") in (\"site\", \"ai_vision\"):", "        if entry:"),
    ("onward link always recorded as not published", "scrapers.py", "            if not onward:\n                for _np, _ev in _np_found.items():", "            if True:\n                for _np, _ev in _np_found.items():"),
    ("market zip ignored", "scrapers.py", "    bank_zip = market_zip(bank) or _health.get(\"zip_gate_zip\") or get_bank_zip(bank)", "    bank_zip = _health.get(\"zip_gate_zip\") or get_bank_zip(bank)"),
    ("market zip loses to the learned zip", "scrapers.py", "    bank_zip = market_zip(bank) or _health.get(\"zip_gate_zip\") or get_bank_zip(bank)", "    bank_zip = _health.get(\"zip_gate_zip\") or market_zip(bank) or get_bank_zip(bank)"),
    ("market zip takes any text", "scrapers.py", "        m = re.fullmatch(r\"\\s*(\\d{5})(?:-\\d{4})?\\s*\", str(v or \"\"))", "        m = re.fullmatch(r\"\\s*(\\d+)\\s*\", str(v or \"\"))"),
    ("run setting wins over the row", "scrapers.py", "    for v in (bank.get(\"market_zip\"), os.environ.get(\"RATE_RADAR_MARKET_ZIP\")):", "    for v in (os.environ.get(\"RATE_RADAR_MARKET_ZIP\"), bank.get(\"market_zip\")):"),
    ("market zip never stored", "storage.py", "                row[\"market_zip\"] = _z if re.fullmatch(r\"\\d{5}\", _z) else None", "                row[\"market_zip\"] = None"),
    ("market zip accepts anything", "storage.py", "                row[\"market_zip\"] = _z if re.fullmatch(r\"\\d{5}\", _z) else None", "                row[\"market_zip\"] = _z or None"),
    ("market zip ignores the requested zip", "storage.py", "(meta or {}).get(\"zip\") or r.get(\"market_zip_used\") or \"\"", "(meta or {}).get(\"zip\") or \"\""),
    ("scope state never stored", "storage.py", "                row[\"scope_state\"] = state_abbr((meta or {}).get(\"scope\"))", "                row[\"scope_state\"] = None"),
    ("new columns not detected", "storage.py", "_RATE_OBS_EXTRA = \"review_flags,tier_note,market_zip,scope_state\"", "_RATE_OBS_EXTRA = \"review_flags,tier_note\""),
    ("result drops the zip used", "scrapers.py", "        \"market_zip_used\":           bank_zip if zip_gate[\"used_on\"] else None,", "        \"market_zip_used\":           None,"),
    ("RR01 apply: market_zip unchecked", "sql/RR01_Apply.sql", "add column market_zip text check (market_zip ~ '^[0-9]{5}$');", "add column market_zip text;"),
    ("RR01 apply: scope_state unchecked", "sql/RR01_Apply.sql", "add column scope_state text check (scope_state ~ '^[A-Z]{2}$');", "add column scope_state text;"),
    ("RR01 rollback: leaves market_zip", "sql/RR01_Rollback.sql", "alter table public.rate_observations drop column if exists market_zip;\n", ""),
    ("RR01 rollback: leaves scope_state", "sql/RR01_Rollback.sql", "alter table public.rate_observations drop column if exists scope_state;\n", ""),
]

# Mutants the suite cannot distinguish from the original, with the reason (none today).
EQUIVALENT = {}


def run_suite(workdir):
    r = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests/integrity"], cwd=workdir, capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout + r.stderr)


def main():
    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    tmp = Path(tempfile.mkdtemp(prefix="rr_mut_"))
    try:
        base = tmp / "base"
        shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns("__pycache__", "RateRadar_Exports", "RateCache", "*.pyc"))
        rc, out = run_suite(base)
        print("baseline:", "PASS" if rc == 0 else "FAIL")
        if rc != 0:
            print(out[-1500:])
            sys.exit(1)
        ok = True
        killed = missed = 0
        for name, fname, old, new in M:
            if only and only not in name.lower():
                continue
            work = tmp / "m"
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(base, work)
            p = work / fname
            with open(p, encoding="utf-8", newline="") as fh:
                text = fh.read().replace("\r\n", "\n")
            if old not in text:
                print(f"MISS {name}: mutation target not found in {fname}")
                ok = False
                missed += 1
                continue
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(text.replace(old, new, 1))
            rc, out = run_suite(work)
            if rc != 0:
                killed += 1
                print(f"OK   {name}")
            elif name in EQUIVALENT:
                print(f"EQUIV {name}: {EQUIVALENT[name]}")
            else:
                missed += 1
                ok = False
                print(f"MISS {name}: the suite still passes")
        print(f"\n{killed} killed, {missed} missed, {len(M)} mutations")
        print("RESULT:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
