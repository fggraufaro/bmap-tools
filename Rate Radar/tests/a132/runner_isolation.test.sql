-- pgTAP: a132 Rate Radar runner role, watchlist and policies.
-- Run (after draft/a132_runner_watchlist.sql is applied): pg_prove -d <db> "Rate Radar/tests/a132/runner_isolation.test.sql"
-- Locally: python "Rate Radar/tests/a132/run_local_pg.py" (scratch PostgreSQL, pgTAP shim, mutation checks).
-- Everything runs inside begin/rollback on synthetic data.
begin;
create extension if not exists pgtap;
select plan(59);

-- ---------- fixtures (as owner) ----------
insert into public.bank_registry (bank_key, bank_name, bank_url, rssdid) values
  ('bank-a.example', 'Bank A', 'https://bank-a.example', '1000001'),
  ('bank-b.example', 'Bank B', 'https://bank-b.example', '1000002'),
  ('bank-c.example', 'Bank C', 'https://bank-c.example', '1000003');
insert into public.rate_radar_watchlist (rssdid, bank_key, bank_name, bank_url, added_by) values
  ('1000001', 'bank-a.example', 'Bank A', 'https://bank-a.example', 'approver_1'),
  ('1000002', 'bank-b.example', 'Bank B', 'https://bank-b.example', 'approver_1');
insert into public.url_pattern_registry (pattern, category, bank_key, active) values
  ('/rates', 'generic', null, true), ('/old', 'generic', null, false), ('/a-rates', 'bank_specific', 'bank-a.example', true);
insert into public.extraction_cache (content_hash, bank_name, page_url, extracted_json) values
  ('h-a', 'Bank A', 'u', '{}'), ('h-c', 'Bank C', 'u', '{}');
insert into raw.raw_rate_radar (run_id, bank_name, rssdid, status) values
  ('r0', 'Bank A', '1000001', 'Found'), ('r0', 'Bank C', '1000003', 'Found');
-- pre-RSSD history rows (rssdid NULL): Bank A's name is flagged unique, Bank B's is not, Bank C is not on the watchlist
insert into raw.raw_rate_radar (run_id, bank_name, rssdid, status) values
  ('r0n', 'Bank A', null, 'Found'), ('r0n', 'Bank B', null, 'Found'), ('r0n', 'Bank C', null, 'Found');
update public.rate_radar_watchlist set name_unique_in_registry = true where rssdid = '1000001';
insert into public.canary_other values (1, 'x');
insert into analytics.canary_snapshot values (1, 'x');

-- Supabase (PostgreSQL 16+): the migration role may not be allowed to SET ROLE to the runner, and the runner
-- must see the schema that holds pgTAP. Both are granted inside this transaction only and roll back with it.
do $$ begin
  begin execute 'grant rate_radar_runner to current_user'; exception when others then null; end;
  if exists (select 1 from pg_extension where extname = 'pgtap') then
    execute format('grant usage on schema %I to rate_radar_runner',
                   (select n.nspname from pg_extension e join pg_namespace n on n.oid = e.extnamespace where e.extname = 'pgtap'));
  end if;
end $$;

-- ---------- A: closed to the API roles ----------
set local role anon;
select throws_ok($$select * from public.rate_radar_watchlist$$, '42501', null, 'A1 anon cannot read the watchlist');
select is((select count(*)::int from (select 1 from public.bank_registry union all select 1 from public.url_pattern_registry
           union all select 1 from public.extraction_cache union all select 1 from public.rate_radar_runs
           union all select 1 from public.rate_observations union all select 1 from raw.raw_rate_radar) t), 0,
          'A2 anon still reads nothing from the six Rate Radar tables (RLS, no policy)');
reset role;
set local role authenticated;
select throws_ok($$select * from public.rate_radar_watchlist$$, '42501', null, 'A3 authenticated cannot read the watchlist');
select is((select count(*)::int from (select 1 from public.bank_registry union all select 1 from public.url_pattern_registry
           union all select 1 from public.extraction_cache union all select 1 from public.rate_radar_runs
           union all select 1 from public.rate_observations union all select 1 from raw.raw_rate_radar) t), 0,
          'A4 authenticated still reads nothing from the six Rate Radar tables (RLS, no policy)');
reset role;
select is((select count(*)::int from pg_class c, aclexplode(c.relacl) a
           where c.oid = 'public.rate_radar_watchlist'::regclass
             and (a.grantee = 0 or a.grantee in (select oid from pg_roles where rolname in ('anon', 'authenticated')))), 0,
          'A5 PUBLIC, anon and authenticated hold no grant on the watchlist');
