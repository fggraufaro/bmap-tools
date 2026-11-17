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
                       "savings": "tiered: 0.12% up to $2,499 (rate that applies at the lowest balance tier); other tiers: 0.42% from $2,500"},
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

    def test_a_tiered_rate_is_marked_conditional_and_a_plain_one_is_not(self):
        r = rows_by(storage.build_rate_observations([harbor()], "run1"))
        self.assertTrue(r["money_market"][0]["is_conditional"])
        self.assertTrue(r["savings"][0]["is_conditional"])
        self.assertFalse(r["checking"][0]["is_conditional"])
        h = harbor()
        h["rate_tags"] = {"checking": ["conditional"]}
        self.assertTrue(rows_by(storage.build_rate_observations([h], "run1"))["checking"][0]["is_conditional"])

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

    def test_tier_rule_beats_the_nearest_generic_match(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            r = extraction.extract_rates(self.TIERED)
        self.assertEqual(r["money_market"], 0.05)
        self.assertIn("tiered", r["rate_tags"]["money_market"])
        self.assertIn("opening minimum", r["tier_notes"]["money_market"])

    def test_default_reports_the_highest_advertised_rate(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            r = extraction.extract_rates(self.TIERED)
        self.assertEqual(r["money_market"], 0.62)
        self.assertIn("tiered", r["rate_tags"]["money_market"])
        self.assertIn("$2,500 minimum to open", r["tier_notes"]["money_market"])

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
        self.assertEqual(r["savings"], 0.42)
        self.assertIn("0.12% up to $2,499", r["tier_notes"]["savings"])
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            e = extraction.extract_rates("Type Interest Rate APY Savings Accounts ($2,499 or less) 0.12% 0.12% Savings Accounts ($2,500 or more) 0.42% 0.42%")
        self.assertEqual(e["savings"], 0.12)
        self.assertIn("lowest balance tier", e["tier_notes"]["savings"])

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
        self.assertEqual(len([g for g in fake.gets if "select=review_flags,tier_note,market_zip,scope_state" in g]), 1)
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


class TableExtractionTests(unittest.TestCase):
    def test_extract_rates_reads_a_multi_product_table_page(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            r = extraction.extract_rates(SUMMIT_PAGE)
        self.assertEqual((r["checking"], r["savings"], r["money_market"]), (0.15, 0.15, 0.50))
        self.assertEqual(r["cd"], 3.51)
        self.assertEqual({e["term"]: e["apy"] for e in r["cd_ladder"]}, {"6-month": 3.15, "7-month": 3.51, "12-month": 1.75})
        self.assertIn("other products", r["tier_notes"]["checking"])
        self.assertIn("tiered", r["rate_tags"]["money_market"])

    def test_entry_rule_through_extract_rates(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            r = extraction.extract_rates(SUMMIT_PAGE)
        self.assertEqual(r["checking"], 0.08)

    def test_a_table_wins_over_a_tier_block_for_the_same_product(self):
        text = "Money Market $10,000 or less -- 0.05% APY $10,000.01 and over -- 0.99% APY\n" + SUMMIT_PAGE
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            self.assertEqual(extraction.extract_rates(text)["money_market"], 0.50)

    def test_a_page_with_no_table_is_unchanged(self):
        r = extraction.extract_rates("Savings 2.50% APY\nCD 12-month 4.00% APY\n")
        self.assertEqual((r["savings"], r["cd"]), (2.5, 4.0))
        self.assertEqual(r["tier_notes"], {})


SUMMIT_PAGE = SUMMIT_TABLES_TEXT = (
    "Interest Rates\nCompare our deposit rates.\n"
    "Checking\nProduct\tBalance Tiers to Obtain APY\tInterest Rate\tAPY\n"
    "Choice Checking\t$0.01 and above\t0.08%\t0.08%\nGold Checking\tUp to $99,999.99\t0.06%\t0.06%\n\t$100,000 and above\t0.15%\t0.15%\n"
    "Community Hero Checking\t$0.01 and above\t0.08%\t0.08%\n"
    "Savings\nPersonal Savings\t$0.01 and above\t0.15%\t0.15%\nHSA Savings\t$0.01 - $4,999.99\t0.04%\t0.04%\n\t$5,000 and above\t0.10%\t0.10%\n"
    "Money Market\nPersonal Money Market\tUnder $2,000\t0.00%\t0.00%\n\t$2,000 - $9,999.99\t0.10%\t0.10%\n\t$10,000 - $24,999.99\t0.15%\t0.15%\n\t$100,000 and above\t0.50%\t0.50%\n"
    "Certificates of Deposit\nTerm\tInterest Rate\tAPY\n6 months\t3.11%\t3.15%\n7 months\t3.46%\t3.51%\n12 months\t1.73%\t1.75%\n")


class _FakeLink:
    def __init__(self, href, text):
        self.href, self.text = href, text

    async def get_attribute(self, name):
        return self.href

    async def inner_text(self):
        return self.text


class _RobotsResp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self, errors=None):
        return self.body


class _RobotsSession:
    def __init__(self, status=200, body="", raises=False):
        self.status, self.body, self.raises, self.gets = status, body, raises, []

    def get(self, url, **k):
        self.gets.append(url)
        if self.raises:
            raise OSError("network down")
        return _RobotsResp(self.status, self.body)


class CrawlerHookTests(unittest.TestCase):
    def setUp(self):
        try:
            import scrapers  # noqa: F401
        except Exception as e:   # pragma: no cover
            self.skipTest(f"scrapers not importable here: {e}")
        self.s = sys.modules["scrapers"]
        self.s._ROBOTS_CACHE.clear()
        self.patches = [mock.patch.object(self.s, "aiohttp", mock.Mock(ClientTimeout=lambda total=None: total), create=True), mock.patch.object(self.s, "AIOHTTP_OK", True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.s._ROBOTS_CACHE.clear()

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    # -- the links
    def test_collect_rates_page_links_is_domain_locked_and_stricter_than_the_general_link_scan(self):
        D = "https://www.summitbank.example"
        links = [_FakeLink("/personal/checking-savings/interest-rates", "see rates"), _FakeLink("https://other.example/rates", "Rates"),
                 _FakeLink("/open-account", "Open an account"), _FakeLink("#top", "see rates"), _FakeLink("mailto:a@b.example", "rates"),
                 _FakeLink(D + "/rates#section", "Current rates"), _FakeLink("/mortgage-rates", "Mortgage rates"), _FakeLink("/personal/checking-savings/interest-rates", "see rates")]
        out = self.run_async(self.s.collect_rates_page_links(links, D))
        self.assertEqual(out, [D + "/personal/checking-savings/interest-rates", D + "/rates"])

    def test_collect_rates_page_links_respects_the_limit(self):
        D = "https://www.summitbank.example"
        links = [_FakeLink(f"/rates/{i}", "rates") for i in range(10)]
        self.assertEqual(len(self.run_async(self.s.collect_rates_page_links(links, D, limit=3))), 3)

    # -- robots.txt
    def test_robots_disallowed_link_is_not_followed(self):
        sess = _RobotsSession(200, "User-agent: *\nDisallow: /personal/checking-savings/interest-rates\n")
        self.assertFalse(self.run_async(self.s.robots_allows("https://www.summitbank.example/personal/checking-savings/interest-rates", sess)))
        self.assertTrue(self.run_async(self.s.robots_allows("https://www.summitbank.example/personal/other", sess)))

    def test_robots_allowing_everything(self):
        sess = _RobotsSession(200, "User-agent: *\nDisallow:\n")
        self.assertTrue(self.run_async(self.s.robots_allows("https://www.summitbank.example/rates", sess)))

    def test_missing_robots_txt_means_no_restrictions(self):
        for status in (404, 403, 410):
            self.s._ROBOTS_CACHE.clear()
            self.assertTrue(self.run_async(self.s.robots_allows("https://www.summitbank.example/rates", _RobotsSession(status))), status)

    def test_unreadable_robots_txt_means_the_link_is_not_followed(self):
        for sess in (_RobotsSession(500), _RobotsSession(503), _RobotsSession(raises=True)):
            self.s._ROBOTS_CACHE.clear()
            self.assertFalse(self.run_async(self.s.robots_allows("https://www.summitbank.example/rates", sess)))

    def test_robots_is_fetched_once_per_origin(self):
        sess = _RobotsSession(200, "User-agent: *\nDisallow: /private\n")
        for path in ("/a", "/b", "/private/c"):
            self.run_async(self.s.robots_allows("https://www.summitbank.example" + path, sess))
        self.run_async(self.s.robots_allows("https://other.example/a", sess))
        self.assertEqual(sess.gets, ["https://www.summitbank.example/robots.txt", "https://other.example/robots.txt"])

    def test_a_url_that_is_not_http_is_refused(self):
        self.assertFalse(self.run_async(self.s.robots_allows("javascript:void(0)", _RobotsSession(404))))
        self.assertFalse(self.run_async(self.s.robots_allows("", _RobotsSession(404))))

    def test_robots_rules_for_the_wildcard_agent_apply(self):
        sess = _RobotsSession(200, "User-agent: googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /secret\n")
        self.assertTrue(self.run_async(self.s.robots_allows("https://www.summitbank.example/rates", sess)))
        self.assertFalse(self.run_async(self.s.robots_allows("https://www.summitbank.example/secret/x", sess)))


class BreakerFlagTests(unittest.TestCase):
    def test_values_produced_while_the_breaker_was_open_are_flagged(self):
        s = {"bank_name": "Summit Bank", "bank_url": "https://www.summitbank.example", "checking_apy": 0.08, "breaker_skipped": True,
             "provenance": {"checking": ri.make_source(ri.SEARCH, label="[Search] x")}}
        row = storage.build_rate_observations([s], "run1", extra_cols=True)[0]
        self.assertIn("circuit_breaker_search_only", row["review_flags"])
        self.assertIn("search_only", row["review_flags"])

    def test_no_flag_without_the_marker_or_for_a_manual_confirmation(self):
        s = {"bank_name": "Summit Bank", "bank_url": "https://www.summitbank.example", "checking_apy": 0.08,
             "provenance": {"checking": ri.make_source(ri.SEARCH, label="[Search] x")}}
        self.assertNotIn("circuit_breaker_search_only", storage.build_rate_observations([s], "run1", extra_cols=True)[0]["review_flags"])
        s.update(breaker_skipped=True, _edited=True)
        self.assertNotIn("circuit_breaker_search_only", storage.build_rate_observations([s], "run1", extra_cols=True)[0]["review_flags"])


class _RegResp:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data if data is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data

    async def text(self):
        return ""


class _RegAiohttp:
    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    def __init__(self, prior):
        self.prior, self.posts = prior, []
        outer = self

        class ClientSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, **k):
                return _RegResp(200, [outer.prior])

            def post(self, url, **k):
                import json
                outer.posts.append(json.loads(k["data"]))
                return _RegResp(201)

        self.ClientSession = ClientSession


class BankRegistryTests(unittest.TestCase):
    """update_bank_registry feeds the circuit breaker: it must classify by the recorded provenance (it once called a removed helper)."""

    def run_reg(self, result, prior_failures=0):
        import asyncio
        fake = _RegAiohttp({"known_rate_urls": [], "consecutive_failures": prior_failures})
        bank = {"bank_name": "Summit Bank", "bank_url": "https://www.summitbank.example"}
        with mock.patch.object(storage, "aiohttp", fake, create=True), mock.patch.object(storage, "AIOHTTP_OK", True), \
                mock.patch.object(storage, "SUPABASE_URL", "https://db.example"), mock.patch.object(storage, "SUPABASE_KEY", "k" * 20):
            ret = asyncio.run(storage.update_bank_registry(bank, result))
        return ret, fake.posts[0]

    def test_a_value_read_from_the_site_resets_the_count(self):
        res = {"cd_apy": 4.0, "provenance": {"cd": ri.make_source(ri.SITE, url="https://www.summitbank.example/cd", verified=True)}}
        ret, row = self.run_reg(res, prior_failures=3)
        self.assertEqual((row["consecutive_failures"], row["needs_review"], row["last_successful_url"]), (0, False, "https://www.summitbank.example/cd"))
        self.assertIsNone(ret)

    def test_search_only_values_count_as_a_failure(self):
        res = {"cd_apy": 4.0, "provenance": {"cd": ri.make_source(ri.SEARCH, label="[Search] x")}}
        ret, row = self.run_reg(res, prior_failures=2)
        self.assertEqual((row["consecutive_failures"], row["needs_review"]), (3, True))
        self.assertEqual(ret, 3)

    def test_a_skipped_bank_advances_its_count_so_the_next_probe_can_come(self):
        res = {"cd_apy": None, "status": "Skipped", "breaker_skipped": True}
        ret, row = self.run_reg(res, prior_failures=3)
        self.assertEqual(row["consecutive_failures"], 4)    # 4 % PROBE_EVERY == 0: the breaker is no longer "open"

    def test_an_old_result_without_provenance_uses_only_its_own_source(self):
        res = {"cd_apy": 4.0, "source_url_cd": "https://www.summitbank.example/cd", "source_url": "https://www.summitbank.example/other"}
        ret, row = self.run_reg(res)
        self.assertEqual(row["last_successful_url"], "https://www.summitbank.example/cd")

    def test_the_breaker_marks_the_row_skipped_not_found_and_says_which_bank(self):
        self.assertIn('result["status"] = STATUS_SKIPPED', SCRAPERS_SRC)
        self.assertIn("SKIPPED by the circuit breaker for {bank['bank_name']}", SCRAPERS_SRC)
        self.assertIn("await update_bank_registry(bank, result)", SCRAPERS_SRC)
        i = SCRAPERS_SRC.index('result["status"] = STATUS_SKIPPED')
        j = SCRAPERS_SRC.index('crawl_state["results"].append(result)', i)
        self.assertLess(i, j)


class ScopeAndDateRowTests(unittest.TestCase):
    TODAY = __import__("datetime").date(2026, 11, 17)

    def result(self, scope="South Carolina", as_of="2026-03-01", raw="03/01/2026", state="SC", name="Summit Bank"):
        return {"bank_name": name, "bank_url": "https://www.summitbank.example", "state": state, "checking_apy": 0.15,
                "provenance": {"checking": ri.make_source(ri.SITE, url="https://www.summitbank.example/rates", verified=True)},
                "rate_meta": {"checking": {"scope": scope, "scope_raw": "x", "zip": None, "as_of": as_of, "as_of_raw": raw, "as_of_order": "month/day/year" if as_of else None}}}

    def row(self, results, i=0):
        return storage.build_rate_observations(results, "run1", extra_cols=True, today=self.TODAY)[i]

    def test_an_old_page_date_makes_the_value_low_and_flagged_but_kept(self):
        r = self.row([self.result()])        # 261 days old
        self.assertEqual((r["apy"], r["confidence"]), (0.15, "low"))
        self.assertIn("stale_page_date", r["review_flags"])

    def test_a_recent_page_date_keeps_high(self):
        r = self.row([self.result(as_of="2026-11-01", raw="11/01/2026")])
        self.assertEqual(r["confidence"], "high")
        self.assertNotIn("stale_page_date", r["review_flags"])

    def test_scope_and_date_are_in_the_note_with_the_raw_text(self):
        r = self.row([self.result(as_of="2026-11-01", raw="11/01/2026")])
        self.assertIn("scope: South Carolina; rates dated 11/01/2026 (read month/day/year)", r["tier_note"])
        self.assertIn("regional_scope", r["review_flags"])

    def test_an_unparsed_date_is_flagged_and_never_makes_a_value_stale(self):
        r = self.row([self.result(as_of=None, raw="25/03/2026")])
        self.assertIn("as_of_unparsed", r["review_flags"])
        self.assertNotIn("stale_page_date", r["review_flags"])
        self.assertEqual(r["confidence"], "high")

    def test_a_search_value_is_not_made_stale_by_a_page_date(self):
        res = self.result()
        res["provenance"]["checking"] = ri.make_source(ri.SEARCH, label="[Search] x")
        r = self.row([res])
        self.assertNotIn("stale_page_date", r["review_flags"])

    def test_the_note_joins_the_tier_note_and_the_scope(self):
        res = self.result(as_of="2026-11-01", raw="11/01/2026")
        res["tier_notes"] = {"checking": "tiered: Gold 0.15%"}
        self.assertTrue(self.row([res])["tier_note"].startswith("tiered: Gold 0.15% | scope: South Carolina"))

    def test_a_state_other_than_the_banks_home_state_is_flagged(self):
        r = self.row([self.result(scope="Texas", as_of="2026-11-01", raw="11/01/2026", state="SC")])
        self.assertIn("scope_differs_from_home_state", r["review_flags"])
        self.assertNotIn("scope_differs_from_home_state", self.row([self.result(scope="South Carolina", as_of="2026-11-01", raw="11/01/2026")])["review_flags"])

    def test_a_state_other_than_the_one_most_pages_of_the_run_state_is_flagged(self):
        run = [self.result(scope="South Carolina", name="A"), self.result(scope="South Carolina", name="B"), self.result(scope="Texas", name="C")]
        rows = storage.build_rate_observations(run, "run1", extra_cols=True, today=self.TODAY)
        self.assertEqual([("scope_differs_from_run" in r["review_flags"]) for r in rows], [False, False, True])

    def test_a_run_with_one_state_flags_nothing_against_the_run(self):
        rows = storage.build_rate_observations([self.result(name="A"), self.result(name="B")], "run1", extra_cols=True, today=self.TODAY)
        self.assertFalse(any("scope_differs_from_run" in r["review_flags"] for r in rows))

    def test_first_class_market_columns(self):
        res = self.result(scope="Texas", as_of="2026-11-01", raw="11/01/2026", state="TX")
        res["rate_meta"]["checking"]["zip"] = "76109"
        r = self.row([res])
        self.assertEqual((r["market_zip"], r["scope_state"]), ("76109", "TX"))

    def test_market_zip_falls_back_to_the_zip_the_run_asked_for(self):
        res = self.result()
        res["market_zip_used"] = "29201"
        self.assertEqual(self.row([res])["market_zip"], "29201")
        res["market_zip_used"] = "2920"
        self.assertIsNone(self.row([res])["market_zip"])       # not five digits: never stored (the table would refuse it)

    def test_no_scope_means_null_market_fields(self):
        res = self.result()
        res["rate_meta"] = {}
        r = self.row([res])
        self.assertEqual((r["market_zip"], r["scope_state"]), (None, None))

    def test_scope_state_is_a_two_capital_code_or_null(self):
        self.assertEqual(self.row([self.result(scope="South Carolina")])["scope_state"], "SC")
        self.assertIsNone(self.row([self.result(scope="Atlantis")])["scope_state"])

    def test_default_rows_have_no_market_columns(self):
        r = storage.build_rate_observations([self.result()], "run1", today=self.TODAY)[0]
        self.assertNotIn("market_zip", r)
        self.assertNotIn("scope_state", r)

    def test_the_crawler_keeps_the_page_meta_with_the_value(self):
        self.assertIn("_meta = find_rate_page_meta(_judge)", SCRAPERS_SRC)
        self.assertIn("rate_meta[k] = _meta", SCRAPERS_SRC)
        self.assertIn('"rate_meta":                 {k: dict(v) for k, v in rate_meta.items()},', SCRAPERS_SRC)


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

    def test_followed_links_are_checked_against_robots_txt(self):
        self.assertIn("if from_link and not await robots_allows(url, http_session):", SCRAPERS_SRC)
        self.assertEqual(SCRAPERS_SRC.count("from_link=True)"), 3)      # detail links, rates links, general rate links
        self.assertIn("async def visit(url, priority=False, from_link=False):", SCRAPERS_SRC)

    def test_a_page_that_links_onward_to_rates_does_not_record_not_published(self):
        self.assertIn("onward = [u for u in rates_page_links if u not in visited and u != url]", SCRAPERS_SRC)
        i = SCRAPERS_SRC.index("            if not onward:\n                for _np, _ev in _np_found.items():")
        self.assertGreater(i, 0)
        self.assertNotIn("not_published.add(_np)\n                    _np_found", SCRAPERS_SRC)

    def test_the_onward_rates_pages_are_followed_while_a_product_is_missing(self):
        self.assertIn("if onward and not all(best[k] is not None for k in core_keys):", SCRAPERS_SRC)
        self.assertIn("rates_page_links = await collect_rates_page_links(links, domain)", SCRAPERS_SRC)

    def test_the_breaker_marks_its_results(self):
        self.assertIn('result["breaker_skipped"] = True', SCRAPERS_SRC)

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
        for key in ('"provenance":', '"tier_notes":', '"not_published":', '"not_published_evidence":', '"identity":', '"source_url_money_market":', '"market_zip_used":'):
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

    def test_market_zip_from_the_row_then_the_run_setting_only_five_digits(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("RATE_RADAR_MARKET_ZIP", None)
            self.assertIsNone(self.s.market_zip({}))
            self.assertEqual(self.s.market_zip({"market_zip": "76109"}), "76109")
            self.assertEqual(self.s.market_zip({"market_zip": " 76109-1234 "}), "76109")
            self.assertIsNone(self.s.market_zip({"market_zip": "7610"}))
            self.assertIsNone(self.s.market_zip({"market_zip": "abcde"}))
        with mock.patch.dict(os.environ, {"RATE_RADAR_MARKET_ZIP": "29201"}):
            self.assertEqual(self.s.market_zip({}), "29201")
            self.assertEqual(self.s.market_zip({"market_zip": "76109"}), "76109")      # the row wins over the run setting
            self.assertEqual(self.s.market_zip({"market_zip": "bad"}), "29201")

    def test_the_market_zip_overrides_the_learned_and_branch_zip(self):
        self.assertIn('bank_zip = market_zip(bank) or _health.get("zip_gate_zip") or get_bank_zip(bank)', SCRAPERS_SRC)

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
