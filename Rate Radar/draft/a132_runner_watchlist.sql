-- a132 DRAFT: Rate Radar weekly runner: watchlist table, dedicated login role, scoped grants and policies.
--
-- DO NOT APPLY without Session 4's sign-off AND Francisco's yes (planned Wed 21 Oct, together with the
-- a40 deploy). Built and tested only on a local scratch PostgreSQL; nothing here has touched production.
--
-- What it does (everything is additive):
--   0. Saves the current ACL and RLS state of the six Rate Radar tables to backup.a132_acl_before.
--   1. Creates public.rate_radar_watchlist (keyed on RSSD, RLS on, no grants to PUBLIC, anon or authenticated).
--   2. Creates the login role rate_radar_runner: no superuser, no BYPASSRLS, no DDL, no DELETE, no inheritance,
--      connection limit 3, short statement timeout. It has NO PASSWORD here: it cannot sign in until the
--      credential step in draft/a132_make_runner_credential.py is run by Francisco.
--   3. Grants the runner the minimum on the named objects only (column-level UPDATE where possible).
--   4. Adds explicit policies "TO rate_radar_runner" on every table it touches. The six existing tables already
--      have RLS enabled with no policies (so API roles read nothing); that is unchanged.
--   5. Ends with an in-transaction assertion: if the runner holds any privilege beyond the intended set, or the
--      watchlist is reachable by an API role, the whole migration fails and rolls back.
--
-- Role name and the Supabase pooler: the role is created under its plain name, rate_radar_runner. The pooler (Supavisor)
-- needs the project reference in the USER NAME it is given, rate_radar_runner.<project-ref>; that suffix only routes the
-- connection and never exists in Postgres (current_user is rate_radar_runner, which is what the runner checks).
-- Use the session-mode pooler, port 5432 (see RAILWAY_CRON_WEEKLY.md).
--
-- Rollback: draft/a132_runner_watchlist_rollback.sql.

-- ---------------------------------------------------------------- 0. save current state
create schema if not exists backup;

create table backup.a132_acl_before as
select now() as saved_at, n.nspname as schema_name, c.relname as table_name, c.relkind::text as kind,
       c.relrowsecurity as rls_enabled, c.relforcerowsecurity as rls_forced, c.relacl::text as acl
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where (n.nspname, c.relname) in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                                  ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'));
alter table backup.a132_acl_before enable row level security;
revoke all on backup.a132_acl_before from public, anon, authenticated;

-- ---------------------------------------------------------------- 1. watchlist
create table public.rate_radar_watchlist (
  rssdid        text primary key,                                   -- identity key (names collide: three "First Bank")
  bank_key      text not null references public.bank_registry (bank_key),
  bank_name     text not null,
  bank_url      text,
  active        boolean not null default true,                      -- the runner crawls only active rows
  manual_only   boolean not null default false,                     -- blocked or large banks: skip the crawl, report "manual verification due"
  manual_reason text,
  added_by      text not null,
  added_at      timestamptz not null default now(),
  notes         text,
  last_run_id   text,                                               -- written by the runner after each run
  last_run_at   timestamptz,
  last_status   text,
  constraint rate_radar_watchlist_rssdid_digits check (rssdid ~ '^[0-9]+$'),
  constraint rate_radar_watchlist_manual_reason check (not manual_only or length(coalesce(manual_reason, '')) > 0)
);
alter table public.rate_radar_watchlist enable row level security;
revoke all on public.rate_radar_watchlist from public, anon, authenticated;

-- ---------------------------------------------------------------- 2. the role (no password here)
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'rate_radar_runner') then
    create role rate_radar_runner login nosuperuser nocreatedb nocreaterole noinherit nobypassrls connection limit 3;
  end if;
end $$;
alter role rate_radar_runner set statement_timeout = '30s';
alter role rate_radar_runner set idle_in_transaction_session_timeout = '30s';
alter role rate_radar_runner set lock_timeout = '5s';

-- ---------------------------------------------------------------- 3. grants (the minimum)
grant usage on schema public to rate_radar_runner;
grant usage on schema raw    to rate_radar_runner;

-- watchlist: read everything; update ONLY the three last_* columns; never insert or delete
grant select on public.rate_radar_watchlist to rate_radar_runner;
grant update (last_run_id, last_run_at, last_status) on public.rate_radar_watchlist to rate_radar_runner;

-- bank_registry: read; update ONLY the learning and counter columns (never name, url, rssdid)
grant select on public.bank_registry to rate_radar_runner;
grant update (known_rate_urls, last_successful_url, needs_review, consecutive_failures, zip_gate_zip, updated_at)
  on public.bank_registry to rate_radar_runner;

-- url_pattern_registry: read only (the storage code only reads it)
grant select on public.url_pattern_registry to rate_radar_runner;

-- extraction_cache: read and insert (content-keyed, never updated)
grant select, insert on public.extraction_cache to rate_radar_runner;

-- run log and results: insert only; the runner never reads them back
grant insert on public.rate_radar_runs   to rate_radar_runner;
grant insert on public.rate_observations to rate_radar_runner;
grant select, insert on raw.raw_rate_radar to rate_radar_runner;      -- select: last-known-good lookup for watchlist banks

