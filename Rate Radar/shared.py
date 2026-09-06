# ── Shared cross-cutting state & config ──────────────────────────────────────
# The one thing every other module needs a handle on: the mutable crawl_state
# dict (progress/log accumulator for the run in progress), plus config values
# read by both the scraper side (rate_radar.py) and the storage side
# (storage.py). Lives in its own leaf module with no dependency on either of
# them, so both can import from here without a circular import.

import os
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY", "")
                or os.environ.get("SUPABASE_KEY", "")).strip()

crawl_state = {
    "running":        False,
    "banks":          [],
    "results":        [],
    "log":            [],
    "done":           False,
    "ai_calls":       0,      # track AI vision calls this run
    "chat_calls":     0,      # track chat interactions this run
    "llm_input_tokens":  0,   # cumulative Anthropic input tokens this run
    "llm_output_tokens": 0,   # cumulative Anthropic output tokens this run
    "llm_web_searches":  0,   # cumulative billed web_search tool invocations this run
    "crawl_mode":     "auto",   # auto: standard → ai_agent fallback
    "cr_data":        {},     # {rssdid (int): {...implied APY fields...}} — current quarter
    "cr_period":      None,   # e.g. "Dec 2025"
    "prev_cr_data":   {},     # prior quarter data
    "cr_prev_period": None,   # e.g. "Sep 2025"
    "cr_quarters":    [],     # all available quarter labels
}

# Registry/cache calls run once per bank inside the same MAX_CONCURRENT_BANKS
# window, so up to 5 of these can be in flight against Supabase at once —
# a live run showed 10s wasn't always enough headroom under that load.
SUPABASE_CALL_TIMEOUT_S = 15

# Phase 3: circuit breaker. A bank with enough consecutive site-crawl failures
# skips the expensive browser crawl this run — but still probes with a real
# attempt every PROBE_EVERY-th failure, so a fixed site eventually gets
# noticed instead of being blacklisted forever.
CIRCUIT_BREAKER_THRESHOLD   = 3
CIRCUIT_BREAKER_PROBE_EVERY = 4


def _estimate_cost_usd(input_tokens, output_tokens, web_searches):
    """Claude Haiku 4.5: $1/MTok input, $5/MTok output. Web search tool: $10/1000 searches."""
    return round(
        input_tokens  / 1_000_000 * 1.0 +
        output_tokens / 1_000_000 * 5.0 +
        web_searches  / 1000 * 10.0,
        4
    )


RATE_PATHS = [
    "", "/rates", "/personal/rates", "/personal-banking/rates",
    "/home/current-rates", "/savings", "/personal-banking/savings",
    "/checking", "/personal-banking/checking", "/cds",
    "/certificates-of-deposit", "/personal-banking/cds",
    "/commercial-banking", "/business-banking", "/deposits",
    "/personal-banking/money-market", "/money-market",
    # expanded — community bank common patterns
    "/personal/deposit-rates", "/products/deposits", "/banking/rates",
    "/accounts/rates", "/personal-banking/deposit-accounts",
    "/consumer/rates", "/rates/deposit", "/deposit-rates",
    "/personal/savings-accounts", "/personal/checking-accounts",
    "/personal/cds", "/current-rates", "/rate-sheet", "/interest-rates",
    "/about/rates", "/resources/rates", "/banking/deposit-rates",
    "/personal/money-market", "/products/rates", "/retail/rates",
    "/personal-banking/certificates", "/certificates",
    "/savings-accounts", "/checking-accounts", "/deposit-accounts",
    "/rates/personal", "/rates/consumer", "/rates/deposits",
    "/personal/deposit-accounts", "/banking/savings",
    "/banking/checking", "/banking/cds", "/banking/money-market",
    "/accounts/savings", "/accounts/checking", "/accounts/cds",
    "/products/savings", "/products/checking", "/products/cds",
    "/services/rates", "/services/deposits",
    # Kasasa platform (very common community-bank site builder) —
    # its default rates page is /tools/rates.html
    "/tools/rates.html", "/tools/rates", "/rates.html",
    "/personal/rates.html", "/personal-banking/rates.html",
]

