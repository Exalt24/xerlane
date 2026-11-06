"""The command line, run as a real process: output a person reads, and exit codes a script can branch on."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TENANT
from xerlane import db

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, env_extra: dict | None = None):
    import os
    env = {**os.environ, **(env_extra or {})}
    p = subprocess.run([sys.executable, "-m", "xerlane", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
    return p.returncode, p.stdout, p.stderr


def test_load_validate_reconcile_diff_on_a_clean_file(clean, xer):
    path, _ = xer(tasks=80)
    code, out, _ = run("load", path, "--tenant", TENANT)
    assert code == 0 and "LOADED" in out and "quarantined 0" in out and "sha256" in out
    code, out, _ = run("load", path, "--tenant", TENANT)
    assert code == 0 and "UNCHANGED" in out
    code, out, _ = run("validate", path, "--tenant", TENANT)
    assert code == 0 and "before load  0 errors" in out and "after load   0 errors" in out
    code, out, _ = run("reconcile", path, "--tenant", TENANT)
    assert code == 0 and "RECONCILED" in out and "TASK" in out
    code, out, _ = run("diff", path, "--tenant", TENANT)
    assert code == 0 and "0 change(s)" in out


def test_exit_codes_for_each_outcome(clean, xer):
    ok, _ = xer(tasks=60, name="ok.xer")
    bad, _ = xer(tasks=60, defects=["dup_task_code"], name="bad.xer")
    cut, _ = xer(tasks=60, defects=["truncated"], name="cut.xer")
    assert run("validate", ok)[0] == 0
    assert run("validate", bad)[0] == 1
    assert run("validate", cut)[0] == 1
    assert run("load", cut, "--tenant", TENANT)[0] == 2
    assert run("load", bad, "--tenant", TENANT)[0] == 1
    assert run("load", ok, "--tenant", TENANT)[0] == 0
    assert run("reconcile", bad, "--tenant", TENANT)[0] == 1, "the loaded file is ok, so bad.xer (different content) must not reconcile"
    assert run("load", str(Path(ok).with_name("missing.xer")), "--tenant", TENANT)[0] == 2
    assert run()[0] == 2, "no command at all is a usage error"


def test_load_prints_the_quarantined_rows_with_line_and_reason(clean, xer):
    path, planted = xer(tasks=60, defects=["cycle"])
    code, out, _ = run("load", path, "--tenant", TENANT)
    assert code == 1 and f"line {planted[0].line} TASKPRED: CYCLE" in out and "dependency cycle:" in out


def test_diff_prints_the_change_report_and_exits_1(clean, xer, tmp_path):
    import make_sample_xer as gen
    tables = gen.build_tables(4, 60)
    p1 = tmp_path / "v1.xer"
    p1.write_bytes(gen.render(tables))
    T = {t.name: t for t in tables}["TASK"]
    T.rows[2][T.fields.index("early_end_date")] = "2028-01-03 15:00"
    del T.rows[9]
    p2 = tmp_path / "v2.xer"
    p2.write_bytes(gen.render(tables))
    assert run("load", str(p1), "--tenant", TENANT)[0] == 0
    code, out, _ = run("diff", str(p2), "--tenant", TENANT)
    assert code == 1 and "FINISH_MOVED" in out and "2028-01-03 15:00" in out and "TASK_REMOVED" in out
    code, out, _ = run("load", str(p2), "--tenant", TENANT)
    assert "change report:" in out and "finish_moved 1" in out


def test_diff_for_a_project_not_stored_yet_says_so(clean, xer):
    path, _ = xer(tasks=30)
    code, out, _ = run("diff", path, "--tenant", TENANT)
    assert code == 1 and "not stored yet" in out


def test_reconcile_failure_prints_the_difference(clean, xer, admin):
    path, _ = xer(tasks=60)
    run("load", path, "--tenant", TENANT)
    admin.execute("update taskrsrc set target_cost = target_cost + 0.01 where tenant_id = %s and taskrsrc_id = (select min(taskrsrc_id) from taskrsrc where tenant_id = %s)", (TENANT, TENANT))
    code, out, _ = run("reconcile", path, "--tenant", TENANT)
    assert code == 1 and "DIFFERENCE SUM TASKRSRC: target_cost" in out and "DIFFERENCE(S) FOUND" in out


def test_tenant_is_required_and_the_database_must_be_reachable(clean, xer):
    path, _ = xer(tasks=30)
    assert run("load", path)[0] == 2
    code, _, err = run("load", path, "--tenant", TENANT, "--dsn", db.retarget(db.default_dsn(), port="1"))
    assert code == 2 and err.startswith("xerlane: ") and ("Timeout" in err or "OperationalError" in err)


def test_a_superuser_dsn_is_refused_by_the_cli(clean, xer):
    path, _ = xer(tasks=30)
    code, _, err = run("load", path, "--tenant", TENANT, "--dsn", clean)
    assert code == 2 and "UnsafeRole" in err


def test_init_db_can_be_run_twice(clean):
    assert run("init-db")[0] == 0 and run("init-db")[0] == 0


def test_help_lists_every_command():
    code, out, _ = run("--help")
    assert code == 0 and all(c in out for c in ("init-db", "load", "validate", "reconcile", "diff"))
