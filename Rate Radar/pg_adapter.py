# ── Postgres write adapter for the weekly runner (a132) ──────────────────────
# Replaces the storage-layer functions the crawler calls (PostgREST + service key)
# with direct SQL as the dedicated login role rate_radar_runner. That role can
# read the watchlist, insert run/observation/raw/cache rows for watchlist banks,
# and update only the learning and last_* columns (draft/a132_runner_watchlist.sql).
# So every statement here is shaped to need nothing more:
#   - inserts use ON CONFLICT DO NOTHING (no conflict target, no UPDATE, no SELECT needed)
#   - bank_registry is UPDATE-only (watchlist banks must already be in the registry)
#   - nothing here DELETEs, and nothing reads rate_radar_runs or rate_observations back
# The signatures match storage.py so install() can swap them in without touching the crawler.

import asyncio
import threading
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extensions
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor, execute_values

from shared import (
    crawl_state, RATE_PATHS, BANK_EXTRA_URLS, CIRCUIT_BREAKER_THRESHOLD, _estimate_cost_usd,
)

RUNNER_ROLE = "rate_radar_runner"
DSN_ENV = "RATE_RADAR_DATABASE_URL"
APPLICATION_NAME = "rate_radar_weekly_runner"

# storage.py names the crawler imported directly, so each must be replaced in every module that holds a copy
HOOK_NAMES = (
    "fetch_last_known_good", "push_to_supabase", "push_rate_observations", "load_bank_url_config",
    "_extraction_cache_get", "_extraction_cache_put", "update_bank_registry", "push_run_summary",
)


class RunnerConfigError(RuntimeError):
    """The connection is not the restricted runner role (or is not configured). Never carries the DSN."""


def _log(msg):
    crawl_state["log"].append(msg)


