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
        self.assertEqual(ri.DEFAULT_TIER_RULE, "entry")
        self.assertEqual(sorted(ri.TIER_RULES), ["entry", "highest"])

    def test_default_rule_is_the_entry_rate(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ri.TIER_RULE_ENV, None)
            self.assertEqual(ri.tier_mode(), "entry")
            self.assertEqual(ri.tier_rule(), ri.TIER_RULES["entry"])

    def test_one_setting_switches_the_rule(self):
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "highest"}):
            self.assertEqual(ri.tier_mode(), "highest")
            self.assertEqual(ri.tier_rule(), ri.TIER_RULES["highest"])
        with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: "  HIGHEST "}):
            self.assertEqual(ri.tier_mode(), "highest")

    def test_an_unknown_setting_falls_back_to_the_default(self):
        for v in ("", "top", "entry-rate", "1"):
            with mock.patch.dict(os.environ, {ri.TIER_RULE_ENV: v}):
                self.assertEqual(ri.tier_mode(), "entry", v)

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
        r = ri.extract_tiered_products(MM_TEXT)
        mm = r["money_market"]
        self.assertEqual(mm["apy"], 0.05)
        self.assertEqual(mm["min_open"], 2500.0)
        self.assertEqual(len(mm["tiers"]), 3)
        self.assertIn("0.62% $10,000 to $25,000", mm["note"].replace("$10,000.01", "$10,000"))
        self.assertIn("other tiers", mm["note"])

    def test_only_the_first_block_of_a_product_type_is_used(self):
        r = ri.extract_tiered_products(MM_TEXT)
        self.assertEqual(r["money_market"]["min_open"], 2500.0)   # not the $25,000 premium block

    def test_premium_block_alone(self):
        r = ri.extract_tiered_products(EDGE_TEXT)
        self.assertEqual(r["money_market"]["apy"], 0.62)
        self.assertNotEqual(r["money_market"]["apy"], 3.56)
        self.assertEqual(r["money_market"]["min_open"], 25000.0)

    def test_rate_table_with_apy_only_in_the_header(self):
        r = ri.extract_tiered_products(SAV_TEXT)
        sav = r["savings"]
        self.assertEqual(sav["apy"], 0.12)
        self.assertIsNone(sav["min_open"])
        self.assertIn("lowest balance tier", sav["note"])
        self.assertIn("0.42% over $2,500", sav["note"])

    def test_table_row_rate_and_apy_are_told_apart(self):
        r = ri.extract_tiered_products("Savings Accounts ($2,499 or less) 0.10% 0.12% Savings Accounts ($2,500 or more) 0.40% 0.42%")["savings"]
        self.assertEqual(r["apy"], 0.12)
        self.assertEqual(sorted(t["apy"] for t in r["tiers"]), [0.12, 0.42])

    def test_no_tiers_means_no_result(self):
        self.assertEqual(ri.extract_tiered_products("Savings 2.50% APY. CD 12-month 4.00% APY."), {})
        self.assertEqual(ri.extract_tiered_products(""), {})

    def test_tier_lines_without_a_product_keyword_are_ignored(self):
        self.assertEqual(ri.extract_tiered_products("$1,000 or less -- 0.50% APY $1,000.01 to $5,000 -- 0.75% APY"), {})


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
