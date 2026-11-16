# Smaller Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nightly backups become gzipped, retention keeps N daily + M weekly, and finished months of arrivals/forecast are archived once and left out of the nightly tar.
**Architecture:** Backup-only change: a new `app/backup/meta_archive.py` (archive, slim copy, startup restore, archive rewrite) and `app/backup/retention.py` (pure selection), wired into `snapshot`, the lifespan (restore at start) and the day-removal job. The live `meta.db` is never shrunk or changed by backups.
**Tech Stack:** Python 3.12, sqlite3, tarfile/gzip, FastAPI, pytest (hypothesis present in the repo).
**Spec:** `docs/specs/2026-11-16-smaller-backups-design.md` (approved by the user 2026-11-16; plan conflicts resolve against it).
**Assumptions:** Assumes one app process and `service_date` is `YYYY-MM-DD` text - will NOT work if service dates are stored in another format. Assumes finished months get no new arrivals after the 2-day grace - late rows are handled by the count check (the month then stays whole in the tar), not by archiving them later. Assumes the archive folder (default `<BACKUP_DIR>/archive`) survives together with the backup tars - will NOT protect against loss of that disk (README already says to copy it off-host).

## Global Constraints

- Work on branch `feat/backup-month-archive` (already created; the spec is committed there). Commit at the end of every task; end commit messages with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Python only via `/home/david/dbus/.venv/bin/python` after `ulimit -v 5000000`. NEVER run the whole `tests/` in one pytest command; run directories separately with `timeout 280` and `-q -p no:cacheprovider` (`tests/backup` ~70 s, `tests/api` ~4.5 min, `tests/collect` ~2 min). Max 2 subagents at once; subagent models sonnet or cheaper.
- Never touch the user's original backup `C:\Users\david\Downloads\analytics-20261116T133604Z.tar` or the scratchpad copies `bk`, `run`, `bench`; real-data checks use a NEW copy.
- Live `meta.db` rows are never modified or deleted by backup code. Only the staged COPY inside a snapshot may be slimmed.
- A month is slimmed out of a tar only if its archive file exists, passes `integrity_check`, and its recorded `arrivals` and `forecast` counts equal that month's counts in the copy; otherwise the month stays whole and a warning is logged. With the archive disabled (`archive_dir` is None) no meta archive is written and no month is slimmed.
- Backup/archive code must never raise out of `snapshot` because of archive problems (log and continue), matching the existing `archive_months` isolation; restore must never stop the app from starting.
- Finished month: `now >= first day of the next month 00:00 UTC + COMPACT_GRACE_S` (2 days, existing constant in `app/backup/backup.py`).
- Archive files: `meta-YYYY-MM.db.gz` in the archive folder, written to a temp file, fsynced, renamed, never overwritten (except by the day-removal rewrite in Task 6). Tables inside: `arrivals`, `forecast` with the same columns and SAME `id`/`arrival_id` values, plus `info(month TEXT, arrivals INT, forecast INT, max_id INT)`.
- Defaults: `BACKUP_KEEP=3` (was 7), `BACKUP_KEEP_WEEKLY=4` (0 disables), gzip `compresslevel=3`, file names `analytics-<UTC stamp>[_NNN].tar.gz`; existing plain `.tar` files are still listed, downloaded, restored and pruned.
- Dockerfile `HEALTHCHECK --start-period` becomes `15m`.

## Review Focus

- **Data loss by slimming:** every way a month could be left out of the tar without a complete, verified archive behind it (no file, corrupt file, count mismatch because rows arrived late, archive disabled, archive written for a different row set). Expected: month stays whole in the tar (pinned in Task 4).
- **Day removal vs archive:** removing a day inside an archived month must rewrite the archive before touching live rows and abort on failure, otherwise a restore resurrects deleted data (Task 6).
- **Restore safety:** partial import, crash mid-import, missing/damaged archive, running twice, ids colliding with rows already in the restored database (Task 5).
- **Retention edge cases:** ISO week at year end, backups in the future, a new file never deleted, mixed `.tar`/`.tar.gz` (Tasks 1-2).
- **Writes during a snapshot:** the poller commits to `meta.db` between copies and while the archive is built (Tasks 3-4).

