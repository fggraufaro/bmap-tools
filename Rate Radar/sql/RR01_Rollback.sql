-- RR01 ROLLBACK. ONLY if RR01_Apply.sql was applied and must be undone. One transaction. It drops public.rate_not_published (and its rows) and the four columns on public.rate_observations.
-- Nothing else is touched. The Rate Radar code stops writing the dropped objects by itself (it checks that they exist), so no redeploy is needed.
-- DATA WARNING: the review_flags / tier_note / market_zip / scope_state values and the not-published rows written since the apply are lost by this rollback (the rate values themselves are not).
begin;

do $$
begin
  if to_regclass('public.rate_not_published') is null and not exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations' and column_name in ('review_flags', 'tier_note', 'market_zip', 'scope_state')) then
    raise exception 'rr01 rollback: nothing to roll back (already rolled back?): stop and tell Session 3';
  end if;
end $$;

drop table if exists public.rate_not_published;
alter table public.rate_observations drop column if exists review_flags;
alter table public.rate_observations drop column if exists tier_note;
alter table public.rate_observations drop column if exists market_zip;
alter table public.rate_observations drop column if exists scope_state;

do $$
begin
  if (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'rate_observations') <> 18 then
    raise exception 'rr01 rollback: public.rate_observations does not have 18 columns after the rollback: nothing is kept';
  end if;
  if to_regclass('public.rate_not_published') is not null then
    raise exception 'rr01 rollback: public.rate_not_published still exists: nothing is kept';
  end if;
end $$;

commit;
