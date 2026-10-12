"""Offline checks for the a40 crawler quality fixes. No network, no Supabase, no browser.
Run:  python test_quality_fixes.py   (from the Rate Radar folder)"""
import asyncio
import os
import sys

os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)
os.environ.pop("SUPABASE_KEY", None)

from extraction import (apply_plausibility_rules, choose_vision_targets, extract_rates,  # noqa: E402
                        is_business_url, tag_rate_context)

fails = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails.append(label)


# 1. The BankFirst case: a rates table with no product labels and no CDs. The extractor's
#    last-resort fallback labels the page's highest APY a CD ("best found").
BANKFIRST_LIKE = """Rates as of 07/27/2026
1.25% APY up to $15,000 with qualifiers
0.150% APY above $15,000
0.010% APY without qualifiers
Minimum balance to open $25
"""
r = extract_rates(BANKFIRST_LIKE)
before_cd, before_term = r.get("cd"), r.get("cd_term")
check("setup: the extractor really produces the bad 'best found' CD", before_cd == 1.25 and before_term == "best found",
      f"cd={before_cd} term={before_term}")
r, notes = apply_plausibility_rules(r, BANKFIRST_LIKE, "https://www.bankfirstfs.com/rates")
check("BankFirst-like page: the CD is rejected", r.get("cd") is None and r.get("cd_term") is None,
      f"after cd={r.get('cd')}")
check("...and the rejection is reported", any("CD rejected" in n for n in notes), str(notes))

# 2. A real CD page keeps its CD
CD_PAGE = """Certificates of Deposit
12-Month Certificate of Deposit 4.25% APY
24-Month Certificate of Deposit 3.90% APY
"""
r2 = extract_rates(CD_PAGE)
r2, notes2 = apply_plausibility_rules(r2, CD_PAGE, "https://www.example-bank.com/personal/cds")
check("real CD page: the CD is kept with its term", r2.get("cd") == 4.25 and r2.get("cd_term") == "12-month",
      f"cd={r2.get('cd')} term={r2.get('cd_term')} notes={notes2}")

# 3. A rate with a term but no CD section on the page is rejected
NO_SECTION = "Our Premium Savings: 12-month 4.10% APY promotional rate for new money"
r3 = extract_rates(NO_SECTION)
had_cd = r3.get("cd")
r3, notes3 = apply_plausibility_rules(r3, "Premium savings account. Open today.", "https://www.example-bank.com/savings")
check("a CD reading on a page with no CD section is rejected", r3.get("cd") is None, f"had cd={had_cd}, notes={notes3}")

# 4. Business URL: nothing consumer is taken from it
rb = {"checking": 0.05, "savings": 0.05, "cd": 0.2, "cd_term": "12-month", "money_market": None, "rate_tags": {"checking": ["conditional"]}}
rb, nb = apply_plausibility_rules(rb, "Business checking 0.05% APY", "http://www.bankplus.net/business/bank/products/checking")
check("business-product URL: consumer rates dropped", all(rb.get(k) is None for k in ("checking", "savings", "cd")), str(rb))
check("is_business_url: business / personal / root",
      is_business_url("https://x.com/business/checking") and is_business_url("https://x.com/small-business/rates")
      and not is_business_url("https://x.com/personal/checking") and not is_business_url("https://x.com")
      and not is_business_url("https://businessbank.example.com/personal/rates"))

# 5. Tiered rates are tagged
TEXT = "Interest Checking 0.150% APY on balances above $15,000"
check("tiered language is tagged 'tiered'", "tiered" in tag_rate_context(TEXT, TEXT.find("0.150")),
      str(tag_rate_context(TEXT, TEXT.find("0.150"))))
check("plain rate text is not tagged tiered", "tiered" not in tag_rate_context("Savings 0.50% APY", 8))

# 6. Vision targets: reached pages beat guessed URLs; business pages never chosen
dom, base = "https://www.regions.com", "http://www.regions.com"
t = choose_vision_targets(None, ["https://www.regions.com/personal-banking/savings/cd-account",
                                 "https://www.regions.com/personal-banking/rates"], [], [], dom, base)
check("vision targets: pages the crawl reached come first", t[0].endswith("/savings/cd-account") and len(t) == 2, str(t))
t = choose_vision_targets("https://www.x.com/rates", ["https://www.x.com/a"], [], [], dom, base)
check("vision targets: a found page wins", t == ["https://www.x.com/rates"], str(t))
t = choose_vision_targets(None, ["https://www.x.com/business/rates"], [], [], dom, base)
check("vision targets: business pages are never targets, falls back to guesses",
      t == [dom + "/rates", dom + "/personal/rates", base], str(t))
t = choose_vision_targets(None, [], ["https://www.x.com/s1", "https://www.x.com/s2", "https://www.x.com/s3"], [], dom, base)
check("vision targets: search candidates still used (max 2)", t == ["https://www.x.com/s1", "https://www.x.com/s2"], str(t))

# 7. The pending-navigation fix (scrapers helper), with a fake page
try:
    import scrapers  # noqa: E402

    class FakePage:
        def __init__(self):
            self.calls = []
            self.interrupted_once = False

        async def evaluate(self, js):
            self.calls.append(("evaluate", js))

        async def goto(self, url, timeout=0, wait_until=None):
            self.calls.append(("goto", url))
            if url != "about:blank" and not self.interrupted_once:
                self.interrupted_once = True
                raise Exception('Page.goto: Navigation to "%s" is interrupted by another navigation to "https://www.bing.com/search"' % url)
            return "RESPONSE"

    async def go():
        p = FakePage()
        await scrapers._settle_page(p)
        resp = await scrapers._goto_with_retry(p, "https://www.x.com/rates", timeout=1000, retries=1)
        return p, resp

    p, resp = asyncio.run(go())
    gotos = [c[1] for c in p.calls if c[0] == "goto"]
    check("settle: window.stop and about:blank are issued first", p.calls[0][0] == "evaluate" and gotos[0] == "about:blank", str(p.calls))
    check("an 'interrupted by another navigation' error is retried and succeeds", resp == "RESPONSE" and gotos.count("https://www.x.com/rates") == 2,
          str(gotos))
except Exception as e:  # import of scrapers failed
    check("scrapers imports and the navigation helpers work", False, f"{type(e).__name__}: {e}")

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
