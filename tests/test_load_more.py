"""Loader details pinned after the mutation check: diff without writing, the shape of results, scoping of quarantine rows."""
from __future__ import annotations

import pytest

import make_sample_xer as gen
from conftest import TENANT, snapshot
from test_load import counts, evolve, write
from xerlane.load import Change, diff_file, load_file, make_plan, task_changes
from xerlane.parse import parse_file

pytestmark = pytest.mark.db


def test_diff_of_a_project_not_stored_yet_says_not_stored_and_writes_nothing(conn, xer, admin):
    path, _ = xer(tasks=40)
    out = diff_file(conn, TENANT, path)
    assert out == {1010: (False, [])}
    assert counts(conn) == dict.fromkeys(counts(conn), 0)


def test_diff_lists_the_changes_a_load_would_make_and_writes_nothing(conn, tmp_path, admin):
    tables = gen.build_tables(4, 60)
    load_file(conn, TENANT, write(tmp_path, tables, "v1.xer"))
    before = snapshot(admin, TENANT)
    runs = counts(conn)["load_run"]
    v2 = write(tmp_path, evolve(tables, move_finish=3, float_to=5, drop_task=10, add_task=True), "v2.xer")
    existed, changes = diff_file(conn, TENANT, v2)[1010]
    assert existed is True
    assert {c.kind for c in changes} == {"TASK_ADDED", "TASK_REMOVED", "FINISH_MOVED", "FLOAT_CHANGED"}
    assert snapshot(admin, TENANT) == before and counts(conn)["load_run"] == runs
    assert [c for c in load_file(conn, TENANT, v2).projects[0].changes] == changes, "the preview and the real load report the same changes"


def test_diff_of_an_identical_file_is_empty(conn, xer):
    path, _ = xer(tasks=40)
    load_file(conn, TENANT, path)
    assert diff_file(conn, TENANT, path) == {1010: (True, [])}


def test_diff_covers_every_project_of_a_multi_project_file(conn, xer):
    path, _ = xer(tasks=60, projects=3)
    assert diff_file(conn, TENANT, path) == {1010: (False, []), 1011: (False, []), 1012: (False, [])}
    load_file(conn, TENANT, path)
    assert set(diff_file(conn, TENANT, path)) == {1010, 1011, 1012} and all(v == (True, []) for v in diff_file(conn, TENANT, path).values())


def test_diff_of_a_rejected_file_raises_with_the_reason(conn, xer):
    path, _ = xer(defects=["truncated"])
    with pytest.raises(ValueError, match="%E"):
        diff_file(conn, TENANT, path)


def test_diff_is_per_tenant(conn, xer):
    path, _ = xer(tasks=40)
    load_file(conn, TENANT, path)
    assert diff_file(conn, "another", path) == {1010: (False, [])}


def test_load_result_counts_the_rows_it_read_including_unmodeled_tables(conn, xer):
    path, _ = xer(tasks=40)
    r = load_file(conn, TENANT, path)
    assert r.rows_read["TASK"] == 40 and r.rows_read["CURRTYPE"] == 1 and r.rows_read["CALENDAR"] == 3
    assert r.label == "plan.xer" and r.path == path and r.sha256 == parse_file(path).sha256 and r.seconds > 0


def test_project_result_counts_each_project_table(conn, xer):
    path, _ = xer(tasks=40)
    p = load_file(conn, TENANT, path).projects[0]
    assert (p.proj_id, p.short_name, p.status) == (1010, "XL-01", "loaded")
    assert p.counts["TASK"] == 40 and p.counts["TASKPRED"] > 40 and p.counts["PROJWBS"] >= 13 and p.counts["TASKRSRC"] > 0 and "CALENDAR" not in p.counts
    assert p.quarantined == 0 and p.error is None


def test_exit_code_is_0_for_notes_only_1_for_quarantine_and_2_for_a_failure(conn, xer):
    ok, _ = xer(tasks=40, name="ok.xer")
    bad, _ = xer(tasks=40, defects=["cycle"], name="bad.xer")
    cut, _ = xer(tasks=40, defects=["truncated"], name="cut.xer")
    assert load_file(conn, TENANT, ok).exit_code == 0
    assert load_file(conn, TENANT, bad).exit_code == 1
    assert load_file(conn, TENANT, cut).exit_code == 2


