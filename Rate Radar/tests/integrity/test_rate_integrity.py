"""Tests for rate_integrity (stdlib unittest: python -m unittest discover -s tests/integrity).

Run from the "Rate Radar" folder. Fixtures use INVENTED bank names and rate values shaped like the pages that went wrong in run 20261117_103944
(tiered money-market blocks, a rate table whose APY column is only in the header, a site branded for a different bank, an "inquire for rates" page).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import rate_integrity as ri  # noqa: E402

BANK_URL = "https://www.harborsavings.example"
SRC = "https://www.harborsavings.example/banking/cd/"


def site(verified=True, url=SRC):
    return ri.make_source(ri.SITE, url=url, verified=verified)


class ResolveTests(unittest.TestCase):
    def test_site_verified_on_own_domain_is_high(self):
        self.assertEqual(ri.resolve(site(), BANK_URL, "ok"), (ri.SITE, ri.HIGH, SRC, []))

    def test_site_identity_not_checked_keeps_high(self):
        self.assertEqual(ri.resolve(site(), BANK_URL, None)[1], ri.HIGH)
        self.assertEqual(ri.resolve(site(), BANK_URL, "unknown")[1], ri.HIGH)

    def test_site_identity_mismatch_is_low_and_flagged(self):
        m, c, u, f = ri.resolve(site(), BANK_URL, "mismatch")
        self.assertEqual((m, c, u), (ri.SITE, ri.LOW, SRC))
        self.assertIn("identity_mismatch", f)

    def test_site_value_not_verified_in_text_is_medium(self):
        m, c, u, f = ri.resolve(site(verified=False), BANK_URL, "ok")
        self.assertEqual(c, ri.MEDIUM)
        self.assertIn("not_in_page_text", f)

    def test_site_off_domain_is_medium_and_flagged(self):
        m, c, u, f = ri.resolve(site(url="https://other.example/rates"), BANK_URL, "ok")
        self.assertEqual(c, ri.MEDIUM)
        self.assertIn("off_domain", f)

    def test_site_without_a_url_is_not_a_site_value(self):
        m, c, u, f = ri.resolve(ri.make_source(ri.SITE, url=None, verified=True), BANK_URL, "ok")
        self.assertEqual((m, c, u), (ri.SEARCH, ri.LOW, None))
        self.assertIn("source_unknown", f)

    def test_ai_value_found_in_page_text_is_medium_never_high(self):
        m, c, u, f = ri.resolve(ri.make_source(ri.AI_VISION, url=SRC, verified=True), BANK_URL, "ok")
        self.assertEqual((m, c, u, f), (ri.AI_VISION, ri.MEDIUM, SRC, []))

    def test_ai_value_not_in_page_text_is_low_and_flagged(self):
        m, c, u, f = ri.resolve(ri.make_source(ri.AI_VISION, url=SRC, verified=False), BANK_URL, "ok")
        self.assertEqual((m, c), (ri.AI_VISION, ri.LOW))
        self.assertIn("not_in_page_text", f)

    def test_ai_value_on_a_page_of_another_bank_is_low(self):
        m, c, u, f = ri.resolve(ri.make_source(ri.AI_VISION, url=SRC, verified=True), BANK_URL, "mismatch")
        self.assertEqual(c, ri.LOW)
        self.assertIn("identity_mismatch", f)

    def test_search_is_always_low_and_never_borrows_a_page_url(self):
        m, c, u, f = ri.resolve(ri.make_source(ri.SEARCH, label="[Search] Bank's own site (harborsavings.example)"), BANK_URL, "ok")
        self.assertEqual((m, c), (ri.SEARCH, ri.LOW))
        self.assertEqual(u, "[Search] Bank's own site (harborsavings.example)")
        self.assertIn("search_only", f)
        self.assertIsNone(ri.resolve(ri.make_source(ri.SEARCH), BANK_URL, "ok")[2])

    def test_search_without_a_label_has_no_source_even_when_given_a_url(self):
        self.assertIsNone(ri.resolve(ri.make_source(ri.SEARCH, url=SRC), BANK_URL, "ok")[2])

    def test_search_never_high_even_with_a_verified_flag(self):
        self.assertEqual(ri.resolve(ri.make_source(ri.SEARCH, url=SRC, verified=True), BANK_URL, "ok")[1], ri.LOW)

    def test_last_known_good_is_low(self):
        self.assertEqual(ri.resolve(ri.make_source(ri.LAST_KNOWN_GOOD), BANK_URL, "ok")[:2], (ri.LAST_KNOWN_GOOD, ri.LOW))

    def test_aggregator_is_medium(self):
        self.assertEqual(ri.resolve(ri.make_source(ri.AGGREGATOR, url="https://www.depositaccounts.com/x"), BANK_URL, "ok")[:2], (ri.AGGREGATOR, ri.MEDIUM))

    def test_no_recorded_source_is_low_with_no_url(self):
        self.assertEqual(ri.resolve(None, BANK_URL, "ok"), (ri.SEARCH, ri.LOW, None, ["source_unknown"]))

    def test_unknown_method_is_low(self):
        self.assertEqual(ri.resolve({"method": "telepathy", "url": SRC}, BANK_URL, "ok")[1], ri.LOW)

    def test_manual_confirmation_path_is_unchanged(self):
        self.assertEqual(ri.resolve(None, BANK_URL, "mismatch", edited=True)[:2], (ri.SITE, ri.HIGH))


class LegacyEntryTests(unittest.TestCase):
    def test_savings_is_not_mistaken_for_high_yield_savings_note(self):
        note = "CD 4.03% | high_yield_savings from search: 3.56"
        e = ri.legacy_entry("savings", "[AI] https://www.harborsavings.example/savings/", note)
        self.assertEqual(e["method"], ri.AI_VISION)
        self.assertEqual(ri.legacy_entry("high_yield_savings", "", note)["method"], ri.SEARCH)

    def test_whole_word_from_search_note_for_the_product(self):
        self.assertEqual(ri.legacy_entry("savings", "", "x | savings from search: 0.5")["method"], ri.SEARCH)
        self.assertEqual(ri.legacy_entry("money_market", "", "x | money_market from search: 0.55")["method"], ri.SEARCH)

    def test_search_label_url(self):
        e = ri.legacy_entry("cd", "[Search] Different domain than a.example's known site (b.example) - verify", "")
        self.assertEqual(e["method"], ri.SEARCH)
        self.assertTrue(e["label"].startswith("[Search]"))

    def test_aggregator_url(self):
        self.assertEqual(ri.legacy_entry("cd", "https://www.depositaccounts.com/banks/x.html", "")["method"], ri.AGGREGATOR)

    def test_http_url_is_a_site_value(self):
        e = ri.legacy_entry("cd", SRC, "")
        self.assertEqual((e["method"], e["url"], e["verified"]), (ri.SITE, SRC, True))

    def test_no_product_source_means_no_entry_not_the_general_url(self):
        self.assertIsNone(ri.legacy_entry("money_market", "", "CD 4.03% | Source: " + SRC))

    def test_last_known_good_note(self):
        self.assertEqual(ri.legacy_entry("cd", SRC, "⏱ No fresh data found today - showing last confirmed rate")["method"], ri.LAST_KNOWN_GOOD)


class ValueInTextTests(unittest.TestCase):
    def test_present_value(self):
        self.assertTrue(ri.value_in_text(4.75, "12-month CD 4.75% APY"))
        self.assertTrue(ri.value_in_text(3.5, "3.50% APY"))
        self.assertTrue(ri.value_in_text(3.875, "rate 3.875% apy"))
        self.assertTrue(ri.value_in_text(1.0, "Edge 1.00% APY"))

    def test_absent_value(self):
        self.assertFalse(ri.value_in_text(0.01, "Savings 0.12% APY and 0.42% APY"))

    def test_value_that_is_a_suffix_of_a_larger_number_is_absent(self):
        self.assertFalse(ri.value_in_text(4.75, "rate 14.75% APY"))
        self.assertFalse(ri.value_in_text(0.5, "10.50% APY"))

    def test_a_number_without_a_percent_sign_is_not_a_rate(self):
        self.assertFalse(ri.value_in_text(4.75, "Call 4.75 today. Rates 3.00%"))
        self.assertFalse(ri.value_in_text(4.75, "version 4.75 released"))

    def test_empty_inputs(self):
        self.assertFalse(ri.value_in_text(None, "4.75%"))
        self.assertFalse(ri.value_in_text(4.75, ""))
        self.assertFalse(ri.value_in_text("abc", "4.75%"))


class VerifyAiValuesTests(unittest.TestCase):
    PAGE = "Savings 0.12% APY. Money Market 0.05% APY. Premium 0.62% APY. Edge 1.00% APY."

    def test_value_on_the_page_is_kept_and_verified(self):
        kept, dropped, ver = ri.verify_ai_values({"savings": 0.12, "money_market": 0.05, "cd": None}, self.PAGE)
        self.assertEqual((kept["savings"], kept["money_market"]), (0.12, 0.05))
        self.assertEqual(dropped, [])
        self.assertTrue(ver["savings"] and ver["money_market"])

    def test_value_not_on_a_page_that_shows_rates_is_dropped(self):
        kept, dropped, ver = ri.verify_ai_values({"savings": 0.01, "money_market": 0.01}, self.PAGE)
        self.assertIsNone(kept["savings"])
        self.assertIsNone(kept["money_market"])
        self.assertEqual(sorted(dropped), [("money_market", 0.01), ("savings", 0.01)])

    def test_image_only_page_keeps_the_value_unverified(self):
        kept, dropped, ver = ri.verify_ai_values({"savings": 2.0}, "Welcome to our bank. Apply now.")
        self.assertEqual(kept["savings"], 2.0)
        self.assertFalse(ver["savings"])
        self.assertEqual(dropped, [])

    def test_made_up_cd_ladder_is_dropped_and_the_term_cleared(self):
        ai = {"cd": 4.75, "cd_term": "12-month", "cd_ladder": [{"term": "12-month", "apy": 4.75}, {"term": "24-month", "apy": 4.65}]}
        kept, dropped, ver = ri.verify_ai_values(ai, self.PAGE)
        self.assertIsNone(kept["cd"])
        self.assertIsNone(kept["cd_term"])
        self.assertIsNone(kept["cd_ladder"])

    def test_ladder_keeps_only_the_rungs_on_the_page(self):
        page = "3-month 3.00% APY 6-month 4.10% APY 12-month 4.35% APY"
        ai = {"cd": 4.35, "cd_term": "12-month", "cd_ladder": [{"term": "6-month", "apy": 4.10}, {"term": "9-month", "apy": 4.99}]}
        kept, dropped, ver = ri.verify_ai_values(ai, page)
        self.assertEqual(kept["cd_ladder"], [{"term": "6-month", "apy": 4.10}])

    def test_input_is_not_modified(self):
        ai = {"savings": 0.01}
        ri.verify_ai_values(ai, self.PAGE)
        self.assertEqual(ai, {"savings": 0.01})


# A money-market block as it appears in textContent: every tier on one run, no spaces between blocks.
MM_TEXT = ("Savings Accounts. Whether you are saving for tuition or a nest egg, we have competitive interest rates. "
           "Money Market Keep your balance up and avoid an extra fee with this account. "
           "$2,500 minimum to open$10,000 or less -- 0.05% APY$10,000.01 to $25,000 -- 0.62% APY"
           "Balances over $25,000.01 -- 0.10% APYNo balance requirement fee if account stays above $2,500Mobile Check Deposit "
           "Premium Money Market This is a premium choice for anyone needing to service a larger balance. "
           "$25,000 minimum to open$25,000 or less -- 0.05% APY$25,000.01 to $75,000 -- 0.62% APYBalances over $75,000.01 -- 0.62% APYNo fee ")
EDGE_TEXT = ("Personal Edge Premium Money Market This is a premium choice. $25,000 minimum to open "
             "$50,000 or less -- 0.62% APY $50,001 to $250,000 -- 3.17% APY $250,001 or over -- 3.56% APY ")
SAV_TEXT = ("Type Interest Rate ** APY* (based on daily compounding) Savings Accounts ($2,499 or less) 0.12% 0.12% "
            "Savings Accounts ($2,500 or more) 0.42% 0.42% *The interest rate and APY may change after account opening.")


class TierTests(unittest.TestCase):
    def test_rule_is_stated(self):
        self.assertIn("minimum opening balance", ri.TIER_RULES["entry"])
        self.assertIn("never report the top tier", ri.TIER_RULES["entry"])
        self.assertIn("HIGHEST advertised rate", ri.TIER_RULES["highest"])

    def test_the_setting_is_one_documented_environment_variable(self):
        self.assertEqual(ri.TIER_RULE_ENV, "RATE_RADAR_TIER_RULE")
        self.assertEqual(ri.DEFAULT_TIER_RULE, "highest")
        self.assertEqual(sorted(ri.TIER_RULES), ["entry", "highest"])

    def test_default_rule_is_the_highest_advertised_rate(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            self.assertEqual(ri.tier_mode(), "highest")
            self.assertEqual(ri.tier_rule(), ri.TIER_RULES["highest"])

    def test_one_setting_switches_the_rule(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            self.assertEqual(ri.tier_mode(), "entry")
            self.assertEqual(ri.tier_rule(), ri.TIER_RULES["entry"])
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "  ENTRY "}):
            self.assertEqual(ri.tier_mode(), "entry")

    def test_an_unknown_setting_falls_back_to_the_default(self):
        for v in ("", "top", "entry-rate", "1"):
            with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: v}):
                self.assertEqual(ri.tier_mode(), "highest", v)

    def test_highest_tier_with_ties_takes_the_lowest_balance_tier(self):
        tiers = [{"lo": 10000.0, "hi": None, "apy": 1.0}, {"lo": 0.0, "hi": 9999.0, "apy": 1.0}, {"lo": 20.0, "hi": 30.0, "apy": 0.5}]
        self.assertEqual(ri.pick_highest_tier(tiers)["lo"], 0.0)
        self.assertIsNone(ri.pick_highest_tier([]))

    def test_pick_tier_follows_the_mode(self):
        tiers = [{"lo": 0.0, "hi": 50000.0, "apy": 0.62}, {"lo": 50000.01, "hi": 250000.0, "apy": 3.17}, {"lo": 250000.01, "hi": None, "apy": 3.56}]
        self.assertEqual(ri.pick_tier(tiers, 25000.0, "entry")["apy"], 0.62)
        self.assertEqual(ri.pick_tier(tiers, 25000.0, "highest")["apy"], 3.56)
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "highest"}):
            self.assertEqual(ri.pick_tier(tiers, 25000.0)["apy"], 3.56)

    def test_highest_mode_reports_the_top_tier_and_still_lists_the_others(self):
        r = ri.extract_tiered_products(EDGE_TEXT, mode="highest")["money_market"]
        self.assertEqual(r["apy"], 3.56)
        self.assertIn("highest advertised rate", r["note"])
        self.assertIn("other tiers", r["note"])
        self.assertIn("0.62% up to $50,000", r["note"])

    def test_the_setting_drives_the_parser_when_no_mode_is_passed(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "highest"}):
            self.assertEqual(ri.extract_tiered_products(EDGE_TEXT)["money_market"]["apy"], 3.56)
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "entry"}):
            self.assertEqual(ri.extract_tiered_products(EDGE_TEXT)["money_market"]["apy"], 0.62)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            self.assertEqual(ri.extract_tiered_products(EDGE_TEXT)["money_market"]["apy"], 3.56)

    def test_pick_tier_at_the_opening_minimum(self):
        tiers = [{"lo": 25000.01, "hi": 75000.0, "apy": 0.62}, {"lo": 0.0, "hi": 25000.0, "apy": 0.05}, {"lo": 75000.01, "hi": None, "apy": 0.62}]
        self.assertEqual(ri.pick_entry_tier(tiers, 25000.0)["apy"], 0.05)
        self.assertEqual(ri.pick_entry_tier(tiers, 50000.0)["apy"], 0.62)

    def test_opening_minimum_exactly_at_a_tier_ceiling_belongs_to_that_tier(self):
        tiers = [{"lo": 0.0, "hi": 100.0, "apy": 1.0}, {"lo": 100.01, "hi": 200.0, "apy": 2.0}, {"lo": 200.01, "hi": None, "apy": 3.0}]
        self.assertEqual(ri.pick_entry_tier(tiers, 200.0)["apy"], 2.0)
        self.assertEqual(ri.pick_entry_tier(tiers, 100.0)["apy"], 1.0)

    def test_an_opening_minimum_in_a_gap_between_tiers_falls_back_to_the_lowest_tier(self):
        tiers = [{"lo": 0.0, "hi": 100.0, "apy": 1.0}, {"lo": 200.0, "hi": None, "apy": 3.0}]
        self.assertEqual(ri.pick_entry_tier(tiers, 150.0)["apy"], 1.0)
        self.assertEqual(ri.pick_entry_tier(tiers, 50.0)["apy"], 1.0)

    def test_pick_lowest_tier_without_a_minimum(self):
        tiers = [{"lo": 2500.0, "hi": None, "apy": 0.42}, {"lo": 0.0, "hi": 2499.0, "apy": 0.12}]
        self.assertEqual(ri.pick_entry_tier(tiers, None)["apy"], 0.12)

    def test_pick_never_the_top_tier_by_default(self):
        tiers = [{"lo": 0.0, "hi": 50000.0, "apy": 0.62}, {"lo": 50000.01, "hi": 250000.0, "apy": 3.17}, {"lo": 250000.01, "hi": None, "apy": 3.56}]
        self.assertEqual(ri.pick_entry_tier(tiers)["apy"], 0.62)

    def test_pick_on_empty(self):
        self.assertIsNone(ri.pick_entry_tier([]))

    def test_money_market_block_uses_the_rate_at_the_opening_minimum(self):
        r = ri.extract_tiered_products(MM_TEXT, mode="entry")
        mm = r["money_market"]
        self.assertEqual(mm["apy"], 0.05)
        self.assertEqual(mm["min_open"], 2500.0)
        self.assertEqual(len(mm["tiers"]), 3)
        self.assertIn("0.62% $10,000 to $25,000", mm["note"].replace("$10,000.01", "$10,000"))
        self.assertIn("other tiers", mm["note"])

    def test_entry_rule_uses_the_first_plain_block_and_files_the_premium_block_as_high_yield(self):
        r = ri.extract_tiered_products(MM_TEXT, mode="entry")
        self.assertEqual(r["money_market"]["min_open"], 2500.0)   # not the $25,000 premium block
        self.assertEqual(r["high_yield_savings"]["min_open"], 25000.0)
        self.assertEqual(r["high_yield_savings"]["apy"], 0.05)

    def test_default_rule_reports_the_highest_advertised_rate_with_its_conditions(self):
        r = ri.extract_tiered_products(MM_TEXT)
        mm = r["money_market"]
        self.assertEqual(mm["apy"], 0.62)
        self.assertIn("0.62% $10,000 to $25,000", mm["note"].replace("$10,000.01", "$10,000"))
        self.assertIn("highest advertised rate", mm["note"])
        self.assertIn("$2,500 minimum to open", mm["note"])
        self.assertIn("other tiers", mm["note"])
        self.assertIn("0.05% up to $10,000", mm["note"])
        hy = r["high_yield_savings"]
        self.assertEqual(hy["apy"], 0.62)
        self.assertIn("$25,000 minimum to open", hy["note"])

    def test_a_premium_block_on_its_own_is_the_money_market(self):
        r = ri.extract_tiered_products(EDGE_TEXT, mode="entry")
        self.assertEqual(r["money_market"]["apy"], 0.62)
        self.assertEqual(r["money_market"]["min_open"], 25000.0)
        self.assertNotIn("high_yield_savings", r)
        top = ri.extract_tiered_products(EDGE_TEXT)
        self.assertEqual(top["money_market"]["apy"], 3.56)
        self.assertIn("from $250,001", top["money_market"]["note"])
        self.assertIn("$25,000 minimum to open", top["money_market"]["note"])

    def test_several_premium_blocks_report_the_highest_one_by_default_and_the_first_for_entry(self):
        r = ri.extract_tiered_products(MM_TEXT + " " + EDGE_TEXT)
        self.assertEqual(r["money_market"]["apy"], 0.62)
        self.assertEqual(r["high_yield_savings"]["apy"], 3.56)
        e = ri.extract_tiered_products(MM_TEXT + " " + EDGE_TEXT, mode="entry")
        self.assertEqual(e["high_yield_savings"]["apy"], 0.05)

    def test_rate_table_with_apy_only_in_the_header(self):
        sav = ri.extract_tiered_products(SAV_TEXT)["savings"]
        self.assertEqual(sav["apy"], 0.42)
        self.assertIsNone(sav["min_open"])
        self.assertIn("0.42% from $2,500", sav["note"])
        self.assertIn("highest advertised rate", sav["note"])
        self.assertIn("0.12% up to $2,499", sav["note"])
        entry = ri.extract_tiered_products(SAV_TEXT, mode="entry")["savings"]
        self.assertEqual(entry["apy"], 0.12)
        self.assertIn("lowest balance tier", entry["note"])

    def test_table_row_rate_and_apy_are_told_apart(self):
        r = ri.extract_tiered_products("Savings Accounts ($2,499 or less) 0.10% 0.12% Savings Accounts ($2,500 or more) 0.40% 0.44%")["savings"]
        self.assertEqual(r["apy"], 0.44)
        self.assertEqual(sorted(t["apy"] for t in r["tiers"]), [0.12, 0.44])

    def test_no_tiers_means_no_result(self):
        self.assertEqual(ri.extract_tiered_products("Savings 2.50% APY. CD 12-month 4.00% APY."), {})
        self.assertEqual(ri.extract_tiered_products(""), {})

    def test_tier_lines_without_a_product_keyword_are_ignored(self):
        self.assertEqual(ri.extract_tiered_products("$1,000 or less -- 0.50% APY $1,000.01 to $5,000 -- 0.75% APY"), {})


CHECKING_TEXT = ("Harbor Edge Checking This is our rewards checking. Balances up to $25,000 -- 1.00% APY Balances over $25,000 -- 0.10% APY "
                 "Mobile Check Deposit ")
FULL_PAGE = CHECKING_TEXT + MM_TEXT + EDGE_TEXT + SAV_TEXT


class HighestAdvertisedRateTests(unittest.TestCase):
    """Francisco's rule (17 Nov 2026): record the HIGHEST advertised rate of the product, with its conditions and the other tiers in the note."""

    def setUp(self):
        self.r = ri.extract_tiered_products(FULL_PAGE)

    def test_the_four_values(self):
        self.assertEqual(self.r["savings"]["apy"], 0.42)
        self.assertEqual(self.r["money_market"]["apy"], 0.62)
        self.assertEqual(self.r["checking"]["apy"], 1.00)
        self.assertEqual(self.r["high_yield_savings"]["apy"], 3.56)

    def test_conditions_are_in_the_note(self):
        self.assertIn("$2,500 minimum to open", self.r["money_market"]["note"])
        self.assertIn("0.62% $10,000 to $25,000", self.r["money_market"]["note"].replace("$10,000.01", "$10,000"))
        self.assertIn("1.00% up to $25,000", self.r["checking"]["note"])
        self.assertIn("from $250,001", self.r["high_yield_savings"]["note"])
        self.assertIn("$25,000 minimum to open", self.r["high_yield_savings"]["note"])
        self.assertIn("0.42% from $2,500", self.r["savings"]["note"])

    def test_the_other_tiers_are_listed(self):
        self.assertIn("other tiers: 0.10% from $25,000", self.r["checking"]["note"])
        self.assertIn("0.62% up to $50,000", self.r["high_yield_savings"]["note"])
        self.assertIn("3.17%", self.r["high_yield_savings"]["note"])
        self.assertIn("0.12% up to $2,499", self.r["savings"]["note"])

    def test_the_entry_rule_remains_available(self):
        e = ri.extract_tiered_products(FULL_PAGE, mode="entry")
        self.assertEqual((e["savings"]["apy"], e["money_market"]["apy"], e["checking"]["apy"], e["high_yield_savings"]["apy"]), (0.12, 0.05, 1.00, 0.05))


