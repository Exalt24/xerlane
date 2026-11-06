"""APS loader, reconcile and tenant isolation against a real PostgreSQL (its own database, see aps_dbfix.py). These tests fail, never skip, when Postgres is down."""
from __future__ import annotations

import json
import threading
import time

import psycopg
import pytest

from aps_dbfix import APS_TABLES, TENANT, aadmin, aconn, aps_admin_dsn, aps_app_dsn, aps_clean, table_state  # noqa: F401  (fixtures)
from aps_stand_in import GUID_2D, GUID_3D, URN, VIEWS, aps_server, make_client, obj, sample_collection  # noqa: F401
from xerlane import aps, db
from xerlane.aps import ApsPayloadError, load_view, reconcile, sync_model
from xerlane.db import tenant_tx

pytestmark = pytest.mark.db


def q(conn, tenant, query, args=()):
    with tenant_tx(conn, tenant) as cur:
        cur.execute(query, args)
        return cur.fetchall()


def load(conn, collection, tenant=TENANT, urn=URN, guid=GUID_3D, **kw):
    return load_view(conn, tenant, urn, guid, collection, **kw)


def add_project(admin, tenant, proj_id, codes):
    """XER rows made by hand (a calendar, a project, a WBS node and tasks) so reconcile has tasks to compare with."""
    admin.execute("insert into calendar (tenant_id, clndr_id) values (%s, 1) on conflict do nothing", (tenant,))
    admin.execute("insert into project (tenant_id, proj_id, proj_short_name, clndr_id) values (%s, %s, %s, 1)", (tenant, proj_id, f"P{proj_id}"))
    admin.execute("insert into projwbs (tenant_id, wbs_id, proj_id, wbs_short_name) values (%s, %s, %s, 'W')", (tenant, proj_id * 1000, proj_id))
    for i, code in enumerate(codes, 1):
        admin.execute("insert into task (tenant_id, task_id, proj_id, wbs_id, clndr_id, task_code, task_name) values (%s, %s, %s, %s, 1, %s, %s)",
                      (tenant, proj_id * 1000 + i, proj_id, proj_id * 1000, code, f"Task {code}"))


# ---- schema and roles ----------------------------------------------------------------------------------------------------------------------------

def test_every_aps_table_has_forced_row_level_security_and_one_tenant_policy(aadmin):
    rows = aadmin.execute("select c.relname, c.relrowsecurity, c.relforcerowsecurity, (select count(*) from pg_policy p where p.polrelid = c.oid) "
                          "from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'xerlane' and c.relname like 'aps\\_%' and c.relkind = 'r' order by 1").fetchall()
    assert [r[0] for r in rows] == sorted(APS_TABLES)
    assert all(r[1] and r[2] and r[3] == 1 for r in rows), rows


def test_the_join_view_runs_with_the_callers_privileges(aadmin):
    opts = aadmin.execute("select reloptions from pg_class where relname = 'aps_object_task'").fetchone()[0]
    assert "security_invoker=true" in opts


def test_every_aps_table_has_tenant_id_in_its_primary_key(aadmin):
    for t in APS_TABLES:
        cols = aadmin.execute("select a.attname from pg_index i join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey) "
                              "where i.indrelid = %s::regclass and i.indisprimary", (f"xerlane.{t}",)).fetchall()
        assert "tenant_id" in [c[0] for c in cols], t


def test_init_schema_is_safe_to_run_again_and_keeps_the_data(aconn, aps_clean):
    load(aconn, sample_collection())
    aps.init_schema(aps_clean)
    aps.init_schema(aps_clean)
    assert q(aconn, TENANT, "select count(*) from aps_object")[0][0] == 4


def test_the_app_role_can_use_every_aps_table_and_the_view(aconn):
    for t in (*APS_TABLES, "aps_object_task"):
        assert q(aconn, TENANT, f"select count(*) from {t}")[0][0] == 0


# ---- first load ----------------------------------------------------------------------------------------------------------------------------------

def test_first_load_stores_every_row_one_run_and_added_events(aconn):
    res = load(aconn, sample_collection())
    assert (res.status, res.added, res.changed, res.removed, res.unchanged, res.quarantined) == ("changed", 4, 0, 0, 0, 0)
    rows = q(aconn, TENANT, "select object_id, external_id, name, category, task_code, properties->>'Name', length(content_hash) from aps_object order by 1")
    assert rows == [(1, "ext-1", "Model", None, None, "Model", 64), (2, "ext-2", "Wall A", "Revit Walls", "A1000", "Wall A", 64),
                    (3, "ext-3", "Slab", None, "A1010", "Slab", 64), (4, "ext-4", "Door", None, "NOPE-1", "Door", 64)]
    run = q(aconn, TENANT, "select run_id, model_urn, view_guid, counts from aps_load_run")
    assert run == [(res.run_id, URN, GUID_3D, {"added": 4, "changed": 0, "removed": 0, "unchanged": 0, "quarantined": 0})]
    ev = q(aconn, TENANT, "select kind, object_id, fields, old_hash, new_hash is not null from aps_change_event order by event_id")
    assert ev == [("OBJECT_ADDED", i, None, None, True) for i in (1, 2, 3, 4)]


