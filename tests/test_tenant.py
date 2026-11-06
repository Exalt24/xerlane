"""Tenant isolation. Every claim starts with a positive control: the same query that must come back empty is first shown to return rows for the owner."""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg import sql

from conftest import snapshot
from xerlane import db
from xerlane.db import UnsafeRole, tenant_tx
from xerlane.load import load_file
from xerlane.reconcile import reconcile
from xerlane.schema import TABLES

pytestmark = pytest.mark.db

DATA_TABLES = [t.pg for t in TABLES] + ["load_run", "change_event", "quarantine", "aps_object", "aps_load_run", "aps_change_event", "aps_quarantine"]


def owner_login(admin):
    """Make sure the table owner role can log in with the password from the environment. A database made by an older run may hold another; the password is
    only reset when a login fails, because two workers altering the same role at once fail with 'tuple concurrently updated'."""
    try:
        psycopg.connect(db.default_owner_dsn(), connect_timeout=5).close()
    except psycopg.OperationalError:
        admin.execute(sql.SQL("alter role xerlane_owner with password {}").format(sql.Literal(db.password("XERLANE_OWNER_PASSWORD"))))


def count(conn, tenant, table):
    with tenant_tx(conn, tenant) as cur:
        cur.execute(f"select count(*) from {table}")
        return cur.fetchone()[0]


@pytest.fixture()
def two(conn, xer):
    """The SAME file (same ids everywhere) loaded for tenant a, and a different-sized one for tenant b with the same ids too."""
    a, _ = xer(seed=11, tasks=60, name="a.xer")
    b, _ = xer(seed=11, tasks=90, name="b.xer")
    load_file(conn, "tenant-a", a)
    return conn, a, b


def test_positive_control_owner_sees_own_rows(two):
    conn, a, _ = two
    assert count(conn, "tenant-a", "task") == 60 and count(conn, "tenant-a", "taskpred") > 60


def test_other_tenant_sees_nothing_of_the_first(two):
    conn, _, _ = two
    for table in DATA_TABLES:
        assert count(conn, "tenant-b", table) == 0, table


def test_no_tenant_setting_means_no_rows_at_all(two):
    conn, _, _ = two
    for table in DATA_TABLES:
        assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
    conn.rollback()


def test_empty_tenant_setting_means_no_rows(two):
    conn, _, _ = two
    with conn.transaction():
        conn.execute("select set_config('app.tenant_id', '', true)")
        assert conn.execute("select count(*) from task").fetchone()[0] == 0


def test_same_ids_in_two_tenants_do_not_collide_and_do_not_leak(two):
    conn, a, b = two
    r = load_file(conn, "tenant-b", b)
    assert r.status == "loaded"
    assert count(conn, "tenant-a", "task") == 60 and count(conn, "tenant-b", "task") == 90
    with tenant_tx(conn, "tenant-a") as cur:
        cur.execute("select distinct tenant_id from task")
        assert cur.fetchall() == [("tenant-a",)]
        cur.execute("select count(*) from task where task_id in (select task_id from task)")
        assert cur.fetchone()[0] == 60


def test_cannot_read_another_tenants_row_by_its_exact_key(two, admin):
    conn, a, b = two
    load_file(conn, "tenant-b", b)
    key = admin.execute("select task_id from task where tenant_id = 'tenant-b' order by task_id desc limit 1").fetchone()[0]
    assert admin.execute("select count(*) from task where task_id = %s and tenant_id = 'tenant-b'", (key,)).fetchone()[0] == 1          # control: the row exists
    with tenant_tx(conn, "tenant-a") as cur:
        cur.execute("select count(*) from task where task_id = %s and tenant_id = 'tenant-b'", (key,))
        assert cur.fetchone()[0] == 0


def test_cannot_write_a_row_for_another_tenant(two):
    conn, _, _ = two
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(conn, "tenant-a") as cur:
            cur.execute("insert into calendar (tenant_id, clndr_id) values ('tenant-b', 5000)")
    with tenant_tx(conn, "tenant-a") as cur:                                                          # control: the same insert for the own tenant works
        cur.execute("insert into calendar (tenant_id, clndr_id) values ('tenant-a', 5000)")