SUMMIT_TABLES = (
    "Interest Rates\n"
    "Checking\n"
    "Product\tBalance Tiers to Obtain APY\tInterest Rate\tAPY\n"
    "Choice Checking\t$0.01 and above\t0.08%\t0.08%\n"
    "Gold Checking\tUp to $99,999.99\t0.06%\t0.06%\n"
    "\t$100,000 and above\t0.15%\t0.15%\n"
    "Community Hero Checking\t$0.01 and above\t0.08%\t0.08%\n"
    "Savings\n"
    "Personal Savings\t$0.01 and above\t0.15%\t0.15%\n"
    "HSA Savings\t$0.01 - $4,999.99\t0.04%\t0.04%\n"
    "\t$5,000 and above\t0.10%\t0.10%\n"
    "Money Market\n"
    "Personal Money Market\tUnder $2,000\t0.00%\t0.00%\n"
    "\t$2,000 - $9,999.99\t0.10%\t0.10%\n"
    "\t$10,000 - $24,999.99\t0.15%\t0.15%\n"
    "\t$100,000 and above\t0.50%\t0.50%\n"
    "Certificates of Deposit\n"
    "Term\tInterest Rate\tAPY\n"
    "6 months\t3.11%\t3.15%\n"
    "7 months\t3.46%\t3.51%\n"
    "12 months\t1.73%\t1.75%\n")


