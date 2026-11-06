"""Rules pinned after the mutation check found lines nothing tested (kept apart so test_validate.py stays the readable base)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal

import pytest

from test_validate import base_world, row
from xerlane.parse import parse_bytes
from xerlane.validate import Analysis, Finding, analyze, severity_counts, world_from_parse


def edges_world(edges, extra_tasks=()):
    w = base_world()
    for k, tid in enumerate(extra_tasks):
        w["TASK"].append(row("TASK", 11 + k, task_id=tid, proj_id=10, wbs_id=20, clndr_id=1, task_code=f"T{tid - 100}"))
    w["TASKPRED"] = [row("TASKPRED", 20 + i, task_pred_id=i + 1, task_id=s, pred_task_id=p, proj_id=10, pred_proj_id=10, pred_type="PR_FS") for i, (p, s) in enumerate(edges)]
    w["TASKRSRC"] = []
    return w


def messages(w, rule):
    return [f.message for f in analyze(w).findings if f.rule == rule]


def test_a_row_that_breaks_two_rules_is_reported_once_under_the_first():
    w = base_world()
    w["TASK"][0].v["target_drtn_hr_cnt"] = Decimal(-5)
    w["TASK"][0].v["clndr_id"] = 99
    a = analyze(w)
    assert a.rejected[("TASK", 5)][0] == "OUT_OF_RANGE"
    assert [f.rule for f in a.findings if f.table == "TASK"] == ["OUT_OF_RANGE"]


def test_a_task_that_is_both_out_of_range_and_backwards_in_time_is_reported_once_for_the_first_rule():
    w = base_world()
    w["TASK"][0].v["target_drtn_hr_cnt"] = Decimal(-5)
    w["TASK"][0].v["target_start_date"], w["TASK"][0].v["target_end_date"] = datetime(2026, 2, 1), datetime(2026, 1, 1)
    a = analyze(w)
    assert a.rejected[("TASK", 5)][0] == "OUT_OF_RANGE"
    assert [f.rule for f in a.findings if f.table == "TASK"] == ["OUT_OF_RANGE"]


def test_children_of_a_missing_project_are_each_rejected_for_the_project():
    w = base_world()
    w["PROJECT"] = []
    got = {k[0]: v[0] for k, v in analyze(w).rejected.items()}
    assert got == {"PROJWBS": "MISSING_PROJECT", "TASK": "MISSING_PROJECT", "TASKPRED": "MISSING_PROJECT", "TASKRSRC": "MISSING_PROJECT"}


def test_taskpred_and_taskrsrc_each_name_the_missing_project_even_when_their_tasks_exist():
    w = base_world()
    w["TASKPRED"][0].v["proj_id"] = w["TASKPRED"][0].v["pred_proj_id"] = 99
    w["TASKRSRC"][0].v["proj_id"] = 99
    got = {k[0]: v[0] for k, v in analyze(w).rejected.items()}
    assert got == {"TASKPRED": "MISSING_PROJECT", "TASKRSRC": "MISSING_PROJECT"}


def test_a_cycle_path_is_the_loop_only_not_the_tasks_walked_before_it():
    assert messages(edges_world([(101, 102), (101, 103), (103, 101)]), "CYCLE") == ["dependency cycle: T1 -> T3 -> T1"]


def test_tasks_with_several_successors_all_get_visited():
    w = edges_world([(101, 102), (101, 103), (103, 104), (104, 103)], extra_tasks=(104,))
    assert messages(w, "CYCLE") == ["dependency cycle: T3 -> T4 -> T3"]


def test_a_diamond_is_not_a_cycle():
    assert analyze(edges_world([(101, 102), (101, 103), (102, 104), (103, 104)], extra_tasks=(104,))).findings == []


def test_a_task_reached_twice_is_walked_once():
    """T2 and T3 both lead to T4, and T4 leads back to T1: one loop through the first route, and the second route must not report it again."""
    w = edges_world([(101, 102), (101, 103), (102, 104), (103, 104), (104, 101)], extra_tasks=(104,))
    found = messages(w, "CYCLE")
    assert len(found) == 1 and found[0].startswith("dependency cycle: T1 -> T2 -> T4 -> T1")


def test_wbs_loop_message_lists_each_element_once_and_closes_the_loop():
    w = base_world()
    w["PROJWBS"] = [row("PROJWBS", 4, wbs_id=20, proj_id=10, parent_wbs_id=21, wbs_short_name="A"), row("PROJWBS", 13, wbs_id=21, proj_id=10, parent_wbs_id=20, wbs_short_name="B")]
    assert messages(w, "WBS_CYCLE") == ["WBS parents form a loop: 20 -> 21 -> 20"]


def test_a_wbs_chain_leading_into_a_loop_is_not_part_of_the_loop():
    w = base_world()
    w["PROJWBS"] = [row("PROJWBS", 4, wbs_id=20, proj_id=10, parent_wbs_id=21, wbs_short_name="A"), row("PROJWBS", 13, wbs_id=21, proj_id=10, parent_wbs_id=22, wbs_short_name="B"),
                    row("PROJWBS", 14, wbs_id=22, proj_id=10, parent_wbs_id=21, wbs_short_name="C")]
    assert messages(w, "WBS_CYCLE") == ["WBS parents form a loop: 21 -> 22 -> 21"]


def test_two_wbs_chains_into_one_loop_report_the_loop_once():
    w = base_world()
    w["PROJWBS"] = [row("PROJWBS", 4, wbs_id=20, proj_id=10, parent_wbs_id=22, wbs_short_name="A"), row("PROJWBS", 13, wbs_id=21, proj_id=10, parent_wbs_id=22, wbs_short_name="B"),
                    row("PROJWBS", 14, wbs_id=22, proj_id=10, parent_wbs_id=23, wbs_short_name="C"), row("PROJWBS", 15, wbs_id=23, proj_id=10, parent_wbs_id=22, wbs_short_name="D")]
    assert messages(w, "WBS_CYCLE") == ["WBS parents form a loop: 22 -> 23 -> 22"]


def test_a_finding_is_immutable_and_errors_lists_only_errors():
    f = Finding("R", "error", "TASK", "1", 4, "m")
    with pytest.raises(FrozenInstanceError):
        f.rule = "S"
    a = Analysis(findings=[f, Finding("W", "warning", None, None, None, "w"), Finding("I", "info", None, None, None, "i")])
    assert [x.rule for x in a.errors()] == ["R"]
    assert severity_counts(a.findings) == {"error": 1, "warning": 1, "info": 1}
    assert severity_counts([f, f]) == {"error": 2}
    assert severity_counts([]) == {}
    assert str(Finding("W", "warning", None, None, None, "w")) == "[warning] W file: w"
    assert str(Finding("R", "error", "TASK", None, None, "m")) == "[error] R TASK: m"
    assert str(f) == "[error] R TASK 1 (line 4): m"


def test_world_from_parse_leaves_out_repeated_blocks_and_unmodeled_tables():
    lines = ["ERMHDR\t1", "%T\tCALENDAR", "%F\tclndr_id", "%R\t1", "%T\tCALENDAR", "%F\tclndr_id", "%R\t2", "%T\tFOO", "%F\ta", "%R\t9", "%E"]
    world = world_from_parse(parse_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252")))
    assert [r.v["clndr_id"] for r in world["CALENDAR"]] == [1]
    assert "FOO" not in world