select is((select count(*)::int from pg_policies where schemaname = 'public' and tablename = 'rate_radar_watchlist'
           and roles && array['anon', 'authenticated', 'public']::name[]), 0,
          'A6 no policy on the watchlist names an API role or PUBLIC');
select is((select count(*)::int from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where (n.nspname, c.relname) in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                 ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'), ('public','rate_radar_watchlist'))
             and not c.relrowsecurity), 0,
          'A7 RLS is enabled on all six Rate Radar tables and the watchlist');

-- ---------- B: the role ----------
select ok((select rolcanlogin and not rolsuper and not rolbypassrls and not rolcreatedb and not rolcreaterole and not rolinherit
                  and rolconnlimit between 1 and 5 from pg_roles where rolname = 'rate_radar_runner'),
          'B1 runner can log in, is not superuser, has no BYPASSRLS, no DDL role attributes, no inheritance, a connection limit');
select is((select count(*)::int from pg_auth_members m where m.member = 'rate_radar_runner'::regrole), 0,
          'B2 runner is a member of no other role');
select ok(exists (select 1 from pg_db_role_setting s join pg_roles r on r.oid = s.setrole
                  where r.rolname = 'rate_radar_runner' and 'statement_timeout=30s' = any (s.setconfig)),
          'B3 runner has a statement timeout');

-- ---------- C: nothing outside the intended set ----------
-- Privileges are read from ACL entries naming the runner. has_table_privilege() would also count privileges that
-- come only through PUBLIC (production has some, on relations the runner cannot reach); those are checked in C8.
select is((select count(*)::int from (
             select c.oid from pg_class c join pg_namespace n on n.oid = c.relnamespace cross join lateral aclexplode(c.relacl) a
              where a.grantee = 'rate_radar_runner'::regrole::oid and c.relkind in ('r', 'v', 'm', 'p', 'f')
                and (n.nspname, c.relname) not in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                   ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'), ('public','rate_radar_watchlist'))
             union all
             select c.oid from pg_attribute at join pg_class c on c.oid = at.attrelid join pg_namespace n on n.oid = c.relnamespace
               cross join lateral aclexplode(at.attacl) a
              where a.grantee = 'rate_radar_runner'::regrole::oid
                and (n.nspname, c.relname) not in (('raw','raw_rate_radar'), ('public','rate_observations'), ('public','rate_radar_runs'),
                   ('public','extraction_cache'), ('public','url_pattern_registry'), ('public','bank_registry'), ('public','rate_radar_watchlist'))) x), 0,
          'C1 no table, view or column outside the seven intended is granted to the runner');
select is((select count(*)::int from pg_class c cross join lateral aclexplode(c.relacl) a
           where c.relkind = 'S' and a.grantee = 'rate_radar_runner'::regrole::oid), 0,
          'C2 no sequence is granted to the runner');
select is((select count(*)::int from pg_namespace n
           where n.nspname = any (array['analytics', 'backup', 'geo', 'ref', 'app', 'tenant', 'auth', 'storage', 'vault', 'realtime', 'cron', 'extensions'])
             and has_schema_privilege('rate_radar_runner', n.oid, 'USAGE')), 0,
          'C3 runner has no USAGE on the analytics, backup, geo, ref, auth, storage, vault, realtime, cron or extensions schemas');
select is((select count(*)::int from pg_namespace n
           where n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast') and n.nspname not like 'pg\_temp\_%' and n.nspname not like 'pg\_toast\_temp\_%'
             and has_schema_privilege('rate_radar_runner', n.oid, 'CREATE')), 0,
          'C4 runner can create objects in no schema');
select is((select count(*)::int from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where p.prosecdef and p.proname <> 'st_estimatedextent' and n.nspname not in ('pg_catalog', 'information_schema')
             and has_function_privilege('rate_radar_runner', p.oid, 'EXECUTE')), 0,
          'C5 runner can execute no SECURITY DEFINER function (the PostGIS extent estimator is the known exception)');
set local role rate_radar_runner;
select throws_ok($$select * from public.canary_other$$, '42501', null, 'C6 runner cannot read another public table');
select throws_ok($$select * from analytics.canary_snapshot$$, '42501', null, 'C7 runner cannot read another schema');
reset role;
select is((select count(*)::int from pg_class c join pg_namespace n on n.oid = c.relnamespace cross join lateral aclexplode(c.relacl) a
           where a.grantee = 0 and c.relkind in ('r', 'v', 'm', 'p', 'f', 'S')
             and n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
             and n.nspname not like 'pg\_temp\_%' and n.nspname not like 'pg\_toast\_temp\_%'
             and has_schema_privilege('rate_radar_runner', n.oid, 'USAGE')
             and not ((n.nspname, c.relname) in (('public','spatial_ref_sys'), ('public','geography_columns'), ('public','geometry_columns'))
                      and a.privilege_type = 'SELECT')), 0,
          'C8 no PUBLIC grant on a relation in a schema the runner can use, except the three read-only PostGIS objects');

