-- RR01 APPLY (Rate Radar rate-integrity, branch fix/rate-radar-accuracy). DRAFT: awaiting Session 4 review. NOT APPLIED. Run this whole file once in the Supabase SQL editor
-- (project tuiiywphoynbmkxpoyps) only after review and Francisco's "run it". The Rate Radar code works with or without it: it checks that each object exists before writing to it,
-- so the collector can be deployed before or after this file. Additive only: no existing row, column, constraint, grant, policy or view changes.
--
-- WHAT IT DOES (one transaction; if anything fails, nothing is kept):
--   1. public.rate_observations gets two columns:
--        review_flags text[] not null default '{}'   why a person should look at the value: tiered, not_in_page_text, identity_mismatch, off_domain, search_only, source_unknown, last_known_good
--        tier_note    text (nullable)                for a tiered product: the tier reported (by the one tier-rule setting) and the other tiers
--      The default fills the existing rows with an empty array; their values are not touched.
--   2. A NEW table public.rate_not_published: the "the bank does not publish this rate" outcome, one row per run, bank and product (product_type may be 'all'), with the evidence
--      sentence and the page it was on. It is a separate table because public.rate_observations.apy is NOT NULL (a bank that says "inquire for rates" has no number to store) and
--      because the outcome is per product, not per rate. Row security ON with no policy; service_role only (select, insert, update, delete); nothing for anon or authenticated.
--
-- PRE-STATE (read-only, 2026-11-17): public.rate_observations has 18 columns, RLS on, no policies, CHECKs on confidence / extraction_method / product_type, a foreign key run_id -> rate_radar_runs(run_id);
-- public.rate_not_published does not exist. The way back is RR01_Rollback.sql.
begin;

do $$
begin
  if to_regclass('public.rate_observations') is null or to_regclass('public.rate_radar_runs') is null then
    raise exception 'rr01: public.rate_observations or public.rate_radar_runs does not exist: stop and tell Session 3';
  end if;
  if exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations' and column_name in ('review_flags', 'tier_note')) then
    raise exception 'rr01 is already applied (review_flags or tier_note exists): stop and tell Session 3';
  end if;
  if to_regclass('public.rate_not_published') is not null then
    raise exception 'rr01 is already applied (public.rate_not_published exists): stop and tell Session 3';
  end if;
  if (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations') <> 18 then
    raise exception 'rr01: public.rate_observations does not have the 18 columns this was written against: stop and tell Session 3';
  end if;
end $$;

-- 1. the two columns
alter table public.rate_observations add column review_flags text[] not null default '{}';
alter table public.rate_observations add column tier_note text;
comment on column public.rate_observations.review_flags is 'Why a person should look at this value: tiered, not_in_page_text, identity_mismatch, off_domain, search_only, source_unknown, last_known_good (set by Rate Radar).';
comment on column public.rate_observations.tier_note is 'For a tiered product: the tier reported (by the tier-rule setting: the rate at the opening minimum, or the highest advertised rate) and the other tiers.';

-- 2. the "not published by the bank" outcome
create table public.rate_not_published (
  id bigint generated always as identity primary key,
  run_id text not null references public.rate_radar_runs (run_id),
  bank_name text not null,
  rssdid text,
  product_type text not null check (product_type in ('checking', 'savings', 'high_yield_savings', 'money_market', 'cd', 'all')),
  evidence text,                                   -- the sentence on the bank's own page ("Interest Bearing: Inquire for Rates")
  source_url text,                                 -- the page it was on
  observed_at timestamptz not null default now(),
  unique (run_id, bank_name, product_type)
);
create index idx_rate_not_published_bank on public.rate_not_published (bank_name, observed_at desc);
alter table public.rate_not_published enable row level security;
revoke all on public.rate_not_published from public, anon, authenticated, service_role;
grant select, insert, update, delete on public.rate_not_published to service_role;
comment on table public.rate_not_published is 'Rate Radar: products a bank''s own pages say it does not publish a rate for (inquire / call for rates), one row per run, bank and product (product_type all = every product). No search value is stored in its place.';

do $$
begin
  if (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations') <> 20 then
    raise exception 'rr01: public.rate_observations does not have 20 columns after the change: nothing is kept';
  end if;
  if not (select c.relrowsecurity from pg_class c where c.oid = 'public.rate_not_published'::regclass) then
    raise exception 'rr01: row security is not on for public.rate_not_published: nothing is kept';
  end if;
  if has_table_privilege('anon', 'public.rate_not_published', 'select') or has_table_privilege('authenticated', 'public.rate_not_published', 'select') then
    raise exception 'rr01: anon or authenticated can read public.rate_not_published: nothing is kept';
  end if;
end $$;

commit;
