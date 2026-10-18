-- LOCAL TEST ONLY. Recreates the Supabase roles and the few pgTAP functions the a132 tests use, so the
-- pgTAP-style test file runs on a plain PostgreSQL. On Supabase the real pgTAP extension is used instead.
-- Never apply to Supabase.

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon')          then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role')  then create role service_role nologin bypassrls; end if;
end $$;

create or replace function plan(n int) returns text language plpgsql as $$
begin
  create temp table if not exists tap_state (k text primary key, v int);
  grant all on tap_state to public;
  delete from tap_state;
  insert into tap_state values ('count', 0), ('failed', 0), ('planned', n);
  return '1..' || n;
end $$;

create or replace function _tap_result(passed boolean, descr text, detail text default null) returns text language plpgsql as $$
declare c int;
begin
  update tap_state set v = v + 1 where k = 'count';
  select v into c from tap_state where k = 'count';
  if not passed then update tap_state set v = v + 1 where k = 'failed'; end if;
  return case when passed then 'ok ' else 'not ok ' end || c || ' - ' || coalesce(descr, '')
         || case when not passed and detail is not null then E'\n# ' || detail else '' end;
end $$;

create or replace function ok(passed boolean, descr text default null) returns text language sql as
$$ select _tap_result(coalesce(passed, false), descr) $$;

create or replace function is(have anyelement, want anyelement, descr text default null) returns text language sql as
$$ select _tap_result(have is not distinct from want, descr, 'have: ' || coalesce(have::text, 'NULL') || E'\n#         want: ' || coalesce(want::text, 'NULL')) $$;

create or replace function lives_ok(q text, descr text default null) returns text language plpgsql as $$
begin
  execute q;
  return _tap_result(true, descr);
exception when others then
  return _tap_result(false, descr, 'threw ' || sqlstate || ': ' || sqlerrm);
end $$;

create or replace function throws_ok(q text, errcode text, errmsg text, descr text default null) returns text language plpgsql as $$
begin
  execute q;
  return _tap_result(false, descr, 'caught: no exception' || E'\n#         wanted: ' || errcode);
exception when others then
  if sqlstate = errcode and (errmsg is null or sqlerrm = errmsg) then
    return _tap_result(true, descr);
  end if;
  return _tap_result(false, descr, 'caught: ' || sqlstate || ': ' || sqlerrm || E'\n#         wanted: ' || errcode);
end $$;

create or replace function finish() returns setof text language plpgsql as $$
declare f int; p int; c int;
begin
  select v into f from tap_state where k = 'failed';
  select v into p from tap_state where k = 'planned';
  select v into c from tap_state where k = 'count';
  if c <> p then return next '# Looks like you planned ' || p || ' tests but ran ' || c; end if;
  if f > 0 then return next '# Looks like you failed ' || f || ' test' || case when f > 1 then 's' else '' end || ' of ' || c; end if;
end $$;
