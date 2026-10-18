-- LOCAL TEST ONLY. Replica of the production Rate Radar objects (read from production on 2026-10-18, read-only)
-- so the a132 migration and tests can run on a scratch PostgreSQL. Never apply to Supabase.
-- Mirrors production: the six tables exist with RLS ENABLED and NO policies; anon and authenticated hold SELECT;
-- service_role holds everything; new tables in public receive Supabase's default grants.

create schema if not exists raw;
create schema if not exists analytics;
create schema if not exists backup;

grant usage on schema public, raw to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;

create table public.bank_registry (
  bank_key            text primary key,
  bank_name           text not null,
  bank_url            text,
  rssdid              text,
  bank_type           text,
  branch_address      text,
  known_rate_urls     text[] not null default '{}',
  last_successful_url text,
  needs_review        boolean not null default false,
  consecutive_failures integer not null default 0,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  zip_gate_zip        text
);

create table public.url_pattern_registry (
  id        bigint generated always as identity primary key,
  pattern   text not null,
  category  text not null check (category in ('generic', 'bank_specific')),
  bank_key  text references public.bank_registry (bank_key) on delete cascade,
  active    boolean not null default true,
  created_at timestamptz not null default now()
);

create table public.extraction_cache (
  content_hash   text primary key,
  bank_name      text,
  page_url       text,
  extracted_json jsonb not null,
  model_used     text,
  created_at     timestamptz not null default now()
);

create table public.rate_radar_runs (
  run_id             text primary key,
  run_date           date not null,
  started_at         timestamptz not null,
  finished_at        timestamptz,
  banks_attempted    integer not null default 0,
  banks_found        integer not null default 0,
  banks_partial      integer not null default 0,
  banks_error        integer not null default 0,
  ai_vision_calls    integer not null default 0,
  chat_calls         integer not null default 0,
  notes              text,
  inserted_at        timestamptz not null default now(),
  llm_input_tokens   integer not null default 0,
  llm_output_tokens  integer not null default 0,
  llm_web_searches   integer not null default 0,
  estimated_cost_usd numeric not null default 0
);

create table public.rate_observations (
  id                bigint generated always as identity primary key,
  run_id            text not null references public.rate_radar_runs (run_id),
  bank_name         text not null,
  rssdid            text,
  product_type      text not null check (product_type in ('checking','savings','high_yield_savings','money_market','cd')),
  term_months       integer,
  apy               numeric not null,
  min_balance       text,
  is_conditional    boolean not null default false,
  is_promo          boolean not null default false,
  source_url        text,
  extraction_method text not null check (extraction_method in ('site','ai_vision','aggregator','search','last_known_good','manual')),
  confidence        text not null check (confidence in ('high','medium','low')),
  observed_at       timestamptz not null,
  crawled_at        timestamptz not null default now(),
  manually_verified boolean not null default false,
  verified_by       text,
  verified_at       timestamptz
);

create table raw.raw_rate_radar (
  id                       bigint generated always as identity primary key,
  run_id                   text not null,
  run_date                 date,
  crawled_at               timestamptz,
  bank_name                text not null,
  bank_url                 text,
  rssdid                   text,
  bank_type                text,
  branch_address           text,
  checking_apy             numeric,
  savings_apy              numeric,
  cd_apy                   numeric,
  cd_term                  text,
  money_market_apy         numeric,
  min_balance              text,
  status                   text check (status in ('Found','Partial','Not public','Error','Skipped')),
  note                     text,
  source_url               text,
  source_url_checking      text,
  source_url_savings       text,
  source_url_cd            text,
  vulnerability_flag       text,
  cr_period                text,
  cr_total_deposits_m      numeric,
  cr_savings_apy           numeric,
  cr_checking_apy          numeric,
  cr_cd_apy                numeric,
  cr_cost_of_deposits      numeric,
  cr_prev_period           text,
  prev_cr_total_deposits_m numeric,
  prev_cr_savings_apy      numeric,
  prev_cr_checking_apy     numeric,
  prev_cr_cd_apy           numeric,
  prev_cr_cost_of_deposits numeric,
  delta_savings_apy        numeric,
  delta_checking_apy       numeric,
  delta_cd_apy             numeric,
  delta_cost_of_deposits   numeric,
  inserted_at              timestamptz not null default now(),
  constraint raw_rate_radar_run_bank_uq unique (run_id, bank_name)
);

-- production state: RLS on, no policies, anon/authenticated SELECT, service_role everything
alter table public.bank_registry        enable row level security;
alter table public.url_pattern_registry enable row level security;
alter table public.extraction_cache     enable row level security;
alter table public.rate_radar_runs      enable row level security;
alter table public.rate_observations    enable row level security;
alter table raw.raw_rate_radar          enable row level security;
revoke all on public.bank_registry, public.url_pattern_registry, public.extraction_cache,
              public.rate_radar_runs, public.rate_observations, raw.raw_rate_radar from anon, authenticated;
grant select on public.bank_registry, public.url_pattern_registry, public.extraction_cache,
                public.rate_radar_runs, public.rate_observations, raw.raw_rate_radar to anon, authenticated;
grant all on raw.raw_rate_radar to service_role;

-- production-shaped relations granted to PUBLIC (read from production, read-only, 2026-10-18). The three PostGIS objects
-- in public are reachable by anyone with schema usage; the cron and extensions relations sit in schemas the runner has
-- no USAGE on. has_table_privilege() is true for the runner on every one of them, which is what broke the first assertion.
create table public.spatial_ref_sys (srid int primary key, srtext text);
create view public.geometry_columns as select 1 as f_table_catalog;
create view public.geography_columns as select 1 as f_table_catalog;
grant select on public.spatial_ref_sys, public.geometry_columns, public.geography_columns to public;
create schema cron;
create schema extensions;
create table cron.job (jobid int primary key);
create table cron.job_run_details (runid int primary key);
create sequence cron.jobid_seq;
create view extensions.pg_stat_statements as select 1 as queryid;
grant select on cron.job to public;
grant select, delete on cron.job_run_details to public;
grant select on sequence cron.jobid_seq to public;
grant select on extensions.pg_stat_statements to public;

-- canary tables the runner must never reach
create table public.canary_other (id int primary key, secret text);
alter table public.canary_other enable row level security;
create table analytics.canary_snapshot (id int primary key, secret text);
alter table analytics.canary_snapshot enable row level security;
