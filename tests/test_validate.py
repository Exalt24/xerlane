"""Validation rules, on small hand-built schedules and on every planted defect kind."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

import make_sample_xer as gen
from xerlane.parse import parse_bytes
from xerlane.schema import BY_NAME, TABLES
from xerlane.validate import WRow, analyze, validate_parse, world_from_parse


def row(table, ref, **v):
    t = BY_NAME[table]
    full = {c.name: None for c in t.cols}
    full.update(v)
    return WRow(table, ref, ref, full)


def base_world(**over):
    """One project, one calendar, one WBS element, three tasks T1 -> T2 -> T3."""
    w = {t.name: [] for t in TABLES}
    w["CALENDAR"] = [row("CALENDAR", 1, clndr_id=1)]
    w["RSRC"] = [row("RSRC", 2, rsrc_id=5, clndr_id=1)]
    w["PROJECT"] = [row("PROJECT", 3, proj_id=10, proj_short_name="P", clndr_id=1)]
    w["PROJWBS"] = [row("PROJWBS", 4, wbs_id=20, proj_id=10, wbs_short_name="W")]
    for i, ref in zip((1, 2, 3), (5, 6, 7)):
        w["TASK"].append(row("TASK", ref, task_id=100 + i, proj_id=10, wbs_id=20, clndr_id=1, task_code=f"T{i}", target_drtn_hr_cnt=Decimal(8)))
    w["TASKPRED"] = [row("TASKPRED", 8, task_pred_id=1, task_id=102, pred_task_id=101, proj_id=10, pred_proj_id=10, pred_type="PR_FS", lag_hr_cnt=Decimal(0)),
                     row("TASKPRED", 9, task_pred_id=2, task_id=103, pred_task_id=102, proj_id=10, pred_proj_id=10, pred_type="PR_SS", lag_hr_cnt=Decimal(-8))]
    w["TASKRSRC"] = [row("TASKRSRC", 10, taskrsrc_id=1, task_id=101, rsrc_id=5, proj_id=10, target_cost=Decimal("12.50"))]
    for k, v in over.items():
        w[k] = v
    return w


def rules(a):
    return sorted({f.rule for f in a.findings})


def test_a_clean_world_has_no_findings():
    a = analyze(base_world())
    assert a.findings == [] and a.rejected == {}


def test_duplicate_key_rejects_the_later_row_and_names_the_first():
    w = base_world()
    w["TASK"].append(row("TASK", 11, task_id=101, proj_id=10, wbs_id=20, clndr_id=1, task_code="T9"))
    a = analyze(w)
    assert a.rejected[("TASK", 11)][0] == "DUP_KEY" and "line 5" in a.rejected[("TASK", 11)][1]
    assert ("TASK", 5) not in a.rejected


def test_duplicate_task_code_is_per_project():
    w = base_world()
    w["PROJECT"].append(row("PROJECT", 12, proj_id=11, proj_short_name="Q", clndr_id=1))
    w["PROJWBS"].append(row("PROJWBS", 13, wbs_id=21, proj_id=11, wbs_short_name="W2"))
    w["TASK"].append(row("TASK", 14, task_id=200, proj_id=11, wbs_id=21, clndr_id=1, task_code="T1"))       # same code, other project: allowed
    assert analyze(w).findings == []
    w["TASK"].append(row("TASK", 15, task_id=201, proj_id=10, wbs_id=20, clndr_id=1, task_code="T1"))       # same project: not
    a = analyze(w)
    assert a.rejected[("TASK", 15)][0] == "DUP_TASK_CODE" and len(a.rejected) == 1


def test_finish_before_start_for_each_date_pair_and_equal_dates_are_fine():
    for lo, hi in (("target_start_date", "target_end_date"), ("early_start_date", "early_end_date"), ("late_start_date", "late_end_date"), ("act_start_date", "act_end_date")):
        w = base_world()
        w["TASK"][0].v[lo], w["TASK"][0].v[hi] = datetime(2026, 2, 1), datetime(2026, 1, 1)
        a = analyze(w)
        assert a.rejected[("TASK", 5)][0] == "FINISH_BEFORE_START", lo
        assert rules(a) == ["FINISH_BEFORE_START", "MISSING_TASK", "ORPHAN_ASSIGNMENT"], "the task's relationship and assignment follow it out"
        w["TASK"][0].v[hi] = datetime(2026, 2, 1)
        assert analyze(w).findings == [], "equal dates are a milestone, not a defect"


def test_a_rejected_task_takes_its_relationships_and_assignments_with_it():
    w = base_world()
    w["TASK"][0].v["clndr_id"] = 99
    a = analyze(w)
    assert a.rejected[("TASK", 5)][0] == "MISSING_CALENDAR"
    assert a.rejected[("TASKPRED", 8)][0] == "MISSING_TASK"
    assert a.rejected[("TASKRSRC", 10)][0] == "ORPHAN_ASSIGNMENT"
    assert ("TASKPRED", 9) not in a.rejected and ("TASK", 6) not in a.rejected


def test_a_rejected_wbs_takes_its_tasks():
    w = base_world()
    w["PROJWBS"][0].v["parent_wbs_id"] = 777
    a = analyze(w)
    assert a.rejected[("PROJWBS", 4)][0] == "MISSING_WBS_PARENT"
    assert {k for k in a.rejected if k[0] == "TASK"} == {("TASK", 5), ("TASK", 6), ("TASK", 7)}
    assert all(a.rejected[k][0] == "MISSING_WBS" for k in a.rejected if k[0] == "TASK")


def test_wbs_parent_must_be_in_the_same_project():
    w = base_world()
    w["PROJECT"].append(row("PROJECT", 12, proj_id=11, proj_short_name="Q"))
    w["PROJWBS"].append(row("PROJWBS", 13, wbs_id=21, proj_id=11, parent_wbs_id=20, wbs_short_name="W2"))
    assert analyze(w).rejected[("PROJWBS", 13)][0] == "MISSING_WBS_PARENT"


def test_wbs_loop_is_cut_and_names_the_loop_and_does_not_hang():
    w = base_world()
    w["PROJWBS"] = [row("PROJWBS", 4, wbs_id=20, proj_id=10, parent_wbs_id=21, wbs_short_name="A"), row("PROJWBS", 13, wbs_id=21, proj_id=10, parent_wbs_id=22, wbs_short_name="B"),
                    row("PROJWBS", 14, wbs_id=22, proj_id=10, parent_wbs_id=20, wbs_short_name="C")]
    a = analyze(w)
    loops = [f for f in a.findings if f.rule == "WBS_CYCLE"]
    assert len(loops) == 1 and loops[0].message.count("->") == 3


def test_wbs_pointing_at_itself():
    w = base_world()
    w["PROJWBS"][0].v["parent_wbs_id"] = 20
    assert "WBS_CYCLE" in rules(analyze(w))


def test_task_must_name_a_project_wbs_and_calendar_that_exist():
    w = base_world()
    w["TASK"][0].v["proj_id"] = 99
    w["TASK"][1].v["wbs_id"] = 99
    w["TASK"][2].v["clndr_id"] = 99
    got = {k[1]: v[0] for k, v in analyze(w).rejected.items() if k[0] == "TASK"}
    assert got == {5: "MISSING_PROJECT", 6: "MISSING_WBS", 7: "MISSING_CALENDAR"}


def test_calendar_on_project_and_resource_must_exist():
    w = base_world()
    w["PROJECT"][0].v["clndr_id"] = 99
    w["RSRC"][0].v["clndr_id"] = 99
    a = analyze(w)
    assert a.rejected[("RSRC", 2)][0] == "MISSING_CALENDAR" and a.rejected[("PROJECT", 3)][0] == "MISSING_CALENDAR"
    assert a.rejected[("PROJWBS", 4)][0] == "MISSING_PROJECT"                 # the project is gone, so is everything under it


def test_predecessor_must_exist_on_both_ends():
    for field in ("task_id", "pred_task_id"):
        w = base_world()
        w["TASKPRED"][0].v[field] = 999
        assert analyze(w).rejected[("TASKPRED", 8)][0] == "MISSING_TASK", field


@pytest.mark.parametrize("code", ["PR_FS", "PR_SS", "PR_FF", "PR_SF"])
def test_all_four_relationship_types_pass(code):
    w = base_world()
    w["TASKPRED"][0].v["pred_type"] = code
    assert analyze(w).findings == []


@pytest.mark.parametrize("bad", ["PR_XX", "FS", "pr_fs", "PR_FF1", ""])
def test_other_relationship_types_are_rejected(bad):
    w = base_world()
    w["TASKPRED"][0].v["pred_type"] = bad
    assert analyze(w).rejected[("TASKPRED", 8)][0] == "UNKNOWN_PRED_TYPE"


def test_cross_project_relationship_is_rejected_by_name():
    w = base_world()
    w["TASKPRED"][0].v["pred_proj_id"] = 11
    assert analyze(w).rejected[("TASKPRED", 8)][0] == "CROSS_PROJECT_PRED"


def test_cycle_of_two_three_and_one():
    for edges in ([(101, 102), (102, 101)], [(101, 102), (102, 103), (103, 101)], [(101, 101)]):
        w = base_world()
        w["TASKPRED"] = [row("TASKPRED", 8 + i, task_pred_id=i + 1, task_id=s, pred_task_id=p, proj_id=10, pred_proj_id=10, pred_type="PR_FS") for i, (p, s) in enumerate(edges)]
        a = analyze(w)
        cyc = [f for f in a.findings if f.rule == "CYCLE"]
        assert len(cyc) == 1, edges
        path = cyc[0].message.split(": ", 1)[1].split(" -> ")
        assert path[0] == path[-1] and len(path) == len(edges) + 1
        assert len(a.rejected) == 1, "exactly one relationship is set aside to break the loop"


def test_two_separate_cycles_each_get_a_path():
    w = base_world()
    w["TASK"].append(row("TASK", 11, task_id=104, proj_id=10, wbs_id=20, clndr_id=1, task_code="T4"))
    w["TASK"].append(row("TASK", 12, task_id=105, proj_id=10, wbs_id=20, clndr_id=1, task_code="T5"))
    w["TASKPRED"] = [row("TASKPRED", 20 + i, task_pred_id=i + 1, task_id=s, pred_task_id=p, proj_id=10, pred_proj_id=10, pred_type="PR_FS")
                     for i, (p, s) in enumerate([(101, 102), (102, 101), (104, 105), (105, 104)])]
    assert len([f for f in analyze(w).findings if f.rule == "CYCLE"]) == 2


def test_deep_chain_is_not_a_cycle_and_does_not_recurse():
    n = 5000
    w = base_world()
    w["TASK"] = [row("TASK", 1000 + i, task_id=i + 1, proj_id=10, wbs_id=20, clndr_id=1, task_code=f"C{i}") for i in range(n)]
    w["TASKPRED"] = [row("TASKPRED", 9000 + i, task_pred_id=i + 1, task_id=i + 2, pred_task_id=i + 1, proj_id=10, pred_proj_id=10, pred_type="PR_FS") for i in range(n - 1)]
    w["TASKRSRC"] = []
    assert analyze(w).findings == []


def test_assignment_needs_a_task_and_a_resource_but_a_role_only_assignment_is_fine():
    w = base_world()
    w["TASKRSRC"][0].v["rsrc_id"] = None
    assert analyze(w).findings == []
    w["TASKRSRC"][0].v["rsrc_id"] = 99
    assert analyze(w).rejected[("TASKRSRC", 10)][0] == "ORPHAN_ASSIGNMENT"
    w["TASKRSRC"][0].v["rsrc_id"] = 5
    w["TASKRSRC"][0].v["task_id"] = 99
    assert analyze(w).rejected[("TASKRSRC", 10)][0] == "ORPHAN_ASSIGNMENT"


@pytest.mark.parametrize("table,col,val,ok", [
    ("TASK", "target_drtn_hr_cnt", Decimal(0), True), ("TASK", "target_drtn_hr_cnt", Decimal(-1), False), ("TASK", "target_drtn_hr_cnt", Decimal(10**8), True),
    ("TASK", "target_drtn_hr_cnt", Decimal(10**8 + 1), False), ("TASK", "total_float_hr_cnt", Decimal(-500), True), ("TASK", "total_float_hr_cnt", Decimal(-(10**8) - 1), False),
    ("TASKPRED", "lag_hr_cnt", Decimal(-40), True), ("TASKPRED", "lag_hr_cnt", Decimal(10**9), False),
    ("TASKRSRC", "target_cost", Decimal("0.00"), True), ("TASKRSRC", "target_cost", Decimal("-0.01"), False), ("TASKRSRC", "act_ot_qty", Decimal(-1), False),
])
def test_numeric_ranges(table, col, val, ok):
    w = base_world()
    target = {"TASK": w["TASK"][0], "TASKPRED": w["TASKPRED"][0], "TASKRSRC": w["TASKRSRC"][0]}[table]
    target.v[col] = val
    a = analyze(w)
    assert (("OUT_OF_RANGE" not in rules(a))) is ok


def test_ids_must_be_positive():
    w = base_world()
    w["CALENDAR"][0].v["clndr_id"] = 0
    assert analyze(w).rejected[("CALENDAR", 1)][0] == "OUT_OF_RANGE"


def test_findings_name_table_key_and_line():
    w = base_world()
    w["TASK"][0].v["clndr_id"] = 99
    f = analyze(w).findings[0]
    assert (f.table, f.key, f.line, f.severity) == ("TASK", "101", 5, "error")
    assert "TASK 101 (line 5)" in str(f)


@pytest.mark.parametrize("kind", sorted(gen.DEFECTS))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_planted_defect_kind_is_reported_with_its_code(kind, seed):
    """Generated across the defect vocabulary: each planted kind must come back as its own named code, and a clean file of the same seed must not."""
    clean, _ = gen.make(seed, 150)
    findings, _ = validate_parse(parse_bytes(clean))
    assert [f for f in findings if f.severity == "error"] == []
    data, planted = gen.make(seed, 150, defects=[kind])
    findings, analysis = validate_parse(parse_bytes(data))
    got = {f.rule for f in findings if f.severity == "error"}
    assert set(planted[0].expect) <= got, f"{kind}: expected {planted[0].expect}, got {sorted(got)}"


def test_validate_parse_uses_only_clean_rows():
    data, _ = gen.make(5, 60, defects=["bad_number"])
    pr = parse_bytes(data)
    world = world_from_parse(pr)
    assert len(world["TASK"]) == 59