def test_properties_are_stored_as_json_with_their_nesting(aconn):
    load(aconn, [obj(1, "Wall", Dimensions={"Width": "200 mm", "Layers": [1, 2, {"m": "brick"}]}, Flag=True, Nothing=None, Ratio=0.5)])
    props = q(aconn, TENANT, "select properties from aps_object")[0][0]
    assert props["Dimensions"] == {"Width": "200 mm", "Layers": [1, 2, {"m": "brick"}]} and props["Flag"] is True and props["Nothing"] is None and props["Ratio"] == 0.5


def test_two_views_of_one_model_are_independent(aconn):
    load(aconn, [obj(1), obj(2)], guid=GUID_3D)
    load(aconn, [obj(1)], guid=GUID_2D)
    load(aconn, [obj(1)], guid=GUID_3D)                              # 3d loses object 2; the 2d view is not touched
    assert set(q(aconn, TENANT, "select view_guid, object_id from aps_object")) == {(GUID_3D, 1), (GUID_2D, 1)}


def test_the_same_object_id_in_two_models_is_two_rows(aconn):
    load(aconn, [obj(1, "a")], urn="bW9kZWwtYQ")
    load(aconn, [obj(1, "b")], urn="bW9kZWwtYg")
    assert q(aconn, TENANT, "select model_urn, name from aps_object order by name") == [("bW9kZWwtYQ", "a"), ("bW9kZWwtYg", "b")]


# ---- idempotence ---------------------------------------------------------------------------------------------------------------------------------

def sequences(admin):
    return admin.execute("select sequencename, last_value from pg_sequences where schemaname = 'xerlane' and sequencename like 'aps\\_%' order by 1").fetchall()


def test_second_run_of_the_same_payload_writes_nothing(aconn, aadmin):
    payload = sample_collection() + [{"name": "no id"}, obj(9), obj(9)]
    first = load(aconn, payload)
    assert first.quarantined == 3 and first.quarantined_new == 3
    xmins = aadmin.execute("select object_id, xmin::text from aps_object order by 1").fetchall()
    state, seqs = table_state(aadmin, TENANT), sequences(aadmin)
    second = load(aconn, payload)
    assert (second.status, second.run_id, second.added, second.changed, second.removed, second.unchanged) == ("unchanged", None, 0, 0, 0, 4)
    assert (second.quarantined, second.quarantined_new) == (3, 0)
    assert table_state(aadmin, TENANT) == state
    assert aadmin.execute("select object_id, xmin::text from aps_object order by 1").fetchall() == xmins          # no row version was written
    assert sequences(aadmin) == seqs                                                                                # not even a sequence value was used


def test_a_payload_in_a_different_key_order_is_still_a_no_op(aconn):
    load(aconn, sample_collection())
    shuffled = [dict(reversed(list(o.items()))) for o in reversed(sample_collection())]
    res = load(aconn, shuffled)
    assert res.status == "unchanged" and res.unchanged == 4


# ---- changes -------------------------------------------------------------------------------------------------------------------------------------

def test_a_changed_property_updates_the_row_and_writes_one_change_event(aconn):
    load(aconn, sample_collection())
    old_hash = q(aconn, TENANT, "select content_hash from aps_object where object_id = 3")[0][0]
    changed = sample_collection()
    changed[2]["properties"]["Width"] = "300 mm"
    res = load(aconn, changed)
    assert (res.status, res.added, res.changed, res.removed, res.unchanged) == ("changed", 0, 1, 0, 3)
    new_hash = q(aconn, TENANT, "select content_hash from aps_object where object_id = 3")[0][0]
    assert new_hash != old_hash
    assert q(aconn, TENANT, "select properties->>'Width' from aps_object where object_id = 3") == [("300 mm",)]
    ev = q(aconn, TENANT, "select kind, object_id, name, fields, old_hash, new_hash from aps_change_event where run_id = %s", (res.run_id,))
    assert ev == [("OBJECT_CHANGED", 3, "Slab", "properties", old_hash, new_hash)]
    assert q(aconn, TENANT, "select count(*) from aps_load_run")[0][0] == 2