class RateTableTests(unittest.TestCase):
    def test_highest_rate_of_each_product_family(self):
        r = ri.extract_rate_tables(SUMMIT_TABLES, mode="highest")
        self.assertEqual((r["checking"]["apy"], r["savings"]["apy"], r["money_market"]["apy"]), (0.15, 0.15, 0.50))
        self.assertEqual(r["checking"]["name"], "Gold Checking")
        self.assertNotIn("high_yield_savings", r)

    def test_default_rule_is_the_highest(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            self.assertEqual(ri.extract_rate_tables(SUMMIT_TABLES)["money_market"]["apy"], 0.50)

    def test_entry_rule_takes_the_first_product_and_its_lowest_band(self):
        r = ri.extract_rate_tables(SUMMIT_TABLES, mode="entry")
        self.assertEqual((r["checking"]["apy"], r["savings"]["apy"]), (0.08, 0.15))
        self.assertEqual(r["checking"]["name"], "Choice Checking")
        self.assertEqual(r["money_market"]["apy"], 0.10)      # the 0.00% band is not a rate

    def test_zero_rate_bands_are_not_rates(self):
        r = ri.extract_rate_tables("Money Market\nBasic Money Market\t$0 and above\t0.00%\t0.00%\n")
        self.assertEqual(r, {})

    def test_note_names_the_product_the_band_and_the_others(self):
        n = ri.extract_rate_tables(SUMMIT_TABLES, mode="highest")["checking"]["note"]
        self.assertIn("Gold Checking: 0.15% from $100,000", n)
        self.assertIn("highest advertised rate", n)
        self.assertIn("other tiers: 0.06% up to $99,999.99", n)
        self.assertIn("other products: Choice Checking: 0.08% from $0.01; Community Hero Checking: 0.08% from $0.01", n)

    def test_the_product_name_carries_down_to_the_tier_rows_that_leave_it_blank(self):
        tiers = ri.extract_rate_tables(SUMMIT_TABLES)["money_market"]["tiers"]
        self.assertEqual(sorted(t["apy"] for t in tiers), [0.10, 0.15, 0.50])      # three bands of the one product

    def test_cd_rows_are_terms_with_their_apy(self):
        self.assertEqual(ri.extract_table_cd_rows(SUMMIT_TABLES), [("6-month", 3.15), ("7-month", 3.51), ("12-month", 1.75)])

    def test_cd_terms_in_years_are_converted(self):
        self.assertEqual(ri.extract_table_cd_rows("CDs\n2 years\t4.00%\t4.07%\n"), [("24-month", 4.07)])

    def test_rows_before_any_product_heading_are_ignored(self):
        self.assertEqual(ri.extract_rate_tables("Choice Checking\t$0.01 and above\t0.08%\t0.08%\n"), {})
        self.assertEqual(ri.extract_table_cd_rows("12 months\t1.73%\t1.75%\n"), [])

    def test_prose_lines_with_a_percentage_are_not_rows(self):
        self.assertEqual(ri.extract_rate_tables("Savings\nEarn up to 2.00% on your savings with us today.\n"), {})

    def test_a_row_with_dollar_amounts_that_are_not_a_band_is_ignored(self):
        self.assertEqual(ri.extract_rate_tables("Checking\nSome Checking\tMonthly fee $12.00\t0.10%\t0.10%\n"), {})

    def test_rows_without_a_band_are_one_flat_rate(self):
        r = ri.extract_rate_tables("Savings\nStandard Savings\t0.25%\t0.25%\n")
        self.assertEqual(r["savings"]["apy"], 0.25)

    def test_band_cell_variants(self):
        f = ri._tier_from_cell
        self.assertEqual(f("$0.01 and above"), (0.01, None))
        self.assertEqual(f("Up to $99,999.99"), (0.0, 99999.99))
        self.assertEqual(f("$2,500 or less"), (0.0, 2500.0))
        self.assertEqual(f("Under $2,000"), (0.0, 2000.0))
        self.assertEqual(f("$2,000 - $9,999.99"), (2000.0, 9999.99))
        self.assertEqual(f("$10,000 to $24,999.99"), (10000.0, 24999.99))
        self.assertEqual(f("$100,000+"), (100000.0, None))
        self.assertEqual(f("$100,000 or more"), (100000.0, None))
        self.assertEqual(f("Over $250,000"), (250000.0, None))
        self.assertEqual(f("$500"), (500.0, None))
        self.assertIsNone(f("Choice Checking"))
        self.assertIsNone(f("0.08%"))

    def test_section_headings(self):
        for head, ptype in (("Money Market", "money_market"), ("Money Market Accounts", "money_market"), ("Personal Savings", "savings"),
                            ("Savings Accounts", "savings"), ("Checking", "checking"), ("Personal Checking Accounts", "checking"),
                            ("High-Yield Savings", "high_yield_savings"), ("CDs", "cd"), ("Certificates of Deposit", "cd")):
            rows = ri._parse_table_rows(f"{head}\nSome Product\t$1 and above\t1.00%\t1.00%\n" if ptype != "cd" else f"{head}\n12 months\t1.00%\t1.00%\n")
            self.assertEqual(rows[0]["section"], ptype, head)


class RatesLinkTests(unittest.TestCase):
    def test_links_that_point_at_a_rates_page(self):
        for href, text in (("/personal/checking-savings/interest-rates", "see rates"), ("/rates", "Rates"), ("/personal/rates/", ""),
                           ("/x", "View current rates"), ("/x", "See today's rates"), ("/deposit-rates", ""), ("/rate-sheet.pdf", "Rate sheet"),
                           ("/x", "Check our rates")):
            self.assertTrue(ri.is_rates_page_link(href, text), (href, text))

    def test_links_that_do_not(self):
        for href, text in (("/personal/savings-accounts", "Savings"), ("/open-account", "Open an account"), ("/about-us", "About"), ("/x", "Learn more"),
                           ("/blog/iterate", "Iterate"), ("", "")):
            self.assertFalse(ri.is_rates_page_link(href, text), (href, text))

    def test_loan_and_mortgage_rates_are_not_deposit_rates(self):
        for href, text in (("/mortgage-rates", "Mortgage rates"), ("/loans/auto-rates", "see rates"), ("/home-equity/rates", "View rates"),
                           ("/credit-card-rates", ""), ("/x", "See loan rates")):
            self.assertFalse(ri.is_rates_page_link(href, text), (href, text))


import datetime as _dt  # noqa: E402

PAGE_SENTENCES = ("Interest rates and Annual Percentage Yield (APY) shown below are for the state of South Carolina. "
                  "Interest rates are current as of 03/01/2026.")


class PageMetaTests(unittest.TestCase):
    def test_scope_and_date_from_the_sentence_above_the_tables(self):
        m = ri.find_rate_page_meta("Interest Rates\n" + PAGE_SENTENCES + "\nChecking\n")
        self.assertEqual((m["scope"], m["as_of"], m["as_of_raw"], m["as_of_order"]), ("South Carolina", "2026-03-01", "03/01/2026", "month/day/year"))
        self.assertEqual(m["scope_raw"], "Interest rates and Annual Percentage Yield (APY) shown below are for the state of South Carolina.")

    def test_nothing_stated_means_none(self):
        self.assertIsNone(ri.find_rate_page_meta("Savings 2.00% APY. Visit a branch."))
        self.assertIsNone(ri.find_rate_page_meta(""))
        self.assertIsNone(ri.find_rate_page_meta(None))

    def test_scope_without_a_date_and_date_without_a_scope(self):
        a = ri.find_rate_page_meta("Rates shown are for the state of Texas.")
        self.assertEqual((a["scope"], a["as_of_raw"]), ("Texas", None))
        b = ri.find_rate_page_meta("Rates are effective 3/1/2026.")
        self.assertEqual((b["scope"], b["as_of"]), (None, "2026-03-01"))

    def test_two_word_and_three_word_scopes(self):
        self.assertEqual(ri.find_rate_page_meta("APYs shown are for the state of North Carolina.")["scope"], "North Carolina")
        self.assertEqual(ri.find_rate_page_meta("Rates shown below apply to the District of Columbia.")["scope"], "District of Columbia")

    def test_scope_stops_at_the_sentence_end(self):
        m = ri.find_rate_page_meta("Rates are for the state of Georgia. Interest rates are current as of 01/02/2026.")
        self.assertEqual(m["scope"], "Georgia")

    def test_location_picked_by_zip(self):
        m = ri.find_rate_page_meta("Showing information for Zip Code 29201\nRates are current as of 03/01/2026")
        self.assertEqual(m["zip"], "29201")
        self.assertIn("shown for zip 29201", ri.meta_note(m))
        self.assertEqual(ri.find_rate_page_meta("Showing rates for ZIP 77001-1234")["zip"], "77001")

    def test_showing_information_for_the_state_of(self):
        self.assertEqual(ri.find_rate_page_meta("Showing information for the State of Texas")["scope"], "Texas")

    def test_numeric_dates_are_month_first_and_never_reinterpreted_day_first(self):
        self.assertEqual(ri.parse_as_of("03/01/2026"), _dt.date(2026, 3, 1))     # March 1, not January 3
        self.assertEqual(ri.parse_as_of("3-1-26"), _dt.date(2026, 3, 1))
        self.assertEqual(ri.parse_as_of("12.31.2025"), _dt.date(2025, 12, 31))
        self.assertIsNone(ri.parse_as_of("25/03/2026"))                           # day-first form: not guessed
        self.assertIsNone(ri.parse_as_of("31/12/2025"))
        self.assertIsNone(ri.parse_as_of("02/30/2026"))                           # impossible date
        self.assertIsNone(ri.parse_as_of("13/01/2026"))

    def test_unparsed_numeric_date_keeps_its_raw_text(self):
        m = ri.find_rate_page_meta("Interest rates are current as of 25/03/2026.")
        self.assertEqual((m["as_of"], m["as_of_raw"], m["as_of_order"]), (None, "25/03/2026", None))
        self.assertIn("date not understood", ri.meta_note(m))

    def test_named_month_dates(self):
        self.assertEqual(ri.parse_as_of("March 1, 2026"), _dt.date(2026, 3, 1))
        self.assertEqual(ri.parse_as_of("Mar. 1st, 2026"), _dt.date(2026, 3, 1))
        self.assertEqual(ri.parse_as_of("1 March 2026"), _dt.date(2026, 3, 1))
        self.assertEqual(ri.parse_as_of("Sept 5, 2026"), _dt.date(2026, 9, 5))
        self.assertEqual(ri.parse_as_of("December 31, 2025"), _dt.date(2025, 12, 31))
        self.assertIsNone(ri.parse_as_of("Foo 1, 2026"))
        self.assertIsNone(ri.parse_as_of(""))
        self.assertIsNone(ri.parse_as_of(None))

    def test_named_month_in_page_text(self):
        m = ri.find_rate_page_meta("Rates updated on March 1, 2026.")
        self.assertEqual((m["as_of"], m["as_of_order"]), ("2026-03-01", "named month"))

    def test_staleness_uses_the_limit_and_counts_days_strictly(self):
        meta = {"as_of": "2026-03-01"}
        self.assertEqual(ri.page_age_days(meta, _dt.date(2026, 3, 11)), 10)
        self.assertFalse(ri.is_stale(meta, _dt.date(2026, 4, 30), days=60))        # exactly 60 days: not stale
        self.assertTrue(ri.is_stale(meta, _dt.date(2026, 5, 1), days=60))          # 61 days
        self.assertFalse(ri.is_stale({"as_of": None}, _dt.date(2030, 1, 1)))
        self.assertFalse(ri.is_stale(None))
        self.assertFalse(ri.is_stale(meta, _dt.date(2026, 2, 1)))                  # a future date is not stale

    def test_the_stale_limit_is_one_setting_default_60(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ri.STALE_DAYS_ENV, None)
            self.assertEqual(ri.stale_days(), 60)
        with mock.patch.dict(os.environ, {ri.STALE_DAYS_ENV: "30"}):
            self.assertEqual(ri.stale_days(), 30)
        for v in ("", "abc", "0", "-5"):
            with mock.patch.dict(os.environ, {ri.STALE_DAYS_ENV: v}):
                self.assertEqual(ri.stale_days(), 60, v)
        with mock.patch.dict(os.environ, {ri.STALE_DAYS_ENV: "30"}):
            self.assertTrue(ri.is_stale({"as_of": "2026-03-01"}, _dt.date(2026, 4, 5)))   # 35 days with a 30-day limit

    def test_a_stale_value_is_kept_but_low_with_a_flag(self):
        e = ri.make_source(ri.SITE, url=SRC, verified=True)
        self.assertEqual(ri.resolve(e, BANK_URL, "ok", stale=True), (ri.SITE, ri.LOW, SRC, ["stale_page_date"]))
        self.assertEqual(ri.resolve(e, BANK_URL, "ok", stale=False)[1], ri.HIGH)
        self.assertEqual(ri.resolve(e, BANK_URL, "ok", edited=True, stale=True)[1], ri.HIGH)   # a person confirmed it

    def test_state_codes(self):
        self.assertEqual(ri.state_abbr("South Carolina"), "SC")
        self.assertEqual(ri.state_abbr("sc"), "SC")
        self.assertEqual(ri.state_abbr(" Texas. "), "TX")
        self.assertEqual(ri.state_abbr("District of Columbia"), "DC")
        self.assertIsNone(ri.state_abbr("Atlantis"))
        self.assertIsNone(ri.state_abbr("xx"))
        self.assertIsNone(ri.state_abbr(None))

    def test_home_state_of_a_result(self):
        self.assertEqual(ri.home_state_of({"state": "GA"}), "GA")
        self.assertEqual(ri.home_state_of({"bank_state": "Texas"}), "TX")
        self.assertEqual(ri.home_state_of({"branch_address": "100 Main St, Columbia, SC 29201"}), "SC")
        self.assertIsNone(ri.home_state_of({}))

    def test_scope_flags(self):
        m = {"scope": "Texas"}
        self.assertEqual(ri.scope_flags(m), ["regional_scope"])
        self.assertEqual(ri.scope_flags(m, home_state="SC"), ["regional_scope", "scope_differs_from_home_state"])
        self.assertEqual(ri.scope_flags(m, home_state="TX"), ["regional_scope"])
        self.assertEqual(ri.scope_flags(m, home_state="TX", run_majority="SC"), ["regional_scope", "scope_differs_from_run"])
        self.assertEqual(ri.scope_flags({"scope": "Atlantis"}, home_state="SC", run_majority="SC"), ["regional_scope"])
        self.assertEqual(ri.scope_flags(None, "SC", "SC"), [])
        self.assertEqual(ri.scope_flags({"scope": None, "as_of": "2026-01-01"}, "SC", "SC"), [])


class IdentityTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(ri.name_tokens("Harbor Savings, F.A."), ["harbor"])
        self.assertEqual(ri.name_tokens("Alder Bank"), ["alder"])
        self.assertEqual(ri.name_tokens("Larchmont State Bank, National Association"), ["larchmont"])
        self.assertEqual(ri.name_tokens("First Federal Savings Bank"), ["first"])
        self.assertEqual(ri.name_tokens("The Bank of Trust"), ["bank", "trust"])

    def test_page_that_names_the_bank_is_ok(self):
        self.assertEqual(ri.check_identity("Harbor Savings, F.A.", BANK_URL, "Savings Accounts | Harbor Savings", "", "Copyright Harbor Savings"), "ok")

    def test_site_branded_for_a_different_bank_is_a_mismatch(self):
        text = "Beacon Bank USA. Our rates. 12 month CD 4.35% APY. Member FDIC."
        self.assertEqual(ri.check_identity("Alder Bank", "https://www.beaconbank.example", "Our Rates | Beacon Bank USA", "Beacon Bank USA", text), "mismatch")

    def test_the_banks_own_domain_counts(self):
        self.assertEqual(ri.check_identity("Alder Bank", "https://www.alderbank.example", "Rates", "", "12 month CD 4.35% APY"), "ok")

    def test_spacing_and_punctuation_are_ignored(self):
        self.assertEqual(ri.check_identity("Larchmont Bank", "https://x.example", "", "", "Welcome to Larch Mont"), "ok")
        self.assertEqual(ri.check_identity("Southgate Bank", "https://x.example", "", "", "South Gate Bank rates"), "ok")

    def test_no_text_is_unknown_not_a_mismatch(self):
        self.assertEqual(ri.check_identity("Alder Bank", "https://www.other.example", "", "", ""), "unknown")

    def test_a_name_whose_only_token_is_too_short_to_judge_is_unknown_not_a_mismatch(self):
        self.assertEqual(ri.check_identity("BB Bank", "https://www.other.example", "", "", "Welcome to another bank website"), "unknown")

    def test_a_name_with_no_usable_token_is_unknown(self):
        self.assertEqual(ri.check_identity("", BANK_URL, "x", "x", "x"), "unknown")


class NotPublishedTests(unittest.TestCase):
    def test_interest_bearing_inquire_for_rates_is_checking(self):
        self.assertEqual(ri.detect_not_published("Checking Accounts. Non-Interest Bearing: No fee. Interest Bearing: Inquire for Rates"), {"checking"})

    def test_rates_on_request_names_no_product(self):
        self.assertEqual(ri.detect_not_published("Our certificate rates are available upon request."), {"cd"})
        self.assertEqual(ri.detect_not_published("Rates available upon request."), {"all"})

    def test_call_for_rates_by_product(self):
        self.assertEqual(ri.detect_not_published("Money Market: call for rates"), {"money_market"})
        self.assertEqual(ri.detect_not_published("Savings - please call us for our current rates"), {"savings"})

    def test_a_page_with_numbers_and_no_phrase_is_published(self):
        self.assertEqual(ri.detect_not_published("Savings 2.50% APY. CD 12-month 4.00% APY. See rates"), set())

    def test_empty(self):
        self.assertEqual(ri.detect_not_published(""), set())
        self.assertEqual(ri.detect_not_published(None), set())

    def test_evidence_is_the_sentence_that_says_so(self):
        ev = ri.find_not_published("Checking Accounts.\n  Interest   Bearing: Inquire for Rates\nMore text.")
        self.assertEqual(list(ev), ["checking"])
        self.assertIn("Inquire for Rates", ev["checking"])
        self.assertNotIn("\n", ev["checking"])
        self.assertLessEqual(len(ev["checking"]), 160)   # bounded by the pattern window

    def test_evidence_keeps_the_first_occurrence_per_product(self):
        ev = ri.find_not_published("Money Market: call for rates." + (" Filler text about branches and hours." * 8) + " Money Market: contact us for rates.")
        self.assertEqual(list(ev), ["money_market"])
        self.assertIn("call for rates", ev["money_market"])
        self.assertNotIn("contact us", ev["money_market"])

    def test_detect_is_the_key_set_of_find(self):
        text = "Savings: call for rates. Rates available upon request."
        self.assertEqual(ri.detect_not_published(text), set(ri.find_not_published(text)))


class SourceRuleTests(unittest.TestCase):
    def test_domain_helpers(self):
        self.assertEqual(ri.domain_of("https://www.Example.com/a/b"), "example.com")
        self.assertEqual(ri.domain_of("[Search] x"), "")
        self.assertEqual(ri.domain_of(None), "")
        self.assertTrue(ri.same_site("https://www.harborsavings.example/rates", "https://harborsavings.example"))
        self.assertTrue(ri.same_site("https://rates.harborsavings.example/x", "https://www.harborsavings.example"))
        self.assertFalse(ri.same_site("https://harborsavings.example.evil.example/", BANK_URL))
        self.assertFalse(ri.same_site("", BANK_URL))

    def test_nerdwallet_is_blocked_everywhere_it_can_appear(self):
        self.assertTrue(ri.is_blocked_source("nerdwallet.com"))
        self.assertTrue(ri.is_blocked_source("https://www.nerdwallet.com/best/banking/cd-rates"))
        self.assertTrue(ri.is_blocked_source("WWW.NerdWallet.COM"))
        self.assertFalse(ri.is_blocked_source("bankrate.com"))
        self.assertFalse(ri.is_blocked_source(""))
        self.assertFalse(ri.is_blocked_source(None))


if __name__ == "__main__":
    unittest.main()
