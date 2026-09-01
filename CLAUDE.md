# CLAUDE.md

Peliqan Data Apps for a timesheet system, one `.py` per app in `scripts/`,
named `<app_id>_<app_name>.py`. The warehouse is `dw_3202`, schema `ts_prod`.

## Deploying: pushing to `main` IS the deploy

`.github/workflows/sync-peliqan-scripts.yml` runs `push_peliqan_data_apps.py`
on every push to `main` that touches `scripts/**.py`, so a merged change is
live in the Peliqan account without any further step. Don't tell the user a
change "needs deploying" — check whether it's on `main` instead.

Two consequences:

- CI commits the updated `scripts/.manifest.json` back to `main`, which
  advances the branch past your local clone. Expect to `git pull` before the
  next edit.
- CI only fires on `main`. Work on a branch is *not* live; `python
  push_peliqan_data_apps.py` pushes it manually.

`README.md` has the full sync model (drift detection, `--force`, `--dry-run`).

## Which apps enforce anything

Roles come from `ts_prod.users.scope`: 1 employee, 2 manager, 3 admin,
cumulative. Only **two** surfaces know about it:

- `12011_ts_my_week` — Google login required, scope-aware grid.
- `11383_ts_mcp_server` — Bearer token, `min_scope` per tool.

Every other app (`11282`, `11286`, `11290`, `11954`, `12507`, `12761`) has no
login and no scope check at all. `11286_ts_users_UI` writes `ts_prod.users`, and a row
there is what grants access to the two gated apps. Don't assume a rule you
find in one app holds anywhere else — check.

## Data model traps

- **The warehouse runs on Baserow under the hood** (confirmed by a leaked error
  stack trace: `/baserow/backend/src/baserow/...`). Table names get resolved
  through Baserow's own catalog, not just checked for physical existence — a
  raw `CREATE TABLE` via `dbconn.execute()` reaches the underlying Postgres
  but Baserow never learns about it, so `insert`/`update`/`upsert`/`fetch`
  against that table still 404 as `ERROR_TABLE_DOES_NOT_EXIST`. Only
  `dbconn.create_table(db_name, schema_name, table_name, fields=[{"name":...,
  "type":...}], pk=[...])` registers a new table both places. `dbconn.write()`
  /`write_records` doesn't either — despite looking like an auto-create path,
  it left nothing queryable. Don't reach for either shortcut again.
- **`ts_prod.planned_assignments` is a standalone schedule, not derived from
  `timetable`.** `12761_ts_planner` calls `dbconn.create_table()` (see above)
  before every `dbconn.upsert`, keyed on a synthetic `id` = `f"{user_id}_
  {date}"` — the same shape `ts_my_week` uses for `timetable_submissions`. It
  is deliberately NOT compared against logged hours yet; that comparison is a
  known follow-up, not built.
- **`dbconn.update`/`insert` run string values through `Decimal()`.** Passing
  `"true"` for a boolean column returns a 400 with
  `[<class 'decimal.ConversionSyntax'>]`. Always pass real Python bools.
- **Vocabulary vs columns.** Everything a user reads says *validated*. The
  columns are still `timetable.approved` / `approved_by`, and
  `timetable_submissions.status` is `'confirmed'`. Renaming those is out of
  scope — see below.
- **`ts_reporting.fact_timetable` is a Peliqan query table and its SQL is not
  in this repo.** `11954` and `11970` select `approved` from it. Renaming that
  column would break them, and the fix would be a manual edit in the Peliqan
  UI that can't be verified from here. It also lags writes to `ts_prod`, which
  is why the monthly export reads the live tables instead.
- **`clients.user_list` is many-to-many and never comes back from
  `dbconn.fetch(schema, table)`.** The only read is the internal junction
  table `_pq_metadata._pq_rl_1339ee9e`, used by `11383` and `12011` to limit
  employees to their assigned clients. That name changes if the field is ever
  deleted and recreated — if client assignments come back empty, check the
  field's current relation id first.

## Peliqan runtime constraints

- **Apps cannot import each other.** Sharing code means duplicating it; say so
  in a comment on both copies, or move the logic into one app and link to it.
- **`st.set_page_config` must take literal arguments.** The runtime lifts that
  call into a prepend that executes before the script body, so a variable
  there fails.
- Each script starts with a `RUN_CONTEXT in globals()` check: present means
  running on Peliqan, absent means local `streamlit run` with
  `PELIQAN_API_KEY` set.
- Testing without Peliqan: `exec()` the module with a stubbed `pq` and a
  seeded `CACHE`, then call the functions directly. The MCP's `@tool`
  decorator returns the plain function, so tools are callable in a test.

## Don't hand-edit

- `scripts/.manifest.json` — the sync's baseline; editing it causes false
  "drifted" or "nothing to sync" results.
- `data_apps.json` / `data_apps.csv` — gitignored output of
  `fetch_peliqan_data_apps.py`. They contain every app's source as JSON
  blobs, so they pollute repo-wide greps; search `scripts/` instead.
- `scripts/*.remote.py` — transient conflict artifacts from a fetch.
