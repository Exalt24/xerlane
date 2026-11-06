"""Reconcile: it passes on a faithful load (positive control) and fails, by name, every time the stored data is damaged by one row, one cent or one character."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import TENANT
from xerlane.load import load_file
from xerlane.reconcile import reconcile

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def loaded(conn, xer, admin):
    path, _ = xer(seed=21, tasks=100, defects=["bad_number", "orphan_assignment"])           # a file with quarantined rows, so the quarantine paths are exercised too
    load_file(conn, TENANT, path)
    return conn, path, admin


def checks(result):
    return {f.check for f in result.findings}


def test_positive_control_a_faithful_load_reconciles(loaded):
    conn, path, _ = loaded
    r = reconcile(conn, TENANT, path)
    assert r.ok, [str(f) for f in r.findings]
    by = {ln.table: ln for ln in r.lines}
    assert by["TASK"].file_rows == 100 and by["TASK"].stored == 99 and by["TASK"].quarantined == 1
    assert by["TASK"].file_rows == by["TASK"].stored + by["TASK"].quarantined
    assert by["TASKRSRC"].sums_checked == 8 and by["TASK"].sampled == 99


def test_clean_file_reconciles(conn, xer):
    path, _ = xer(seed=3, tasks=200, projects=2)
    load_file(conn, TENANT, path)
    r = reconcile(conn, TENANT, path)
    assert r.ok and all(ln.quarantined == 0 for ln in r.lines)


DAMAGE = {
    # name: (sql run as the administrator, checks that must fire)
    "one row removed": ("delete from taskrsrc where tenant_id = %(t)s and taskrsrc_id = (select min(taskrsrc_id) from taskrsrc where tenant_id = %(t)s)", {"COUNT", "SUM", "ROW"}),
    "one row added": ("insert into taskrsrc (tenant_id, taskrsrc_id, task_id, proj_id, target_cost) select tenant_id, 999999, task_id, proj_id, 0 from taskrsrc where tenant_id = %(t)s limit 1", {"COUNT"}),
    "one cent": ("update taskrsrc set target_cost = target_cost + 0.01 where tenant_id = %(t)s and taskrsrc_id = (select min(taskrsrc_id) from taskrsrc where tenant_id = %(t)s)", {"SUM", "ROW"}),
    "one cent less": ("update taskrsrc set act_reg_cost = act_reg_cost - 0.01 where tenant_id = %(t)s and taskrsrc_id = (select min(taskrsrc_id) from taskrsrc where tenant_id = %(t)s)", {"SUM", "ROW"}),
    "one character": ("update task set task_name = overlay(task_name placing 'Z' from 1 for 1) where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"ROW"}),
    "one character appended": ("update task set task_code = task_code || 'x' where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"ROW"}),
    "one minute on a date": ("update task set early_end_date = early_end_date + interval '1 minute' where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"ROW"}),
    "one hour of float": ("update task set total_float_hr_cnt = total_float_hr_cnt + 1 where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"SUM", "ROW"}),
    "a value blanked": ("update task set status_code = null where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"ROW"}),
    "an unmodeled column changed": ("update task set extra = jsonb_set(extra, '{guid}', '\"other\"') where tenant_id = %(t)s and task_id = (select min(task_id) from task where tenant_id = %(t)s)", {"ROW"}),
    "a relationship type changed": ("update taskpred set pred_type = 'PR_SS' where tenant_id = %(t)s and pred_type = 'PR_FS' and task_pred_id = (select min(task_pred_id) from taskpred where tenant_id = %(t)s and pred_type = 'PR_FS')", {"ROW"}),
    "a lag changed": ("update taskpred set lag_hr_cnt = lag_hr_cnt + 8 where tenant_id = %(t)s and task_pred_id = (select min(task_pred_id) from taskpred where tenant_id = %(t)s)", {"SUM", "ROW"}),
    "a project date changed": ("update project set plan_end_date = plan_end_date + interval '1 day' where tenant_id = %(t)s", {"ROW"}),
    "a calendar renamed": ("update calendar set clndr_name = clndr_name || '.' where tenant_id = %(t)s and clndr_id = (select min(clndr_id) from calendar where tenant_id = %(t)s)", {"ROW"}),
    "a quarantined row edited by one character": ("update quarantine set raw = jsonb_set(raw, '{1}', '\"tampered\"') where tenant_id = %(t)s and table_name = 'TASK'", {"QUARANTINE"}),
    "a quarantined row deleted": ("delete from quarantine where tenant_id = %(t)s and table_name = 'TASK'", {"COUNT", "SUM"}),
    "a quarantine row for a line that is not a row": ("update quarantine set line_no = 3 where tenant_id = %(t)s and table_name = 'TASKRSRC'", {"QUARANTINE", "COUNT"}),
}


@pytest.mark.parametrize("name", sorted(DAMAGE))
def test_damage_is_found_by_name(loaded, name):
    conn, path, admin = loaded
    assert reconcile(conn, TENANT, path).ok, "control: it passes before the damage"
    sql, expected = DAMAGE[name]
    cur = admin.execute(sql, {"t": TENANT})
    assert cur.rowcount >= 1, "the damage statement must change something, or this test proves nothing"
    r = reconcile(conn, TENANT, path)
    assert not r.ok, f"{name}: reconcile still passed"
    assert expected <= checks(r), f"{name}: expected {expected}, got {checks(r)}: {[str(f) for f in r.findings][:3]}"


def test_a_single_character_is_found_in_a_large_table_when_sampling_covers_the_row(conn, xer, admin):
    path, _ = xer(seed=4, tasks=400)
    load_file(conn, TENANT, path)
    admin.execute("update task set task_name = task_name || '!' where tenant_id = %s and task_id = (select max(task_id) from task where tenant_id = %s)", (TENANT, TENANT))
    assert reconcile(conn, TENANT, path, sample=10_000).findings
    assert reconcile(conn, TENANT, path, sample=400).findings


def test_sampling_is_seeded_so_a_rerun_samples_the_same_rows(conn, xer, admin):
    path, _ = xer(seed=4, tasks=400)
    load_file(conn, TENANT, path)
    admin.execute("update task set task_name = task_name || '!' where tenant_id = %s", (TENANT,))
    a = [str(f) for f in reconcile(conn, TENANT, path, sample=20, seed=5).findings]
    b = [str(f) for f in reconcile(conn, TENANT, path, sample=20, seed=5).findings]
    assert a == b and len(a) == 20


def test_a_different_file_does_not_reconcile(conn, xer):
    a, _ = xer(seed=1, tasks=80, name="a.xer")
    b, _ = xer(seed=2, tasks=80, name="b.xer")
    load_file(conn, TENANT, a)
    assert reconcile(conn, TENANT, a).ok
    assert not reconcile(conn, TENANT, b).ok


def test_file_edited_after_the_load_does_not_reconcile(conn, xer):
    path, _ = xer(seed=1, tasks=80)
    load_file(conn, TENANT, path)
    data = Path(path).read_bytes()
    Path(path).write_bytes(data.replace(b"Activity 5 ", b"Activity 6 ", 1))
    assert not reconcile(conn, TENANT, path).ok


def test_nothing_loaded_does_not_reconcile(conn, xer):
    path, _ = xer(seed=1, tasks=80)
    r = reconcile(conn, TENANT, path)
    assert not r.ok and "COUNT" in checks(r)


def test_a_truncated_file_never_reconciles(conn, xer):
    path, _ = xer(seed=1, tasks=80, defects=["truncated"])
    r = reconcile(conn, TENANT, path)
    assert not r.ok and "FILE" in checks(r)


def test_a_file_without_header_never_reconciles(conn, tmp_path):
    p = tmp_path / "x.xer"
    p.write_bytes(b"nope\r\n%E\r\n")
    assert checks(reconcile(conn, TENANT, str(p))) == {"FILE"}


def test_unmodeled_tables_are_noted_not_failed(loaded):
    conn, path, _ = loaded
    r = reconcile(conn, TENANT, path)
    assert any("CURRTYPE" in n for n in r.notes) and r.ok


def test_reconcile_does_not_import_the_parser_or_the_loader():
    """Independence is a property of the code, so it is tested on the code."""
    tree = ast.parse((ROOT / "xerlane" / "reconcile.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported.update(f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = {"xerlane.parse", "xerlane.load", "xerlane.validate", ".parse", ".load", ".validate"}
    assert not any(i in banned or i.startswith(tuple(b + "." for b in banned)) for i in imported), imported


def test_reconcile_only_reads(loaded, admin):
    conn, path, _ = loaded
    from conftest import snapshot
    before = snapshot(admin, TENANT)
    reconcile(conn, TENANT, path)
    assert snapshot(admin, TENANT) == before


def test_the_default_sample_is_three_hundred_rows_per_table(conn, xer):
    path, _ = xer(seed=3, tasks=400)
    load_file(conn, TENANT, path)
    by = {ln.table: ln for ln in reconcile(conn, TENANT, path).lines}
    assert by["TASK"].sampled == 300 and by["TASKPRED"].sampled == 300 and by["PROJWBS"].sampled == by["PROJWBS"].file_rows
    by = {ln.table: ln for ln in reconcile(conn, TENANT, path, sample=7).lines}
    assert by["TASK"].sampled == 7


def test_the_default_seed_is_zero(conn, xer, admin):
    path, _ = xer(seed=4, tasks=400)
    load_file(conn, TENANT, path)
    admin.execute("update task set task_name = task_name || '!' where tenant_id = %s", (TENANT,))
    a = [str(f) for f in reconcile(conn, TENANT, path, sample=20).findings]
    b = [str(f) for f in reconcile(conn, TENANT, path, sample=20, seed=0).findings]
    c = [str(f) for f in reconcile(conn, TENANT, path, sample=20, seed=1).findings]
    assert a == b and a != c


def test_each_table_reports_how_many_numeric_columns_were_summed_and_how_many_rows_sampled(conn, xer):
    path, _ = xer(seed=3, tasks=60)
    load_file(conn, TENANT, path)
    by = {ln.table: ln for ln in reconcile(conn, TENANT, path).lines}
    assert {k: v.sums_checked for k, v in by.items()} == {"CALENDAR": 2, "RSRC": 0, "PROJECT": 0, "PROJWBS": 0, "TASK": 4, "TASKPRED": 1, "TASKRSRC": 8}
    assert {k: v.sampled for k, v in by.items()} == {"CALENDAR": 3, "RSRC": 12, "PROJECT": 1, "PROJWBS": by["PROJWBS"].file_rows, "TASK": 60, "TASKPRED": by["TASKPRED"].file_rows, "TASKRSRC": by["TASKRSRC"].file_rows}


def test_a_table_missing_from_the_file_has_zero_rows_zero_samples_and_still_reconciles(conn, tmp_path):
    import make_sample_xer as gen
    tables = [t for t in gen.build_tables(3, 40) if t.name != "TASKRSRC"]
    path = tmp_path / "no_assign.xer"
    path.write_bytes(gen.render(tables))
    load_file(conn, TENANT, str(path))
    r = reconcile(conn, TENANT, str(path))
    by = {ln.table: ln for ln in r.lines}
    assert r.ok and (by["TASKRSRC"].file_rows, by["TASKRSRC"].stored, by["TASKRSRC"].quarantined, by["TASKRSRC"].sampled) == (0, 0, 0, 0)