def test_the_change_event_names_every_column_that_changed(aconn):
    load(aconn, [obj(1, "Wall", task_code="A1", **{"__category__": {"Category": "Walls"}})])
    moved = obj(1, "Wall 2", external="new-ext", task_code="A2", **{"__category__": {"Category": "Floors"}})
    moved["properties"]["Name"] = "Wall"
    load(aconn, [moved])
    fields = q(aconn, TENANT, "select fields from aps_change_event where kind = 'OBJECT_CHANGED'")[0][0]
    assert fields == "external_id,name,category,task_code,properties"
    row = q(aconn, TENANT, "select external_id, name, category, task_code from aps_object")
    assert row == [("new-ext", "Wall 2", "Floors", "A2")]


def test_a_change_only_in_the_name_is_reported_as_the_name(aconn):
    load(aconn, [obj(1, "Wall")])
    renamed = obj(1, "Wall")
    renamed["name"] = "Wall (renamed)"
    load(aconn, [renamed])
    assert q(aconn, TENANT, "select fields from aps_change_event where kind = 'OBJECT_CHANGED'") == [("name",)]


def test_an_object_that_leaves_the_payload_is_removed_with_an_event(aconn):
    load(aconn, sample_collection())
    hash2 = q(aconn, TENANT, "select content_hash from aps_object where object_id = 2")[0][0]
    res = load(aconn, [o for o in sample_collection() if o["objectid"] != 2])
    assert (res.added, res.changed, res.removed, res.unchanged) == (0, 0, 1, 3)
    assert [r[0] for r in q(aconn, TENANT, "select object_id from aps_object order by 1")] == [1, 3, 4]
    assert q(aconn, TENANT, "select kind, object_id, external_id, name, old_hash, new_hash from aps_change_event where run_id = %s", (res.run_id,)) == \
        [("OBJECT_REMOVED", 2, "ext-2", "Wall A", hash2, None)]


def test_added_changed_and_removed_in_one_load_share_one_run(aconn):
    load(aconn, [obj(1), obj(2), obj(3)])
    edited = [obj(1, "Renamed"), obj(3), obj(4)]
    res = load(aconn, edited)
    assert (res.added, res.changed, res.removed, res.unchanged) == (1, 1, 1, 1)
    ev = q(aconn, TENANT, "select kind, object_id from aps_change_event where run_id = %s order by event_id", (res.run_id,))
    assert ev == [("OBJECT_ADDED", 4), ("OBJECT_CHANGED", 1), ("OBJECT_REMOVED", 2)]
    assert q(aconn, TENANT, "select counts from aps_load_run where run_id = %s", (res.run_id,))[0][0] == {"added": 1, "changed": 1, "removed": 1, "unchanged": 1, "quarantined": 0}


def test_a_change_event_cannot_outlive_its_run(aadmin, aconn):
    load(aconn, [obj(1)])
    aadmin.execute("delete from aps_load_run")
    assert aadmin.execute("select count(*) from aps_change_event").fetchone()[0] == 0


# ---- quarantine ----------------------------------------------------------------------------------------------------------------------------------

BAD = [{"name": "no id"}, {"objectid": "x"}, {"objectid": 50, "name": 5}, {"objectid": 51, "properties": [1]}, {"objectid": 52, "name": "a\x00b"},
       {"objectid": 53, "properties": {"blob": "x" * 70_000}}, "junk", obj(60, "first dup"), obj(60, "second dup")]
BAD_CODES = ["NO_OBJECT_ID", "BAD_OBJECT_ID", "BAD_FIELD_TYPE", "BAD_PROPERTIES", "BAD_CHARACTER", "VALUE_TOO_LARGE", "MALFORMED_OBJECT", "DUP_OBJECT_ID", "DUP_OBJECT_ID"]


def test_bad_items_are_quarantined_with_a_reason_and_the_good_ones_still_load(aconn):
    res = load(aconn, [obj(1), *BAD, obj(2)])
    assert (res.added, res.quarantined, res.quarantined_new) == (2, 9, 9)
    assert [r[0] for r in q(aconn, TENANT, "select object_id from aps_object order by 1")] == [1, 2]
    rows = q(aconn, TENANT, "select item_index, reason_code, reason <> '' from aps_quarantine order by item_index")
    assert rows == [(i + 1, code, True) for i, code in enumerate(BAD_CODES)]


def test_quarantined_raw_is_readable_and_never_holds_what_postgres_cannot_store(aconn):
    load(aconn, [*BAD])
    raw = {r[0]: r[1] for r in q(aconn, TENANT, "select item_index, raw from aps_quarantine")}
    assert raw[0] == {"name": "no id"} and raw[1] == {"objectid": "x"} and raw[6] == "junk"
    assert raw[4] == {"unstorable": True, "objectid": 52}
    assert raw[5]["truncated"] is True and raw[5]["objectid"] == 53 and raw[5]["bytes"] > 70_000
    assert len(json.dumps(raw[5])) < 200


