"""Time and explain the statements behind the earned value report, with and without three candidate secondary indexes (the schema ships none, see docs/query-tuning.md).

    python tools/explain_report.py --projects 8 --tasks 40000

It makes a database of its own (xerlane_tune), loads an invented schedule of several projects for one tenant, writes a cost export and a commitment export that
fit it, loads both, then for each statement of xerlane/evm.py prints the median of several runs and the plan, first without the candidate indexes
(two on cost_line, one on commitment), then with them. docs/query-tuning.md quotes one run.
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from xerlane import cost, db, evm  # noqa: E402
from xerlane.load import load_file  # noqa: E402

import make_sample_cost as gen_cost  # noqa: E402
import make_sample_xer as gen_xer  # noqa: E402

NAME = "xerlane_tune"
TENANT = "acme"
INDEXES = {"cost_line_task": "cost_line (tenant_id, proj_id, task_code)", "cost_line_code": "cost_line (tenant_id, proj_id, cost_code)", "commitment_code": "commitment (tenant_id, proj_id, cost_code)"}
STATEMENTS = {"tasks": evm.TASK_SQL, "no_task": evm.NO_TASK_SQL, "codes": evm.CODE_SQL}


def build(projects: int, tasks: int, workdir: Path) -> str:
    base = db.default_admin_dsn()
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{NAME}"')
    admin = db.retarget(base, dbname=NAME)
    db.init_db(admin)
    data, _ = gen_xer.make(11, tasks, projects, [], 1000, progress_hours=600)
    xer = workdir / "plan.xer"
    xer.write_bytes(data)
    conn = db.connect(db.retarget(db.default_dsn(), dbname=NAME))
    t0 = time.monotonic()
    load_file(conn, TENANT, str(xer))
    print(f"loaded {projects} projects, {tasks} tasks in {time.monotonic() - t0:.1f} s")
    rows_cost, rows_commit = gen_cost.make(str(xer), 5, [])
    import csv
    import io
    for rows, kind in ((rows_cost, "cost"), (rows_commit, "commitment")):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(rows)
        r = cost.load_bytes(conn, TENANT, kind, buf.getvalue().encode(), f"{kind}.csv")
        print(f"loaded {kind}: {r.status}, {r.stored} rows, {len(r.quarantined)} set aside")
    conn.close()
    return admin


def measure(admin: str, runs: int) -> dict[str, tuple[float, str]]:
    out = {}
    with psycopg.connect(admin) as c:
        c.execute("SET search_path = xerlane")
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
        c.execute("SET ROLE xerlane_owner")                 # forced row level security applies to the owner too, so the plans include the tenant policy
        c.execute("ANALYZE")
        for name, stmt in STATEMENTS.items():
            params = {"tenant": TENANT, "proj": 1010}
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                c.execute(stmt, params).fetchall()
                times.append((time.perf_counter() - t0) * 1000)
            plan = "\n".join(r[0] for r in c.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) " + stmt, params).fetchall())
            out[name] = (statistics.median(times), plan)
        c.rollback()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", type=int, default=8)
    ap.add_argument("--tasks", type=int, default=40000)
    ap.add_argument("--runs", type=int, default=15)
    ap.add_argument("--plans", action="store_true", help="print every plan")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        admin = build(a.projects, a.tasks, Path(tmp))
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute("SET search_path = xerlane")
            for ix in INDEXES:
                c.execute(f"DROP INDEX IF EXISTS {ix}")
            without = measure(admin, a.runs)
            for ix, spec in INDEXES.items():
                c.execute(f"CREATE INDEX {ix} ON {spec}")
            with_ix = measure(admin, a.runs)
        print(f"\n{'statement':10} {'without indexes':>18} {'with indexes':>14}   (median of {a.runs} runs, milliseconds)")
        for name in STATEMENTS:
            print(f"{name:10} {without[name][0]:>18.2f} {with_ix[name][0]:>14.2f}")
        if a.plans:
            for name in STATEMENTS:
                print(f"\n--- {name}, without the indexes\n{without[name][1]}\n--- {name}, with the indexes\n{with_ix[name][1]}")
    finally:
        with psycopg.connect(db.default_admin_dsn(), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
