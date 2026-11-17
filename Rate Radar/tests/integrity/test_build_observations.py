"""Tests for storage.build_rate_observations, extraction.extract_rates (tier rule) and the scrapers hooks
(stdlib unittest: python -m unittest discover -s tests/integrity). Run from the "Rate Radar" folder.

The result dicts reproduce the SHAPE of what went wrong in run 20261117_103944, with invented banks and values:
  Harbor  - tiered savings / money market pages, AI-vision values, search-filled checking and high-yield (the "Colonial" case)
  Alder   - rates on a site branded for another bank (the "Monet" case)
  Prairie - a bank that says "inquire for rates" and has search values (the "Cattlemens" case)
  Summit  - circuit breaker, preflight search only (the "SouthState" case)
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest import mock  # noqa: E402

import extraction  # noqa: E402
import rate_integrity as ri  # noqa: E402
import storage  # noqa: E402

HERE = os.path.dirname(__file__)
SCRAPERS_SRC = open(os.path.join(HERE, "..", "..", "scrapers.py"), encoding="utf-8").read()

BASE = "https://www.harborsavings.example"
CD_PAGE = BASE + "/banking/personal-banking/cd/"
SAV_PAGE = BASE + "/banking/personal-banking/savings-accounts/"


def rows_by(rows):
    out = {}
    for r in rows:
        out.setdefault(r["product_type"], []).append(r)
    return out


def harbor():
    return {
        "bank_name": "Harbor Savings, F.A.", "bank_url": BASE, "RSSDID": "100001",
        "checking_apy": 1.0, "savings_apy": 0.12, "high_yield_savings_apy": 3.56,
        "money_market_apy": 0.05, "cd_apy": 4.03, "cd_term": "6-month",
        "cd_ladder": [{"term": "6-month", "apy": 4.03}, {"term": "12-month", "apy": 3.30}],
        "min_balance": "$2,500",
        "source_url": CD_PAGE, "source_url_cd": CD_PAGE, "source_url_savings": SAV_PAGE,
        "source_url_checking": "", "source_url_high_yield_savings": "",
        "note": "CD 4.03% (6-month) | checking from search: 1.0 | high_yield_savings from search: 3.56",
        "rate_tags": {"cd": ["promo"], "money_market": ["tiered"], "savings": ["tiered"]},
        "tier_notes": {"money_market": "tiered: 0.05% up to $10,000 (rate that applies at the $2,500 opening minimum); other tiers: 0.62% $10,000 to $25,000",
                       "savings": "tiered: 0.12% up to $2,499 (rate that applies at the lowest balance tier); other tiers: 0.42% over $2,500"},
        "identity": "ok", "not_published": [],
        "provenance": {
            "cd": ri.make_source(ri.SITE, url=CD_PAGE, verified=True),
            "savings": ri.make_source(ri.SITE, url=SAV_PAGE, verified=True),
            "money_market": ri.make_source(ri.SITE, url=SAV_PAGE, verified=True),
            "checking": ri.make_source(ri.SEARCH, label="[Search] Bank's own site (harborsavings.example)"),
            "high_yield_savings": ri.make_source(ri.SEARCH, label="[Search] Bank's own site (harborsavings.example)"),
        },
    }


class BuildObservationTests(unittest.TestCase):
    def test_every_value_keeps_its_own_source_and_method(self):
        r = rows_by(storage.build_rate_observations([harbor()], "run1"))
        mm = r["money_market"][0]
        self.assertEqual((mm["source_url"], mm["extraction_method"], mm["confidence"]), (SAV_PAGE, "site", "high"))
        self.assertEqual(r["savings"][0]["source_url"], SAV_PAGE)
        for cd in r["cd"]:
            self.assertEqual((cd["source_url"], cd["extraction_method"], cd["confidence"]), (CD_PAGE, "site", "high"))
        for p in ("checking", "high_yield_savings"):
            row = r[p][0]
            self.assertEqual((row["extraction_method"], row["confidence"]), ("search", "low"))
            self.assertNotEqual(row["source_url"], CD_PAGE)     # not the first page the crawler read
            self.assertTrue(row["source_url"].startswith("[Search]"))

    def test_money_market_read_by_ai_vision_is_never_site_high_and_never_the_cd_page(self):
        r = harbor()
        r["provenance"]["money_market"] = ri.make_source(ri.AI_VISION, url=SAV_PAGE, verified=False)
        mm = rows_by(storage.build_rate_observations([r], "run1"))["money_market"][0]
        self.assertEqual((mm["extraction_method"], mm["confidence"]), ("ai_vision", "low"))
        self.assertEqual(mm["source_url"], SAV_PAGE)
        self.assertNotEqual(mm["source_url"], CD_PAGE)

    def test_ai_vision_value_found_in_the_page_text_is_medium(self):
        r = harbor()
        r["provenance"]["money_market"] = ri.make_source(ri.AI_VISION, url=SAV_PAGE, verified=True)
        mm = rows_by(storage.build_rate_observations([r], "run1"))["money_market"][0]
        self.assertEqual((mm["extraction_method"], mm["confidence"]), ("ai_vision", "medium"))

    def test_savings_is_not_labelled_search_because_high_yield_came_from_search(self):
        r = harbor()
        r["provenance"].pop("savings")          # an old result: no recorded provenance for savings
        r["source_url_savings"] = "[AI] " + SAV_PAGE
        sav = rows_by(storage.build_rate_observations([r], "run1"))["savings"][0]
        self.assertEqual(sav["extraction_method"], "ai_vision")
        self.assertEqual(sav["source_url"], SAV_PAGE)

    def test_money_market_without_any_recorded_source_has_no_url_and_is_low(self):
        r = harbor()
        r["provenance"].pop("money_market")
        r["source_url_money_market"] = ""
        mm = rows_by(storage.build_rate_observations([r], "run1"))["money_market"][0]
        self.assertEqual((mm["extraction_method"], mm["confidence"], mm["source_url"]), ("search", "low", None))

    def test_site_value_on_another_domain_is_medium_and_flagged(self):
        h = harbor()
        h["provenance"]["cd"] = ri.make_source(ri.SITE, url="https://other.example/rates", verified=True)
        cd = rows_by(storage.build_rate_observations([h], "run1", extra_cols=True))["cd"][0]
        self.assertEqual(cd["confidence"], "medium")
        self.assertIn("off_domain", cd["review_flags"])

    def test_manual_confirmation_keeps_the_url_the_person_gave(self):
        h = harbor()
        h["_edited"] = True
        h["source_url_money_market"] = "https://confirmed.example/rates"
        h["provenance"]["money_market"] = ri.make_source(ri.SITE, url=SAV_PAGE, verified=True)
        mm = rows_by(storage.build_rate_observations([h], "run1"))["money_market"][0]
        self.assertEqual(mm["source_url"], "https://confirmed.example/rates")

    def test_page_that_never_names_the_bank_cannot_be_high(self):
        a = harbor()
        a["bank_name"] = "Alder Bank"
        a["identity"] = "mismatch"
        rows = storage.build_rate_observations([a], "run1")
        site_rows = [x for x in rows if x["extraction_method"] == "site"]
        self.assertTrue(site_rows)
        self.assertTrue(all(x["confidence"] == "low" for x in site_rows))
        self.assertFalse(any(x["confidence"] == "high" for x in rows))

    def test_identity_ok_or_unknown_keeps_high(self):
        for ident in ("ok", "unknown", None):
            h = harbor()
            h["identity"] = ident
            cd = rows_by(storage.build_rate_observations([h], "run1"))["cd"][0]
            self.assertEqual(cd["confidence"], "high", ident)

    def test_bank_that_says_inquire_for_rates_is_not_overwritten_by_search_values(self):
        p = {"bank_name": "Prairie Bank", "bank_url": "https://www.prairiebank.example", "RSSDID": "100002",
             "checking_apy": None, "savings_apy": 0.5, "money_market_apy": 0.55, "cd_apy": 1.0, "cd_term": "12-month",
             "note": "Rates not publicly listed | savings from search: 0.5 | cd from search: 1.0 | money_market from search: 0.55",
             "not_published": ["all"],
             "provenance": {"savings": ri.make_source(ri.SEARCH, label="[Search] x"), "cd": ri.make_source(ri.SEARCH, label="[Search] x"),
                            "money_market": ri.make_source(ri.SEARCH, label="[Search] x")}}
        self.assertEqual(storage.build_rate_observations([p], "run1"), [])

    def test_not_published_product_only_blocks_that_product(self):
        h = harbor()
        h["not_published"] = ["checking"]
        r = rows_by(storage.build_rate_observations([h], "run1"))
        self.assertNotIn("checking", r)
        self.assertIn("cd", r)
        self.assertIn("high_yield_savings", r)

    def test_not_published_savings_also_blocks_high_yield_search_value(self):
        h = harbor()
        h["not_published"] = ["savings"]
        r = rows_by(storage.build_rate_observations([h], "run1"))
        self.assertNotIn("high_yield_savings", r)
        self.assertEqual(r["savings"][0]["extraction_method"], "site")     # read from the bank's own page: stays

    def test_search_only_result_is_low_with_the_source_label(self):
        s = {"bank_name": "Summit Bank", "bank_url": "https://www.summitbank.example", "RSSDID": "100003",
             "checking_apy": 0.08, "savings_apy": 0.15, "money_market_apy": 0.1, "cd_apy": 1.75, "cd_term": "12-month",
             "source_url": "[Search] Different domain than summitbank.example's known site (ratesite.example) — verify",
             "note": "CD 1.75% (12-month) | ⊘ Circuit breaker",
             "provenance": {p: ri.make_source(ri.SEARCH, label="[Search] Different domain than summitbank.example's known site (ratesite.example) — verify")
                            for p in ("checking", "savings", "money_market", "cd")}}
        rows = storage.build_rate_observations([s], "run1")
        self.assertEqual(len(rows), 4)
        for x in rows:
            self.assertEqual((x["extraction_method"], x["confidence"]), ("search", "low"))
            self.assertTrue(x["source_url"].startswith("[Search] Different domain"))

    def test_manual_confirmation_path_is_unchanged(self):
        h = harbor()
        h["_edited"] = True
        h["identity"] = "mismatch"
        rows = storage.build_rate_observations([h], "run1")
        self.assertTrue(rows)
        for x in rows:
            self.assertEqual((x["extraction_method"], x["confidence"], x["manually_verified"]), ("site", "high", True))
            self.assertEqual(x["verified_by"], "rate-radar.html (manual save)")

    def test_only_a_site_value_read_from_the_page_is_ever_high(self):
        h = harbor()
        for method in (ri.SEARCH, ri.AI_VISION, ri.AGGREGATOR, ri.LAST_KNOWN_GOOD):
            for verified in (True, False):
                h["provenance"]["money_market"] = ri.make_source(method, url=SAV_PAGE, verified=verified, label="[Search] x")
                mm = rows_by(storage.build_rate_observations([h], "run1"))["money_market"][0]
                self.assertNotEqual(mm["confidence"], "high", (method, verified))

    def test_default_rows_have_the_existing_columns_only(self):
        row = storage.build_rate_observations([harbor()], "run1")[0]
        self.assertEqual(sorted(row), sorted([
            "run_id", "bank_name", "rssdid", "product_type", "term_months", "apy", "min_balance", "source_url", "extraction_method",
            "confidence", "is_conditional", "is_promo", "observed_at", "manually_verified", "verified_by", "verified_at"]))

    def test_extra_columns_carry_flags_and_the_tier_note(self):
        h = harbor()
        h["provenance"]["money_market"] = ri.make_source(ri.AI_VISION, url=SAV_PAGE, verified=False)
        r = rows_by(storage.build_rate_observations([h], "run1", extra_cols=True))
        mm = r["money_market"][0]
        self.assertIn("not_in_page_text", mm["review_flags"])
        self.assertIn("tiered", mm["review_flags"])
        self.assertIn("other tiers", mm["tier_note"])
        self.assertIsNone(r["cd"][0]["tier_note"])
        self.assertEqual(r["cd"][0]["review_flags"], [])

    def test_high_yield_duplicate_of_savings_is_still_skipped(self):
        h = harbor()
        h["savings_apy"] = h["high_yield_savings_apy"]
        self.assertNotIn("savings", rows_by(storage.build_rate_observations([h], "run1")))

    def test_ladder_rows_share_the_cd_provenance(self):
        cds = rows_by(storage.build_rate_observations([harbor()], "run1"))["cd"]
        self.assertEqual(sorted(c["term_months"] for c in cds), [6, 12])
        self.assertEqual({c["source_url"] for c in cds}, {CD_PAGE})

    def test_money_market_source_url_field_is_used_for_legacy_results(self):
        h = harbor()
        h["provenance"].pop("money_market")
        h["source_url_money_market"] = SAV_PAGE
        mm = rows_by(storage.build_rate_observations([h], "run1"))["money_market"][0]
        self.assertEqual((mm["source_url"], mm["extraction_method"]), (SAV_PAGE, "site"))


class TierExtractionTests(unittest.TestCase):
    TIERED = ("Money Market earns up to 0.62% APY. $2,500 minimum to open "
              "$10,000 or less -- 0.05% APY $10,000.01 and over -- 0.62% APY ")

    def test_tier_rule_beats_the_nearest_or_highest_generic_match(self):
        r = extraction.extract_rates(self.TIERED)
        self.assertEqual(r["money_market"], 0.05)
        self.assertIn("tiered", r["rate_tags"]["money_market"])
        self.assertIn("opening minimum", r["tier_notes"]["money_market"])

    def test_the_one_setting_changes_the_rate_taken_from_the_same_block(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "highest"}):
            r = extraction.extract_rates(self.TIERED)
        self.assertEqual(r["money_market"], 0.62)
        self.assertIn("highest advertised rate", r["tier_notes"]["money_market"])
        self.assertIn("0.05% up to $10,000", r["tier_notes"]["money_market"])
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            self.assertEqual(extraction.extract_rates(self.TIERED)["money_market"], 0.05)

    def test_a_tiered_cd_block_does_not_overwrite_the_cd_rate(self):
        text = ("Certificates of Deposit $1,000 or less -- 1.00% APY $1,000.01 or more -- 4.00% APY\n"
                "12-month 4.50% APY\n")
        r = extraction.extract_rates(text)
        self.assertEqual(r["cd"], 4.5)
        self.assertNotIn("cd", r["tier_notes"])

    def test_header_labelled_savings_table(self):
        r = extraction.extract_rates("Type Interest Rate APY Savings Accounts ($2,499 or less) 0.12% 0.12% Savings Accounts ($2,500 or more) 0.42% 0.42%")
        self.assertEqual(r["savings"], 0.12)
        self.assertIn("lowest balance tier", r["tier_notes"]["savings"])

    def test_untiered_pages_are_unchanged(self):
        r = extraction.extract_rates("Savings 2.50% APY\nCD 12-month 4.00% APY\n")
        self.assertEqual(r["savings"], 2.5)
        self.assertEqual(r["tier_notes"], {})
        self.assertNotIn("tiered", r["rate_tags"].get("savings", []))

    def test_cd_ladder_is_not_touched_by_the_tier_parser(self):
        r = extraction.extract_rates("3-month 3.00% APY\n6-month 4.10% APY\n12-month 4.35% APY\n")
        self.assertEqual(r["cd"], 4.35)
        self.assertEqual(r["tier_notes"], {})


class NotPublishedOutcomeTests(unittest.TestCase):
    def prairie(self):
        return {"bank_name": "Prairie Bank", "RSSDID": "100002", "not_published": ["checking", "all"],
                "not_published_evidence": {"checking": {"text": "Interest Bearing: Inquire for Rates", "url": "https://www.prairiebank.example/checking"},
                                           "all": {"text": "Rates available upon request", "url": "https://www.prairiebank.example/rates"}}}

    def test_one_row_per_bank_and_product_with_its_evidence(self):
        rows = storage.build_not_published_rows([self.prairie()], "run1")
        self.assertEqual([r["product_type"] for r in rows], ["all", "checking"])
        chk = [r for r in rows if r["product_type"] == "checking"][0]
        self.assertEqual((chk["bank_name"], chk["rssdid"], chk["run_id"]), ("Prairie Bank", "100002", "run1"))
        self.assertEqual(chk["evidence"], "Interest Bearing: Inquire for Rates")
        self.assertEqual(chk["source_url"], "https://www.prairiebank.example/checking")
        self.assertEqual(sorted(chk), sorted(["run_id", "bank_name", "rssdid", "product_type", "evidence", "source_url", "observed_at"]))

    def test_a_person_confirmed_value_outranks_the_statement(self):
        p = self.prairie()
        p["_edited"] = True
        self.assertEqual(storage.build_not_published_rows([p], "run1"), [])

    def test_rows_are_in_product_order_whatever_the_input_order(self):
        rows = storage.build_not_published_rows([{"bank_name": "X", "not_published": ["checking", "savings", "all"]}], "run1")
        self.assertEqual([r["product_type"] for r in rows], ["all", "checking", "savings"])

    def test_unknown_products_and_banks_without_a_name_give_no_row(self):
        self.assertEqual(storage.build_not_published_rows([{"bank_name": "X", "not_published": ["telepathy"]}], "run1"), [])
        self.assertEqual(storage.build_not_published_rows([{"not_published": ["cd"]}], "run1"), [])
        self.assertEqual(storage.build_not_published_rows([{"bank_name": "X"}], "run1"), [])

    def test_missing_evidence_is_stored_as_null(self):
        rows = storage.build_not_published_rows([{"bank_name": "X", "not_published": ["cd"]}], "run1")
        self.assertEqual((rows[0]["evidence"], rows[0]["source_url"]), (None, None))

    def test_every_allowed_product_is_accepted(self):
        for p in ("checking", "savings", "high_yield_savings", "money_market", "cd", "all"):
            self.assertEqual(len(storage.build_not_published_rows([{"bank_name": "X", "not_published": [p]}], "run1")), 1, p)


class FeatureDetectTests(unittest.TestCase):
    """The collector must work before and after the additive migrations."""

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_extra_column_check_is_cached_per_process(self):
        with mock.patch.dict(storage.crawl_state, {"_ro_extra_cols": True}):
            self.assertTrue(self.run_async(storage._rate_obs_has_extra_columns()))
        with mock.patch.dict(storage.crawl_state, {"_ro_extra_cols": False}):
            self.assertFalse(self.run_async(storage._rate_obs_has_extra_columns()))

    def test_extra_column_check_says_no_without_a_database(self):
        state = dict(storage.crawl_state)
        state.pop("_ro_extra_cols", None)
        with mock.patch.dict(storage.crawl_state, state, clear=True), mock.patch.object(storage, "SUPABASE_URL", ""), mock.patch.object(storage, "SUPABASE_KEY", ""):
            self.assertFalse(self.run_async(storage._rate_obs_has_extra_columns()))

    def test_not_published_table_check_is_cached_per_process(self):
        with mock.patch.dict(storage.crawl_state, {"_np_table": True}):
            self.assertTrue(self.run_async(storage._not_published_table_exists()))
        with mock.patch.dict(storage.crawl_state, {"_np_table": False}):
            self.assertFalse(self.run_async(storage._not_published_table_exists()))

    def test_push_is_skipped_and_logged_when_the_table_does_not_exist(self):
        res = [{"bank_name": "X", "not_published": ["cd"]}]
        log_before = len(storage.crawl_state["log"])
        with mock.patch.object(storage, "SUPABASE_URL", "https://db.example"), mock.patch.object(storage, "SUPABASE_KEY", "k" * 20), \
                mock.patch.object(storage, "AIOHTTP_OK", True), mock.patch.dict(storage.crawl_state, {"_np_table": False}):
            self.assertFalse(self.run_async(storage.push_not_published(res, "run1")))
        self.assertIn("does not exist yet", storage.crawl_state["log"][-1])
        self.assertEqual(len(storage.crawl_state["log"]), log_before + 1)

    def test_push_with_nothing_to_write_does_nothing(self):
        with mock.patch.object(storage, "SUPABASE_URL", "https://db.example"), mock.patch.object(storage, "SUPABASE_KEY", "k" * 20), \
                mock.patch.object(storage, "AIOHTTP_OK", True):
            self.assertFalse(self.run_async(storage.push_not_published([{"bank_name": "X"}], "run1")))

    def test_the_run_writes_the_outcomes_after_the_rates(self):
        i = SCRAPERS_SRC.index('await push_rate_observations(crawl_state["results"], saved[0])')
        j = SCRAPERS_SRC.index('await push_not_published(crawl_state["results"], saved[0])')
        self.assertLess(i, j)


class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return ""

    async def read(self):
        return b""


class _FakeAiohttp:
    """Minimal stand-in for the aiohttp module: records every call, answers the feature checks with `check_status`."""

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    def __init__(self, check_status):
        self.check_status = check_status
        self.gets, self.posts = [], []
        outer = self

        class ClientSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, **k):
                outer.gets.append(url)
                return _FakeResp(outer.check_status)

            def post(self, url, **k):
                outer.posts.append((url, k.get("data")))
                return _FakeResp(201)

        self.ClientSession = ClientSession


class PushPathTests(unittest.TestCase):
    def run_push(self, check_status, results):
        import asyncio
        import json
        fake = _FakeAiohttp(check_status)
        state = {k: v for k, v in storage.crawl_state.items() if k not in ("_ro_extra_cols", "_np_table")}
        with mock.patch.object(storage, "aiohttp", fake, create=True), mock.patch.object(storage, "AIOHTTP_OK", True), \
                mock.patch.object(storage, "SUPABASE_URL", "https://db.example"), mock.patch.object(storage, "SUPABASE_KEY", "k" * 20), \
                mock.patch.dict(storage.crawl_state, state, clear=True):
            asyncio.run(storage.push_rate_observations(results, "run1"))
            asyncio.run(storage.push_rate_observations(results, "run2"))
        return fake, [json.loads(d) for _, d in fake.posts]

    def test_rows_carry_the_new_columns_when_the_table_has_them(self):
        fake, bodies = self.run_push(200, [harbor()])
        self.assertTrue(bodies and all("review_flags" in r and "tier_note" in r for b in bodies for r in b))

    def test_rows_do_not_carry_the_new_columns_when_the_table_lacks_them(self):
        fake, bodies = self.run_push(400, [harbor()])
        self.assertTrue(bodies and not any("review_flags" in r or "tier_note" in r for b in bodies for r in b))

    def test_the_column_check_is_made_once_per_process(self):
        fake, bodies = self.run_push(200, [harbor()])
        self.assertEqual(len([g for g in fake.gets if "select=review_flags,tier_note" in g]), 1)
        self.assertEqual(len(bodies), 2)


class NotPublishedPushTests(unittest.TestCase):
    def run_push(self, check_status):
        import asyncio
        import json
        fake = _FakeAiohttp(check_status)
        state = {k: v for k, v in storage.crawl_state.items() if k not in ("_ro_extra_cols", "_np_table")}
        res = [{"bank_name": "Prairie Bank", "RSSDID": "100002", "not_published": ["checking"],
                "not_published_evidence": {"checking": {"text": "Interest Bearing: Inquire for Rates", "url": "https://www.prairiebank.example/c"}}}]
        with mock.patch.object(storage, "aiohttp", fake, create=True), mock.patch.object(storage, "AIOHTTP_OK", True), \
                mock.patch.object(storage, "SUPABASE_URL", "https://db.example"), mock.patch.object(storage, "SUPABASE_KEY", "k" * 20), \
                mock.patch.dict(storage.crawl_state, state, clear=True):
            ok = asyncio.run(storage.push_not_published(res, "run1"))
        return ok, fake, [(u, json.loads(d)) for u, d in fake.posts]

    def test_writes_the_rows_when_the_table_exists(self):
        ok, fake, posts = self.run_push(200)
        self.assertTrue(ok)
        self.assertEqual(len(posts), 1)
        url, body = posts[0]
        self.assertIn("/rest/v1/rate_not_published?on_conflict=run_id,bank_name,product_type", url)
        self.assertEqual(body[0]["evidence"], "Interest Bearing: Inquire for Rates")
        self.assertEqual(body[0]["product_type"], "checking")

    def test_writes_nothing_when_the_table_does_not_exist(self):
        ok, fake, posts = self.run_push(404)
        self.assertFalse(ok)
        self.assertEqual(posts, [])
        self.assertEqual(len([g for g in fake.gets if "rate_not_published?select=id" in g]), 1)


class SourceCodeRuleTests(unittest.TestCase):
    """Rules that live in the prompts and the scraper wiring (strings, because those paths need a browser and an API key)."""

    def test_ai_prompts_state_the_tier_rule_not_the_highest_rate(self):
        self.assertNotIn("use the HIGHEST advertised APY for that product type", SCRAPERS_SRC)
        self.assertGreaterEqual(SCRAPERS_SRC.count("{tier_rule()}"), 2)      # vision prompt and search prompt

    def test_vision_prompt_forbids_guessing(self):
        self.assertIn("never guess or estimate", SCRAPERS_SRC)

    def test_search_prompt_still_refuses_nerdwallet_and_the_result_is_discarded_if_it_cites_it(self):
        self.assertIn("never NerdWallet, its rate data is proprietary and off-limits", SCRAPERS_SRC)
        self.assertIn('is_blocked_source(result.get("source_domain"))', SCRAPERS_SRC)
        self.assertIn('is_blocked_source(result.get("source_note"))', SCRAPERS_SRC)

    def test_nerdwallet_is_not_among_the_aggregator_hosts_the_crawler_visits(self):
        urls = re.findall(r"https?://[^\s\"']+", SCRAPERS_SRC)
        self.assertFalse([u for u in urls if "nerdwallet" in u.lower()])

    def test_the_evidence_sentence_is_recorded_with_its_page(self):
        self.assertIn('np_evidence.setdefault(_np, {"text": _ev, "url": url})', SCRAPERS_SRC)

    def test_ai_values_are_verified_against_the_page_text_before_use(self):
        self.assertIn("verify_ai_values(ai_result, _vtext)", SCRAPERS_SRC)
        self.assertIn("prov[k] = make_source(AI_VISION, url=v_url, verified=_ver.get(k, False))", SCRAPERS_SRC)

    def test_every_site_value_records_its_own_provenance(self):
        self.assertIn("prov[k] = make_source(SITE, url=url, verified=True)", SCRAPERS_SRC)
        self.assertIn("prov[k] = make_source(AGGREGATOR", SCRAPERS_SRC)

    def test_search_fills_never_override_a_product_the_bank_does_not_publish(self):
        self.assertEqual(SCRAPERS_SRC.count("_search_blocked(result, k"), 4)   # preflight, retry, chat, last-known-good

    def test_hidden_text_is_read_on_rate_pages_that_still_miss_a_product(self):
        self.assertIn("harvest_hidden_text(page)", SCRAPERS_SRC)
        self.assertIn("if is_rate_url and not all(r.get(k) for k in core_keys):", SCRAPERS_SRC)

    def test_crawl_result_fields_hold_the_recorded_values(self):
        for line in ('"provenance":                {k: dict(v) for k, v in prov.items()},',
                     '"tier_notes":                dict(tier_notes),',
                     '"not_published":             sorted(np_final),',
                     '"identity":                  identity,',
                     '"source_url_money_market":   source_urls.get("money_market", ""),',
                     '"not_published_evidence":    {p: np_evidence[p] for p in np_final if p in np_evidence},'):
            self.assertIn(line, SCRAPERS_SRC, line)

    def test_crawl_result_carries_the_integrity_fields(self):
        for key in ('"provenance":', '"tier_notes":', '"not_published":', '"not_published_evidence":', '"identity":', '"source_url_money_market":'):
            self.assertIn(key, SCRAPERS_SRC, key)


class ScrapersHelperTests(unittest.TestCase):
    def setUp(self):
        try:
            import scrapers  # noqa: F401
        except Exception as e:   # pragma: no cover - environment without playwright/flask deps
            self.skipTest(f"scrapers not importable here: {e}")
        self.s = sys.modules["scrapers"]

    def test_record_search_uses_the_search_label_and_creates_the_provenance_map(self):
        res = {}
        self.s._record_search(res, "money_market", "Third-party aggregator (bankrate.com)")
        e = res["provenance"]["money_market"]
        self.assertEqual((e["method"], e["label"]), ("search", "[Search] Third-party aggregator (bankrate.com)"))
        self.assertIsNone(e["url"])

    def test_search_blocked(self):
        self.assertTrue(self.s._search_blocked({"not_published": ["all"]}, "cd"))
        self.assertTrue(self.s._search_blocked({"not_published": ["checking"]}, "checking"))
        self.assertTrue(self.s._search_blocked({"not_published": ["savings"]}, "high_yield_savings"))
        self.assertFalse(self.s._search_blocked({"not_published": ["checking"]}, "cd"))
        self.assertFalse(self.s._search_blocked({}, "cd"))

    def test_preflight_result_builder_records_search_provenance_for_every_value(self):
        pre = {"checking": 0.1, "savings": 0.2, "cd": 1.5, "cd_term": "12-month", "money_market": 0.3, "_source_type": "aggregator",
               "_source_domain": "bankrate.com", "_source_note": ""}
        res = self.s._build_result_from_preflight({"bank_name": "Summit Bank", "bank_url": "https://www.summitbank.example"}, pre)
        self.assertEqual(sorted(res["provenance"]), ["cd", "checking", "money_market", "savings"])
        self.assertTrue(all(e["method"] == "search" for e in res["provenance"].values()))
        self.assertTrue(res["source_url_money_market"].startswith("[Search]"))


if __name__ == "__main__":
    unittest.main()
