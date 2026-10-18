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
  stray int;
begin
  -- the runner holds no privilege on any relation other than the seven intended ones
  select count(*) into stray
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where c.relkind in ('r','v','m','p','f','S')
    and n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
    and n.nspname not like 'pg\_temp\_%' and n.nspname not like 'pg\_toast\_temp\_%'    -- other sessions' temp tables
    and (n.nspname, c.relname) not in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                                       ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'),
                                       ('public','rate_radar_watchlist'))
    and (case when c.relkind = 'S'
              then has_sequence_privilege('rate_radar_runner', c.oid, 'USAGE,SELECT,UPDATE')
              else has_table_privilege('rate_radar_runner', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
         end);
  if stray <> 0 then
    raise exception 'a132: rate_radar_runner holds a privilege on % relation(s) outside the intended set', stray;
  end if;

  if exists (select 1 from pg_roles where rolname = 'rate_radar_runner' and (rolsuper or rolbypassrls or rolcreatedb or rolcreaterole or rolinherit)) then
    raise exception 'a132: rate_radar_runner has a role attribute it must not have';
  end if;

  if has_table_privilege('anon', 'public.rate_radar_watchlist', 'SELECT,INSERT,UPDATE,DELETE')
     or has_table_privilege('authenticated', 'public.rate_radar_watchlist', 'SELECT,INSERT,UPDATE,DELETE') then
    raise exception 'a132: an API role can reach rate_radar_watchlist';
  end if;
end $$;