---

## File Structure

- `app/backup/retention.py` (new): pure `choose_keep`.
- `app/backup/meta_archive.py` (new): `finished_months`, `archive_meta_months`, `slim_meta_copy`, `restore_archived_months`, `rewrite_archive_without`.
- Modify: `app/backup/backup.py` (name pattern, gzip tar, retention, wiring), `app/backup/api.py` (download media type, pass weekly), `app/backup/scheduler.py` (pass weekly), `app/core/config.py`, `app/main.py` (restore at startup), `app/maintenance/{day,jobs,api}.py` (archive rewrite), `Dockerfile`, `docker-compose.example.yml`, `docker-compose.release.yml`, `analytics.env.example`, `README.md`, `pyproject.toml`, `tests/test_docs.py`.
- Tests: `tests/backup/test_retention.py`, `tests/backup/test_meta_archive.py`, `tests/backup/test_roundtrip.py`, changes in `tests/backup/test_backup.py`, `tests/backup/test_scheduler.py`, `tests/maintenance/test_day.py`.

### Task 0: Settings and docs parity

**Files:** Modify `app/core/config.py`, `docker-compose.example.yml`, `docker-compose.release.yml`, `analytics.env.example`, `README.md` (env table only), `tests/test_docs.py`; Test `tests/core/` config test file (find the existing Settings test) or `tests/backup/test_scheduler.py` if that is where defaults are asserted.

**Interfaces:**
- Consumes: nothing
- Produces: `Settings.backup_keep: int = 3` (default changed from 7), `Settings.backup_keep_weekly: int = 4`; env `BACKUP_KEEP_WEEKLY` (integer >= 0; `0` allowed, negative or non-integer raises `ValueError` naming the variable like the other settings).

**Security flag:** `none`

- [x] **Step 1: Failing tests:** `test_backup_keep_defaults`: `Settings("http://x", Path("/x")).backup_keep == 3` and `.backup_keep_weekly == 4`; `test_backup_keep_weekly_env`: `BACKUP_KEEP_WEEKLY=0` gives 0, `2` gives 2, `-1` and `abc` raise `ValueError` mentioning `BACKUP_KEEP_WEEKLY`; `BACKUP_KEEP=7` still gives 7. Update any existing test asserting the old default 7.
- [x] **Step 2:** Run the settings test file and `tests/test_docs.py` - FAIL.
- [x] **Step 3: Implement** the field, a small `_nonneg_int(name, default)` helper beside `_pos_int`, and `from_env` wiring. Docs parity: README env table gets a `BACKUP_KEEP_WEEKLY` row (`4`, "Also keep the newest backup of each of the previous N ISO weeks; `0` turns weekly retention off") and the `BACKUP_KEEP` row text/default becomes `3` ("Newest nightly backups to keep"); compose example `BACKUP_KEEP: "${ANALYTICS_BACKUP_KEEP:-3}"` + `BACKUP_KEEP_WEEKLY: "${ANALYTICS_BACKUP_KEEP_WEEKLY:-4}"`; release compose `BACKUP_KEEP: "3"` + `BACKUP_KEEP_WEEKLY: "4"`; `analytics.env.example` updated lines; add the new name to the expected-vars set in `tests/test_docs.py`.
- [x] **Step 4:** Run `tests/core`, `tests/test_docs.py`, `tests/backup/test_scheduler.py` - PASS.
- [x] **Step 5:** Commit `Add BACKUP_KEEP_WEEKLY and lower the BACKUP_KEEP default to 3`.

### Task 1: Gzipped tars

