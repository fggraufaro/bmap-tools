# BMAP Session Coordination — Sep 10, 2026

Four Claude Code sessions are working the Verlocity Platform Roadmap's Open Actions in parallel: three execution sessions, each owning a themed slice of work, and one Roadmap Manager session that keeps the roadmap and tracker themselves accurate as the other three report progress. This file is shared context — paste it into all four sessions so each knows its own scope and how the whole thing fits together.

## The roadmap, in two places

- **Live artifact** (source of truth for Open Actions): `https://claude.ai/code/artifact/f2513088-1af1-43f5-a384-f59f2e32f79e`
- **Git-tracked copy**: `bmap-tools/verlocity-platform-roadmap.html` on `main`, GitHub `fggraufaro/bmap-tools`
- The two must stay in sync. The pattern every session should follow after touching either: publish the HTML to the artifact URL, copy the same file into the repo, commit, push. Never publish without passing `url` — that updates the existing artifact instead of creating a new one.
- Open Actions live in the artifact's own database (`actions` collection), not just as static HTML — use `Artifact` tool actions `read_db` (to check current state) and `write_db` (to mark items done, set `owner`, etc.) rather than hand-editing the checklist markup.

## Session 1 — Data Pipeline & Automation

Owns action items: **a13, a14, a15, a16, a17, a24**

| id | Task |
|---|---|
| a13 | Build Zillow ZHVI ingestion script (monthly) — most stale source today (~5 months behind), simplest to automate |
| a14 | Build Call Reports / UBPR / NCUA FS220 ingestion script (quarterly) — replaces `call_reports.py`'s manual folder-drop + hardcoded quarter-list. **Correction (verified 2026-09-10): the existing loader is at `bmap-tools/Rate Radar/call_reports.py`, not in `bmap-snapshot`** — it's a standalone FFIEC RI/RCE/RCK reader with no crawl/scrape dependency. Port/adapt it into `bmap-snapshot`'s automation rather than rebuilding from scratch; copy the file into this worktree first since Session 1's scope can't see `bmap-tools`. |
| a15 | Build FDIC SOD + Census ACS ingestion scripts (annual) |
| a16 | Wire scheduling via `pg_cron` (installed on the Supabase project, unused) or a Railway cron service; auto-invoke `refresh_bmap_after_upload` / `refresh_branch_opportunity_base` after each load |
| a17 | Provision a Census API key — none of the 5 raw sources have programmatic credentials today |
| a24 | Fix CLAUDE.md's reference to `schedule_rate_radar.py` — confirmed it doesn't exist in either repo or their git history |

**Key context from the pipeline audit:** there is no ingestion code anywhere for `raw.raw_sod`, `raw.raw_income`, `raw.raw_population`, `raw.raw_zhvi`, or `analytics.bank_financial_snapshot_latest` — every refresh today is a manual CSV load, almost certainly via the Supabase Table Editor. `raw_income`/`raw_population` still carry literal Census bulk-export column names (`"Geography"`, `"Geographic Area Name"`), confirming this. Build order matters: ZHVI first (a13), then Call Reports/NCUA (a14), then SOD/Census (a15), then scheduling (a16) once ingestion scripts actually exist — there's nothing to "just schedule" until then. Credentials needed: `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` for all writes (non-public schemas need an `Accept-Profile` header); a Census API key still needs provisioning (a17).

## Session 2 — New Datasets

Owns action items: **a18, a19, a20, a21, a22, a23**

| id | Dataset | Why |
|---|---|---|
| a18 | IRS SOI county-to-county migration | Income-weighted migration as a leading indicator ahead of SOD deposit growth |
| a19 | HMDA Modified LAR | Mortgage share by named competitor — a real transaction signal for vulnerability scoring |
| a20 | CFPB Consumer Complaint Database | Real-time competitor attrition-risk signal, one REST endpoint |
| a21 | Census Business Formation Statistics | Leading indicator vs. the existing static SMB density snapshot |
| a22 | BLS QCEW | Current industry-mix/wage growth to refresh the persona layer, which leans on 1-2yr-lagged ACS |
| a23 | FDIC public enforcement actions | Free early-warning layer on top of existing capital-ratio vulnerability scoring |

