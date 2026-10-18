#!/usr/bin/env python3
"""Rate Radar weekly runner (a132): headless crawl of the watchlist banks, banks' OWN published rates only.

    python weekly_runner.py --dry-run          # read the watchlist, print the plan, write nothing
    python weekly_runner.py                    # crawl the plan and record the results

Reads the active, non-manual rows of public.rate_radar_watchlist (at most 10 banks, oldest last_run_at first),
runs the existing crawler (scrapers.run_crawler) over them, and records everything through pg_adapter as the
restricted login role rate_radar_runner. It never uses the Supabase service key and refuses to start when one
is present in the environment.

Environment:
    RATE_RADAR_DATABASE_URL   libpq URL for rate_radar_runner (required; never printed)
    ANTHROPIC_API_KEY         the crawler's own LLM key (warns if absent; the crawl then skips every AI step)
    RATE_RADAR_MAX_COST_USD   per-run LLM cost cap, default 2.00 (a flag overrides it)
    RATE_RADAR_MAX_MINUTES    wall-clock cap, default 25 (the same ceiling the app's crawl thread uses)

Exit codes: 0 recorded, 1 not clean (nothing usable recorded, or the crawler crashed after a partial record),
2 configuration or guard refused, 3 database error.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import types
from datetime import datetime, timezone

MAX_BANKS_HARD = 10                 # never crawl more than this in one run, whatever the flags say
DEFAULT_MAX_COST_USD = 2.00
DEFAULT_MAX_MINUTES = 25
FORBIDDEN_ENV = ("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
DSN_ENV = "RATE_RADAR_DATABASE_URL"
EXIT_OK, EXIT_NOTHING, EXIT_CONFIG, EXIT_DB = 0, 1, 2, 3

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


class SelectionError(ValueError):
    pass


def forbidden_env(env):
    """Names of service-key variables that are set (non-empty). The runner must not start with any of them."""
    return [name for name in FORBIDDEN_ENV if (env.get(name) or "").strip()]


def _domain_of(url):
    from extraction import extract_domain
    return extract_domain(url)


def _as_aware(ts):
    if ts is None:
        return _EPOCH
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def select_banks(rows, max_banks=MAX_BANKS_HARD, only=None, domain_of=_domain_of):
    """Choose which watchlist rows to crawl. Returns {"selected", "deferred", "skipped", "inactive"}.

    Only active rows qualify. manual_only rows are never crawled (reported as manual verification due), a row whose
    URL domain does not match its bank_key is skipped (the registry write-back would miss it), and when more than
    max_banks qualify the ones crawled longest ago go first. `only` narrows to given RSSDs but can never add a bank
    that is not an active watchlist row."""
    max_banks = max(1, min(int(max_banks), MAX_BANKS_HARD))
    active = [r for r in rows if r.get("active")]
    inactive = len(rows) - len(active)
    if only:
        wanted = {str(x).strip() for x in only}
        missing = sorted(wanted - {r["rssdid"] for r in active})
        if missing:
            raise SelectionError(f"not an active watchlist row: {', '.join(missing)}")
        active = [r for r in active if r["rssdid"] in wanted]
    eligible, skipped = [], []
    for r in active:
        if r.get("manual_only"):
            skipped.append((r, f"manual verification due: {r.get('manual_reason')}"))
            continue
        url = (r.get("bank_url") or "").strip()
        if not url:
            skipped.append((r, "no bank_url"))
            continue
        dom = domain_of(url if url.startswith("http") else "https://" + url).lower()
        if dom != (r.get("bank_key") or "").lower():
            skipped.append((r, f"bank_key mismatch: url domain '{dom}' vs watchlist '{r.get('bank_key')}'"))
            continue
        eligible.append(r)
    eligible.sort(key=lambda r: (_as_aware(r.get("last_run_at")), r["rssdid"]))
    return {"selected": eligible[:max_banks], "deferred": eligible[max_banks:], "skipped": skipped, "inactive": inactive}


def bank_dict(row):
    """The bank dict shape run_crawler expects (the same keys as the app's CSV upload)."""
    return {"bank_name": row["bank_name"], "bank_url": row.get("bank_url") or "", "RSSDID": row["rssdid"],
            "bank_type": row.get("bank_type") or "", "branch_address": row.get("branch_address") or ""}


def print_plan(plan, max_cost, max_minutes, dry_run):
    print(f"Rate Radar weekly run{' (DRY RUN: nothing will be crawled or written)' if dry_run else ''}")
    print(f"  caps: {len(plan['selected'])}/{MAX_BANKS_HARD} banks, ${max_cost:.2f} LLM cost, {max_minutes} min")
    for r in plan["selected"]:
        print(f"  crawl   {r['rssdid']:>9}  {r['bank_name']}  {r.get('bank_url')}")
    for r in plan["deferred"]:
        print(f"  defer   {r['rssdid']:>9}  {r['bank_name']}  (over the {MAX_BANKS_HARD}-bank cap; goes first next run)")
    for r, why in plan["skipped"]:
        print(f"  skip    {r['rssdid']:>9}  {r['bank_name']}  ({why})")
    if plan["inactive"]:
        print(f"  {plan['inactive']} inactive watchlist row(s) ignored")


def _load_crawler():
    import shared, storage, scrapers          # heavy (Playwright, Anthropic): only imported for a real run
    return types.SimpleNamespace(shared=shared, storage=storage, scrapers=scrapers, crawl_state=shared.crawl_state,
                                 run_crawler=scrapers.run_crawler, estimate=shared._estimate_cost_usd)


def _cost(crawler):
    s = crawler.crawl_state
    return crawler.estimate(s.get("llm_input_tokens", 0), s.get("llm_output_tokens", 0), s.get("llm_web_searches", 0))


async def run_with_limits(crawler, banks, max_cost, max_seconds):
    """Run the crawler as a task and cancel it when the cost or time cap is reached. Returns the reason or None."""
    task = asyncio.ensure_future(crawler.run_crawler(banks))
    started = time.monotonic()
    reason = None
    while True:
        done, _ = await asyncio.wait({task}, timeout=1.0)
        if done:
            break
        cost = _cost(crawler)
        if cost >= max_cost:
            reason = f"COST CAP reached: ${cost:.2f} of ${max_cost:.2f}"
        elif time.monotonic() - started >= max_seconds:
            reason = f"TIME CAP reached: {int(max_seconds // 60)} min"
        if reason:
            task.cancel()
            break
    try:
        await task
    except asyncio.CancelledError:
        if reason is None:
            raise
    return reason


async def finalize_partial(crawler, store, started_at, note):
    """run_crawler's closing steps (save, then the three pushes) for a run that was cut short or crashed."""
    s = crawler.crawl_state
    s["running"], s["done"] = False, True
    results = s["results"]
    found = sum(1 for r in results if r["status"] == "Found")
    partial = sum(1 for r in results if r["status"] == "Partial")
    s["log"].append(f"⚠️ RUN CUT SHORT — {note}; recording the {len(results)} bank result(s) finished so far")
    run_id, run_date = crawler.storage.auto_save(results)
    if run_id:
        s["_a132_run_id"] = run_id
        await store.push_to_supabase(results, run_id, run_date)
        await store.push_run_summary(run_id, run_date, started_at, found, partial, len(results) - found - partial,
                                     s.get("ai_calls", 0), s.get("chat_calls", 0), s.get("llm_input_tokens", 0),
                                     s.get("llm_output_tokens", 0), s.get("llm_web_searches", 0), notes=note)
        await store.push_rate_observations(results, run_id)
    return run_id


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Rate Radar weekly runner: crawls the active watchlist banks, banks' own rates only.")
    ap.add_argument("--dry-run", action="store_true", help="read the watchlist and print the plan; crawl and write nothing")
    ap.add_argument("--bank", action="append", metavar="RSSDID", help="restrict to this RSSD (repeatable); must be an active watchlist row")
    ap.add_argument("--max-banks", type=int, default=MAX_BANKS_HARD, help=f"banks per run, never above {MAX_BANKS_HARD}")
    ap.add_argument("--max-cost-usd", type=float, default=float(os.environ.get("RATE_RADAR_MAX_COST_USD", DEFAULT_MAX_COST_USD)))
    ap.add_argument("--max-minutes", type=float, default=float(os.environ.get("RATE_RADAR_MAX_MINUTES", DEFAULT_MAX_MINUTES)))
    ap.add_argument("--watchlist-file", help="JSON list of watchlist rows to plan from (dry run only; offline testing)")
    return ap.parse_args(argv)


def main(argv=None, env=None, store_factory=None, crawler_loader=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    env = os.environ if env is None else env
    args = parse_args(argv)

    bad = forbidden_env(env)
    if bad:
        print(f"REFUSED: {', '.join(bad)} is set. The weekly runner uses its own restricted database login "
              f"({DSN_ENV}) and must never run with the Supabase service key. Unset it and retry.", file=sys.stderr)
        return EXIT_CONFIG
    if args.watchlist_file and not args.dry_run:
        print("REFUSED: --watchlist-file is for --dry-run only; a real run reads the watchlist from the database.", file=sys.stderr)
        return EXIT_CONFIG
    if args.max_cost_usd <= 0 or args.max_minutes <= 0:
        print("REFUSED: the cost and time caps must be positive.", file=sys.stderr)
        return EXIT_CONFIG

    import pg_adapter
    store = None
    try:
        if args.watchlist_file:
            with open(args.watchlist_file, encoding="utf-8") as f:
                rows = json.load(f)
            for r in rows:
                if r.get("last_run_at") and isinstance(r["last_run_at"], str):
                    r["last_run_at"] = datetime.fromisoformat(r["last_run_at"])
        else:
            factory = store_factory or pg_adapter.RunnerStore
            store = factory(env.get(DSN_ENV, ""), read_only=args.dry_run)
            rows = store.load_watchlist()
            print(f"Connected to database '{store.database}' as {store.role}")
        try:
            plan = select_banks(rows, args.max_banks, args.bank)
        except SelectionError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return EXIT_CONFIG
        print_plan(plan, args.max_cost_usd, args.max_minutes, args.dry_run)
        if args.dry_run:
            return EXIT_OK
        if not plan["selected"]:
            print("Nothing to crawl: no active, non-manual watchlist bank is eligible.")
            return EXIT_NOTHING
        if not (env.get("ANTHROPIC_API_KEY") or "").strip():
            print("WARNING: ANTHROPIC_API_KEY is not set; every AI step in the crawler will be skipped.")
        return _crawl(store, plan, args, crawler_loader or _load_crawler)
    except pg_adapter.RunnerConfigError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as e:
        print(f"DATABASE/RUN ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_DB
    finally:
        if store is not None:
            store.close()


def _crawl(store, plan, args, crawler_loader):
    import pg_adapter
    crawler = crawler_loader()
    replaced = pg_adapter.install(store, crawler.storage, crawler.scrapers)
    missing = [n for n in pg_adapter.HOOK_NAMES if not any(name == n for _, name in replaced)]
    if missing:
        print(f"REFUSED: the crawler has no {', '.join(missing)} to redirect; refusing to risk a direct write.", file=sys.stderr)
        return EXIT_CONFIG

    banks = [bank_dict(r) for r in plan["selected"]]
    state = crawler.crawl_state
    state.update({"running": True, "results": [], "log": [], "done": False, "ai_calls": 0, "chat_calls": 0,
                  "crawl_mode": "standard", "force_refresh": True, "preflight_total": 0, "preflight_done": 0,
                  "phase": "starting", "removed": set(), "llm_input_tokens": 0, "llm_output_tokens": 0,
                  "llm_web_searches": 0, "_newly_flagged": [], "banks": banks, "_a132_run_id": None})

    orig_auto_save = crawler.scrapers.auto_save           # remember the run id the crawler's own save picks

    def capturing_auto_save(results):
        saved = orig_auto_save(results)
        if saved and saved[0]:
            state["_a132_run_id"] = saved[0]
        return saved
    crawler.scrapers.auto_save = capturing_auto_save

    started_at = datetime.now()
    limit_reason, crashed = None, None
    try:
        limit_reason = asyncio.run(run_with_limits(crawler, banks, args.max_cost_usd, args.max_minutes * 60))
    except Exception as e:
        crashed = f"crawler crashed: {type(e).__name__}: {e}"
    if limit_reason or crashed:
        asyncio.run(finalize_partial(crawler, store, started_at, limit_reason or crashed))

    run_id = state.get("_a132_run_id")
    results = state["results"]
    by_rssd = {str(r.get("RSSDID")): r for r in results}
    for row in plan["selected"]:
        status = (by_rssd.get(row["rssdid"]) or {}).get("status") or "not_run"
        try:
            if run_id:
                store.mark_watchlist(row["rssdid"], run_id, status)
        except Exception as e:
            state["log"].append(f"watchlist update failed for {row['rssdid']}: {type(e).__name__}")
    print("\n".join(state["log"]))

    found = sum(1 for r in results if r["status"] == "Found")
    summary = {"run_id": run_id, "banks_planned": len(banks), "banks_with_result": len(results), "found": found,
               "partial": sum(1 for r in results if r["status"] == "Partial"),
               "error": sum(1 for r in results if r["status"] == "Error"), "llm_cost_usd": round(_cost(crawler), 4),
               "limit": limit_reason or crashed}
    print("SUMMARY " + json.dumps(summary))
    if crashed or not run_id or not results or summary["error"] == len(results):
        return EXIT_NOTHING          # a crashed crawl still recorded its partial results, but the cron job must show as failed
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