def test_the_same_payload_again_adds_no_quarantine_rows_but_a_changed_one_does(aconn):
    payload = [obj(1), {"name": "no id"}]
    load(aconn, payload)
    load(aconn, payload)
    assert q(aconn, TENANT, "select count(*) from aps_quarantine")[0][0] == 1
    res = load(aconn, [obj(1), obj(2), {"name": "no id"}])                       # the same bad item inside a different payload is a new record
    assert res.quarantined_new == 1
    assert q(aconn, TENANT, "select count(*) from aps_quarantine")[0][0] == 2


def test_a_payload_with_only_new_quarantine_rows_still_writes_a_run(aconn):
    load(aconn, [obj(1)])
    res = load(aconn, [obj(1), {"name": "no id"}])
    assert (res.status, res.added, res.changed, res.removed, res.quarantined_new) == ("changed", 0, 0, 0, 1)
    assert q(aconn, TENANT, "select counts->>'quarantined' from aps_load_run where run_id = %s", (res.run_id,)) == [("1",)]
    assert q(aconn, TENANT, "select count(*) from aps_change_event where run_id = %s", (res.run_id,)) == [(0,)]


def test_an_object_that_turns_bad_leaves_the_table_and_lands_in_quarantine(aconn):
    load(aconn, [obj(1), obj(2)])
    res = load(aconn, [obj(1), {"objectid": 2, "name": 5}])
    assert res.removed == 1 and res.quarantined_new == 1
    assert [r[0] for r in q(aconn, TENANT, "select object_id from aps_object")] == [1]
    assert q(aconn, TENANT, "select kind, object_id from aps_change_event where run_id = %s", (res.run_id,)) == [("OBJECT_REMOVED", 2)]


def test_an_empty_payload_would_delete_everything_so_it_is_refused(aconn):
    load(aconn, [obj(1), obj(2)])
    with pytest.raises(ApsPayloadError, match="2 are stored"):
        load(aconn, [])
    with pytest.raises(ApsPayloadError):
        load(aconn, [{"name": "all bad"}])
    assert q(aconn, TENANT, "select count(*) from aps_object")[0][0] == 2
    assert q(aconn, TENANT, "select count(*) from aps_quarantine")[0][0] == 0                 # the refused load rolled back completely
    res = load(aconn, [], allow_empty=True)
    assert res.removed == 2 and q(aconn, TENANT, "select count(*) from aps_object")[0][0] == 0


def test_an_empty_payload_on_an_empty_view_is_a_harmless_no_op(aconn):
    res = load(aconn, [])
    assert res.status == "unchanged" and q(aconn, TENANT, "select count(*) from aps_load_run")[0][0] == 0


def test_a_collection_that_is_not_a_list_is_refused(aconn):
    with pytest.raises(ApsPayloadError):
        load(aconn, {"objectid": 1})


def test_limits_can_be_passed_to_the_loader(aconn):
    res = load(aconn, [obj(1, task_code="X1"), obj(2, Plan={"Activity ID": "Y2"})], max_properties_bytes=43, task_code_keys=("Activity ID",))
    assert res.quarantined == 1                                                                 # obj(1) has 45 bytes of properties, obj(2) has 43
    res = load(aconn, [obj(2, Plan={"Activity ID": "Y2"})], task_code_keys=("Activity ID",))
    assert q(aconn, TENANT, "select task_code from aps_object") == [("Y2",)]