def test_quarantine_rows_of_two_projects_stay_with_their_own_project(conn, xer, admin):
    """A defect planted in the second project is stored under the second project's id, so reloading the first never touches it."""
    path, planted = xer(seed=3, tasks=90, projects=3, defects=["bad_number"])
    load_file(conn, TENANT, path)
    rows = admin.execute("select proj_id, table_name, reason_code from xerlane.quarantine where tenant_id = %s order by line_no", (TENANT,)).fetchall()
    assert rows and {r[0] for r in rows} == {rows[0][0]} and rows[0][0] in (1010, 1011, 1012)
    owner = rows[0][0]
    other = 1010 if owner != 1010 else 1011
    assert admin.execute("select count(*) from xerlane.quarantine where tenant_id = %s and proj_id = %s", (TENANT, other)).fetchone()[0] == 0


def test_quarantine_of_a_row_whose_project_is_not_loaded_is_stored_without_a_project(conn, xer, admin):
    path, planted = xer(seed=3, tasks=60, defects=["orphan_assignment"])
    load_file(conn, TENANT, path)
    pids = {r[0] for r in admin.execute("select proj_id from xerlane.quarantine where tenant_id = %s and reason_code = 'ORPHAN_ASSIGNMENT'", (TENANT,)).fetchall()}
    assert pids == {1010}, "the assignment names project 1010, which is loaded, so the row is scoped to it"


def test_make_plan_accepts_every_clean_row_and_names_the_rest(xer):
    path, _ = xer(seed=2, tasks=50, defects=["wrong_field_count"])
    plan = make_plan(parse_file(path))
    assert len(plan.accepted["TASK"]) == 49 and len(plan.accepted["CALENDAR"]) == 3
    assert [q.code for q in plan.quarantined][:1] == ["WRONG_FIELD_COUNT"] and plan.quarantined == sorted(plan.quarantined, key=lambda q: q.line)
    assert all(q.reason for q in plan.quarantined) and all(isinstance(q.raw, tuple) for q in plan.quarantined)


def test_changes_print_with_their_field_and_old_and_new_values():
    c = Change("FINISH_MOVED", 7, "A100", "early_end_date", "2026-01-01 07:00", "2026-02-01 07:00")
    assert str(c) == "FINISH_MOVED A100 (task 7) early_end_date: 2026-01-01 07:00 -> 2026-02-01 07:00"
    assert str(Change("TASK_ADDED", 8, "A200")) == "TASK_ADDED A200 (task 8)"


def test_changes_are_listed_in_task_order_with_finish_before_float_for_one_task():
    from datetime import datetime
    old = {"task_code": "A", "early_end_date": datetime(2026, 1, 1), "target_end_date": datetime(2026, 1, 2), "total_float_hr_cnt": 0, "free_float_hr_cnt": 0}
    new = dict(old, early_end_date=datetime(2026, 1, 5), target_end_date=datetime(2026, 1, 6), total_float_hr_cnt=8, free_float_hr_cnt=4)
    got = task_changes({2: new, 1: new}, {2: old, 1: old, 3: old})
    assert [(c.task_id, c.kind, c.field) for c in got] == [
        (1, "FINISH_MOVED", "early_end_date"), (1, "FINISH_MOVED", "target_end_date"), (1, "FLOAT_CHANGED", "total_float_hr_cnt"), (1, "FLOAT_CHANGED", "free_float_hr_cnt"),
        (2, "FINISH_MOVED", "early_end_date"), (2, "FINISH_MOVED", "target_end_date"), (2, "FLOAT_CHANGED", "total_float_hr_cnt"), (2, "FLOAT_CHANGED", "free_float_hr_cnt"),
        (3, "TASK_REMOVED", None)]
    assert got[0].old == "2026-01-01 00:00" and got[0].new == "2026-01-05 00:00"
    assert got[2].old == "0" and got[2].new == "8"


def test_a_change_to_a_none_value_prints_as_none():
    from datetime import datetime
    old = {"task_code": "A", "early_end_date": None, "target_end_date": None, "total_float_hr_cnt": None, "free_float_hr_cnt": None}
    new = dict(old, early_end_date=datetime(2026, 1, 5))
    got = task_changes({1: new}, {1: old})
    assert got == [Change("FINISH_MOVED", 1, "A", "early_end_date", None, "2026-01-05 00:00")]