**Files:** Modify `app/backup/backup.py`, `app/backup/api.py`; Test `tests/backup/test_backup.py`.

**Interfaces:**
- Consumes: nothing
- Produces: `_TAR` pattern `^analytics-(\d{8}T\d{6}Z)(_\d{3})?\.tar(\.gz)?$` (group 3 = `.gz` or None); `snapshot(...)` now returns a `.tar.gz` path; `list_backups`, `newest_backup`, `_stamp`, `_reserve` accept both extensions.

**Security flag:** `none`

**Does NOT cover:** converting existing `.tar` files; changing retention (Task 2).

- [x] **Step 1: Failing tests** (`tests/backup/test_backup.py`): `test_snapshot_writes_tar_gz` (returned name ends `.tar.gz`, `tarfile.open(p, "r:gz")` lists `meta.db` and the samples files); `test_plain_and_gz_backups_listed_together_oldest_first` (create `analytics-20260101T000000Z.tar` and `analytics-20260102T000000Z.tar.gz` fake files with content: `list_backups` returns both ordered by stamp; `newest_backup` returns the `.gz`); `test_reserve_counter_shared_between_extensions` (same stamp, existing `.tar` -> new file gets `_001` and `.tar.gz`); `test_download_gz_media_type` (API download of a `.tar.gz` has content type `application/gzip`, `.tar` stays `application/x-tar`). Update every existing assertion that expects a `.tar` name from `snapshot`.
- [x] **Step 2:** Run `tests/backup/test_backup.py` - FAIL.
- [x] **Step 3: Implement** in `backup.py`: widen `_TAR`; `_stamp` unchanged (uses group 1); `_reserve(tmp_tar, backup_dir, stamp)` builds `analytics-{stamp}{_NNN}.tar.gz` and counts existing ones across both extensions; `snapshot` opens `tarfile.open(tmp_tar, "w:gz", compresslevel=3)` (rename the temp to `out.tar.gz`). `api.py`: `media_type` by suffix.
- [x] **Step 4:** Run `tests/backup`, `tests/maintenance` - PASS.
- [x] **Step 5:** Commit `Write nightly backups as gzipped tars`.

### Task 2: Retention (daily + weekly)

**Files:** Create `app/backup/retention.py`; Modify `app/backup/backup.py`, `app/backup/api.py`, `app/backup/scheduler.py`; Test `tests/backup/test_retention.py`, `tests/backup/test_backup.py`, `tests/backup/test_scheduler.py`.

**Interfaces:**
- Consumes: `list_backups`, `_stamp`, `_is_future` from Task 1's `backup.py`.
- Produces: `choose_keep(items: list[tuple[Path, int]], keep: int, weekly: int) -> set[Path]` where `items` are `(path, stamp_epoch_seconds)` sorted oldest-first and already exclude future-dated files; `snapshot(data_dir, backup_dir=None, keep=7, archive_dir=None, keep_weekly=0, now=None)` (new keywords `keep_weekly`, `now`; `now` defaults to `time.time()`).

**Security flag:** `none`

**Does NOT cover:** pruning the archive folder (never pruned).

Rule: keep the newest `keep` items; then walk ISO weeks (UTC, `datetime.isocalendar()` year+week) backwards from the week of the newest item: for each of the next `weekly` distinct weeks that contain at least one not-yet-kept item, keep the newest such item of that week. Everything else is deleted.

