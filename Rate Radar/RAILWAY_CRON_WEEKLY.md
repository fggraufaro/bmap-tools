# Rate Radar weekly run: Railway cron definition (a132)

**Status: a document only. No Railway service, variable or schedule has been created.** Nothing here is applied until
Session 4 signs off and Francisco says yes. Owner of the crawler: Session 1 (reviews the code). Built by Session 3.

## What it runs

`python weekly_runner.py` (in `Rate Radar/`): crawls the active, non-manual rows of `public.rate_radar_watchlist`
(at most 10 banks, longest-since-last-run first), banks' own published rates only, and records the results as the
restricted database login `rate_radar_runner`. First pitch: Tue 27 Oct 2026; first scheduled run: Mon 26 Oct 11:00 UTC.

## Service definition

| Setting (Railway service Settings) | Value |
| --- | --- |
| Service name | `rate-radar-weekly` (a **new** service; do not reuse `bmap-snapshot` or the Rate Radar app service) |
| Source | the `bmap-tools` repo, branch to deploy: the merged a132 runner branch; root directory `Rate Radar` |
| Builder | the existing `Rate Radar/Dockerfile` (Playwright Python image; unchanged) |
| Start command | **stage 1:** `python weekly_runner.py --dry-run` · **stage 2:** `python weekly_runner.py` |
| Cron Schedule | **stage 1: empty (disabled)** · **stage 2: `0 11 * * 1`** (Monday 11:00 UTC) |
| Restart policy | **Never.** A failed run must not be retried automatically: a retry would crawl and spend again |
| Region | a US region, same as the other services |
| Resources | Chromium needs memory: start with 2 GB and check the first run's usage |

Railway facts this relies on (from its cron documentation): schedules run in UTC; the minimum interval is 5 minutes;
the start command runs on the schedule and the process must exit when done, otherwise the next run is skipped; if a
previous run is still active the next one is skipped, not stacked. `weekly_runner.py` always exits (its own wall-clock
cap is 25 minutes). Config as Code (`railway.json`) is deprecated and cannot be adopted by new services, so this is
configured in the dashboard. Setting the schedule is the only thing that turns the service on.

### "Created disabled": three independent layers

1. **No schedule** in stage 1, so Railway never starts it on its own.
2. **`--dry-run` in the start command**: even a deploy or manual trigger only reads the watchlist (read-only database
   session) and prints the plan. It cannot crawl and it cannot write.
3. **Credential not set until the go**: leave `RATE_RADAR_DATABASE_URL` unset until the migration is applied. Without it the
   runner prints `REFUSED` and exits 2.

Creating a service from a repo deploys it once; with these settings that deploy is a harmless dry run or a refusal.

## Variables (service-level only)

| Variable | Value | Notes |
| --- | --- | --- |
| `RATE_RADAR_DATABASE_URL` | the `rate_radar_runner` connection string | Session pooler, user `rate_radar_runner.<project-ref>`, password from the file written by `draft/a132_make_runner_credential.py`. Copy the host from the Supabase **Connect** page. Never put it in chat, git or a shared variable |
| `ANTHROPIC_API_KEY` | an API key for this service | Prefer a **dedicated key with a spend limit** so a runaway cannot touch other work |
| `RATE_RADAR_MAX_COST_USD` | `2.00` | LLM cost cap per run; the run is cancelled and its partial results recorded when it is reached |
| `RATE_RADAR_MAX_MINUTES` | `25` | wall-clock cap, same ceiling as the app's crawl thread |

### Connection string for `RATE_RADAR_DATABASE_URL` (no password shown here)

```
postgresql://rate_radar_runner.tuiiywphoynbmkxpoyps:<PASSWORD>@<POOLER-HOST>:5432/postgres?sslmode=require
```

- **Use the session-mode pooler (Supavisor), port 5432.** Copy `<POOLER-HOST>` from the Supabase dashboard, Connect page, "Session pooler"
  (it looks like `aws-<n>-eu-north-1.pooler.supabase.com`; the project is in eu-north-1). Database `postgres`.
- **User is `rate_radar_runner.<project-ref>`** (here `rate_radar_runner.tuiiywphoynbmkxpoyps`). The pooler needs the project
  reference in the user name to route the connection; the suffix does not exist in Postgres. The role is created as plain
  `rate_radar_runner`, and the runner checks `current_user = rate_radar_runner` after connecting, so a wrong role is refused.
- **Not transaction mode (port 6543):** the runner relies on session state (the read-only session used by `--dry-run`) that
  transaction pooling does not preserve. **Not the direct connection:** it is IPv6-only unless the project has the IPv4
  add-on, and I have not verified Railway's outbound IPv6, so the pooler is the safe choice.