def test_updating_a_project_changes_only_the_rows_that_differ(conn, tmp_path, admin):
    tables = gen.build_tables(4, 60)
    load_file(conn, TENANT, write(tmp_path, tables, "v1.xer"))
    xmin_before = dict(admin.execute("select task_id, xmin::text from xerlane.task where tenant_id = %s", (TENANT,)).fetchall())
    T = {t.name: t for t in tables}["TASK"]
    T.rows[4][T.fields.index("task_name")] = "Renamed"
    load_file(conn, TENANT, write(tmp_path, tables, "v2.xer"))
    xmin_after = dict(admin.execute("select task_id, xmin::text from xerlane.task where tenant_id = %s", (TENANT,)).fetchall())
    changed = [k for k in xmin_before if xmin_before[k] != xmin_after[k]]
    assert changed == [int(T.rows[4][0])], "an update rewrites the one row that changed, not every row"


def test_change_and_quarantine_rows_are_immutable():
    from dataclasses import FrozenInstanceError
    from xerlane.load import QRow
    with pytest.raises(FrozenInstanceError):
        Change("TASK_ADDED", 1, "A").kind = "X"
    with pytest.raises(FrozenInstanceError):
        QRow("TASK", 1, None, "C", "r", ()).line = 2


def test_a_project_that_failed_to_load_reports_no_counts_changes_or_quarantine(conn, xer, monkeypatch):
    from xerlane import load as loadmod
    from xerlane.validate import Finding
    path, _ = xer(tasks=30)
    monkeypatch.setattr(loadmod, "validate_db", lambda cur, tenant, ids: [Finding("CYCLE", "error", "TASKPRED", "1", None, "pretend")])
    p = load_file(conn, TENANT, path).projects[0]
    assert (p.status, p.counts, p.changes, p.quarantined) == ("failed", {}, [], 0) and p.error.startswith("PostLoadError: ")


def test_data_after_the_end_marker_is_an_error_finding_with_exit_code_1_though_no_row_is_set_aside(conn, tmp_path):
    tables = gen.build_tables(2, 30)
    p = tmp_path / "trailing.xer"
    p.write_bytes(gen.render(tables) + b"%R\t1\t2\r\n")
    r = load_file(conn, TENANT, str(p))
    assert r.status == "loaded" and r.quarantined == [] and r.exit_code == 1
    assert [f.rule for f in r.findings if f.severity == "error"] == ["TRAILING_DATA"]


def test_a_reexport_that_differs_only_in_its_header_changes_no_data_but_follows_the_quarantine_to_the_new_hash(conn, tmp_path, admin):
    from xerlane.reconcile import reconcile
    tables = gen.build_tables(5, 60)
    gen.plant(tables, ["cycle"], 5)
    old = tmp_path / "v1.xer"
    old.write_bytes(gen.render(tables, header_date="2026-11-06"))
    new = tmp_path / "v2.xer"
    new.write_bytes(gen.render(tables, header_date="2026-11-13"))
    load_file(conn, TENANT, str(old), label="plan.xer")
    before = snapshot(admin, TENANT)
    r = load_file(conn, TENANT, str(new), label="plan.xer")
    assert r.status == "unchanged" and snapshot(admin, TENANT) == before
    shas = {x[0] for x in admin.execute("select source_sha256 from xerlane.quarantine where tenant_id = %s", (TENANT,)).fetchall()}
    assert shas == {r.sha256}, "the quarantine rows now belong to the file that was just loaded"
    assert reconcile(conn, TENANT, str(new)).ok


def test_reloading_the_same_defective_file_under_a_new_label_relabels_its_quarantine(conn, xer, admin):
    path, _ = xer(seed=3, tasks=60, defects=["cycle"])
    load_file(conn, TENANT, path, label="first")
    load_file(conn, TENANT, path, label="second")
    rows = admin.execute("select distinct source_label from xerlane.quarantine where tenant_id = %s", (TENANT,)).fetchall()
    assert rows == [("second",)]