- [x] **Step 1: Failing tests** (`tests/backup/test_retention.py`, build `items` from date strings): `test_keeps_newest_n_only_when_weekly_zero`; `test_weekly_picks_newest_per_week` (14 daily stamps, keep=3, weekly=2 -> the 3 newest plus the newest of each of the two previous weeks); `test_year_end_iso_week` (stamps 2026-12-28..2027-01-04 land in the correct ISO weeks, 2026-W53 vs 2027-W01); `test_weekly_skips_weeks_already_covered_by_daily`; `test_fewer_items_than_keep_keeps_all`; `test_keep_one_weekly_zero_keeps_exactly_newest`. In `test_backup.py`: `test_snapshot_never_deletes_new_file_and_ignores_future_dated` and `test_snapshot_prunes_with_weekly` (fake files named with old stamps; call `snapshot(..., keep=2, keep_weekly=1)`); update scheduler test fakes so `snapshot_fn` accepts `keep_weekly`.
- [x] **Step 2:** FAIL.
- [x] **Step 3: Implement** `choose_keep`; in `snapshot` replace the current "delete oldest beyond keep-1" block: build `items` from `list_backups` (excluding `final` and future-dated) plus `final` itself, call `choose_keep(items, keep, keep_weekly)`, delete listed files not chosen, never `final`. `scheduler.run_once` and `api.create_backup` pass `keep_weekly=settings.backup_keep_weekly` (`getattr(..., 0)` in the API like the existing pattern).
- [x] **Step 4:** `tests/backup`, `tests/test_main.py` - PASS.
- [x] **Step 5:** Commit `Keep N daily plus M weekly backups`.

### Task 3: Archive finished months of arrivals and forecast

**Files:** Create `app/backup/meta_archive.py`; Modify `app/backup/backup.py` (call after `archive_months`); Test `tests/backup/test_meta_archive.py`.