def test_a_failure_in_the_middle_of_a_load_leaves_nothing_half_written(aconn, monkeypatch):
    load(aconn, [obj(1), obj(2)])
    before = q(aconn, TENANT, "select object_id, content_hash from aps_object order by 1")

    def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setattr(aps, "_changed_fields", boom)
    with pytest.raises(RuntimeError):
        load(aconn, [obj(1, "changed"), obj(3), {"name": "bad"}])
    monkeypatch.undo()
    assert q(aconn, TENANT, "select object_id, content_hash from aps_object order by 1") == before
    assert q(aconn, TENANT, "select count(*) from aps_load_run")[0][0] == 1
    assert q(aconn, TENANT, "select count(*) from aps_quarantine")[0][0] == 0
    assert aconn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_two_loads_of_the_same_view_take_turns(aps_app_dsn, aps_clean, aadmin):
    """A second loader for the same tenant and view must wait for the first to commit, so it reads the rows the first wrote and is a no-op, not a primary key clash."""
    holder, runner = db.connect(aps_app_dsn), db.connect(aps_app_dsn)
    out: dict = {}
    try:
        cm = tenant_tx(holder, TENANT)
        cur = cm.__enter__()
        cur.execute("select pg_advisory_xact_lock(%s)", (aps.view_lock_key(TENANT, URN, GUID_3D),))
        t = threading.Thread(target=lambda: out.update(res=load(runner, [obj(1)])))
        t.start()
        end = time.monotonic() + 8
        waiting = 0
        while time.monotonic() < end and not waiting:
            waiting = aadmin.execute("select count(*) from pg_locks where locktype = 'advisory' and not granted").fetchone()[0]
            time.sleep(0.05)
        assert waiting == 1, "the second load did not wait for the lock"
        assert t.is_alive() and "res" not in out
        cm.__exit__(None, None, None)
        t.join(10)
        assert not t.is_alive() and out["res"].status == "changed"
        # a different view of the same model is NOT blocked by that lock
        cm = tenant_tx(holder, TENANT)
        cur = cm.__enter__()
        cur.execute("select pg_advisory_xact_lock(%s)", (aps.view_lock_key(TENANT, URN, GUID_3D),))
        assert load(runner, [obj(1)], guid=GUID_2D).status == "changed"
        cm.__exit__(None, None, None)
    finally:
        holder.close()
        runner.close()


def test_view_lock_keys_differ_by_tenant_urn_and_view_and_fit_a_bigint():
    keys = {aps.view_lock_key(*k) for k in [("a", "u", "g"), ("b", "u", "g"), ("a", "v", "g"), ("a", "u", "h"), ("au", "", "g"), ("a", "ug", "")]}
    assert len(keys) == 6 and all(-2**63 <= k < 2**63 for k in keys)
    assert aps.view_lock_key("a", "u", "g") == aps.view_lock_key("a", "u", "g")


# ---- tenant isolation ----------------------------------------------------------------------------------------------------------------------------

def test_a_second_tenant_sees_none_of_the_first_tenants_rows_in_any_table(aconn):
    load(aconn, sample_collection() + [{"name": "bad"}])
    load(aconn, [obj(1), obj(2, "x")], urn="b3RoZXI")
    for t in APS_TABLES:
        assert q(aconn, TENANT, f"select count(*) from {t}")[0][0] > 0
        assert q(aconn, "globex", f"select count(*) from {t}")[0][0] == 0, t


def test_no_tenant_setting_means_no_rows(aconn):
    load(aconn, sample_collection())
    for t in (*APS_TABLES, "aps_object_task"):
        assert aconn.execute(f"select count(*) from {t}").fetchone()[0] == 0
        aconn.rollback()


def test_an_empty_tenant_setting_means_no_rows(aconn):
    load(aconn, sample_collection())
    with aconn.transaction():
        aconn.execute("select set_config('app.tenant_id', '', true)")
        assert aconn.execute("select count(*) from aps_object").fetchone()[0] == 0


def test_the_same_view_loaded_by_two_tenants_stays_separate(aconn):
    load(aconn, [obj(1, "acme wall"), obj(2)], tenant="acme")
    load(aconn, [obj(1, "globex wall")], tenant="globex")
    assert q(aconn, "acme", "select object_id, name from aps_object order by 1") == [(1, "acme wall"), (2, "Wall")]
    assert q(aconn, "globex", "select object_id, name from aps_object order by 1") == [(1, "globex wall")]
    load(aconn, [obj(1, "globex wall v2")], tenant="globex")
    assert q(aconn, "acme", "select name from aps_object where object_id = 1") == [("acme wall",)]
    assert [r[0] for r in q(aconn, "acme", "select object_id from aps_object order by 1")] == [1, 2]            # globex's removal of object 2 never touched acme


def test_a_tenant_cannot_write_a_row_for_another_tenant(aconn):
    load(aconn, [obj(1)])
    row = "insert into aps_object (tenant_id, model_urn, view_guid, object_id, content_hash) values (%s, 'u', 'g', 9, repeat('a', 64))"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        q(aconn, "globex", row % "'acme'")
    with tenant_tx(aconn, "globex") as cur:
        cur.execute("update aps_object set name = 'hijacked' where tenant_id = 'acme'")
        assert cur.rowcount == 0
        cur.execute("delete from aps_object where tenant_id = 'acme'")
        assert cur.rowcount == 0
    assert q(aconn, TENANT, "select name from aps_object") == [("Wall",)]


def test_a_tenant_cannot_move_its_own_row_into_another_tenant(aconn):
    load(aconn, [obj(1)])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        q(aconn, TENANT, "update aps_object set tenant_id = 'globex'")