def test_cannot_update_or_delete_another_tenants_rows(two, admin):
    conn, a, b = two
    load_file(conn, "tenant-b", b)
    before = snapshot(admin, "tenant-b")
    with tenant_tx(conn, "tenant-a") as cur:
        cur.execute("update task set task_name = 'hijacked' where tenant_id = 'tenant-b'")
        assert cur.rowcount == 0
        cur.execute("delete from task where tenant_id = 'tenant-b'")
        assert cur.rowcount == 0
        cur.execute("update task set task_name = 'mine' where tenant_id = 'tenant-a'")                # control
        assert cur.rowcount == 60
    assert snapshot(admin, "tenant-b") == before


def test_cannot_move_a_row_into_another_tenant_by_update(two):
    conn, _, _ = two
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(conn, "tenant-a") as cur:
            cur.execute("update calendar set tenant_id = 'tenant-b' where clndr_id = 1000")


def test_every_table_with_tenant_id_has_row_security_enabled_and_forced(admin):
    rows = admin.execute("""
        select c.relname, c.relrowsecurity, c.relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'xerlane' and c.relkind = 'r'
          and exists (select 1 from pg_attribute a where a.attrelid = c.oid and a.attname = 'tenant_id' and not a.attisdropped)""").fetchall()
    assert {r[0] for r in rows} == set(DATA_TABLES)
    assert all(r[1] and r[2] for r in rows), [r for r in rows if not (r[1] and r[2])]


def test_every_table_has_a_policy_that_checks_reads_and_writes(admin):
    rows = admin.execute("select tablename, qual, with_check from pg_policies where schemaname = 'xerlane'").fetchall()
    assert {r[0] for r in rows} == set(DATA_TABLES)
    assert all("app.tenant_id" in r[1] and "app.tenant_id" in r[2] for r in rows)


def test_table_owner_is_held_to_the_policy_too(two, admin):
    """FORCE matters for the owner role: it is not a superuser, so without FORCE it would see everything."""
    owner_login(admin)
    with psycopg.connect(db.default_owner_dsn()) as owner:
        assert owner.execute("select rolsuper or rolbypassrls from pg_roles where rolname = current_user").fetchone()[0] is False
        assert owner.execute("select count(*) from task").fetchone()[0] == 0                          # no tenant set, owner sees nothing
        with owner.transaction():
            owner.execute("select set_config('app.tenant_id', 'tenant-a', true)")
            assert owner.execute("select count(*) from task").fetchone()[0] == 60                     # control: with the setting, its own rows
            owner.execute("select set_config('app.tenant_id', 'tenant-b', true)")
            assert owner.execute("select count(*) from task").fetchone()[0] == 0


def test_without_force_the_owner_would_see_everything(two, admin):
    """The deliberate breakage that proves the test above can fail: turn FORCE off and the owner reads another tenant's rows."""
    owner_login(admin)
    admin.execute("alter table xerlane.task no force row level security")
    try:
        with psycopg.connect(db.default_owner_dsn()) as owner:
            assert owner.execute("select count(*) from task").fetchone()[0] == 60
    finally:
        admin.execute("alter table xerlane.task force row level security")


def test_the_loader_refuses_a_superuser_connection(clean):
    with pytest.raises(UnsafeRole):
        db.connect(clean)


def test_the_loader_refuses_a_role_that_bypasses_row_security(admin):
    role = f"sneaky_{os.getpid()}"                              # roles are server wide: a second test run at the same time must not share the name
    admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role)))
    admin.execute(sql.SQL("create role {} login password 'x' bypassrls").format(sql.Identifier(role)))
    try:
        with pytest.raises(UnsafeRole):
            db.connect(db.retarget(db.default_dsn(), user=role, password="x"))
    finally:
        admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


def test_tenant_id_must_not_be_empty():
    with pytest.raises(ValueError):
        with tenant_tx(None, "  "):
            pass


def test_the_database_refuses_an_empty_tenant_id(admin):
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("insert into xerlane.calendar (tenant_id, clndr_id) values ('', 1)")


def test_reconcile_for_one_tenant_ignores_the_others_rows(two):
    conn, a, b = two
    load_file(conn, "tenant-b", b)
    assert reconcile(conn, "tenant-a", a).ok and reconcile(conn, "tenant-b", b).ok
    assert not reconcile(conn, "tenant-b", a).ok, "tenant b holds the other file, not this one"


def test_quarantine_and_change_log_are_isolated_too(conn, xer, admin):
    path, _ = xer(seed=2, tasks=50, defects=["bad_number"])
    load_file(conn, "tenant-a", path)
    assert count(conn, "tenant-a", "quarantine") > 0 and count(conn, "tenant-a", "load_run") == 1          # controls
    assert count(conn, "tenant-b", "quarantine") == 0 and count(conn, "tenant-b", "load_run") == 0
