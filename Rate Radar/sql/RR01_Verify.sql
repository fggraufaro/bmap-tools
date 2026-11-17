-- RR01 VERIFY. Run EACH query on its own in the Supabase SQL editor (select one, Run), after RR01_Apply.sql. All read-only. Send Session 3 every result.
-- Expected numbers are stated BEFORE each query (pre-state read-only on 2026-11-17: public.rate_observations 1,049 rows in 58 runs; the row count grows as runs are saved, so it is compared with Query 1's own earlier value).

-- Query 1: the two new columns on public.rate_observations. EXPECTED: columns 20, review_flags 'text[]' not null default '{}', tier_note 'text' nullable; rows_with_flags 0 and rows_with_tier_note 0 until the new collector runs (the existing rows were not touched).
select (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations') as columns,
       (select format_type(a.atttypid, a.atttypmod) || ' not_null=' || a.attnotnull from pg_attribute a where a.attrelid = 'public.rate_observations'::regclass and a.attname = 'review_flags') as review_flags,
       (select format_type(a.atttypid, a.atttypmod) || ' not_null=' || a.attnotnull from pg_attribute a where a.attrelid = 'public.rate_observations'::regclass and a.attname = 'tier_note') as tier_note,
       (select count(*) from public.rate_observations where cardinality(review_flags) > 0) as rows_with_flags, (select count(*) from public.rate_observations where tier_note is not null) as rows_with_tier_note,
       (select count(*) from public.rate_observations) as rows;

-- Query 2: existing rows are unchanged. EXPECTED: constraints still 3 check + 1 primary key + 1 foreign key (checks on confidence, extraction_method, product_type), indexes 3 (pkey, bank, run).
select (select count(*) from pg_constraint c where c.conrelid = 'public.rate_observations'::regclass and c.contype = 'c') as check_constraints,
       (select count(*) from pg_constraint c where c.conrelid = 'public.rate_observations'::regclass and c.contype = 'p') as primary_keys,
       (select count(*) from pg_constraint c where c.conrelid = 'public.rate_observations'::regclass and c.contype = 'f') as foreign_keys,
       (select count(*) from pg_indexes where schemaname = 'public' and tablename = 'rate_observations') as indexes;

-- Query 3: public.rate_not_published. EXPECTED: columns 8, row_security_on true, anon_or_auth_any false, service_role_select true, service_role_insert true, service_role_update true, service_role_delete true, service_role_truncate false,
-- check_constraints 1, unique_constraints 1, foreign_keys 1, rows 0.
select (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'rate_not_published') as columns, c.relrowsecurity as row_security_on,
       (has_table_privilege('anon', c.oid, 'select') or has_table_privilege('anon', c.oid, 'insert') or has_table_privilege('anon', c.oid, 'update') or has_table_privilege('anon', c.oid, 'delete')
        or has_table_privilege('authenticated', c.oid, 'select') or has_table_privilege('authenticated', c.oid, 'insert') or has_table_privilege('authenticated', c.oid, 'update') or has_table_privilege('authenticated', c.oid, 'delete')) as anon_or_auth_any,
       has_table_privilege('service_role', c.oid, 'select') as service_role_select, has_table_privilege('service_role', c.oid, 'insert') as service_role_insert,
       has_table_privilege('service_role', c.oid, 'update') as service_role_update, has_table_privilege('service_role', c.oid, 'delete') as service_role_delete, has_table_privilege('service_role', c.oid, 'truncate') as service_role_truncate,
       (select count(*) from pg_constraint k where k.conrelid = c.oid and k.contype = 'c') as check_constraints, (select count(*) from pg_constraint k where k.conrelid = c.oid and k.contype = 'u') as unique_constraints,
       (select count(*) from pg_constraint k where k.conrelid = c.oid and k.contype = 'f') as foreign_keys, (select count(*) from public.rate_not_published) as rows
from pg_class c where c.oid = 'public.rate_not_published'::regclass;

-- Query 4: the product check accepts the six values. EXPECTED: one row with the check definition listing checking, savings, high_yield_savings, money_market, cd, all.
select pg_get_constraintdef(k.oid) as product_check from pg_constraint k where k.conrelid = 'public.rate_not_published'::regclass and k.contype = 'c';

-- Query 5: nothing else changed. EXPECTED: no policy on either table (0), the two tables and their foreign key are the only objects added.
select (select count(*) from pg_policies where schemaname = 'public' and tablename in ('rate_observations', 'rate_not_published')) as policies,
       (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relname like 'rate\_%' and c.relkind = 'r') as rate_tables;
