# Smaller backups: gzipped tars, weekly retention, finished-month archive of arrivals and forecast

Date: 2026-11-16. Status: draft for user review. Target version 0.6.0.

## Problem

Every nightly backup tar holds the whole `meta.db` (arrivals + forecast, kept forever, about 24 MB/day) and the
current month's raw `samples-*.db` (about 62 MB/day), both uncompressed, and 7 tars are kept. Measured on the
user's Nov 16 backup (5 days, 10 routes): 450 MB per tar, about 3 GB for 7. Extrapolated: about 2.6 GB per tar late in
a month, and `meta.db` alone about 9 GB after a year, so 7 tars would exceed 60 GB. Finished `samples` months are
already gzipped and archived once (README "Data and backups"); finished months of arrivals/forecast are not: they are
re-copied in every nightly tar forever. gzip measured: samples 4.5x smaller, meta.db 2.6x smaller.

## Goals

1. Nightly tars are gzipped (`.tar.gz`).
2. Retention: newest N nightly backups plus one per week for the previous M weeks.
3. A finished month of arrivals+forecast is archived once into a gzipped file and left out of the nightly tars.
4. Existing `.tar` backups, the existing archive folder and existing settings keep working. A one-way path is
   accepted: an OLDER app version cannot restore a new slim backup (user decision, 2026-11-16).

## Non-goals