def test_the_table_owner_is_held_to_the_policy_too(aconn, aps_app_dsn):
    load(aconn, sample_collection())
    owner_dsn = db.retarget(aps_app_dsn, user=db.OWNER_ROLE, password=db.password("XERLANE_OWNER_PASSWORD"))
    with psycopg.connect(owner_dsn) as owner:
        for t in APS_TABLES:
            assert owner.execute(f"select count(*) from {t}").fetchone()[0] == 0, t
        owner.rollback()
        with owner.transaction():
            owner.execute("select set_config('app.tenant_id', 'globex', true)")
            assert owner.execute("select count(*) from aps_object").fetchone()[0] == 0


def test_the_join_view_shows_each_tenant_only_its_own_rows(aconn, aadmin):
    add_project(aadmin, "acme", 1, ["A1000", "A1010"])
    add_project(aadmin, "globex", 7, ["A1000"])
    load(aconn, sample_collection(), tenant="acme")
    load(aconn, [obj(1, "globex wall", task_code="A1000")], tenant="globex")
    assert q(aconn, "acme", "select object_id, proj_id, task_code from aps_object_task order by 1") == [(2, 1, "A1000"), (3, 1, "A1010")]
    assert q(aconn, "globex", "select object_id, proj_id, task_code, name from aps_object_task") == [(1, 7, "A1000", "globex wall")]
    assert aconn.execute("select count(*) from aps_object_task").fetchone()[0] == 0


# ---- join view and reconcile ---------------------------------------------------------------------------------------------------------------------

def test_join_view_links_an_object_to_the_task_with_the_same_code(aconn, aadmin):
    add_project(aadmin, TENANT, 1, ["A1000", "A1010", "A1020"])
    load(aconn, sample_collection())
    rows = q(aconn, TENANT, "select object_id, name, category, task_code, proj_id, task_id, task_name from aps_object_task order by object_id")
    assert rows == [(2, "Wall A", "Revit Walls", "A1000", 1, 1001, "Task A1000"), (3, "Slab", None, "A1010", 1, 1002, "Task A1010")]


def test_reconcile_reports_objects_with_no_task_and_tasks_with_no_object(aconn, aadmin):
    add_project(aadmin, TENANT, 1, ["A1000", "A1010", "A1020"])
    load(aconn, sample_collection())
    rep = reconcile(aconn, TENANT)
    assert (rep.objects, rep.matched_objects, rep.no_task_code, rep.no_task, rep.tasks, rep.tasks_without_object) == (4, 2, 1, 1, 3, 1)
    assert not rep.clean
    assert [(o.object_id, o.name, o.task_code) for o in rep.objects_without_task_code] == [(1, "Model", None)]
    assert [(o.object_id, o.name, o.task_code) for o in rep.objects_without_task] == [(4, "Door", "NOPE-1")]
    assert [(t.proj_id, t.task_id, t.task_code, t.task_name) for t in rep.tasks_missing_object] == [(1, 1003, "A1020", "Task A1020")]
    text = rep.render()
    assert "2 matched" in text and "NOPE-1" in text and "A1020" in text and "tasks 3: 1 have no object" in text


def test_reconcile_is_clean_when_everything_lines_up(aconn, aadmin):
    add_project(aadmin, TENANT, 1, ["A1", "A2"])
    load(aconn, [obj(1, task_code="A1"), obj(2, task_code="A2"), obj(3, task_code="A2")])
    rep = reconcile(aconn, TENANT)
    assert rep.clean and rep.matched_objects == 3 and rep.render().count("\n") == 1


def test_reconcile_does_not_change_either_side(aconn, aadmin):
    add_project(aadmin, TENANT, 1, ["A1000", "A1020"])
    load(aconn, sample_collection())
    tasks = aadmin.execute("select task_id, xmin::text from task order by 1").fetchall()
    state = table_state(aadmin, TENANT)
    reconcile(aconn, TENANT)
    assert aadmin.execute("select task_id, xmin::text from task order by 1").fetchall() == tasks and table_state(aadmin, TENANT) == state


def test_reconcile_on_empty_tables_is_clean_and_zero(aconn):
    rep = reconcile(aconn, TENANT)
    assert rep.clean and (rep.objects, rep.tasks) == (0, 0)


