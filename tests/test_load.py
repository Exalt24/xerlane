"""Loader against a real PostgreSQL: typed rows, idempotence, updates with a change report, one transaction per project, quarantine."""
from __future__ import annotations

import pytest

import make_sample_xer as gen
from conftest import TENANT, snapshot
from xerlane import load as loadmod
from xerlane.load import Change, load_file, task_changes
from xerlane.db import tenant_tx

pytestmark = pytest.mark.db


def counts(conn, tenant=TENANT):
    with tenant_tx(conn, tenant) as cur:
        out = {}
        for t in ("calendar", "rsrc", "project", "projwbs", "task", "taskpred", "taskrsrc", "quarantine", "load_run", "change_event"):
            cur.execute(f"select count(*) from {t}")
            out[t] = cur.fetchone()[0]
    return out


def evolve(tables, *, move_finish=None, float_to=None, drop_task=None, add_task=False):
    """A newer version of the same project: edit the generator's tables in place."""
    by = {t.name: t for t in tables}
    T = by["TASK"]
    ti = {f: i for i, f in enumerate(T.fields)}
    if move_finish:
        row = T.rows[move_finish]
        row[ti["early_end_date"]] = "2027-06-01 15:00"
    if float_to is not None:
        T.rows[float_to][ti["total_float_hr_cnt"]] = "-24"
    if drop_task is not None:
        gone = T.rows.pop(drop_task)[0]
        P, A = by["TASKPRED"], by["TASKRSRC"]
        P.rows[:] = [r for r in P.rows if gone not in (r[1], r[2])]
        A.rows[:] = [r for r in A.rows if r[1] != gone]
    if add_task:
        new = list(T.rows[0])
        new[0], new[ti["task_code"]], new[ti["task_name"]] = "9001", "A9001", "Added later"
        T.rows.append(new)
        by["TASKPRED"].rows.append(["99001", "9001", T.rows[1][0], T.rows[0][ti["proj_id"]], T.rows[0][ti["proj_id"]], "PR_FS", "0"])
    return tables


def write(tmp_path, tables, name):
    p = tmp_path / name
    p.write_bytes(gen.render(tables))
    return str(p)


def test_first_load_stores_every_row_typed(conn, xer, admin):
    path, _ = xer(tasks=100)
    r = load_file(conn, TENANT, path)
    assert r.status == "loaded" and r.exit_code == 0 and r.quarantined == []
    c = counts(conn)
    assert (c["calendar"], c["rsrc"], c["project"], c["task"]) == (3, 12, 1, 100) and c["projwbs"] >= 13 and c["taskpred"] > 100 and c["quarantine"] == 0
    row = admin.execute("select task_id, task_code, target_drtn_hr_cnt, early_start_date, total_float_hr_cnt, extra from task where tenant_id = %s order by task_id limit 1", (TENANT,)).fetchone()
    assert row[1].startswith("A") and row[2] is not None and row[3].year == 2026 and set(row[5]) == {"priority_type", "guid"}
    assert admin.execute("select pg_typeof(early_start_date)::text, pg_typeof(target_cost)::text from task, taskrsrc limit 1").fetchone() == ("timestamp without time zone", "numeric")


def test_unmodeled_table_is_reported_not_stored(conn, xer):
    path, _ = xer()
    r = load_file(conn, TENANT, path)
    assert any(f.rule == "UNMODELED_TABLE" and "CURRTYPE" in f.message for f in r.findings)
    assert r.rows_read["CURRTYPE"] == 1


def test_reload_of_the_same_file_changes_nothing(conn, xer, admin):
    path, _ = xer(tasks=100)
    load_file(conn, TENANT, path)
    before, c0 = snapshot(admin, TENANT), counts(conn)
    r = load_file(conn, TENANT, path)
    assert r.status == "unchanged" and r.projects[0].status == "unchanged" and r.projects[0].changes == []
    assert snapshot(admin, TENANT) == before and counts(conn) == c0, "no row, no run record and no change event may appear"


def test_reload_of_the_same_file_under_another_name_changes_no_data(conn, xer, admin):
    path, _ = xer()
    load_file(conn, TENANT, path)
    before = snapshot(admin, TENANT)
    r = load_file(conn, TENANT, path, label="renamed.xer")
    assert r.status == "unchanged" and snapshot(admin, TENANT) == before


