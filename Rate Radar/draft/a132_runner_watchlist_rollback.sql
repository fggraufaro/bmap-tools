-- a132 DRAFT rollback for draft/a132_runner_watchlist.sql. Not applied anywhere.
-- Order matters: policies on the other tables reference the watchlist, so they go first.
-- backup.a132_acl_before is deliberately kept as the record of the pre-migration state.

drop policy if exists a132_runner_read   on raw.raw_rate_radar;
drop policy if exists a132_runner_insert on raw.raw_rate_radar;
drop policy if exists a132_runner_insert on public.rate_observations;
drop policy if exists a132_runner_insert on public.rate_radar_runs;
drop policy if exists a132_runner_read   on public.extraction_cache;
drop policy if exists a132_runner_insert on public.extraction_cache;
drop policy if exists a132_runner_read   on public.url_pattern_registry;
drop policy if exists a132_runner_read   on public.bank_registry;
drop policy if exists a132_runner_update on public.bank_registry;
drop policy if exists a132_runner_read   on public.rate_radar_watchlist;
drop policy if exists a132_runner_update on public.rate_radar_watchlist;

drop table if exists public.rate_radar_watchlist;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'rate_radar_runner') then
    drop owned by rate_radar_runner;      -- removes every grant the role holds
    drop role rate_radar_runner;
  end if;
end $$;