def test_reconcile_lists_are_cut_at_the_limit_but_the_counts_are_exact(aconn, aadmin):
    add_project(aadmin, TENANT, 1, [f"T{i}" for i in range(5)])
    load(aconn, [obj(i) for i in range(1, 8)])
    rep = reconcile(aconn, TENANT, limit=3)
    assert rep.no_task_code == 7 and len(rep.objects_without_task_code) == 3 and [o.object_id for o in rep.objects_without_task_code] == [1, 2, 3]
    assert rep.tasks_without_object == 5 and len(rep.tasks_missing_object) == 3
    load(aconn, [obj(i, task_code=f"Z{i}") for i in range(1, 8)])
    assert len(reconcile(aconn, TENANT, limit=2).objects_without_task) == 2


def test_reconcile_scopes_by_project_and_by_model(aconn, aadmin):
    add_project(aadmin, TENANT, 1, ["A1", "A2"])
    add_project(aadmin, TENANT, 2, ["A1", "B9"])
    load(aconn, [obj(1, task_code="A1"), obj(2, task_code="B9")], urn="bW9kZWwtYQ")
    load(aconn, [obj(1, task_code="A2")], urn="bW9kZWwtYg")
    both = reconcile(aconn, TENANT)
    assert (both.matched_objects, both.no_task, both.tasks_without_object) == (3, 0, 0)
    p1 = reconcile(aconn, TENANT, proj_id=1)                                                     # project 1 has no B9
    assert (p1.tasks, p1.matched_objects, p1.no_task, p1.tasks_without_object) == (2, 2, 1, 0)
    assert [o.task_code for o in p1.objects_without_task] == ["B9"]
    p2 = reconcile(aconn, TENANT, proj_id=2)                                                     # project 2 has no A2
    assert (p2.tasks, p2.matched_objects, p2.no_task, p2.tasks_without_object) == (2, 2, 1, 0)
    ma = reconcile(aconn, TENANT, model_urn="bW9kZWwtYQ")
    assert (ma.objects, ma.matched_objects) == (2, 2) and ma.tasks_without_object == 1 and [t.task_code for t in ma.tasks_missing_object] == ["A2"]
    both_scoped = reconcile(aconn, TENANT, model_urn="bW9kZWwtYQ", proj_id=1)
    assert (both_scoped.tasks, both_scoped.tasks_without_object, both_scoped.no_task) == (2, 1, 1)


def test_reconcile_never_mixes_tenants(aconn, aadmin):
    add_project(aadmin, "acme", 1, ["A1"])
    add_project(aadmin, "globex", 5, ["G1", "G2"])
    load(aconn, [obj(1, task_code="A1")], tenant="acme")
    load(aconn, [obj(1, task_code="A1"), obj(2, task_code="G1")], tenant="globex")
    a, g = reconcile(aconn, "acme"), reconcile(aconn, "globex")
    assert a.clean and (a.objects, a.tasks) == (1, 1)
    assert (g.objects, g.tasks, g.matched_objects, g.no_task, g.tasks_without_object) == (2, 2, 1, 1, 1)


def test_reconcile_against_tasks_loaded_by_the_real_xer_loader(aconn, xer):
    from xerlane.load import load_file
    path, _ = xer(seed=3, tasks=30)
    load_file(aconn, TENANT, path)
    codes = [r[0] for r in q(aconn, TENANT, "select task_code from task order by task_id")]
    assert len(codes) >= 10
    load(aconn, [obj(i, f"Element {i}", task_code=c) for i, c in enumerate(codes[:6], 1)] + [obj(99, "Orphan", task_code="NO-SUCH-TASK"), obj(100, "Loose")])
    rep = reconcile(aconn, TENANT)
    assert (rep.objects, rep.matched_objects, rep.no_task, rep.no_task_code) == (8, 6, 1, 1)
    assert rep.tasks == len(codes) and rep.tasks_without_object == len(codes) - 6
    assert sorted(t.task_code for t in rep.tasks_missing_object) == sorted(codes[6:])


# ---- sync from the stand-in ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def model(aps_server):
    aps_server.add_model(URN, VIEWS, {GUID_3D: sample_collection(), GUID_2D: [obj(1, "Level 1")]})
    return aps_server


def test_sync_pulls_every_view_and_loads_it(model, aconn):
    out = sync_model(make_client(model), aconn, TENANT, URN)
    assert [(o.view_guid, o.role, o.status, o.load.added) for o in out] == [(GUID_3D, "3d", "changed", 4), (GUID_2D, "2d", "changed", 1)]
    assert set(q(aconn, TENANT, "select view_guid, count(*) from aps_object group by 1")) == {(GUID_3D, 4), (GUID_2D, 1)}


def test_a_second_sync_of_an_unchanged_model_is_a_no_op(model, aconn, aadmin):
    sync_model(make_client(model), aconn, TENANT, URN)
    state = table_state(aadmin, TENANT)
    out = sync_model(make_client(model), aconn, TENANT, URN)
    assert [o.status for o in out] == ["unchanged", "unchanged"] and table_state(aadmin, TENANT) == state