def _plain(value):
    """psycopg2 returns Decimal/date/datetime; the crawler expects what PostgREST's JSON gave it."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class RunnerStore:
    def __init__(self, dsn, read_only=False):
        if not dsn:
            raise RunnerConfigError(f"{DSN_ENV} is not set")
        self._dsn = dsn
        self._read_only = read_only
        self._lock = threading.Lock()
        self._conn = None
        self._connect()

    # ── connection ──────────────────────────────────────────────────────────
    def _connect(self):
        kwargs = {"connect_timeout": 10, "application_name": APPLICATION_NAME}
        try:
            if "sslmode" not in psycopg2.extensions.parse_dsn(self._dsn):
                kwargs["sslmode"] = "require"
            conn = psycopg2.connect(self._dsn, **kwargs)
        except psycopg2.Error as e:
            # the driver's message can echo connection details: keep only its class and first line
            first = (str(e).strip().splitlines() or [""])[0]
            raise RunnerConfigError(f"could not connect: {type(e).__name__}: {first[:160]}") from None
        conn.set_session(readonly=self._read_only, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("select current_user, current_database()")
            who, self.database = cur.fetchone()
        conn.rollback()
        if who != RUNNER_ROLE:
            conn.close()
            raise RunnerConfigError(f"connected as '{who}', refusing to run: only {RUNNER_ROLE} may be used")
        self.role = who
        self._conn = conn

    def close(self):
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()

    def _run(self, fn):
        """Run fn(cursor) in one transaction on the shared connection; reconnect once if the link dropped."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._connect()
                    with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
                        out = fn(cur)
                    self._conn.commit()
                    return out
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    if self._conn is not None and not self._conn.closed:
                        try:
                            self._conn.rollback()
                        except psycopg2.Error:
                            pass
                        self._conn.close()
                    if attempt == 2:
                        raise
                except Exception:
                    if self._conn is not None and not self._conn.closed:
                        self._conn.rollback()
                    raise

    async def _arun(self, fn):
        return await asyncio.to_thread(self._run, fn)

    # ── watchlist (the runner's only source of banks) ───────────────────────
    def load_watchlist(self):
        """All watchlist rows with the registry fields the crawler's bank dict needs."""
        def op(cur):
            cur.execute("""
                select w.rssdid, w.bank_key, w.bank_name, w.bank_url, w.active, w.manual_only, w.manual_reason,
                       w.last_run_at, r.bank_type, r.branch_address
                from public.rate_radar_watchlist w
                left join public.bank_registry r on r.bank_key = w.bank_key
                order by w.rssdid""")
            return [dict(row) for row in cur.fetchall()]
        return self._run(op)

    def mark_watchlist(self, rssdid, run_id, status):
        def op(cur):
            cur.execute("update public.rate_radar_watchlist set last_run_id = %s, last_run_at = now(), last_status = %s "
                        "where rssdid = %s", (run_id, status, str(rssdid)))
            return cur.rowcount
        return self._run(op)

    # ── storage.py replacements (same names and signatures) ─────────────────
    async def load_bank_url_config(self):
        """Merged built-in + registry URL config, same shape as storage.load_bank_url_config."""
        generic = list(RATE_PATHS)
        bank_urls = {k: list(v) for k, v in BANK_EXTRA_URLS.items()}
        health = {}

        def op(cur):
            cur.execute("select pattern, category, bank_key from public.url_pattern_registry where active")
            patterns = cur.fetchall()
            cur.execute("select bank_key, known_rate_urls, last_successful_url, consecutive_failures, needs_review, "
                        "zip_gate_zip from public.bank_registry")
            return patterns, cur.fetchall()

        try:
            patterns, banks = await self._arun(op)
            for row in patterns:
                if row["category"] == "generic":
                    if row["pattern"] not in generic:
                        generic.append(row["pattern"])
                elif row.get("bank_key"):
                    lst = bank_urls.setdefault(row["bank_key"], [])
                    if row["pattern"] not in lst:
                        lst.append(row["pattern"])
            for row in banks:
                lst = bank_urls.setdefault(row["bank_key"], [])
                for u in (row.get("known_rate_urls") or []):
                    if u not in lst:
                        lst.append(u)
                last_good = row.get("last_successful_url")
                if last_good:
                    if last_good in lst:
                        lst.remove(last_good)
                    lst.insert(0, last_good)
                health[row["bank_key"]] = {
                    "consecutive_failures": row.get("consecutive_failures") or 0,
                    "needs_review": bool(row.get("needs_review")),
                    "zip_gate_zip": row.get("zip_gate_zip"),
                }
            _log(f"  ✓ Loaded bank/URL config: {len(generic)} generic paths, {len(bank_urls)} banks with known URLs, "
                 f"{sum(1 for h in health.values() if h['needs_review'])} flagged needs_review")
        except Exception as e:
            _log(f"  Config load warning: {type(e).__name__}: {e} (using built-in defaults)")
        return generic, bank_urls, health

    async def fetch_last_known_good(self, bank_name, http_session):
        def op(cur):
            cur.execute("""
                select * from raw.raw_rate_radar
                where bank_name = %s and (checking_apy is not null or savings_apy is not null
                                          or cd_apy is not null or money_market_apy is not null)
                order by run_date desc nulls last, crawled_at desc nulls last limit 1""", (bank_name,))
            return cur.fetchone()
        try:
            row = await self._arun(op)
            return {k: _plain(v) for k, v in row.items()} if row else None
        except Exception as e:
            _log(f"    [History] lookup failed for {bank_name}: {e}")
            return None

    async def _extraction_cache_get(self, content_hash):
        def op(cur):
            cur.execute("select extracted_json from public.extraction_cache where content_hash = %s", (content_hash,))
            return cur.fetchone()
        try:
            row = await self._arun(op)
            return row["extracted_json"] if row else None
        except Exception:
            return None

    async def _extraction_cache_put(self, content_hash, bank_name, page_url, extracted, model_used):
        """Best effort, insert-only: identical content hash means an identical answer, so a duplicate is skipped."""
        def op(cur):
            cur.execute("insert into public.extraction_cache (content_hash, bank_name, page_url, extracted_json, model_used) "
                        "values (%s, %s, %s, %s, %s) on conflict do nothing",
                        (content_hash, bank_name, page_url, Json(extracted), model_used))
        try:
            await self._arun(op)
        except Exception as e:
            _log(f"    [Cache] write skipped for {bank_name}: {type(e).__name__}")

    async def update_bank_registry(self, bank, result):
        """Learning loop, UPDATE-only. Same decision logic as storage.update_bank_registry; returns the new
        consecutive_failures count when the crawl found no verified URL, else None."""
        from extraction import extract_domain
        from storage import _provenance

        raw_url = (bank.get("bank_url") or "").strip()
        if not raw_url:
            return None
        bank_key = extract_domain(raw_url if raw_url.startswith("http") else "https://" + raw_url).lower()
        if not bank_key:
            return None

        def _clean_url(u):
            if not u:
                return None
            u = u.strip()
            for prefix in ("[AI] ", "[Search] "):
                if u.startswith(prefix):
                    u = u[len(prefix):].strip()
            return u if u.startswith("http") else None

        note = result.get("note")
        general_url = result.get("source_url")
        verified_url = None
        for product, specific in [("checking", result.get("source_url_checking")),
                                   ("savings",  result.get("source_url_savings")),
                                   ("cd",       result.get("source_url_cd"))]:
            if result.get(f"{product}_apy") is None:
                continue
            if _provenance(specific, general_url, note, product) in ("site", "ai_vision"):
                cleaned = _clean_url(specific) or _clean_url(general_url)
                if cleaned:
                    verified_url = cleaned
                    break

        def op(cur):
            cur.execute("select known_rate_urls, consecutive_failures from public.bank_registry where bank_key = %s", (bank_key,))
            prior = cur.fetchone()
            if prior is None:
                return "missing", None
            sets, params = ["updated_at = now()"], []
            zip_gate_zip = result.get("_zip_gate_zip")
            if zip_gate_zip:
                sets.append("zip_gate_zip = %s")
                params.append(zip_gate_zip)
            failures = None
            if verified_url:
                known = set(prior["known_rate_urls"] or [])
                known.add(verified_url)
                sets += ["last_successful_url = %s", "known_rate_urls = %s", "consecutive_failures = 0", "needs_review = false"]
                params += [verified_url, sorted(known)]
            else:
                failures = (prior["consecutive_failures"] or 0) + 1
                sets += ["consecutive_failures = %s", "needs_review = %s"]
                params += [failures, failures >= CIRCUIT_BREAKER_THRESHOLD]
            cur.execute(f"update public.bank_registry set {', '.join(sets)} where bank_key = %s", params + [bank_key])
            return ("ok" if cur.rowcount == 1 else "blocked"), failures

        try:
            state, failures = await self._arun(op)
        except Exception as e:
            _log(f"bank_registry write-back error: {type(e).__name__}: {e}")
            return None
        if state != "ok":
            _log(f"bank_registry write-back skipped for {bank_key}: "
                 + ("no registry row (watchlist banks must exist in bank_registry)" if state == "missing"
                    else "update matched no row (bank not on the watchlist?)"))
            return None
        return failures

    async def push_run_summary(self, run_id, run_date, started_at, banks_found, banks_partial, banks_error,
                               ai_calls, chat_calls, llm_input_tokens=0, llm_output_tokens=0, llm_web_searches=0,
                               notes=None):
        row = (run_id, run_date, started_at.isoformat(), datetime.now().isoformat(),
               banks_found + banks_partial + banks_error, banks_found, banks_partial, banks_error, ai_calls, chat_calls,
               llm_input_tokens, llm_output_tokens, llm_web_searches,
               _estimate_cost_usd(llm_input_tokens, llm_output_tokens, llm_web_searches), notes)

        def op(cur):
            cur.execute("insert into public.rate_radar_runs (run_id, run_date, started_at, finished_at, banks_attempted, "
                        "banks_found, banks_partial, banks_error, ai_vision_calls, chat_calls, llm_input_tokens, "
                        "llm_output_tokens, llm_web_searches, estimated_cost_usd, notes) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing", row)
            return cur.rowcount
        try:
            inserted = await self._arun(op)
        except Exception as e:
            _log(f"Run summary push error: {type(e).__name__}: {e}")
            return False
        _log(f"Postgres: ✓ logged run summary (run {run_id})" if inserted
             else f"Postgres: run {run_id} already logged, left unchanged")
        return bool(inserted)

    async def _insert_rows(self, table, rows, label):
        if not rows:
            return False
        columns = list(rows[0].keys())
        values = [tuple(r.get(c) for c in columns) for r in rows]

        def op(cur):
            execute_values(cur, sql.SQL("insert into {} ({}) values %s on conflict do nothing").format(
                table, sql.SQL(", ").join(map(sql.Identifier, columns))), values, page_size=len(values))   # one statement, so rowcount is the total
            return cur.rowcount
        try:
            inserted = await self._arun(op)
        except Exception as e:
            _log(f"{label} push failed: {type(e).__name__}: {e}")
            return False
        _log(f"Postgres: ✓ pushed {inserted}/{len(rows)} rows to {label}")
        return True

    async def push_to_supabase(self, results, run_id, run_date):
        """Legacy wide table raw.raw_rate_radar. Rows without an RSSD are dropped: the policy would reject the batch."""
        from storage import build_supabase_rows
        rows = build_supabase_rows(results, run_id, run_date)
        keep = [r for r in rows if r.get("rssdid")]
        if len(keep) != len(rows):
            _log(f"raw_rate_radar: dropped {len(rows) - len(keep)} row(s) with no RSSD")
        return await self._insert_rows(sql.Identifier("raw", "raw_rate_radar"), keep, "raw.raw_rate_radar")

    async def push_rate_observations(self, results, run_id):
        from storage import build_rate_observations
        rows = build_rate_observations(results, run_id)
        keep = [r for r in rows if r.get("rssdid")]
        if len(keep) != len(rows):
            _log(f"rate_observations: dropped {len(rows) - len(keep)} row(s) with no RSSD")
        return await self._insert_rows(sql.Identifier("public", "rate_observations"), keep, "rate_observations")

    # ── wiring ──────────────────────────────────────────────────────────────
    def hooks(self):
        return {name: getattr(self, name) for name in HOOK_NAMES}


def install(store, *modules):
    """Point every named storage function, in every module holding a copy, at the runner store. Returns the
    (module, name) pairs replaced so a caller can confirm none was missed."""
    hooks = store.hooks()
    replaced = []
    for mod in modules:
        for name, fn in hooks.items():
            if hasattr(mod, name):
                setattr(mod, name, fn)
                replaced.append((mod.__name__, name))
    return replaced