-- ---------------------------------------------------------------- 4. policies (explicit, TO the runner only)
create policy a132_runner_read on public.rate_radar_watchlist for select to rate_radar_runner using (true);
create policy a132_runner_update on public.rate_radar_watchlist for update to rate_radar_runner using (true) with check (true);

create policy a132_runner_read on public.bank_registry for select to rate_radar_runner
  using (bank_key in (select w.bank_key from public.rate_radar_watchlist w));
create policy a132_runner_update on public.bank_registry for update to rate_radar_runner
  using (bank_key in (select w.bank_key from public.rate_radar_watchlist w))
  with check (bank_key in (select w.bank_key from public.rate_radar_watchlist w));

create policy a132_runner_read on public.url_pattern_registry for select to rate_radar_runner using (active);

create policy a132_runner_read on public.extraction_cache for select to rate_radar_runner
  using (bank_name in (select w.bank_name from public.rate_radar_watchlist w));
create policy a132_runner_insert on public.extraction_cache for insert to rate_radar_runner
  with check (bank_name in (select w.bank_name from public.rate_radar_watchlist w));

create policy a132_runner_insert on public.rate_radar_runs for insert to rate_radar_runner with check (true);

create policy a132_runner_insert on public.rate_observations for insert to rate_radar_runner
  with check (rssdid in (select w.rssdid from public.rate_radar_watchlist w));

create policy a132_runner_read on raw.raw_rate_radar for select to rate_radar_runner
  using (rssdid in (select w.rssdid from public.rate_radar_watchlist w));
create policy a132_runner_insert on raw.raw_rate_radar for insert to rate_radar_runner
  with check (rssdid in (select w.rssdid from public.rate_radar_watchlist w));

-- ---------------------------------------------------------------- 5. assertion: fail and roll back if anything is wider than intended
do $$
declare
  stray   int;
  runner  oid := (select oid from pg_roles where rolname = 'rate_radar_runner');
begin
  -- Privileges are read from the ACL entries, NOT with has_table_privilege(): that function is also true for a
  -- privilege held only through PUBLIC, and production has relations granted to PUBLIC that the runner cannot reach.

  -- (a) no ACL entry names the runner on any relation or column outside the seven intended tables
  select count(*) into stray from (
    select c.oid, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
      cross join lateral aclexplode(c.relacl) a
    where a.grantee = runner
      and c.relkind in ('r','v','m','p','f','S')
      and (n.nspname, c.relname) not in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                                         ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'),
                                         ('public','rate_radar_watchlist'))
    union all
    select c.oid, c.relname from pg_attribute at join pg_class c on c.oid = at.attrelid join pg_namespace n on n.oid = c.relnamespace
      cross join lateral aclexplode(at.attacl) a
    where a.grantee = runner
      and (n.nspname, c.relname) not in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                                         ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'),
                                         ('public','rate_radar_watchlist'))
  ) x;
  if stray <> 0 then
    raise exception 'a132: rate_radar_runner is granted a privilege on % relation/column(s) outside the intended set', stray;
  end if;

  -- (b) a relation granted to PUBLIC counts only where the runner can reach it: it has USAGE on the schema. Today that is
  -- public and raw. In public, production has three read-only PostGIS objects readable by anyone with schema usage
  -- (owned by supabase_admin); they are allowed, SELECT only. The cron and extensions relations granted to PUBLIC are
  -- not reachable because the runner has no USAGE on those schemas. Anything else granted to PUBLIC aborts the migration.
  select count(*) into stray
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
    cross join lateral aclexplode(c.relacl) a
  where a.grantee = 0
    and c.relkind in ('r','v','m','p','f','S')
    and n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
    and n.nspname not like 'pg\_temp\_%' and n.nspname not like 'pg\_toast\_temp\_%'    -- other sessions' temp tables
    and has_schema_privilege('rate_radar_runner', n.oid, 'USAGE')
    and not ((n.nspname, c.relname) in (('public','spatial_ref_sys'), ('public','geography_columns'), ('public','geometry_columns'))
             and a.privilege_type = 'SELECT');
  if stray <> 0 then
    raise exception 'a132: % PUBLIC grant(s) on relations in schemas the runner can use (outside the three read-only PostGIS objects)', stray;
  end if;

  -- (c) the runner can create objects in no schema
  if exists (select 1 from pg_namespace n where n.nspname not like 'pg\_%' and n.nspname <> 'information_schema'
               and has_schema_privilege('rate_radar_runner', n.oid, 'CREATE')) then
    raise exception 'a132: rate_radar_runner can create objects in a schema';
  end if;

  if exists (select 1 from pg_roles where rolname = 'rate_radar_runner' and (rolsuper or rolbypassrls or rolcreatedb or rolcreaterole or rolinherit)) then
    raise exception 'a132: rate_radar_runner has a role attribute it must not have';
  end if;

  if has_table_privilege('anon', 'public.rate_radar_watchlist', 'SELECT,INSERT,UPDATE,DELETE')
     or has_table_privilege('authenticated', 'public.rate_radar_watchlist', 'SELECT,INSERT,UPDATE,DELETE') then
    raise exception 'a132: an API role can reach rate_radar_watchlist';
  end if;
end $$;
