"""Tests for pg_adapter.py and weekly_runner.py (a132).

Run:  python "Rate Radar/tests/a132/test_runner_adapter.py"
The database tests start a throwaway local PostgreSQL (tests/a132/local_pg.py), apply the a132 migration draft to
a replica of the production Rate Radar tables, and connect as the real rate_radar_runner role, so a grant or policy
the adapter relies on but the migration does not give shows up here. They are skipped if no PostgreSQL binaries are
found. The crawler itself (Playwright, Anthropic) is replaced by a small fake that calls the same storage hooks
scrapers.py calls. Nothing touches Supabase, the network or the service key.
"""
import asyncio
import contextlib
import io
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RR = HERE.parent.parent
sys.path.insert(0, str(RR))
sys.path.insert(0, str(HERE))

import psycopg2
import psycopg2.errors

import local_pg
import pg_adapter
import shared
import storage
import weekly_runner

ORIGINALS = {n: getattr(storage, n) for n in (*pg_adapter.HOOK_NAMES, "auto_save")}


def wl_row(rssdid, key, **kw):
    r = dict(rssdid=rssdid, bank_key=key, bank_name=f"Bank {rssdid}", bank_url=f"https://www.{key}/", active=True,
             manual_only=False, manual_reason=None, last_run_at=None, bank_type="Commercial", branch_address="1 Main St")
    r.update(kw)
    return r


def make_result(bank, status="Found", rates=True):
    base = bank["bank_url"].rstrip("/")
    return {**bank, "checking_apy": None, "savings_apy": 4.10 if rates else None, "cd_apy": 4.50 if rates else None,
            "cd_term": "12 months" if rates else None, "money_market_apy": None, "min_balance": "$500", "status": status,
            "note": "test", "source_url": base + "/rates" if rates else "", "source_url_savings": base + "/rates" if rates else "",
            "source_url_cd": base + "/cds" if rates else "", "crawled_at": "2026-10-26 11:05"}


class SelectBanksTests(unittest.TestCase):
    def test_only_active_non_manual_rows_are_selected(self):
        rows = [wl_row("1", "a.example"), wl_row("2", "b.example", active=False),
                wl_row("3", "c.example", manual_only=True, manual_reason="blocks crawlers")]
        plan = weekly_runner.select_banks(rows)
        self.assertEqual([r["rssdid"] for r in plan["selected"]], ["1"])
        self.assertEqual(plan["inactive"], 1)
        self.assertEqual([(r["rssdid"], why) for r, why in plan["skipped"]], [("3", "manual verification due: blocks crawlers")])

    def test_cap_never_exceeds_ten_even_if_asked(self):
        rows = [wl_row(str(100 + i), f"b{i}.example") for i in range(15)]
        self.assertEqual(len(weekly_runner.select_banks(rows, max_banks=50)["selected"]), 10)
        self.assertEqual(len(weekly_runner.select_banks(rows, max_banks=3)["selected"]), 3)
        self.assertEqual(len(weekly_runner.select_banks(rows, max_banks=0)["selected"]), 1)

    def test_oldest_crawl_goes_first_when_over_the_cap(self):
        now = datetime.now(timezone.utc)
        rows = [wl_row(str(100 + i), f"b{i}.example", last_run_at=now - timedelta(days=i)) for i in range(12)]
        rows.append(wl_row("999", "never.example"))             # never crawled: first of all
        plan = weekly_runner.select_banks(rows)
        self.assertEqual(plan["selected"][0]["rssdid"], "999")
        self.assertEqual(sorted(r["rssdid"] for r in plan["deferred"]), ["100", "101", "102"])
        self.assertNotIn("111", [r["rssdid"] for r in plan["deferred"]])    # the stalest ones are in, the freshest deferred
        self.assertIn("100", [r["rssdid"] for r in plan["deferred"]])

    def test_domain_mismatch_and_missing_url_are_skipped_not_crawled(self):
        rows = [wl_row("1", "a.example", bank_url="https://www.other.example/"), wl_row("2", "b.example", bank_url="")]
        plan = weekly_runner.select_banks(rows)
        self.assertEqual(plan["selected"], [])
        reasons = {r["rssdid"]: why for r, why in plan["skipped"]}
        self.assertIn("bank_key mismatch", reasons["1"])
        self.assertEqual(reasons["2"], "no bank_url")

    def test_only_cannot_add_banks_outside_the_active_watchlist(self):
        rows = [wl_row("1", "a.example"), wl_row("2", "b.example", active=False)]
        self.assertEqual([r["rssdid"] for r in weekly_runner.select_banks(rows, only=["1"])["selected"]], ["1"])
        for bad in (["2"], ["77"], ["1", "77"]):
            with self.assertRaises(weekly_runner.SelectionError):
                weekly_runner.select_banks(rows, only=bad)