- Splitting arrivals/forecast into monthly database files (the live `meta.db` is not changed or shrunk).
- Incremental (row-delta) backups; off-host copying (still the user's job, as in the README).
- Changing how closed `samples` months work (they stay immutable; day removal already skips them).
- UI changes beyond the downloaded file name.

## Design

### A. Gzipped tars

`snapshot` writes `analytics-<UTC stamp>[_NNN].tar.gz` (`tarfile` mode `w:gz`, `compresslevel=3`: measured 4.5x on
samples and 2.6x on meta at that level, cheap enough for a Pi at 03:00). The name pattern in `backup.py` accepts
both `.tar` and `.tar.gz`; `list_backups`, `newest_backup`, pruning, the download endpoint (content type
`application/gzip` for `.tar.gz`) and the stale-file cleanup handle both. Old plain `.tar` files stay listed,
downloadable, restorable and are pruned by the same retention. `tar -xf` auto-detects gzip, so the README restore
step is unchanged.

### B. Retention

`BACKUP_KEEP` (newest N, default 3, was 7) and new `BACKUP_KEEP_WEEKLY` (default 4, `0` disables). After a snapshot,
keep: the newest N backups; and for each of the previous M ISO weeks (UTC, counted back from the week of the newest
backup) the newest backup in that week that is not already kept. Delete the rest. Unchanged safety rules: never delete
the file just made; future-dated files are ignored. An existing deployment that sets `BACKUP_KEEP` explicitly (both
compose files do: 7) keeps its value; the compose files and `analytics.env.example` get the new variable and the new
defaults.

### C. Finished-month archive of arrivals and forecast

A month M (`YYYY-MM`, by `arrivals.service_date`) is finished when `now >= first day of the next month 00:00 UTC +
COMPACT_GRACE_S` (2 days, same rule as the samples compaction).

**Writing the archive.** `archive_meta_months(data_dir, archive_dir, now)` runs inside `snapshot` after the existing
`archive_months` call, with the same isolation (an exception is logged and never fails the snapshot). For each
finished month that has rows and no `meta-YYYY-MM.db.gz` in `archive_dir`: read the live `meta.db` read-only, write a
temporary SQLite file holding tables `arrivals` and `forecast` (same columns, SAME `id` and `arrival_id` values) for
that month and a table `info(month, arrivals, forecast, max_id)`; run `integrity_check`; gzip it; verify by
decompressing and comparing the SHA; write temp + fsync + rename; never overwrite an existing archive file.

**Slim copy for the tar.** After the existing `_copy_db` of `meta.db` into staging, for every month with an archive
file whose recorded `arrivals` and `forecast` counts equal that month's counts in the COPY: delete that month's
forecast rows and arrivals rows from the copy, and record the month in a new table `archived_months(month, arrivals,
forecast)` inside the copy. Also empty `result_cache` (derived data). Then `VACUUM` the copy and tar it as `meta.db`.
The live `meta.db` is never modified by backups. The `ru_*` rollup tables are kept (small, derived, consistent).
Safeguards, each with a test: no archive file, archive disabled (`BACKUP_ARCHIVE_DIR` blank), or a count mismatch
(late rows written after archiving) means that month is NOT slimmed (the tar keeps it whole) and a warning is logged.

**Restore.** README restore step 4 gains: put the archive folder back (or copy `meta-*.db.gz` next to the databases).
At startup, before the poller and scheduler start, `restore_archived_months(data_dir, archive_dir)` runs: for each
row in `archived_months` of `meta.db` it finds `meta-YYYY-MM.db.gz` (archive folder first, then `DATA_DIR`), decompresses
to a temp file, checks `integrity_check` and the `info` counts, and in ONE transaction does `insert or ignore` of the
arrivals and forecast rows, verifies the resulting row counts for that month equal the `info` counts, deletes the
marker row and commits; it then clears the result cache. Idempotent and crash-safe; a no-op on a live database
(the table only exists in restored slim copies). A missing or damaged archive file is logged at ERROR naming the
month and file, the marker is kept (so it retries at every start), and the app still starts. The import is
synchronous at startup (one month at a time); the Dockerfile `HEALTHCHECK` `--start-period` rises from 10 s to 15 min
so a long import on a Pi is not flagged unhealthy.

**Removing a day inside an archived month.** The day-removal job, for a day in a month that has an archive file, first
rewrites that archive without the matching rows (route filter respected; decompress to temp, delete forecast then
arrivals rows, update `info`, `integrity_check`, gzip, atomic replace) and only then deletes the live rows. If the
rewrite fails the job stops with an error before touching the live data. Without this a later restore would bring the
deleted data back. The archive path reaches the job through the app settings (`Settings.archive_path`).

### D. Backwards compatibility

- Old `.tar` backups and old archive folders (only samples `.gz`) restore exactly as before; a full `meta.db` has no
  `archived_months` table so nothing is imported.
- Existing env vars keep their meaning; `BACKUP_KEEP` default changes only for deployments that do not set it.
- One way: an older app version restoring a slim tar silently lacks the archived months (documented in the README).

### E. Docs and rollout

README ("Data and backups", env table, restore steps, the one-way note), both compose files, `analytics.env.example`,
`tests/test_docs.py` parity, version 0.6.0. No schema change to the live database.

## Testing

1. gzip: new snapshot is `.tar.gz` and extractable; plain `.tar` and `.tar.gz` coexist in listing, newest, pruning and
   download.
2. Retention: table-driven cases for N and M with fake stamps (daily set, weekly picks across ISO week boundaries and
   year end, `BACKUP_KEEP_WEEKLY=0`, never deleting the new file, ignoring future-dated files).
3. Archive: a month is archived only when finished (fake `now`), once, never overwritten; archive row/id fidelity.
4. Slim copy: tar's `meta.db` lacks archived months and has `archived_months`; live DB unchanged; unarchived or
   mismatched months are kept whole; archive disabled means no slimming; `result_cache` empty in the tar.
5. Round trip (the key test): seed 3 months, snapshot with a fake clock, extract the tar into a new dir, start the
   restore, and compare every arrivals and forecast row with the original (including ids). Re-running restore is a
   no-op. Missing archive: month absent, ERROR logged, marker kept, app starts, then restore succeeds once the file
   is put back.
6. Day removal inside an archived month: archive rewritten first; restore afterwards does not bring the day back; a
   failing rewrite aborts before any live deletion.
7. Real data: run on a copy of the user's Nov 16 backup with a faked `now` after the month end; report tar sizes
   before and after and verify the round trip.

## Failure modes checked

- Critical, handled: slimming without a verified archive (would lose data) -> only slim months whose archive exists
  and whose counts match; tested. Restore with a missing archive silently losing a month -> ERROR log, marker kept,
  retry every start. Day removal resurrected by restore -> archive rewritten first. Late rows after archiving ->
  count check keeps the month whole.
- Minor, accepted: the archive folder sits on the same disk by default (README already tells users to copy it
  elsewhere); a very long import on a Pi (health start period raised); the old samples-archive behaviour is
  unchanged; backups still copy the whole current month plus a slim `meta.db` nightly.