-- ---------- D: exact privileges ----------
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.rate_radar_watchlist', p)), array['SELECT']::text[],
          'D1 watchlist: runner may only SELECT at table level');
select is((select coalesce(array_agg(c.attname order by c.attname), '{}'::name[]) from pg_attribute c
           where c.attrelid = 'public.rate_radar_watchlist'::regclass and c.attnum > 0 and not c.attisdropped
             and has_column_privilege('rate_radar_runner', 'public.rate_radar_watchlist', c.attname, 'UPDATE')),
          array['last_run_at', 'last_run_id', 'last_status']::name[], 'D2 watchlist: runner may update only the last_* columns');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.bank_registry', p)), array['SELECT']::text[],
          'D3 bank_registry: runner may only SELECT at table level');
select is((select coalesce(array_agg(c.attname order by c.attname), '{}'::name[]) from pg_attribute c
           where c.attrelid = 'public.bank_registry'::regclass and c.attnum > 0 and not c.attisdropped
             and has_column_privilege('rate_radar_runner', 'public.bank_registry', c.attname, 'UPDATE')),
          array['consecutive_failures', 'known_rate_urls', 'last_successful_url', 'needs_review', 'updated_at', 'zip_gate_zip']::name[],
          'D4 bank_registry: runner may update only the learning and counter columns');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.url_pattern_registry', p)), array['SELECT']::text[],
          'D5 url_pattern_registry: SELECT only');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.extraction_cache', p)), array['INSERT', 'SELECT']::text[],
          'D6 extraction_cache: SELECT and INSERT only');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.rate_radar_runs', p)), array['INSERT']::text[],
          'D7 rate_radar_runs: INSERT only');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'public.rate_observations', p)), array['INSERT']::text[],
          'D8 rate_observations: INSERT only');
select is((select coalesce(array_agg(p order by p), '{}'::text[]) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
           where has_table_privilege('rate_radar_runner', 'raw.raw_rate_radar', p)), array['INSERT', 'SELECT']::text[],
          'D9 raw.raw_rate_radar: SELECT and INSERT only');

-- ---------- E: behaviour as the runner ----------
set local role rate_radar_runner;

-- watchlist
select is((select count(*)::int from public.rate_radar_watchlist), 2, 'E1 runner reads both watchlist rows');
do $$ declare n int; begin
  update public.rate_radar_watchlist set last_run_id = 'r1', last_run_at = now(), last_status = 'ok' where rssdid = '1000001';
  get diagnostics n = row_count; perform set_config('t.rc', n::text, true);
end $$;
select is(current_setting('t.rc')::int, 1, 'E2 runner can update last_* on a watchlist row');
select throws_ok($$update public.rate_radar_watchlist set active = false$$, '42501', null, 'E3 runner cannot change the active flag');
select throws_ok($$insert into public.rate_radar_watchlist (rssdid, bank_key, bank_name, added_by) values ('1000003', 'bank-c.example', 'Bank C', 'x')$$,
                 '42501', null, 'E4 runner cannot add a bank to the watchlist');
select throws_ok($$delete from public.rate_radar_watchlist$$, '42501', null, 'E5 runner cannot delete watchlist rows');

-- bank_registry
select is((select count(*)::int from public.bank_registry), 2, 'E6 runner sees registry rows for watchlist banks only');
do $$ declare n int; begin
  update public.bank_registry set zip_gate_zip = '10001', updated_at = now() where bank_key = 'bank-a.example';
  get diagnostics n = row_count; perform set_config('t.rc', n::text, true);
end $$;
select is(current_setting('t.rc')::int, 1, 'E7 runner can update learning columns for a watchlist bank');
do $$ declare n int; begin
  update public.bank_registry set zip_gate_zip = '10001', updated_at = now() where bank_key = 'bank-c.example';
  get diagnostics n = row_count; perform set_config('t.rc', n::text, true);
end $$;
select is(current_setting('t.rc')::int, 0, 'E8 runner updates nothing for a bank that is not on the watchlist');
select throws_ok($$update public.bank_registry set bank_name = 'x' where bank_key = 'bank-a.example'$$, '42501', null, 'E9 runner cannot rename a bank');
select throws_ok($$insert into public.bank_registry (bank_key, bank_name) values ('bank-d.example', 'D')$$, '42501', null, 'E10 runner cannot add a registry row');
select throws_ok($$delete from public.bank_registry$$, '42501', null, 'E11 runner cannot delete registry rows');