BANK_EXTRA_URLS = {
    "asb.com":             [
        "https://www.asb.com/personal/rates",
        "https://www.asb.com/personal/checking",
        "https://www.asb.com/personal/savings",
        "https://www.asb.com/",
    ],
    "jpmorganchase.com":   ["https://www.chase.com/personal/savings"],
    "chase.com":           [
        "https://www.chase.com/personal/savings",
        "https://www.chase.com/personal/checking",
        "https://www.chase.com/personal/cds",
    ],
    "texascapitalbank.com": [
        "https://www.texascapitalbank.com/personal/deposits",
        "https://www.texascapitalbank.com/personal/checking",
        "https://www.texascapitalbank.com/rates",
    ],
    "stearnsbank.com":     [
        "https://www.stearnsbank.com/personal/rates",
        "https://www.stearnsbank.com/rates",
    ],
    "gulfbank.com":        [
        "https://www.gulfbank.com/personal/rates",
        "https://www.gulfbank.com/rates",
    ],
    "bankwithfidelity.com": [
        "https://www.bankwithfidelity.com/rates",
        "https://www.bankwithfidelity.com/personal/rates",
    ],
    "nexbank.com":         ["https://nexbankpersonal.com/"],
    "tbkbank.com":         ["https://www.tbkbank.com/rates/"],
    "maplemarkbank.com":   ["https://go.maplemarkbank.com/"],
    "hibernia.bank":       [
        "https://www.hibernia.bank/personal-solutions/checking",
        "https://www.hibernia.bank/personal-solutions/savings",
        "https://www.hibernia.bank/personal-solutions/cds",
        "https://www.hibernia.bank/rates",
        "https://www.hibernia.bank/",
    ],
    "bayfirstfinancial.com": [
        "https://www.bayfirstfinancial.com/personal/rates/",
        "https://www.bayfirstfinancial.com/rates/",
        "https://www.bayfirstfinancial.com/personal-banking/savings/",
        "https://www.bayfirstfinancial.com/personal-banking/checking/",
    ],
}

# ── Export / auto-save + Supabase raw-table schema ───────────────────────────
EXPORTS = Path(__file__).parent / "RateRadar_Exports"
FIELDS  = [
    "run_id", "run_date", "crawled_at",
    "bank_name", "bank_url", "RSSDID", "bank_type", "branch_address",
    "checking_apy", "savings_apy", "cd_apy", "cd_term",
    "money_market_apy", "min_balance", "status", "note",
    "source_url", "source_url_checking", "source_url_savings", "source_url_cd",
    "vulnerability_flag",
    "cr_period", "cr_total_deposits_m",
    "cr_savings_apy", "cr_checking_apy", "cr_cd_apy", "cr_cost_of_deposits",
    "cr_prev_period", "prev_cr_total_deposits_m",
    "prev_cr_savings_apy", "prev_cr_checking_apy", "prev_cr_cd_apy", "prev_cr_cost_of_deposits",
    "delta_savings_apy", "delta_checking_apy", "delta_cd_apy", "delta_cost_of_deposits",
]

SB_NUMERIC_FIELDS = {
    "checking_apy", "savings_apy", "cd_apy", "money_market_apy",
    "cr_total_deposits_m", "cr_savings_apy", "cr_checking_apy", "cr_cd_apy",
    "cr_cost_of_deposits", "prev_cr_total_deposits_m", "prev_cr_savings_apy",
    "prev_cr_checking_apy", "prev_cr_cd_apy", "prev_cr_cost_of_deposits",
    "delta_savings_apy", "delta_checking_apy", "delta_cd_apy",
    "delta_cost_of_deposits",
}