**Interfaces:**
- Consumes: `snapshot(..., now=None)` from Task 2; `COMPACT_GRACE_S`, `_sha`, `_integrity`, `_fsync_dir`, `_clean_stale` helpers in `backup.py`; `open_meta` schema (`arrivals`, `forecast`).
- Produces: `META_ARCHIVE_RE = re.compile(r"^meta-(\d{4})-(\d{2})\.db\.gz$")`; `finished_months(meta_db: Path, now: int) -> list[str]` (sorted `YYYY-MM` that have arrivals rows and are finished); `archive_meta_months(data_dir: Path, archive_dir: Path, now: int) -> list[Path]` (newly written archive paths); `archive_info(gz: Path) -> dict | None` (`{"month", "arrivals", "forecast", "max_id"}` read from the file's `info` table, `None` if unreadable/corrupt).

**Security flag:** `none`

**Does NOT cover:** slimming the tar copy (Task 4); rewriting an existing archive (Task 6); months with zero rows (no file is written).

- [x] **Step 1: Failing tests** (`tests/backup/test_meta_archive.py`, helper builds an `open_meta` DB in `tmp_path` with arrivals/forecast rows for chosen months, explicit ids): `test_finished_months_respects_grace` (`now` = first of next month +1 day -> not finished; +2 days -> finished; the current month never); `test_archive_written_once_with_same_ids` (archive exists after the call, a second call returns `[]` and the file bytes are unchanged; open the gz: arrivals/forecast rows equal the live ones including `id`/`arrival_id`; `info` counts and `max_id` correct; forecast rows only for that month's arrivals); `test_archive_never_overwrites` (pre-existing different file with the same name is left untouched, warning logged); `test_archive_skips_empty_month_and_failures_do_not_raise` (monkeypatch `gzip`/rename to raise -> function logs and returns, temp file removed, no archive left behind); `test_archive_leaves_live_db_unchanged` (row counts and `pragma data_version`-independent content hash of the live tables identical before/after); `test_snapshot_writes_meta_archive_but_not_when_archive_dir_none`.
- [x] **Step 2:** FAIL.
- [x] **Step 3: Implement** in `meta_archive.py`: month selection with a read-only connection (`select distinct substr(service_date,1,7) from arrivals`); for each finished month without a final file: temp SQLite file in `archive_dir` (name `meta-YYYY-MM.db.tmp`), `attach`/copy rows `where service_date like 'YYYY-MM-%'` and `forecast where arrival_id in (select id ...)` in one read transaction, write `info`, `integrity_check`, gzip to `meta-YYYY-MM.db.gz.tmp`, verify by decompress + `_sha` equality, fsync, `os.replace` to the final name, fsync the dir; remove temps in `finally`. In `snapshot`, after the existing `archive_months(...)` call and inside the same try/except, call `archive_meta_months(data_dir, archive_dir, int(now))`.
- [x] **Step 4:** `tests/backup` - PASS.
- [x] **Step 5:** Commit `Archive finished months of arrivals and forecast once`.

### Task 4: Slim copy in the nightly tar

**Files:** Modify `app/backup/meta_archive.py`, `app/backup/backup.py`; Test `tests/backup/test_meta_archive.py`.

**Interfaces:**
- Consumes: `archive_info`, `META_ARCHIVE_RE`, `finished_months` (Task 3); `_copy_db`.
- Produces: `slim_meta_copy(copy: Path, archive_dir: Path | None, now: int) -> list[str]` (months removed from the staged copy; mutates only `copy`).

**Security flag:** `none`

**Does NOT cover:** restore (Task 5); the samples files (unchanged).

Behavior: always `delete from result_cache` in the copy. If `archive_dir` is None return `[]` after that. For each finished month in the copy with `archive_info(...)` present and `info.arrivals`/`info.forecast` equal to that month's counts in the COPY: delete its forecast rows then arrivals rows, insert into `archived_months(month TEXT PRIMARY KEY, arrivals INT, forecast INT)` (created if missing). Months failing any check stay whole, with `log.warning` naming month and reason. If any month was removed: `VACUUM` the copy. In `snapshot`, call it on the staged `meta.db` copy right after `_copy_db` and before `tar.add`, inside try/except so a failure there logs and falls back to the unslimmed copy (restore the copy by re-running `_copy_db`).

- [x] **Step 1: Failing tests:** `test_slim_removes_only_verified_months` (3 finished months archived, 1 unarchived finished month, current month: only the 3 are removed from the copy; `archived_months` lists them with counts; the unarchived and current months' rows intact); `test_slim_keeps_month_on_count_mismatch` (add one late arrival to the copy after archiving -> month kept whole, warning logged); `test_slim_keeps_month_when_archive_corrupt_or_missing` (truncate the gz / delete it); `test_slim_archive_dir_none_changes_only_result_cache`; `test_slim_empties_result_cache`; `test_snapshot_tar_meta_is_slim_and_live_db_untouched` (end to end through `snapshot` with fake `now`: extract the tar, `meta.db` inside lacks archived months and has `archived_months`; the live DB still has every row); `test_slim_failure_falls_back_to_full_copy` (monkeypatch `slim_meta_copy` to raise -> tar still produced with the full meta.db).
- [x] **Step 2:** FAIL.
- [x] **Step 3: Implement** as described; ids of remaining rows untouched.
- [x] **Step 4:** `tests/backup` - PASS.
- [x] **Step 5:** Commit `Leave archived months out of the nightly tar`.

### Task 5: Restore at startup and the round trip

**Files:** Modify `app/backup/meta_archive.py`, `app/main.py`, `Dockerfile`; Test `tests/backup/test_meta_archive.py`, `tests/backup/test_roundtrip.py`, `tests/test_main.py`.

**Interfaces:**
- Consumes: `archive_info`, the `archived_months` table (Task 4); `open_meta`; `app.cache.clear`.
- Produces: `restore_archived_months(data_dir: Path, archive_dir: Path | None) -> list[str]` (months imported this call); called from the lifespan before any background task starts.

**Security flag:** `none` (reads only files in the archive folder and `DATA_DIR`, named by a strict pattern, never a user-supplied path).

**Does NOT cover:** restoring samples months (existing manual step 3/4 of the README); restoring when the app is not started.

Behavior: for each row of `archived_months` (table absent -> return `[]`): locate `meta-YYYY-MM.db.gz` in `archive_dir` then `DATA_DIR`; missing -> `log.error` naming the month and file, keep the marker, continue. Found: decompress to a temp file, `integrity_check`, read `info`, then in ONE transaction on the live connection `insert or ignore into arrivals select ...` and `forecast`, verify the month's resulting row counts equal `info.arrivals`/`info.forecast`, delete the marker row, commit (any mismatch or error: roll back, `log.error`, keep the marker). After at least one import call `cache.clear` on a fresh `open_meta` connection. Idempotent. Lifespan: `await asyncio.to_thread(restore_archived_months, settings.data_dir, settings.archive_path)` at the very start of `lifespan`, wrapped so any exception is logged and never prevents startup. `Dockerfile`: `--start-period=15m`.

- [x] **Step 1: Failing tests:** `test_restore_noop_on_live_db`; `test_restore_imports_month_with_same_ids_and_clears_marker`; `test_restore_twice_is_noop`; `test_restore_missing_archive_logs_error_keeps_marker_and_app_starts` (then put the file back and a second call imports it); `test_restore_rolls_back_on_count_mismatch` (corrupt `info`) leaving no partial rows and the marker kept; `test_restore_finds_archive_in_data_dir_when_not_in_archive_dir`; `test_restore_clears_result_cache`; `test_lifespan_runs_restore_and_survives_exception` (monkeypatch to raise; `with TestClient(app)` starts). Round trip in `tests/backup/test_roundtrip.py`: `test_full_roundtrip_preserves_every_row` - seed 3 finished months + a current month with arrivals/forecast (explicit ids, interleaved month boundaries, a route filter mix), fake `now` after the month ends, run `snapshot` twice (archive written once, second tar slim), extract the newest tar into a new data dir, run `restore_archived_months` with the original archive dir, and assert every row of `arrivals` and `forecast` equals the original (full-table comparison including ids); then run it again (no change) and delete one archive file in a copy of the scenario to assert the loud-failure path.
- [x] **Step 2:** FAIL.
- [x] **Step 3: Implement.**
- [x] **Step 4:** `tests/backup`, `tests/test_main.py` - PASS.
- [x] **Step 5:** Commit `Restore archived months at startup`.

### Task 6: Day removal inside an archived month

**Files:** Modify `app/backup/meta_archive.py`, `app/maintenance/day.py`, `app/maintenance/jobs.py`, `app/maintenance/api.py`; Test `tests/backup/test_meta_archive.py`, `tests/maintenance/test_day.py`, `tests/maintenance/test_api.py`.

**Interfaces:**
- Consumes: `archive_info`, `META_ARCHIVE_RE`, `_sha`, `_integrity` helpers; existing `day.remove(data_dir, d, route, ...)`, `jobs.start(data_dir, days, route, lock)`, `_run(jid, data_dir, days, route, lock)` (read their current signatures first).
- Produces: `rewrite_archive_without(archive_dir: Path | None, d: date, route: str | None) -> bool` (True when an archive file was rewritten, False when there is none to rewrite; raises on a failure so the caller can abort); new optional keyword `archive_dir: Path | None = None` on `day.remove`, `jobs.start` and `jobs._run`; `maintenance/api.py` passes `request.app.state.settings.archive_path`.

**Security flag:** `none`

**Does NOT cover:** closed samples months (already immutable and skipped by the removal job).

Behavior: before the live rows of day `d` (and optional `route`) are deleted by `_remove`, call `rewrite_archive_without`: if `meta-YYYY-MM.db.gz` for `d`'s month exists, decompress to a temp file, delete the matching forecast rows then arrivals rows (`service_date = d.isoformat()` and, if given, `route = ?`), update `info` counts and `max_id`, `integrity_check`, gzip, atomic replace over the old file (temp + fsync + rename). If it raises, the removal job ends in its existing `error` state BEFORE any live row of that day is deleted. `archive_dir=None` or no file for the month: no-op. Rollup/cache handling already in `_remove` is unchanged.

- [x] **Step 1: Failing tests:** `test_rewrite_removes_day_rows_and_updates_info` (archive with 3 days, remove one: the gz has the other days intact, counts/max_id updated, forecast rows of the removed arrivals gone); `test_rewrite_respects_route_filter`; `test_rewrite_no_archive_returns_false`; `test_rewrite_failure_raises_and_leaves_old_archive_intact`; `test_remove_day_rewrites_archive_before_live_delete` (order asserted by monkeypatching the rewrite to record whether live rows still exist when it runs); `test_remove_day_aborts_if_archive_rewrite_fails` (live rows still present, job state `error`); `test_removed_day_not_resurrected_by_restore` (archive a month, remove a day through the job, snapshot, restore into a fresh dir: the removed day's rows are absent); `test_api_passes_archive_path`.
- [x] **Step 2:** FAIL.
- [x] **Step 3: Implement.**
- [x] **Step 4:** `tests/backup`, `tests/maintenance`, `tests/test_main.py` - PASS.
- [x] **Step 5:** Commit `Keep the month archive consistent when a day is removed`.

### Task 7: Docs, real-data check, version

**Files:** Modify `README.md`, `pyproject.toml`, `docker-compose.release.yml`; Create `docs/perf/2026-11-16-smaller-backups.md`.

**Interfaces:** Consumes everything. Produces: nothing.

**Security flag:** `none`

- [ ] **Step 1 (README):** rewrite the "Data and backups" paragraph and restore steps for: `.tar.gz` names (old `.tar` still restorable), the retention rule (`BACKUP_KEEP` newest + `BACKUP_KEEP_WEEKLY` weekly), the finished-month archive (`meta-YYYY-MM.db.gz`, what a slim tar contains, that the live database is unchanged), restore step 4 gaining "put the archive folder back next to/under `DATA_DIR` (or leave it where it is) before starting; the app imports archived months at startup and logs an ERROR if a file is missing", the one-way note (an older version cannot restore a slim backup), the long-import note (health start period 15 min), and that removing a day inside an archived month rewrites the archive. Run `tests/test_docs.py`.
- [ ] **Step 2 (version):** `pyproject.toml` version `0.6.0`; `docker-compose.release.yml` comment and image tag (replicate exactly what the 0.5.0 release commit changed: `git show 0e07b30 -- pyproject.toml docker-compose.release.yml`).
- [ ] **Step 3 (real data):** copy the scratchpad `bk` directory to a NEW directory (never touch the original `bk`/`run`/`bench`); with `ulimit -v 5000000` run a small script that calls `snapshot(copy, backup_dir, keep=3, archive_dir=..., keep_weekly=4, now=<epoch of 2026-12-04 UTC>)` so November counts as finished, then: report tar sizes (a plain `.tar` made the old way for comparison, the new slim `.tar.gz`, and the archive file), extract the new tar into a third directory, run `restore_archived_months`, and verify `select count(*)`/a content hash of `arrivals` and `forecast` equal the original copy. Write the numbers to `docs/perf/2026-11-16-smaller-backups.md` (what was measured, sizes before/after, round-trip result, caveats: faster machine than the Pi, one snapshot timing).
- [ ] **Step 4 (full suite):** per-directory runs (`tests/analysis tests/backup tests/cache tests/collect tests/core tests/maintenance tests/rollup tests/test_docs.py tests/test_main.py tests/test_seed_demo.py tests/api tests/export`) and `cd web && node --test`; all pass.
- [ ] **Step 5:** Commit `Release 0.6.0: smaller backups`.
- [ ] **Step 6:** Final whole-branch review via `requesting-code-review` with the Review Focus above (sonnet), fix round if needed, then `finishing-a-development-branch` (merge/tag/push only on the user's request).
