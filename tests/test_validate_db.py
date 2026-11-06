"""The same rules run on what is stored: world_from_db and validate_db against a real database."""
from __future__ import annotations

import pytest

from conftest import TENANT
from xerlane.db import tenant_tx
from xerlane.load import load_file
from xerlane.validate import validate_db, world_from_db

pytestmark = pytest.mark.db

WIPE = "truncate xerlane.quarantine, xerlane.change_event, xerlane.load_run, xerlane.taskrsrc, xerlane.taskpred, xerlane.task, xerlane.projwbs, xerlane.project, xerlane.rsrc, xerlane.calendar"


@pytest.fixture()
def stored(conn, xer):
    path, _ = xer(seed=12, tasks=60, projects=2)
    load_file(conn, TENANT, path)
    return conn


def test_a_clean_load_has_no_finding_for_all_projects_or_for_one(stored):
    with tenant_tx(stored, TENANT) as cur:
        assert validate_db(cur, TENANT) == []
        assert validate_db(cur, TENANT, [1010]) == []
        assert validate_db(cur, TENANT, [1011]) == []


def test_world_from_db_returns_every_row_without_a_filter_and_only_the_named_project_with_one(stored):
    with tenant_tx(stored, TENANT) as cur:
        everything = world_from_db(cur, TENANT)
        one = world_from_db(cur, TENANT, [1010])
        none = world_from_db(cur, TENANT, [])
    assert len(everything["TASK"]) == 60 and len(everything["PROJECT"]) == 2
    assert len(one["TASK"]) == 30 and [r.v["proj_id"] for r in one["PROJECT"]] == [1010]
    assert {r.v["proj_id"] for r in one["TASKPRED"]} == {1010}
    assert len(one["CALENDAR"]) == len(everything["CALENDAR"]) == 3, "global tables come whole whichever projects are asked for"
    assert none["TASK"] == [] and none["PROJECT"] == [] and len(none["RSRC"]) == 12


def test_world_from_db_rows_are_keyed_by_their_primary_key_and_ordered(stored):
    with tenant_tx(stored, TENANT) as cur:
        w = world_from_db(cur, TENANT)
    ids = [r.ref for r in w["TASK"]]
    assert ids == sorted(ids) and all(r.ref == r.v["task_id"] and r.line is None for r in w["TASK"])


def test_world_from_db_sees_only_the_tenants_own_rows(stored, xer):
    other, _ = xer(seed=12, tasks=10, name="other.xer")
    load_file(stored, "someone-else", other)
    with tenant_tx(stored, TENANT) as cur:
        assert len(world_from_db(cur, TENANT)["TASK"]) == 60
    with tenant_tx(stored, "someone-else") as cur:
        assert len(world_from_db(cur, "someone-else")["TASK"]) == 10


def test_validate_db_names_a_cycle_written_behind_the_loaders_back(stored, admin):
    row = admin.execute("select task_id, pred_task_id, proj_id from xerlane.taskpred where tenant_id = %s order by task_pred_id limit 1", (TENANT,)).fetchone()
    admin.execute("insert into xerlane.taskpred (tenant_id, task_pred_id, task_id, pred_task_id, proj_id, pred_proj_id, pred_type) values (%s, 999999, %s, %s, %s, %s, 'PR_FS')", (TENANT, row[1], row[0], row[2], row[2]))
    with tenant_tx(stored, TENANT) as cur:
        findings = validate_db(cur, TENANT, [row[2]])
    assert [f.rule for f in findings] == ["CYCLE"] and findings[0].key == "999999"
    other = 1011 if row[2] == 1010 else 1010
    with tenant_tx(stored, TENANT) as cur:
        assert validate_db(cur, TENANT, [other]) == [], "the other project is not blamed"


def test_validate_db_finds_a_duplicate_task_code_when_the_unique_key_is_missing(stored, admin):
    admin.execute("alter table xerlane.task drop constraint task_tenant_id_proj_id_task_code_key")
    try:
        a, b = admin.execute("select task_id from xerlane.task where tenant_id = %s and proj_id = 1010 order by task_id limit 2", (TENANT,)).fetchall()
        admin.execute("update xerlane.task set task_code = (select task_code from xerlane.task where task_id = %s and tenant_id = %s) where task_id = %s and tenant_id = %s", (a[0], TENANT, b[0], TENANT))
        with tenant_tx(stored, TENANT) as cur:
            findings = validate_db(cur, TENANT)
        assert (findings[0].rule, findings[0].key) == ("DUP_TASK_CODE", str(b[0]))
        assert {f.rule for f in findings[1:]} <= {"MISSING_TASK", "ORPHAN_ASSIGNMENT"}, "the rest are the relationships and assignments that follow the rejected task"
    finally:
        admin.execute(WIPE)
        admin.execute("alter table xerlane.task add constraint task_tenant_id_proj_id_task_code_key unique (tenant_id, proj_id, task_code) deferrable initially deferred")
