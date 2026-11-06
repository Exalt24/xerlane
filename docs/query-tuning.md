# Query tuning: what was measured and what was left out

The earned value report asks the database three questions (`TASK_SQL`, `NO_TASK_SQL` and `CODE_SQL` in `xerlane/evm.py`; the web app's `web/server/utils/ev.ts` asks the same ones). This page records how they were checked and one decision that came out of it.

## How

`python tools/explain_report.py --projects 8 --tasks 40000 --plans` builds a database of its own, loads an invented schedule of 8 projects and 40,000 tasks for one tenant, loads a generated cost export (28,024 lines) and commitment export (237 rows), runs `ANALYZE`, then times each statement 15 times and prints `EXPLAIN (ANALYZE, BUFFERS)`. It runs as the table owner under forced row level security, so the plans include the tenant policy. It does this twice: without secondary indexes, and with three candidates (`cost_line (tenant_id, proj_id, task_code)`, `cost_line (tenant_id, proj_id, cost_code)` and `commitment (tenant_id, proj_id, cost_code)`).

## What it showed (one run on 2026-11-07, Windows 11, PostgreSQL 17 in Docker, one project of 5,000 tasks and 3,482 cost lines)

| Statement | Median without the indexes | Median with them | Buffers hit, without and with |
|---|---|---|---|
| tasks (BAC, PV, EV and AC per task) | 13.88 ms | 12.93 ms | 266 and 251 |
| no_task (cost lines whose task is gone) | 1.56 ms | 2.19 ms | 266 and 251 |
| codes (budget, committed and invoiced per cost code) | 1.24 ms | 1.56 ms | 74 and 74 |

Every plan, with and without, starts the same way: a bitmap scan on the `(tenant_id, proj_id, ...)` prefix of the table's own key (`cost_line_pkey`, `task_tenant_id_proj_id_task_code_key`) that returns only the one project's rows, then a hash join or hash aggregate over a few thousand rows. The candidate indexes change which index the scan reads and save about 15 buffer reads out of 266. The two faster-looking numbers are inside run to run noise, and two of the three statements got slower.

## The decision

The schema ships no secondary index on `cost_line` or `commitment`. The primary key `(tenant_id, proj_id, line_id)` already makes the project filter an index scan, a secondary index would be written on every load of a 28,000 line export for a gain the measurement does not show, and an index that cannot be shown to help is a cost.

## What would change it

- A project with a cost export two orders of magnitude larger than this one, where the bitmap heap scan on the project's rows stops fitting in cache. Run the tool with larger `--tasks` and read the plans again.
- A report statement that filters on `task_code` or `cost_code` without the project, which these three do not.

## Limits

One machine, one run, generated data, a warm cache. The numbers are for comparing the two variants on the same data, not a claim about any other system.