def test_newer_version_updates_and_writes_the_change_report(conn, xer, tmp_path, admin):
    tables = gen.build_tables(4, 80)
    v1 = write(tmp_path, tables, "v1.xer")
    load_file(conn, TENANT, v1)
    T = {t.name: t for t in tables}["TASK"]
    dropped_id, dropped_code = T.rows[10][0], T.rows[10][4]
    moved_id, moved_code = T.rows[3][0], T.rows[3][4]
    floated_id = T.rows[5][0]
    v2 = write(tmp_path, evolve(tables, move_finish=3, float_to=5, drop_task=10, add_task=True), "v2.xer")
    r = load_file(conn, TENANT, v2)
    assert r.status == "loaded" and r.projects[0].status == "updated"
    got = {(c.kind, c.task_id) for c in r.projects[0].changes}
    assert ("TASK_ADDED", 9001) in got and ("TASK_REMOVED", int(dropped_id)) in got
    assert ("FINISH_MOVED", int(moved_id)) in got and ("FLOAT_CHANGED", int(floated_id)) in got
    moved = [c for c in r.projects[0].changes if c.kind == "FINISH_MOVED" and c.task_id == int(moved_id)][0]
    assert moved.new == "2027-06-01 15:00" and moved.field == "early_end_date" and moved.task_code == moved_code
    removed = [c for c in r.projects[0].changes if c.kind == "TASK_REMOVED"][0]
    assert removed.task_code == dropped_code
    stored = admin.execute("select kind, task_id from change_event where tenant_id = %s order by event_id", (TENANT,)).fetchall()
    assert {(k, t) for k, t in stored} == got
    assert admin.execute("select count(*) from task where tenant_id = %s", (TENANT,)).fetchone()[0] == 80
    assert admin.execute("select count(*) from task where tenant_id = %s and task_id = %s", (TENANT, int(dropped_id))).fetchone()[0] == 0
    assert [x[0] for x in admin.execute("select kind from load_run where tenant_id = %s order by run_id", (TENANT,)).fetchall()] == ["initial", "update"]


def test_updating_back_and_forth_converges_on_the_file(conn, xer, tmp_path, admin):
    tables = gen.build_tables(6, 60)
    v1 = write(tmp_path, tables, "v1.xer")
    load_file(conn, TENANT, v1)
    v2 = write(tmp_path, evolve(gen.build_tables(6, 60), move_finish=2, drop_task=7, add_task=True), "v2.xer")
    load_file(conn, TENANT, v2)
    after_v2 = snapshot(admin, TENANT)
    load_file(conn, TENANT, v1)
    load_file(conn, TENANT, v2)
    assert snapshot(admin, TENANT) == after_v2


def test_swapping_two_task_codes_between_versions_does_not_trip_the_unique_key(conn, tmp_path):
    tables = gen.build_tables(8, 30)
    load_file(conn, TENANT, write(tmp_path, tables, "a.xer"))
    T = {t.name: t for t in tables}["TASK"]
    T.rows[1][4], T.rows[2][4] = T.rows[2][4], T.rows[1][4]
    r = load_file(conn, TENANT, write(tmp_path, tables, "b.xer"))
    assert r.status == "loaded" and r.projects[0].status == "updated"


def test_task_changes_reports_nothing_for_equal_states():
    row = {"task_code": "A", "early_end_date": None, "target_end_date": None, "total_float_hr_cnt": None, "free_float_hr_cnt": None}
    assert task_changes({1: row}, {1: dict(row)}) == []


def test_free_float_change_is_a_float_change():
    a = {"task_code": "A", "early_end_date": None, "target_end_date": None, "total_float_hr_cnt": None, "free_float_hr_cnt": 8}
    b = dict(a, free_float_hr_cnt=0)
    assert task_changes({1: b}, {1: a}) == [Change("FLOAT_CHANGED", 1, "A", "free_float_hr_cnt", "8", "0")]


def test_truncated_file_is_rejected_and_leaves_nothing(conn, xer, admin):
    path, _ = xer(defects=["truncated"])
    r = load_file(conn, TENANT, path)
    assert r.status == "rejected" and r.exit_code == 2 and "MISSING_END" in {f.rule for f in r.findings}
    assert counts(conn) == dict.fromkeys(counts(conn), 0)


def test_rejected_newer_file_leaves_the_stored_project_as_it_was(conn, xer, tmp_path, admin):
    good, _ = xer(seed=2, tasks=60, name="good.xer")
    load_file(conn, TENANT, good)
    before = snapshot(admin, TENANT)
    bad, _ = xer(seed=2, tasks=60, defects=["missing_end"], name="bad.xer")
    assert load_file(conn, TENANT, bad).status == "rejected"
    assert snapshot(admin, TENANT) == before


def test_not_an_xer_file_is_rejected(conn, tmp_path):
    p = tmp_path / "x.xer"
    p.write_bytes(b"just some text\r\n")
    r = load_file(conn, TENANT, str(p))
    assert r.status == "rejected" and "ERMHDR" in r.reason


def test_a_failure_inside_the_transaction_leaves_nothing(conn, xer, monkeypatch, admin):
    path, _ = xer(tasks=60)
    real = loadmod._write
    def boom(cur, tenant, t, d):
        if t.name == "TASKPRED":
            raise loadmod.psycopg.errors.InternalError_("disk full, pretend")
        return real(cur, tenant, t, d)
    monkeypatch.setattr(loadmod, "_write", boom)
    r = load_file(conn, TENANT, path)
    assert r.status == "failed" and r.exit_code == 2 and "disk full" in r.projects[0].error
    assert counts(conn) == dict.fromkeys(counts(conn), 0), "tasks written before the failure must be rolled back"
    monkeypatch.setattr(loadmod, "_write", real)
    assert load_file(conn, TENANT, path).status == "loaded", "and the connection is usable afterwards"


