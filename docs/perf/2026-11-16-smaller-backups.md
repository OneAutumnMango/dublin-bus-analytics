# Smaller backups: measurements (2026-11-16)

## What was measured

A COPY of the real backup from Nov 16 (`meta.db` 121 MB with 346,834 arrivals and 1,634,626 forecast rows; `samples-2026-11.db` 311 MB; about 5 days, 10 routes), copied to a scratch folder. The original backup and every earlier copy were left alone. `snapshot` ran with the clock set to 2026-12-04, so November counted as a finished month (it is still the current month in real life, so nothing has been archived on the real system yet). Measured on a dev machine that is faster than the Raspberry Pi, one run each.

## Results

| | Size |
|---|---|
| Plain tar the old way (`meta.db` + current samples, uncompressed) | 433.6 MB |
| New nightly backup `analytics-<stamp>.tar.gz` on the night the month is archived | 68.8 MB |
| One-time archive file `meta-2026-11.db.gz` (arrivals and forecast of the month) | 36.9 MB |
| `meta.db` inside the new tar (arrivals 0, forecast 0, marker row `2026-11: 346834 / 1634626`) | 0.9 MB |

- `snapshot` took 53 s here, including building and verifying the archive, slimming the copy and gzipping the tar.
- Restore into an empty folder: the app's startup import put the month back in 20 s.
- **Round trip:** every row of `arrivals` and `forecast` in the restored database matches the original, including ids (row counts 346,834 and 1,634,626 and a SHA-256 over every row in id order); a second restore did nothing.
- The live database was byte-for-byte equivalent in content after the snapshot.

## What to expect in normal nightly use (estimate, not measured end to end)

gzip level 3 measured 4.5x on the samples file and 2.6x on `meta.db`, so a nightly tar in the middle of a month (current month's samples plus the whole meta.db not yet archived) is roughly 115 MB for this data instead of 434 MB. Once a month is finished and archived, later tars carry only the current month: the samples part (about 69 MB here) plus a small `meta.db`. With 3 daily plus 4 weekly backups kept, the retained nightly files stay at 7 in total instead of growing without bound with `meta.db`.

## Caveats

- Archive and tar timings are from a faster machine than the Pi; building the archive for a large month needs about 1.6-1.8 GB of free space in the archive folder while it runs.
- The first real archive will be made after the end of November (about 2 days after, by design), so the real-data run above used a faked clock.
- Removing a range of days inside one archived month rewrites that month's archive once per day (slow for a big month); a deferred improvement is to rewrite once per month per job.