def test_a_change_only_in_what_is_set_aside_still_counts_as_an_update_and_is_recorded(conn, tmp_path, admin):
    tables = gen.build_tables(6, 60)
    planted = gen.plant(tables, ["bad_number"], 6)
    T = {t.name: t for t in tables}["TASK"]
    row = next(r for r in T.rows if r[0] == planted[0].key)
    first = tmp_path / "v1.xer"
    first.write_bytes(gen.render(tables))
    load_file(conn, TENANT, str(first), label="plan.xer")
    runs = counts(conn)["load_run"]
    row[T.fields.index("total_float_hr_cnt")] = "4.5.6"                      # a different bad value in the same row: the stored data stays the same
    second = tmp_path / "v2.xer"
    second.write_bytes(gen.render(tables))
    before = snapshot(admin, TENANT)
    r = load_file(conn, TENANT, str(second), label="plan.xer")
    after = snapshot(admin, TENANT)
    assert {k: v for k, v in after.items() if k != "quarantine"} == {k: v for k, v in before.items() if k != "quarantine"}
    assert r.projects[0].status == "updated" and r.projects[0].changes == []
    assert counts(conn)["load_run"] == runs + 1
    assert any("4.5.6" in q[3] for q in after["quarantine"])


def test_the_load_reports_its_own_duration(conn, xer):
    import time
    from xerlane.load import load_parsed
    path, _ = xer(tasks=30)
    r = load_file(conn, TENANT, path)
    assert 0 < r.seconds < 60
    pr = parse_file(path)
    again = load_parsed(conn, TENANT, pr, "x.xer")
    assert 0 < again.seconds < 60, "with no start time given, the clock starts at the call"
    later = load_parsed(conn, TENANT, pr, "x.xer", started=time.perf_counter() - 100)
    assert 100 <= later.seconds < 160, "a start time given by the caller is where the clock starts"
    rejected = load_parsed(conn, TENANT, parse_file(xer(defects=["truncated"], name="cut.xer")[0]), "cut.xer", started=time.perf_counter() - 100)
    assert 100 <= rejected.seconds < 160 and rejected.status == "rejected"


def test_reloading_an_unchanged_defective_file_does_not_rewrite_its_quarantine_rows(conn, xer, admin):
    path, _ = xer(seed=3, tasks=60, defects=["cycle", "orphan_assignment"])
    load_file(conn, TENANT, path)
    before = admin.execute("select quarantine_id, xmin::text from xerlane.quarantine where tenant_id = %s order by quarantine_id", (TENANT,)).fetchall()
    assert len(before) == 2
    load_file(conn, TENANT, path)
    assert admin.execute("select quarantine_id, xmin::text from xerlane.quarantine where tenant_id = %s order by quarantine_id", (TENANT,)).fetchall() == before


def test_a_quarantined_row_that_belongs_to_no_loaded_project_is_kept_without_a_project_and_cleared_when_fixed(conn, tmp_path, admin):
    tables = gen.build_tables(7, 90, projects=3)
    T = {t.name: t for t in tables}["TASK"]
    A = {t.name: t for t in tables}["TASKRSRC"]
    T.rows[5][T.fields.index("proj_id")] = "abc"                        # a project id that is not even a number
    A.rows[2][A.fields.index("proj_id")] = "999"                        # a project that is in no PROJECT row
    path = tmp_path / "plan.xer"
    path.write_bytes(gen.render(tables))
    load_file(conn, TENANT, str(path))
    rows = admin.execute("select table_name, proj_id, reason_code from xerlane.quarantine where tenant_id = %s and proj_id is null order by table_name, line_no", (TENANT,)).fetchall()
    codes = {(t, c) for t, _, c in rows}
    assert ("TASK", "BAD_VALUE") in codes and ("TASKRSRC", "MISSING_PROJECT") in codes
    assert all(r[1] is None for r in rows)
    fixed = gen.build_tables(7, 90, projects=3)
    path.write_bytes(gen.render(fixed))
    load_file(conn, TENANT, str(path))
    assert admin.execute("select count(*) from xerlane.quarantine where tenant_id = %s", (TENANT,)).fetchone()[0] == 0


def test_the_duration_is_also_reported_when_no_project_row_survives(conn, tmp_path):
    import time
    from xerlane.load import load_parsed
    tables = gen.build_tables(1, 20)
    {t.name: t for t in tables}["PROJECT"].rows[0][0] = "abc"
    pr = parse_file(write(tmp_path, tables, "np.xer"))
    r = load_parsed(conn, TENANT, pr, "np.xer", started=time.perf_counter() - 100)
    assert r.status == "rejected" and 100 <= r.seconds < 160