def test_sync_can_be_limited_to_view_roles(model, aconn):
    out = sync_model(make_client(model), aconn, TENANT, URN, roles=("3d",))
    assert [o.view_guid for o in out] == [GUID_3D]
    assert model.count("GET", GUID_2D) == 0


def test_a_view_that_never_gets_ready_fails_alone_and_the_others_load(model, aconn):
    model.models[URN]["pending"][GUID_3D] = 99
    out = sync_model(make_client(model, max_polls=2), aconn, TENANT, URN)
    assert [o.status for o in out] == ["failed", "changed"]
    assert "still not ready" in out[0].error and out[0].load is None
    assert q(aconn, TENANT, "select view_guid from aps_object") == [(GUID_2D,)]


def test_a_malformed_payload_view_fails_alone(model, aconn):
    model.models[URN]["raw"][GUID_2D] = b"not json"
    out = sync_model(make_client(model), aconn, TENANT, URN)
    assert [o.status for o in out] == ["changed", "failed"]


def test_an_empty_answer_for_a_loaded_view_is_refused_as_a_failed_view(model, aconn):
    sync_model(make_client(model), aconn, TENANT, URN)
    model.models[URN]["props"][GUID_3D] = []
    out = sync_model(make_client(model), aconn, TENANT, URN)
    assert out[0].status == "failed" and "delete them all" in out[0].error
    assert q(aconn, TENANT, "select count(*) from aps_object where view_guid = %s", (GUID_3D,)) == [(4,)]


def test_a_changed_model_on_the_next_sync_shows_up_as_change_events(model, aconn):
    sync_model(make_client(model), aconn, TENANT, URN)
    new = sample_collection()
    new[0]["name"] = "Model v2"
    new.append(obj(5, "Beam"))
    model.models[URN]["props"][GUID_3D] = new
    out = sync_model(make_client(model), aconn, TENANT, URN)
    assert (out[0].load.added, out[0].load.changed, out[1].status) == (1, 1, "unchanged")


# ---- command line --------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def cli_env(monkeypatch, model, aps_app_dsn, aps_admin_dsn):
    monkeypatch.setenv("APS_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("APS_CLIENT_SECRET", "test-secret-DO-NOT-LOG-0123456789")
    monkeypatch.setenv("APS_BASE_URL", model.url)
    monkeypatch.setenv("XERLANE_DSN", aps_app_dsn)
    monkeypatch.setenv("XERLANE_ADMIN_DSN", aps_admin_dsn)
    return model


def test_cli_init_schema_sync_and_reconcile(cli_env, aps_clean, aadmin, capsys):
    assert aps.main(["init-schema"]) == 0
    assert "APS tables ready" in capsys.readouterr().out
    assert aps.main(["sync", "--tenant", TENANT, "--urn", URN]) == 0
    out = capsys.readouterr().out
    assert f"view {GUID_3D} (3d): changed, +4 ~0 -0, 0 unchanged, 0 set aside" in out and f"view {GUID_2D} (2d): changed, +1" in out
    assert aadmin.execute("select count(*) from aps_object").fetchone()[0] == 5
    add_project(aadmin, TENANT, 1, ["A1000", "A1010", "A1020"])
    assert aps.main(["reconcile", "--tenant", TENANT, "--urn", URN, "--proj-id", "1"]) == 1               # not clean: exit code 1
    assert "task without object: A1020" in capsys.readouterr().out
    assert aps.main(["sync", "--tenant", TENANT, "--urn", URN, "--roles", "3d, 2d"]) == 0
    assert "unchanged" in capsys.readouterr().out


def test_cli_exit_codes_for_a_failed_view_and_for_missing_credentials(cli_env, aps_clean, monkeypatch, capsys):
    cli_env.models[URN]["pending"][GUID_3D] = 99
    monkeypatch.setattr(aps.ApsClient, "__init__", _fast(aps.ApsClient.__init__))
    assert aps.main(["sync", "--tenant", TENANT, "--urn", URN]) == 1
    assert "FAILED" in capsys.readouterr().out
    monkeypatch.delenv("APS_CLIENT_SECRET")
    assert aps.main(["sync", "--tenant", TENANT, "--urn", URN]) == 2
    assert "APS_CLIENT_ID and APS_CLIENT_SECRET" in capsys.readouterr().err


def _fast(orig):
    """The same constructor with no real waiting, for CLI runs that would otherwise poll for real."""
    def init(self, *a, **kw):
        kw.setdefault("sleep", lambda s: None)
        kw["max_polls"] = 2
        orig(self, *a, **kw)
    return init