class GuardTests(unittest.TestCase):
    def run_main(self, argv, env, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = weekly_runner.main(argv, env=env, **kw)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def no_store(dsn, read_only=False):
        raise AssertionError("the database must not be touched")

    def test_refuses_to_start_with_any_service_key_and_never_echoes_it(self):
        for name in weekly_runner.FORBIDDEN_ENV:
            code, out, err = self.run_main(["--dry-run"], {name: "super-secret-value", weekly_runner.DSN_ENV: "x"},
                                           store_factory=self.no_store)
            self.assertEqual(code, weekly_runner.EXIT_CONFIG, name)
            self.assertIn(name, err)
            self.assertNotIn("super-secret-value", out + err)

    def test_empty_service_key_variable_does_not_block(self):
        self.assertEqual(weekly_runner.forbidden_env({"SUPABASE_SERVICE_KEY": "  ", "SUPABASE_KEY": ""}), [])

    def test_missing_database_url_refuses(self):
        code, out, err = self.run_main(["--dry-run"], {})
        self.assertEqual(code, weekly_runner.EXIT_CONFIG)
        self.assertIn(weekly_runner.DSN_ENV, err)

    def test_watchlist_file_is_dry_run_only(self):
        code, out, err = self.run_main(["--watchlist-file", "x.json"], {}, store_factory=self.no_store)
        self.assertEqual(code, weekly_runner.EXIT_CONFIG)

    def test_dry_run_from_file_touches_neither_database_nor_crawler(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wl.json"
            p.write_text('[{"rssdid": "1", "bank_key": "a.example", "bank_name": "Bank 1", "bank_url": "https://www.a.example/",'
                         ' "active": true, "manual_only": false}, {"rssdid": "2", "bank_key": "b.example", "bank_name": "Bank 2",'
                         ' "bank_url": "https://b.example", "active": true, "manual_only": true, "manual_reason": "blocked"}]')

            def no_crawler():
                raise AssertionError("the crawler must not be loaded on a dry run")
            code, out, err = self.run_main(["--dry-run", "--watchlist-file", str(p)], {},
                                           store_factory=self.no_store, crawler_loader=no_crawler)
        self.assertEqual(code, weekly_runner.EXIT_OK, err)
        self.assertIn("DRY RUN", out)
        self.assertIn("crawl", out)
        self.assertIn("manual verification due: blocked", out)


@unittest.skipUnless(local_pg.find_pgbin(), "PostgreSQL binaries not found (set PGBIN)")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = local_pg.ScratchCluster(local_pg.find_pgbin())
        cls.cluster.start()
        try:
            local_pg.build_runner_db(cls.cluster, "rr")
            cls.cluster.psql("rr", sql="""
                insert into public.bank_registry (bank_key, bank_name, bank_url, rssdid, bank_type, branch_address) values
                  ('bank-a.example', 'Bank A', 'https://www.bank-a.example/', '1000001', 'Commercial', '1 A St'),
                  ('bank-b.example', 'Bank B', 'https://www.bank-b.example/', '1000002', 'Savings', '2 B St'),
                  ('bank-c.example', 'Bank C', 'https://www.bank-c.example/', '1000003', 'Commercial', '3 C St');
                insert into public.rate_radar_watchlist (rssdid, bank_key, bank_name, bank_url, added_by) values
                  ('1000001', 'bank-a.example', 'Bank A', 'https://www.bank-a.example/', 'approver_1'),
                  ('1000002', 'bank-b.example', 'Bank B', 'https://www.bank-b.example/', 'approver_1');
                insert into public.url_pattern_registry (pattern, category, bank_key, active) values
                  ('/rates-x', 'generic', null, true), ('/old-x', 'generic', null, false),
                  ('/a-special', 'bank_specific', 'bank-a.example', true);
                insert into raw.raw_rate_radar (run_id, run_date, crawled_at, bank_name, rssdid, savings_apy, status) values
                  ('hist1', '2026-10-19', '2026-10-19 11:00', 'Bank A', '1000001', 3.95, 'Found');""")
            cls.dsn = cls.cluster.dsn("rr", "rate_radar_runner")
            cls.su = psycopg2.connect(cls.cluster.dsn("rr", "postgres"))
            cls.su.autocommit = True
        except Exception:
            cls.cluster.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.su.close()
        cls.cluster.stop()

    def setUp(self):
        self.store = pg_adapter.RunnerStore(self.dsn)
        self.addCleanup(self.store.close)
        self.exports = tempfile.TemporaryDirectory()
        self.addCleanup(self.exports.cleanup)
        self._exports_orig = storage.EXPORTS
        storage.EXPORTS = Path(self.exports.name)
        self.addCleanup(self._restore)

    def _restore(self):
        storage.EXPORTS = self._exports_orig
        for name, fn in ORIGINALS.items():
            setattr(storage, name, fn)
        shared.crawl_state.update({"llm_input_tokens": 0, "llm_output_tokens": 0, "llm_web_searches": 0})

    def q(self, sql, params=None):
        with self.su.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else cur.rowcount

    def next_run_id(self):
        DatabaseTests._run_no = getattr(DatabaseTests, "_run_no", 0) + 1
        return f"t_{DatabaseTests._run_no:04d}"

    def bank(self, key, rssdid, name=None):
        return {"bank_name": name or f"Bank {key[5].upper()}", "bank_url": f"https://www.{key}/", "RSSDID": rssdid,
                "bank_type": "Commercial", "branch_address": ""}

    # ── connection ──────────────────────────────────────────────────────────
    def test_refuses_any_role_other_than_the_runner(self):
        with self.assertRaises(pg_adapter.RunnerConfigError) as cm:
            pg_adapter.RunnerStore(self.cluster.dsn("rr", "postgres"))
        self.assertIn("rate_radar_runner", str(cm.exception))

    def test_connection_failure_never_leaks_the_password(self):
        with self.assertRaises(pg_adapter.RunnerConfigError) as cm:
            pg_adapter.RunnerStore("postgresql://rate_radar_runner:s3cretpw@127.0.0.1:1/rr?sslmode=disable")  # pragma: allowlist secret
        self.assertNotIn("s3cretpw", str(cm.exception))

    def test_missing_dsn_refuses(self):
        with self.assertRaises(pg_adapter.RunnerConfigError):
            pg_adapter.RunnerStore("")

    def test_read_only_session_cannot_write(self):
        ro = pg_adapter.RunnerStore(self.dsn, read_only=True)
        self.addCleanup(ro.close)
        self.assertEqual(len(ro.load_watchlist()), 2)
        with self.assertRaises(psycopg2.errors.ReadOnlySqlTransaction):
            ro.mark_watchlist("1000001", "x", "x")

    def test_runner_cannot_delete_or_read_other_tables(self):
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.store._run(lambda cur: cur.execute("delete from public.bank_registry"))
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.store._run(lambda cur: cur.execute("select * from public.canary_other"))
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.store._run(lambda cur: cur.execute("select * from public.rate_radar_runs"))

    # ── watchlist ───────────────────────────────────────────────────────────
    def test_watchlist_rows_carry_registry_fields(self):
        rows = {r["rssdid"]: r for r in self.store.load_watchlist()}
        self.assertEqual(sorted(rows), ["1000001", "1000002"])
        self.assertEqual(rows["1000001"]["bank_type"], "Commercial")
        self.assertEqual(rows["1000002"]["branch_address"], "2 B St")

    def test_mark_watchlist_updates_only_the_named_row(self):
        self.store.mark_watchlist("1000002", "run_zz", "Partial")
        got = dict((r[0], r[1:]) for r in self.q("select rssdid, last_run_id, last_status from public.rate_radar_watchlist"))
        self.assertEqual(got["1000002"], ("run_zz", "Partial"))
        self.assertNotEqual(got["1000001"][0], "run_zz")

    # ── crawler hooks ───────────────────────────────────────────────────────
    def test_run_summary_is_insert_only_and_duplicate_is_a_noop(self):
        rid = self.next_run_id()
        args = (rid, "2026-10-26", datetime.now(), 2, 1, 0, 3, 0, 1000, 500, 2)
        self.assertTrue(asyncio.run(self.store.push_run_summary(*args, notes="first")))
        self.assertFalse(asyncio.run(self.store.push_run_summary(*args, notes="second")))
        rows = self.q("select banks_attempted, notes, estimated_cost_usd from public.rate_radar_runs where run_id = %s", (rid,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 3)
        self.assertEqual(rows[0][1], "first")                  # never overwritten
        self.assertGreater(float(rows[0][2]), 0)

    def test_results_land_for_watchlist_banks(self):
        rid = self.next_run_id()
        results = [make_result(self.bank("bank-a.example", "1000001")), make_result(self.bank("bank-b.example", "1000002"))]
        asyncio.run(self.store.push_run_summary(rid, "2026-10-26", datetime.now(), 2, 0, 0, 0, 0))
        self.assertTrue(asyncio.run(self.store.push_to_supabase(results, rid, "2026-10-26")))
        self.assertTrue(asyncio.run(self.store.push_rate_observations(results, rid)))
        self.assertEqual(self.q("select count(*) from raw.raw_rate_radar where run_id = %s", (rid,))[0][0], 2)
        self.assertEqual(self.q("select count(*) from public.rate_observations where run_id = %s", (rid,))[0][0], 4)  # savings + cd each
        self.assertEqual(self.q("select count(distinct rssdid) from public.rate_observations where run_id = %s", (rid,))[0][0], 2)

    def test_a_batch_with_a_bank_off_the_watchlist_writes_nothing(self):
        rid = self.next_run_id()
        results = [make_result(self.bank("bank-a.example", "1000001")), make_result(self.bank("bank-c.example", "1000003"))]
        asyncio.run(self.store.push_run_summary(rid, "2026-10-26", datetime.now(), 2, 0, 0, 0, 0))
        self.assertFalse(asyncio.run(self.store.push_to_supabase(results, rid, "2026-10-26")))
        self.assertFalse(asyncio.run(self.store.push_rate_observations(results, rid)))
        self.assertEqual(self.q("select count(*) from raw.raw_rate_radar where run_id = %s", (rid,))[0][0], 0)
        self.assertEqual(self.q("select count(*) from public.rate_observations where run_id = %s", (rid,))[0][0], 0)

    def test_rows_without_an_rssd_are_dropped_not_fatal(self):
        rid = self.next_run_id()
        no_id = make_result(self.bank("bank-a.example", "1000001"))
        no_id["RSSDID"] = ""
        asyncio.run(self.store.push_run_summary(rid, "2026-10-26", datetime.now(), 2, 0, 0, 0, 0))
        ok = asyncio.run(self.store.push_to_supabase([make_result(self.bank("bank-b.example", "1000002")), no_id], rid, "2026-10-26"))
        self.assertTrue(ok)
        self.assertEqual(self.q("select count(*) from raw.raw_rate_radar where run_id = %s", (rid,))[0][0], 1)

    def test_extraction_cache_round_trip_scoped_to_the_watchlist(self):
        asyncio.run(self.store._extraction_cache_put("hash-a", "Bank A", "u", {"savings": 4.2}, "m"))
        asyncio.run(self.store._extraction_cache_put("hash-a", "Bank A", "u", {"savings": 9.9}, "m"))      # duplicate: skipped
        asyncio.run(self.store._extraction_cache_put("hash-c", "Bank C", "u", {"savings": 1.0}, "m"))      # not on the watchlist: skipped
        self.assertEqual(asyncio.run(self.store._extraction_cache_get("hash-a")), {"savings": 4.2})
        self.assertIsNone(asyncio.run(self.store._extraction_cache_get("hash-c")))
        self.assertEqual(self.q("select count(*) from public.extraction_cache where content_hash = 'hash-c'")[0][0], 0)

    def test_registry_learns_from_a_verified_url(self):
        failures = asyncio.run(self.store.update_bank_registry(
            self.bank("bank-a.example", "1000001"), make_result(self.bank("bank-a.example", "1000001"))))
        self.assertIsNone(failures)
        row = self.q("select last_successful_url, known_rate_urls, consecutive_failures, needs_review "
                     "from public.bank_registry where bank_key = 'bank-a.example'")[0]
        self.assertEqual(row[0], "https://www.bank-a.example/rates")
        self.assertIn("https://www.bank-a.example/rates", row[1])
        self.assertEqual((row[2], row[3]), (0, False))

    def test_registry_counts_failures_and_flags_at_the_threshold(self):
        b = self.bank("bank-b.example", "1000002")
        seen = [asyncio.run(self.store.update_bank_registry(b, make_result(b, "Not public", rates=False))) for _ in range(3)]
        self.assertEqual(seen, [1, 2, shared.CIRCUIT_BREAKER_THRESHOLD])
        self.assertEqual(self.q("select consecutive_failures, needs_review from public.bank_registry where bank_key = 'bank-b.example'")[0], (3, True))

    def test_registry_write_for_a_bank_off_the_watchlist_changes_nothing(self):
        c = self.bank("bank-c.example", "1000003")
        self.assertIsNone(asyncio.run(self.store.update_bank_registry(c, make_result(c, "Not public", rates=False))))
        self.assertEqual(self.q("select consecutive_failures from public.bank_registry where bank_key = 'bank-c.example'")[0][0], 0)

    def test_last_known_good_comes_back_in_the_shape_the_crawler_expects(self):
        # other tests also write Bank A rows, so compare with the newest row the database itself holds
        want = self.q("select run_id, savings_apy, run_date::text from raw.raw_rate_radar where bank_name = 'Bank A' "
                      "and savings_apy is not null order by run_date desc, crawled_at desc nulls last limit 1")[0]
        hist = asyncio.run(self.store.fetch_last_known_good("Bank A", None))
        self.assertEqual(hist["run_id"], want[0])
        self.assertEqual(hist["savings_apy"], float(want[1]))
        self.assertIsInstance(hist["savings_apy"], float)
        self.assertEqual(hist["run_date"], want[2])                     # an ISO string, as PostgREST gave it
        self.assertIsNone(asyncio.run(self.store.fetch_last_known_good("Bank C", None)))

    def add_bank(self, rssd, key, name, unique=False):
        self.q("insert into public.bank_registry (bank_key, bank_name, bank_url, rssdid) values (%s, %s, %s, %s)",
               (key, name, f"https://www.{key}/", rssd))
        self.q("insert into public.rate_radar_watchlist (rssdid, bank_key, bank_name, bank_url, added_by, name_unique_in_registry) "
               "values (%s, %s, %s, %s, 'approver_1', %s)", (rssd, key, name, f"https://www.{key}/", unique))

    def add_history(self, run_id, name, rssd, savings, run_date="2026-10-20"):
        self.q("insert into raw.raw_rate_radar (run_id, run_date, crawled_at, bank_name, rssdid, savings_apy, status) "
               "values (%s, %s, %s::timestamptz, %s, %s, %s, 'Found')", (run_id, run_date, run_date + " 11:00", name, rssd, savings))

    def history_fixtures(self):
        """Three watchlist banks named 'First Bank' (names are not unique in the real data) plus two uniquely named banks,
        with a mix of RSSD-carrying history and legacy pre-RSSD rows (rssdid NULL)."""
        def cleanup():
            self.q("delete from raw.raw_rate_radar where run_id like 'fb\_%'")
            self.q("delete from public.rate_radar_watchlist where rssdid like '30000%'")
            self.q("delete from public.bank_registry where bank_key like 'hist-%.example'")
        self.addCleanup(cleanup)
        for rssd, key in (("3000001", "hist-first-1.example"), ("3000002", "hist-first-2.example"), ("3000004", "hist-first-4.example")):
            self.add_bank(rssd, key, "First Bank")                      # ambiguous name: flag stays false
        self.add_history("fb_1", "First Bank", "3000001", 3.11)
        self.add_history("fb_2", "First Bank", "3000002", 4.22)
        self.add_history("fb_legacy_a", "First Bank", None, 7.77, "2026-09-01")   # legacy rows: whose are they? nobody can say
        self.add_history("fb_legacy_b", "First Bank", None, 8.88, "2026-09-02")
        self.add_bank("3000010", "hist-solo.example", "Solo Bank", unique=True)
        self.add_history("fb_solo_legacy", "Solo Bank", None, 5.55, "2026-08-01")
        # an empty-string rssdid does not occur in production (the writer stores NULL) and is never treated as legacy history
        self.add_history("fb_solo_empty", "Solo Bank", "", 1.23, "2026-10-02")
        self.add_history("fb_first_empty", "First Bank", "", 2.34, "2026-10-02")
        self.add_bank("3000011", "hist-solo-flag-off.example", "Solo Off Bank", unique=False)
        self.add_history("fb_off_legacy", "Solo Off Bank", None, 6.66, "2026-08-01")
        self.add_bank("3000012", "hist-both.example", "Both Bank", unique=True)
        self.add_history("fb_both_id", "Both Bank", "3000012", 2.22, "2026-07-01")
        self.add_history("fb_both_legacy", "Both Bank", None, 9.99, "2026-10-01")   # newer, but the id match wins

    def lookup(self, name, rssd):
        return asyncio.run(self.store.fetch_last_known_good(name, None, rssdid=rssd))

    def test_two_banks_with_the_same_name_never_cross_in_the_history_lookup(self):
        self.history_fixtures()
        self.assertEqual(self.lookup("First Bank", "3000001")["savings_apy"], 3.11)
        self.assertEqual(self.lookup("First Bank", "3000002")["savings_apy"], 4.22)
        # no own history, and the legacy NULL rows carry a name shared by three banks: nothing, never another bank's rates
        self.assertIsNone(self.lookup("First Bank", "3000004"))
        # an RSSD that is not on the watchlist finds nothing
        self.assertIsNone(self.lookup("First Bank", "3000003"))

    def test_legacy_pre_rssd_rows_are_used_only_for_a_name_flagged_unique(self):
        self.history_fixtures()
        solo = self.lookup("Solo Bank", "3000010")
        self.assertEqual((solo["savings_apy"], solo["rssdid"]), (5.55, None))      # id finds nothing, unique name: the NULL row, not the newer '' row
        self.assertIsNone(self.lookup("Solo Off Bank", "3000011"))                  # flag off: the database hides the legacy row
        self.assertEqual(self.lookup("Both Bank", "3000012")["savings_apy"], 2.22)  # an id match beats a newer legacy row

    def test_storage_lookup_asks_for_the_rssd_then_retries_pre_rssd_rows_by_name(self):
        def run(raw_by_filter, registry_rows, rssdid="3000001"):
            urls = []

            class Resp:
                def __init__(self, body):
                    self.status, self._body = 200, body

                async def json(self):
                    return self._body

            class Ctx:
                def __init__(self, body):
                    self._body = body

                async def __aenter__(self):
                    return Resp(self._body)

                async def __aexit__(self, *exc):
                    return False

            class Session:
                def get(self, url, **kw):
                    urls.append(url)
                    if "/bank_registry" in url:
                        return Ctx(registry_rows)
                    for marker, rows in raw_by_filter.items():
                        if marker in url:
                            return Ctx(rows)
                    return Ctx([])
            saved = (storage.SUPABASE_URL, storage.SUPABASE_KEY)
            storage.SUPABASE_URL, storage.SUPABASE_KEY = "http://localhost:1", "x"
            try:
                got = asyncio.run(ORIGINALS["fetch_last_known_good"]("First Bank", Session(), rssdid=rssdid))
            finally:
                storage.SUPABASE_URL, storage.SUPABASE_KEY = saved
            return got, urls

        row, urls = run({"rssdid=eq.3000001": [{"savings_apy": 1.0}]}, [])
        self.assertEqual(row, {"savings_apy": 1.0})
        self.assertEqual(len(urls), 1)                                  # id hit: no second query
        self.assertNotIn("bank_name=", urls[0])
        row, urls = run({"rssdid=is.null": [{"savings_apy": 2.0}]}, [{"bank_key": "a"}])
        self.assertEqual(row, {"savings_apy": 2.0})                     # id empty, unique name: legacy rows
        self.assertIn("rssdid=eq.3000001", urls[0])
        self.assertIn("bank_name=eq.First%20Bank&rssdid=is.null", urls[2])
        row, urls = run({"rssdid=is.null": [{"savings_apy": 3.0}]}, [{"bank_key": "a"}, {"bank_key": "b"}])
        self.assertIsNone(row)                                          # shared name: rssdid only, no legacy query
        self.assertEqual(len(urls), 2)
        row, urls = run({"bank_name=eq.First%20Bank": [{"savings_apy": 4.0}]}, [], rssdid=None)
        self.assertEqual(row, {"savings_apy": 4.0})                     # no id at all: the old name-only path, one query
        self.assertEqual(len(urls), 1)

    def test_the_crawler_call_site_passes_the_rssd_under_either_key(self):
        """Evaluates the real rssdid= argument at the fetch_last_known_good call in scrapers.py, so the test breaks if the
        fallback to the lowercase key is removed (the crawler reads bank["RSSDID"] or bank["rssdid"] elsewhere)."""
        import ast
        tree = ast.parse((RR / "scrapers.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "fetch_last_known_good"]
        self.assertEqual(len(calls), 1)
        arg = next(k.value for k in calls[0].keywords if k.arg == "rssdid")
        def rssd(bank):
            return eval(compile(ast.Expression(arg), "scrapers.py", "eval"), {"bank": bank})
        self.assertEqual(rssd({"RSSDID": "111"}), "111")
        self.assertEqual(rssd({"rssdid": "222"}), "222")                       # lowercase key works
        self.assertEqual(rssd({"RSSDID": "111", "rssdid": "222"}), "111")      # upper-case wins when both exist
        self.assertFalse(rssd({"RSSDID": "", "rssdid": ""}))                   # nothing: falsy, so the old name-only path

    def test_url_config_merges_registry_patterns_and_health(self):
        generic, bank_urls, health = asyncio.run(self.store.load_bank_url_config())
        self.assertIn("/rates-x", generic)
        self.assertNotIn("/old-x", generic)
        self.assertIn("/a-special", bank_urls["bank-a.example"])
        self.assertIn("bank-a.example", health)
        self.assertNotIn("bank-c.example", health)             # not on the watchlist: invisible to the runner

    # ── wiring ──────────────────────────────────────────────────────────────
    def fake_crawler(self, behaviour="ok"):
        """A stand-in for scrapers: keeps the real hook names as module globals (so install() has to replace them)
        and calls them in the order run_crawler does. behaviour: ok | slow (blows the cost cap) | crash."""
        fake = types.ModuleType("fake_scrapers")
        for n in pg_adapter.HOOK_NAMES:
            setattr(fake, n, ORIGINALS[n])

        def auto_save(results):
            return self.next_run_id(), "2026-10-26"
        fake.auto_save = storage.auto_save = auto_save
        seen = {"banks": None}

        async def run_crawler(banks):
            st = shared.crawl_state
            seen["banks"] = list(banks)
            await fake.load_bank_url_config()
            for b in banks:
                res = make_result(b)
                st["results"].append(res)
                await fake.update_bank_registry(b, res)
                await fake._extraction_cache_put(f"h-{b['RSSDID']}", b["bank_name"], b["bank_url"], {"savings": 4.1}, "m")
            if behaviour == "crash":
                raise RuntimeError("boom")
            if behaviour == "slow":
                st["llm_input_tokens"] = 10_000_000             # $10 of input tokens
                await asyncio.sleep(60)
            st["running"], st["done"] = False, True
            run_id, run_date = fake.auto_save(st["results"])
            await fake.push_to_supabase(st["results"], run_id, run_date)
            await fake.push_run_summary(run_id, run_date, datetime.now(), len(st["results"]), 0, 0, 0, 0)
            await fake.push_rate_observations(st["results"], run_id)
        fake.run_crawler = run_crawler
        ns = types.SimpleNamespace(shared=shared, storage=storage, scrapers=fake, crawl_state=shared.crawl_state,
                                   run_crawler=run_crawler, estimate=shared._estimate_cost_usd)
        return ns, seen

    def run_runner(self, crawler, argv=(), env_extra=None):
        env = {weekly_runner.DSN_ENV: self.dsn, "ANTHROPIC_API_KEY": "test-not-real", **(env_extra or {})}  # pragma: allowlist secret
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = weekly_runner.main(list(argv), env=env, crawler_loader=lambda: crawler)
        return code, out.getvalue(), err.getvalue()

    def test_install_replaces_every_hook_in_every_module(self):
        crawler, _ = self.fake_crawler()
        replaced = pg_adapter.install(self.store, crawler.storage, crawler.scrapers)
        self.assertEqual(len(replaced), 2 * len(pg_adapter.HOOK_NAMES))
        for mod in (storage, crawler.scrapers):
            for n in pg_adapter.HOOK_NAMES:
                self.assertIs(getattr(mod, n).__self__, self.store, f"{mod.__name__}.{n}")

    def test_dry_run_reads_the_watchlist_and_writes_nothing(self):
        before = self.q("select (select count(*) from public.rate_radar_runs), (select count(*) from raw.raw_rate_radar), "
                        "(select count(*) from public.rate_observations)")[0]
        stamp = self.q("select array_agg(last_run_id order by rssdid) from public.rate_radar_watchlist")[0][0]

        def no_crawler():
            raise AssertionError("no crawler on a dry run")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = weekly_runner.main(["--dry-run"], env={weekly_runner.DSN_ENV: self.dsn}, crawler_loader=no_crawler)
        self.assertEqual(code, weekly_runner.EXIT_OK, err.getvalue())
        self.assertIn("Bank A", out.getvalue())
        self.assertNotIn("postgresql://", out.getvalue() + err.getvalue())
        after = self.q("select (select count(*) from public.rate_radar_runs), (select count(*) from raw.raw_rate_radar), "
                       "(select count(*) from public.rate_observations)")[0]
        self.assertEqual(before, after)
        self.assertEqual(stamp, self.q("select array_agg(last_run_id order by rssdid) from public.rate_radar_watchlist")[0][0])

    def test_full_run_records_results_and_stamps_the_watchlist(self):
        crawler, seen = self.fake_crawler()
        code, out, err = self.run_runner(crawler, ["--max-cost-usd", "5"])
        self.assertEqual(code, weekly_runner.EXIT_OK, out + err)
        self.assertEqual(sorted(b["RSSDID"] for b in seen["banks"]), ["1000001", "1000002"])
        self.assertEqual(seen["banks"][0]["bank_type"], "Commercial")
        run_id = [l for l in out.splitlines() if l.startswith("SUMMARY")][0].split('"run_id": "')[1].split('"')[0]
        self.assertEqual(self.q("select banks_attempted from public.rate_radar_runs where run_id = %s", (run_id,))[0][0], 2)
        self.assertEqual(self.q("select count(*) from raw.raw_rate_radar where run_id = %s", (run_id,))[0][0], 2)
        self.assertGreater(self.q("select count(*) from public.rate_observations where run_id = %s", (run_id,))[0][0], 0)
        stamped = self.q("select last_run_id, last_status from public.rate_radar_watchlist order by rssdid")
        self.assertEqual(stamped, [(run_id, "Found"), (run_id, "Found")])
        self.assertNotIn(self.dsn, out + err)

    def test_manual_only_and_inactive_banks_are_never_crawled(self):
        self.q("update public.rate_radar_watchlist set manual_only = true, manual_reason = 'blocks crawlers' where rssdid = '1000001'")
        self.addCleanup(self.q, "update public.rate_radar_watchlist set manual_only = false, manual_reason = null where rssdid = '1000001'")
        crawler, seen = self.fake_crawler()
        code, out, err = self.run_runner(crawler)
        self.assertEqual(code, weekly_runner.EXIT_OK, out + err)
        self.assertEqual([b["RSSDID"] for b in seen["banks"]], ["1000002"])
        self.assertIn("manual verification due: blocks crawlers", out)

    def test_ten_bank_cap_with_a_twelve_bank_watchlist(self):
        self.q("update public.rate_radar_watchlist set active = false")
        for i in range(12):
            key = f"cap{i:02d}.example"
            self.q("insert into public.bank_registry (bank_key, bank_name, bank_url, rssdid) values (%s, %s, %s, %s)",
                   (key, f"Cap {i:02d}", f"https://www.{key}/", f"20000{i:02d}"))
            self.q("insert into public.rate_radar_watchlist (rssdid, bank_key, bank_name, bank_url, added_by) values (%s, %s, %s, %s, 'approver_1')",
                   (f"20000{i:02d}", key, f"Cap {i:02d}", f"https://www.{key}/"))

        def cleanup():
            self.q("delete from public.rate_radar_watchlist where rssdid like '20000%'")
            self.q("delete from public.bank_registry where bank_key like 'cap%.example'")
            self.q("update public.rate_radar_watchlist set active = true where rssdid in ('1000001', '1000002')")
        self.addCleanup(cleanup)
        crawler, seen = self.fake_crawler()
        code, out, err = self.run_runner(crawler, ["--max-banks", "99"])
        self.assertEqual(code, weekly_runner.EXIT_OK, out + err)
        self.assertEqual(len(seen["banks"]), 10)
        self.assertEqual(sorted(b["RSSDID"] for b in seen["banks"]), [f"20000{i:02d}" for i in range(10)])
        self.assertIn("defer", out)

    def test_cost_cap_cancels_the_crawl_and_records_the_partial_run(self):
        crawler, seen = self.fake_crawler("slow")
        t0 = time.monotonic()
        code, out, err = self.run_runner(crawler, ["--max-cost-usd", "1"])
        self.assertLess(time.monotonic() - t0, 30)              # cancelled long before the 60 s sleep ends
        self.assertEqual(code, weekly_runner.EXIT_OK, out + err)
        run_id = [l for l in out.splitlines() if l.startswith("SUMMARY")][0].split('"run_id": "')[1].split('"')[0]
        notes = self.q("select notes from public.rate_radar_runs where run_id = %s", (run_id,))[0][0]
        self.assertIn("COST CAP", notes)
        self.assertEqual(self.q("select count(*) from raw.raw_rate_radar where run_id = %s", (run_id,))[0][0], 2)
        self.assertIn("COST CAP", out)

    def test_a_crashed_crawler_still_records_what_finished_and_exits_nonzero(self):
        crawler, _ = self.fake_crawler("crash")
        code, out, err = self.run_runner(crawler)
        self.assertEqual(code, weekly_runner.EXIT_NOTHING, out + err)
        run_id = [l for l in out.splitlines() if l.startswith("SUMMARY")][0].split('"run_id": "')[1].split('"')[0]
        self.assertIn("crawler crashed", self.q("select notes from public.rate_radar_runs where run_id = %s", (run_id,))[0][0])

    def test_refuses_if_the_crawler_has_a_storage_function_it_cannot_redirect(self):
        crawler, _ = self.fake_crawler()
        del crawler.storage.__dict__["push_to_supabase"]        # a renamed storage function must stop the run, not bypass it
        del crawler.scrapers.__dict__["push_to_supabase"]
        code, out, err = self.run_runner(crawler)
        self.assertEqual(code, weekly_runner.EXIT_CONFIG)
        self.assertIn("push_to_supabase", err)


def load_credential_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("a132_cred", RR / "draft" / "a132_make_runner_credential.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CredentialTests(unittest.TestCase):
    def test_rfc7677_self_test_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(load_credential_module().self_test())

    @unittest.skipUnless(local_pg.find_pgbin(), "PostgreSQL binaries not found (set PGBIN)")
    def test_verifier_matches_the_one_postgres_makes_for_the_same_password_and_salt(self):
        import base64
        cred = load_credential_module()
        cluster = local_pg.ScratchCluster(local_pg.find_pgbin(), port=54342)
        cluster.start()
        try:
            cluster.psql("postgres", sql="set password_encryption = 'scram-sha-256'; create role probe login")  # pragma: allowlist secret
            cluster.psql("postgres", sql="alter role probe password 'correct-horse-9'")
            pg_verifier = cluster.psql("postgres", sql="select rolpassword from pg_authid where rolname = 'probe'")
            head, rest = pg_verifier.split("$", 1)
            iters_salt, keys = rest.split("$", 1)
            iters, salt_b64 = iters_salt.split(":")
            mine = cred.scram_verifier("correct-horse-9", salt=base64.b64decode(salt_b64), iterations=int(iters))
            self.assertEqual(mine, pg_verifier)
            # and PostgreSQL accepts a verifier built by the script as a stored password
            cluster.psql("postgres", sql=f"alter role probe password '{cred.scram_verifier('another-pw-1')}'")
        finally:
            cluster.stop()

    def test_script_keeps_the_password_out_of_its_output_and_out_of_git(self):
        import os
        cred = load_credential_module()
        with tempfile.TemporaryDirectory() as d:
            out_file = Path(d) / "sub" / "runner.pw"
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = cred.main(["--out", str(out_file)])
            self.assertEqual(code, 0, err.getvalue())
            password = out_file.read_text().strip()
            self.assertGreaterEqual(len(password), 40)
            self.assertNotIn(password, buf.getvalue() + err.getvalue())
            self.assertIn("ALTER ROLE rate_radar_runner PASSWORD 'SCRAM-SHA-256$4096:", buf.getvalue())
            # refuses to overwrite
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cred.main(["--out", str(out_file)]), 2)
            self.assertEqual(out_file.read_text().strip(), password)
        # refuses a path inside a git repository
        inside = RR / "should_not_exist.pw"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cred.main(["--out", str(inside)]), 2)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