- `<PASSWORD>` is the file written by `draft/a132_make_runner_credential.py` (URL-safe characters, no encoding needed).
  Paste the finished string only into the Railway variable.
- The role's connection limit (3) is enforced by Postgres on top of the pooler's own pool.

**Must NOT exist on this service, or in project/shared variables it could inherit:** `SUPABASE_SERVICE_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_KEY`. The runner refuses to start if any is set. It also never connects as any role
other than `rate_radar_runner`.

## Sequence (everything on Wednesday 21 Oct; nothing on Thursday 22 or Friday 23: freeze)

| When | Step | Who |
| --- | --- | --- |
| Tue 20 Oct | Code review done (Session 1 accepted); tests green; nothing applied | Session 1, 3 |
| Wed 21 Oct, 1 | With Session 4's sign-off **and** Francisco's yes: apply `draft/a132_runner_watchlist.sql` (in one transaction; it aborts and rolls back if its own checks fail) together with the a40 deploy | Francisco |
| Wed 21 Oct, 2 | Insert the approved watchlist rows (each `bank_key` must already be in `bank_registry`); run `python draft/a132_make_runner_credential.py --out <path outside every repo>` and the printed `ALTER ROLE ... PASSWORD` once | Francisco |
| Wed 21 Oct, 3 | Create the Railway service in **stage 1** (above: empty schedule, `--dry-run`, four variables); deploy once; read the dry-run plan and confirm it lists the intended banks | Francisco |
| Wed 21 Oct, 4 | Session 1's forced crawl of the 5 banks runs first | Session 1 |
| Wed 21 Oct, 5 | **Supervised real run**, right after that crawl: set the real start command (`python weekly_runner.py`), schedule still empty, redeploy once; run the verification queries below and read the job log | Session 1, Francisco |
| Wed 21 Oct evening | **Only if that run is clean** (exit 0, run row recorded, one raw row per bank crawled, watchlist `last_*` stamped, cost under the cap, no `REFUSED` or `CUT SHORT` lines): set the Cron Schedule to `0 11 * * 1`. If anything is off, set the start command back to `--dry-run`, leave the schedule empty, and tell Session 4 | Francisco |
| Thu 22 and Fri 23 Oct | **No changes.** No Rate Radar deploys, no production changes | none |
| Mon 26 Oct, 11:00 UTC | First scheduled run. Session 1 checks the result Monday; Francisco reviews it before the Tue 27 Oct pitch. If the schedule was never enabled or the run did not record, Session 1 does a manual forced crawl Monday | Session 1, Francisco |

## Verification after a run (run as the owner in the SQL editor)

```sql
select run_id, run_date, banks_attempted, banks_found, banks_partial, banks_error, estimated_cost_usd, notes
from public.rate_radar_runs order by started_at desc limit 3;

select rssdid, bank_name, last_run_id, last_run_at, last_status
from public.rate_radar_watchlist order by rssdid;

select status, count(*) from raw.raw_rate_radar
where run_id = (select run_id from public.rate_radar_runs order by started_at desc limit 1) group by 1;
```

A run cut short by a cap says so in `rate_radar_runs.notes` (`COST CAP` or `TIME CAP`) and in the job log. Exit codes: 0
recorded, 1 not clean (nothing usable recorded, or the crawler crashed after a partial record), 2 refused (configuration
or guard), 3 database error. Banks marked `manual_only` are listed in the log as "manual verification due" and are
never crawled. Treat `extraction_method = search` with `confidence = low` as unverified, as elsewhere in Rate Radar.

## What the runner can and cannot do

It reads the watchlist; inserts run, observation, raw and cache rows for watchlist banks; updates only the `last_*`
columns on the watchlist and only the learning and counter columns on `bank_registry`. It cannot delete, cannot read
the run log or observations back, cannot see any other table or schema, cannot add banks to the watchlist, and cannot
change which banks are active. Adding or pausing a bank is a manual change by the owner.

Known limits of this first version: no call-report enrichment (the `cr_*` columns stay empty on weekly rows, because
that loader uses the service key); a registry row must already exist for every watchlist bank; re-pushing a run id is a
no-op rather than an overwrite.

## Rotate or turn off

- **Pause:** clear the Cron Schedule (or set the start command back to `--dry-run`).
- **Rotate the password:** delete the password file, run the credential script again, run the new `ALTER ROLE`, update `RATE_RADAR_DATABASE_URL`.
- **Remove everything:** `draft/a132_runner_watchlist_rollback.sql` drops the policies, the watchlist and the role (the saved
  pre-migration ACL stays in `backup.a132_acl_before`); then delete the Railway service.