-- url_pattern_registry
select is((select count(*)::int from public.url_pattern_registry), 2, 'E12 runner sees active URL patterns only');
select throws_ok($$insert into public.url_pattern_registry (pattern, category) values ('/x', 'generic')$$, '42501', null, 'E13 runner cannot add URL patterns');

-- extraction_cache
select is((select count(*)::int from public.extraction_cache), 1, 'E14 runner sees cache rows for watchlist banks only');
select lives_ok($$insert into public.extraction_cache (content_hash, bank_name, page_url, extracted_json, model_used)
                   values ('h-a2', 'Bank A', 'u', '{}', 'm') on conflict (content_hash) do nothing$$, 'E15 runner can cache an extraction for a watchlist bank');
select throws_ok($$insert into public.extraction_cache (content_hash, bank_name, page_url, extracted_json) values ('h-c2', 'Bank C', 'u', '{}')$$,
                 '42501', null, 'E16 runner cannot cache for a bank that is not on the watchlist');
select throws_ok($$update public.extraction_cache set model_used = 'x'$$, '42501', null, 'E17 runner cannot update the cache');

-- rate_radar_runs
select lives_ok($$insert into public.rate_radar_runs (run_id, run_date, started_at) values ('r1', current_date, now()) on conflict do nothing$$,
                'E18 runner can log a run (insert only: ON CONFLICT DO NOTHING needs no SELECT)');
select throws_ok($$select * from public.rate_radar_runs$$, '42501', null, 'E19 runner cannot read the run log');
select throws_ok($$update public.rate_radar_runs set notes = 'x'$$, '42501', null, 'E20 runner cannot update the run log');

-- rate_observations
select lives_ok($$insert into public.rate_observations (run_id, bank_name, rssdid, product_type, apy, extraction_method, confidence, observed_at)
                   values ('r1', 'Bank A', '1000001', 'savings', 4.1, 'site', 'high', now())$$, 'E21 runner can record an observation for a watchlist bank');
select throws_ok($$insert into public.rate_observations (run_id, bank_name, rssdid, product_type, apy, extraction_method, confidence, observed_at)
                   values ('r1', 'Bank C', '1000003', 'savings', 4.1, 'site', 'high', now())$$,
                 '42501', null, 'E22 runner cannot record an observation for a bank that is not on the watchlist');
select throws_ok($$insert into public.rate_observations (run_id, bank_name, rssdid, product_type, apy, extraction_method, confidence, observed_at)
                   values ('r1', 'Bank A', null, 'savings', 4.1, 'site', 'high', now())$$,
                 '42501', null, 'E23 runner cannot record an observation without a bank id');
select throws_ok($$select * from public.rate_observations$$, '42501', null, 'E24 runner cannot read observations back');

-- raw.raw_rate_radar
select is((select count(*)::int from raw.raw_rate_radar), 2, 'E25 runner sees raw rows for watchlist banks only (Bank A: one carrying its RSSD, one pre-RSSD row)');
select lives_ok($$insert into raw.raw_rate_radar (run_id, bank_name, rssdid, status) values ('r1', 'Bank A', '1000001', 'Found')
                   on conflict (run_id, bank_name) do nothing$$, 'E26 runner can write a raw row for a watchlist bank');
select throws_ok($$insert into raw.raw_rate_radar (run_id, bank_name, rssdid, status) values ('r1', 'Bank C', '1000003', 'Found')$$,
                 '42501', null, 'E27 runner cannot write a raw row for a bank that is not on the watchlist');
select throws_ok($$insert into raw.raw_rate_radar (run_id, bank_name, rssdid, status) values ('r1', 'Bank A', null, 'Found')$$,
                 '42501', null, 'E28 runner cannot write a raw row without a bank id');
select throws_ok($$update raw.raw_rate_radar set note = 'x'$$, '42501', null, 'E29 runner cannot update raw rows');
select throws_ok($$delete from raw.raw_rate_radar$$, '42501', null, 'E30 runner cannot delete raw rows');
select is((select count(*)::int from raw.raw_rate_radar where rssdid is null and bank_name = 'Bank A'), 1,
          'E31 runner reads pre-RSSD history for a watchlist bank whose name the owner flagged unique');
select is((select count(*)::int from raw.raw_rate_radar where rssdid is null and bank_name in ('Bank B', 'Bank C')), 0,
          'E32 runner reads no pre-RSSD history for an unflagged watchlist bank or a bank off the watchlist');

reset role;
select * from finish();
rollback;
