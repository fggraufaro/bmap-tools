#!/usr/bin/env python3
"""Run the a132 migration and tests on a THROWAWAY local PostgreSQL, plus mutation checks.

Nothing here touches Supabase or any production database: it starts its own cluster in a temp directory on a
high port, loads the local shim (roles and a few pgTAP functions) and a replica of the production Rate Radar
tables, applies draft/a132_runner_watchlist.sql, runs tests/a132/runner_isolation.test.sql, then repeats with each
control removed ("mutations") and requires the matching tests to fail. The cluster and temp directory are
removed at the end.

Usage: python "Rate Radar/tests/a132/run_local_pg.py" [--keep] [--pgbin DIR] [--port N]
Exit code 0 only if the baseline passes every test and every mutation fails the tests it should.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RR = HERE.parent.parent                       # "Rate Radar"
MIGRATION = RR / "draft" / "a132_runner_watchlist.sql"
ROLLBACK = RR / "draft" / "a132_runner_watchlist_rollback.sql"
TEST = HERE / "runner_isolation.test.sql"
SHIM = HERE / "local_000_shim.sql"
BASELINE = HERE / "local_001_baseline.sql"

# name, SQL applied after the migration, test labels that MUST fail
MUTATIONS = [
    ("grant DELETE on bank_registry to the runner",
     "grant delete on public.bank_registry to rate_radar_runner;", {"D3"}),
    ("drop the insert policy on raw_rate_radar",
     "drop policy a132_runner_insert on raw.raw_rate_radar;", {"E26"}),
    ("give the runner BYPASSRLS",
     "alter role rate_radar_runner bypassrls;", {"B1", "E6", "E8"}),
    ("grant SELECT on an unrelated table",
     "grant select on public.canary_other to rate_radar_runner;", {"C1", "C6"}),
    ("grant SELECT on the watchlist to anon",
     "grant select on public.rate_radar_watchlist to anon;", {"A1", "A5"}),
    ("disable RLS on the watchlist",
     "alter table public.rate_radar_watchlist disable row level security;", {"A7"}),
    ("widen column UPDATE on bank_registry to bank_name",
     "grant update (bank_name) on public.bank_registry to rate_radar_runner;", {"D4", "E9"}),
    ("unscope the bank_registry read policy",
     "drop policy a132_runner_read on public.bank_registry; "
     "create policy a132_runner_read on public.bank_registry for select to rate_radar_runner using (true);", {"E6"}),
    ("make the runner a member of service_role",
     "grant service_role to rate_radar_runner;", {"B2"}),
    ("grant USAGE on the analytics schema",
     "grant usage on schema analytics to rate_radar_runner;", {"C3"}),
    ("open the raw insert policy to any bank",
     "drop policy a132_runner_insert on raw.raw_rate_radar; "
     "create policy a132_runner_insert on raw.raw_rate_radar for insert to rate_radar_runner with check (true);", {"E27", "E28"}),
    ("grant UPDATE on rate_observations",
     "grant update on public.rate_observations to rate_radar_runner;", {"D8"}),
    ("add a SECURITY DEFINER function anyone can run",
     "create function public.leak() returns int language sql security definer as 'select 1';", {"C5"}),
    ("open the extraction_cache insert policy to any bank",
     "drop policy a132_runner_insert on public.extraction_cache; "
     "create policy a132_runner_insert on public.extraction_cache for insert to rate_radar_runner with check (true);", {"E16"}),
    ("allow CREATE on schema public",
     "grant create on schema public to rate_radar_runner;", {"C4"}),
    ("grant USAGE on an identity sequence",
     "grant usage on sequence public.rate_observations_id_seq to rate_radar_runner;", {"C2"}),
    ("grant full UPDATE on the watchlist",
     "grant update on public.rate_radar_watchlist to rate_radar_runner;", {"D1", "D2", "E3"}),
    ("grant SELECT on rate_radar_runs",
     "grant select on public.rate_radar_runs to rate_radar_runner;", {"D7", "E19"}),
]


def find_pgbin(arg):
    cands = [arg, os.environ.get("PGBIN")]
    which = shutil.which("psql")
    if which:
        cands.append(str(Path(which).parent))
    cands += [r"C:\Program Files\PostgreSQL\18\bin", r"C:\Program Files\PostgreSQL\17\bin", r"C:\Program Files\PostgreSQL\16\bin"]
    for c in cands:
        if c and (Path(c) / ("initdb.exe" if os.name == "nt" else "initdb")).exists():
            return Path(c)
    sys.exit("PostgreSQL binaries not found: pass --pgbin")


class Pg:
    def __init__(self, bindir, port, data):
        self.bin, self.port, self.data = bindir, port, data

    def exe(self, name):
        return str(self.bin / (name + (".exe" if os.name == "nt" else "")))

    def run(self, args, **kw):
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", **kw)

    def psql(self, db, sql=None, file=None, single_tx=False, check=True):
        cmd = [self.exe("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-d", db]
        if single_tx:
            cmd.append("-1")
        cmd += ["-c", sql] if sql is not None else ["-f", str(file)]
        r = self.run(cmd)
        if check and r.returncode != 0:
            raise RuntimeError(f"psql failed on {db}: {r.stderr.strip()[:600]}")
        return r


def tap_lines(out):
    return [ln for ln in out.splitlines() if re.match(r"^(ok |not ok |# |1\.\.)", ln)]


def run_tests(pg, db):
    text = TEST.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^create extension if not exists pgtap;\s*$", "", text)   # shim stands in for pgTAP locally
    tmp = Path(pg.data) / "test_filtered.sql"
    tmp.write_text(text, encoding="utf-8")
    r = pg.psql(db, file=tmp, check=False)
    return r, tap_lines(r.stdout)


def failed_labels(lines):
    labels = set()
    for ln in lines:
        m = re.match(r"^not ok \d+ - (\S+)", ln)
        if m:
            labels.add(m.group(1))
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--pgbin")
    ap.add_argument("--port", type=int, default=54339)
    args = ap.parse_args()

    bindir = find_pgbin(args.pgbin)
    work = Path(tempfile.mkdtemp(prefix="a132_pg_"))
    data = work / "data"
    pg = Pg(bindir, args.port, work)
    ok_all = True
    try:
        r = pg.run([pg.exe("initdb"), "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"])
        if r.returncode:
            sys.exit("initdb failed: " + r.stderr[:400])
        # the server inherits pg_ctl's stdio: capturing it through pipes would block forever, so use DEVNULL + a log file
        r = subprocess.run([pg.exe("pg_ctl"), "-D", str(data), "-o", f"-p {args.port} -c listen_addresses=127.0.0.1",
                            "-l", str(work / "pg.log"), "-w", "start"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode:
            sys.exit("pg_ctl start failed; see " + str(work / "pg.log"))
        version = pg.psql("postgres", sql="show server_version").stdout.strip()
        print(f"scratch PostgreSQL {version} on port {args.port} (temp dir removed at the end)")

        pg.psql("postgres", sql="create database tpl")
        pg.psql("tpl", file=SHIM)
        pg.psql("tpl", file=BASELINE)

        def drop_role():
            # roles are cluster-wide: a mutation such as BYPASSRLS or a service_role membership must not leak into the next scenario
            pg.psql("postgres", sql="drop role if exists rate_radar_runner", check=False)

        def scenario(name, mutation_sql=None):
            db = "scen_" + str(int(time.time() * 1000) % 10**9)
            drop_role()
            pg.psql("postgres", sql=f"create database {db} template tpl")
            try:
                pg.psql(db, file=MIGRATION, single_tx=True)
                if mutation_sql:
                    pg.psql(db, sql=mutation_sql)
                return run_tests(pg, db)
            finally:
                pg.psql("postgres", sql=f"drop database {db}", check=False)
                drop_role()

        print("\n== baseline: migration applied, no mutation ==")
        r, lines = scenario("baseline")
        for ln in lines:
            print(ln)
        base_fail = failed_labels(lines)
        n_ok = sum(1 for ln in lines if ln.startswith("ok "))
        print(f"-> {n_ok} ok, {len(base_fail)} failed")
        if base_fail or r.returncode != 0 or n_ok != 56:
            ok_all = False
            print("BASELINE DID NOT PASS", r.stderr.strip()[:400])

        print("\n== mutations: each control removed in turn; the tests must fail ==")
        for name, sql, expected in MUTATIONS:
            r, lines = scenario(name, sql)
            fails = failed_labels(lines)
            good = expected <= fails and r.returncode == 0
            ok_all &= good
            print(f"{'OK  ' if good else 'MISS'} {name}: failed {sorted(fails)}; required {sorted(expected)}")

        print("\n== rollback script on a migrated database ==")
        db = "scen_rb"
        drop_role()
        pg.psql("postgres", sql=f"create database {db} template tpl")
        pg.psql(db, file=MIGRATION, single_tx=True)
        pg.psql(db, file=ROLLBACK, single_tx=True)
        left = pg.psql(db, sql="select (select count(*) from pg_roles where rolname='rate_radar_runner') as role_left, "
                               "(select count(*) from pg_class where relname='rate_radar_watchlist') as table_left, "
                               "(select count(*) from pg_policies where policyname like 'a132_%') as policies_left").stdout.strip()
        pg.psql("postgres", sql=f"drop database {db}", check=False)
        print(f"role|table|policies left after rollback: {left}")
        if left != "0|0|0":
            ok_all = False
    finally:
        if not args.keep:
            pg.run([pg.exe("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"])
            shutil.rmtree(work, ignore_errors=True)

    print("\nRESULT:", "PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