def test_post_load_validation_failure_rolls_the_project_back(conn, xer, monkeypatch):
    from xerlane.validate import Finding
    path, _ = xer(tasks=40)
    monkeypatch.setattr(loadmod, "validate_db", lambda cur, tenant, ids: [Finding("CYCLE", "error", "TASKPRED", "1", None, "pretend")])
    r = load_file(conn, TENANT, path)
    assert r.status == "failed" and "PostLoadError" in r.projects[0].error
    assert counts(conn)["task"] == 0


def test_one_transaction_per_project_a_failing_project_leaves_the_others(conn, xer, monkeypatch, admin):
    path, _ = xer(tasks=90, projects=3)
    real = loadmod.load_project
    def pick(conn_, tenant, plan, proj_id, *a):
        if proj_id == 1011:
            raise loadmod.PostLoadError("second project fails")
        return real(conn_, tenant, plan, proj_id, *a)
    monkeypatch.setattr(loadmod, "load_project", pick)
    r = load_file(conn, TENANT, path)
    assert r.status == "partial" and r.exit_code == 2
    assert [p.status for p in r.projects] == ["loaded", "failed", "loaded"]
    assert sorted(x[0] for x in admin.execute("select distinct proj_id from task where tenant_id = %s", (TENANT,)).fetchall()) == [1010, 1012]


def test_a_real_database_error_in_one_project_rolls_back_only_that_project(conn, xer, admin):
    path, _ = xer(tasks=60, projects=2)
    admin.execute("alter table xerlane.taskrsrc add constraint no_big check (target_cost < 1000) not valid")        # a constraint the second project's data violates
    try:
        r = load_file(conn, TENANT, path)
    finally:
        admin.execute("alter table xerlane.taskrsrc drop constraint no_big")
    assert r.status in ("partial", "failed")
    failed = [p.proj_id for p in r.projects if p.status == "failed"]
    stored = {x[0] for x in admin.execute("select distinct proj_id from task where tenant_id = %s", (TENANT,)).fetchall()}
    assert failed and stored.isdisjoint(failed)


def test_multi_project_file_loads_each_project(conn, xer):
    path, _ = xer(tasks=120, projects=3)
    r = load_file(conn, TENANT, path)
    assert [p.status for p in r.projects] == ["loaded"] * 3 and [p.counts["TASK"] for p in r.projects] == [40, 40, 40]
    assert counts(conn)["project"] == 3 and counts(conn)["task"] == 120


def test_quarantine_keeps_the_raw_row_and_the_reason(conn, xer, admin):
    path, planted = xer(seed=9, tasks=80, defects=["bad_number"])
    r = load_file(conn, TENANT, path)
    assert r.exit_code == 1 and r.status == "loaded"
    q = admin.execute("select table_name, line_no, reason_code, reason, raw from quarantine where tenant_id = %s and reason_code = 'BAD_VALUE'", (TENANT,)).fetchall()
    assert len(q) == 1 and q[0][0] == "TASK" and q[0][1] == planted[0].line and "total_float_hr_cnt='1.2.3'" in q[0][3]
    assert "1.2.3" in q[0][4] and q[0][4][0] == planted[0].key
    assert admin.execute("select count(*) from task where tenant_id = %s and task_id = %s", (TENANT, int(planted[0].key))).fetchone()[0] == 0


def test_quarantine_is_replaced_not_piled_up_on_reload(conn, xer, admin):
    path, _ = xer(seed=9, tasks=80, defects=["bad_number", "orphan_assignment"])
    load_file(conn, TENANT, path)
    n = admin.execute("select count(*) from quarantine").fetchone()[0]
    load_file(conn, TENANT, path)
    load_file(conn, TENANT, path)
    assert admin.execute("select count(*) from quarantine").fetchone()[0] == n > 0


def test_fixing_the_file_empties_the_quarantine(conn, xer, admin):
    bad, _ = xer(seed=9, tasks=80, defects=["bad_number"], name="plan.xer")
    load_file(conn, TENANT, bad)
    assert admin.execute("select count(*) from quarantine").fetchone()[0] >= 1
    good, _ = xer(seed=9, tasks=80, name="plan.xer")
    r = load_file(conn, TENANT, good)
    assert r.projects[0].status == "updated" and admin.execute("select count(*) from quarantine").fetchone()[0] == 0
    assert admin.execute("select count(*) from task").fetchone()[0] == 80


def test_a_file_with_no_surviving_project_row_stores_nothing(conn, tmp_path):
    tables = gen.build_tables(1, 20)
    P = {t.name: t for t in tables}["PROJECT"]
    P.rows[0][0] = "abc"
    r = load_file(conn, TENANT, write(tmp_path, tables, "np.xer"))
    assert r.status == "rejected" and "PROJECT" in r.reason
    assert counts(conn)["task"] == 0


def test_database_rejects_what_the_validator_would(conn, admin):
    """The schema's own checks are a second line: a cyclic pred_type or backwards dates cannot be written even by hand."""
    with pytest.raises(Exception):
        admin.execute("insert into xerlane.taskpred (tenant_id, task_pred_id, task_id, pred_task_id, proj_id, pred_proj_id, pred_type) values ('t', 1, 1, 2, 1, 1, 'PR_XX')")