**Key context:** all 6 are free/public (bulk download or free API key), deliberately chosen over paid options for this scope. Two paid candidates were researched and explicitly excluded from this sprint: **S&P Global/SNL** (deposit market-share rankings, M&A/branch-closure data) and **Placer.ai/Advan** (mobile foot traffic) — both are real, credible options if budget opens up later, but are not action items right now. Don't re-propose them as free alternatives; they aren't.

Each dataset should land in `raw.*` (matching the existing schema convention) and get wired into the specific scoring component named above — not just loaded and left unused.

## Session 3 — Product & Research

Owns action items: **a1, a2, a3, a8, a10, a11, a12**

| id | Task | Note |
|---|---|---|
| a1 | Deposit-flight backtest | Costs nothing (existing data), time-sensitive — BlastPoint just took funding to chase the same signal into community banking |
| a2 | Finalize the BMAP/Rate Radar Bank Assessment doc | Sales-closing asset, not new build |
| a3 | Rate Radar trend analysis | Likely feeds the backtest (a1) directly rather than running as separate work |
| a8 | Prototype the Branch/CMO Copilot | One-day internal test — BankIQ and Curinos already have validated results ($1.6B deposits, one case), so the bar is real |
| a10 | Auto-generated narrative engine test | Cheap, no competitive clock |
| a11 | AudienceFinder/Resonate integration | Backlog, Product B, gated on Product A's proof |
| a12 | Growth Lab as a data-science R&D sandbox | Parked, depends entirely on the a1 backtest result |

This is the most loosely-coupled group — a1/a2/a3 are the near-term priority (time-sensitive per the note above), a8/a10 are cheap validation work that can slot in anywhere, a11/a12 are backlog items not expected to start yet.

## Session 4 — Roadmap Manager

**Does not execute a1–a24.** Its job is keeping the roadmap and tracker accurate as Sessions 1–3 report real progress — a coordination and record-keeping role, not an engineering one.

Responsibilities:
1. Periodically `read_db` the `actions` collection and check the Sprint log for what's actually changed.
2. When a session reports an item done, mark it `done: true` via `write_db` — don't take a session's word for "done" without some evidence (a commit link, a verified query result, a live screenshot) the same way this project always verifies before shipping.
3. When a session reports something significant (a real bug found, a decision made, a design change), add it to the current sprint's Found/Open columns — don't let real findings evaporate into chat history.
4. Keep the git-tracked roadmap copy synced with the live artifact after every change — publish, copy, commit, push, every time, not batched up.
5. If two sessions' changes conflict (e.g. both touched the artifact between reads), re-read the live version and merge onto it — never overwrite blind.
6. Do not invent progress. If a session hasn't reported anything, the roadmap should still say "not started," not something optimistic.

## Ground rules that apply to all four sessions

- **Supabase project**: `tuiiywphoynbmkxpoyps`, region eu-north-1. Schema-namespaced (`analytics.`/`raw.`/`geo.`/`ref.`) — non-public schemas need `Accept-Profile` header on PostgREST calls.
- **RLS blind spot**: owner-level `execute_sql` bypasses RLS. Verify anything user-facing through the real anon-key path, not just MCP.
- **Repos**: frontend is `bmap-tools` (GitHub `fggraufaro/bmap-tools`); backend is a separate repo, `bmap-snapshot` — confirm exact file locations before assuming, several worktree copies exist locally (`bmap-snapshot-ingestion`, `bmap-snapshot-model` are worktrees of the *same* repo on different branches, currently identical to `main`).
- **16-play matrix**: the authoritative campaign/play vocabulary lives in `bmap_assessment_doc.py`'s `PLAY_MATRIX` dict (sourced from `BMAP_Methodology_Part1.docx`). Don't invent new play names.
- **Grants after DDL**: any `DROP` + `CREATE TABLE/VIEW` needs `GRANT SELECT ON [table] TO anon, authenticated, service_role` re-applied, or downstream REST access breaks silently.
- **Verify before shipping**: test against real source files and real deployments, not memory of how something works. This project has a strong track record tonight of finding real bugs by insisting on live verification — keep that standard.
