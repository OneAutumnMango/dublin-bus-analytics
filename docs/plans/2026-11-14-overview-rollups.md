# Overview rollups plan

Spec: `docs/specs/2026-11-14-overview-rollups-design.md`. TDD, commit after each task. Run tests with
`ulimit -v 2500000; .venv/bin/python -m pytest -q <path>` (host RAM is tight).

1. **Contention gate**: single-permit `_gate("all")` inside `_each_tracked`. Test: three concurrent `/all/*` calls never overlap their scans. Commit.
2. **`app/rollup.py` storage**: schema in `core/db.py` META_SCHEMA; `closed_cutoff(today)`, `build_day(c, route, day)`, `ensure_route(c, route, today)`, `drop_days(c, days, route=None)`. Tests (tmp db): build writes expected sums/hist/heat/fc; idempotent; `drop_days`; open day not built. Commit.
3. **Merge/summary maths** in `app/rollup.py`: `Acc` (n, s, s2, hist) with `add_delays(array)`, `add_blob`, `summary(p)`; `rolled_days(c, route, p)`. Parity test vs `_summ` on random delays incl. clamp, empty, n=1. Commit.
4. **`/all/routes` wiring** (eligible path + raw remainder). Parity test: API response with rollups equals response computed raw (rollup disabled) for random filters (since mid-day, until, confidence, window). Commit.
5. **`/all/heatmap` wiring**, weekday/hour filters allowed. Parity test. Commit.
6. **`/all/forecast` wiring**. Parity test. Commit.
7. **`remove_day` drops rollups**; test day removal then `/all/routes` has no ghost data. Commit.
8. **Benchmark** on the real copy and a 10x synthetic copy (scratch dir), baseline from `git stash`/main vs branch; table to user. Update README if it documents performance. Final review (`requesting-code-review`), `verification-before-completion`.
