"""Connection settings, schema set up and the safety checks around them."""
from __future__ import annotations

import concurrent.futures as cf
import os
import secrets

import psycopg
import pytest

from xerlane import db

pytestmark = pytest.mark.db


def test_init_db_run_by_several_processes_at_once_does_not_fail(admin_dsn):
    """Found by running the mutation check on parallel workers: two inits raced on creating the roles and the tables. Fresh role names make the race include creating the roles."""
    tag = os.getpid()                                                       # unique per process: two test runs at once must not share the names
    name, owner, app, schema = f"xl_concurrent_{tag}", f"xl_cc_owner_{tag}", f"xl_cc_app_{tag}", "xl_cc"
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')
        for r in (owner, app):
            c.execute(f'drop role if exists "{r}"')
        c.execute(f'create database "{name}"')
    dsn = db.retarget(admin_dsn, dbname=name)
    try:
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for f in [ex.submit(db.init_db, dsn, secrets.token_hex(8), secrets.token_hex(8), schema=schema, app_role=app, owner_role=owner) for _ in range(6)]:
                f.result()
        with psycopg.connect(dsn) as c:
            tables = {r[0] for r in c.execute("select tablename from pg_tables where schemaname = %s", (schema,)).fetchall()}
        assert tables == {"calendar", "rsrc", "project", "projwbs", "task", "taskpred", "taskrsrc", "load_run", "change_event", "quarantine", "aps_object", "aps_load_run", "aps_change_event", "aps_quarantine",
                      "cost_line", "commitment", "cost_load_run", "app_user", "app_session", "login_attempt"}
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as c:
            c.execute(f'drop database if exists "{name}" with (force)')
            for r in (owner, app):
                c.execute(f'drop role if exists "{r}"')


def test_init_db_keeps_an_existing_roles_password(admin_dsn):
    with psycopg.connect(db.retarget(admin_dsn, user="postgres")) as c:
        before = c.execute("select rolpassword from pg_authid where rolname = 'xerlane_app'").fetchone()
    db.init_db(admin_dsn)
    with psycopg.connect(admin_dsn) as c:
        assert c.execute("select rolpassword from pg_authid where rolname = 'xerlane_app'").fetchone() == before


def test_roles_cannot_bypass_row_security_and_only_the_app_role_logs_in_for_the_loader(admin_dsn):
    with psycopg.connect(admin_dsn) as c:
        rows = c.execute("select rolname, rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname in ('xerlane_app', 'xerlane_owner') order by 1").fetchall()
    assert rows == [("xerlane_app", False, False, True), ("xerlane_owner", False, False, True)]


def test_the_app_role_owns_nothing_and_cannot_change_the_schema(conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("create table xerlane.sneaky (x int)")
    conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("alter table xerlane.task disable row level security")
    conn.rollback()


def test_retarget_changes_only_what_it_is_given():
    dsn = db.retarget("host=h port=1 dbname=a user=u password=p", dbname="b", port="2")
    parts = psycopg.conninfo.conninfo_to_dict(dsn)
    assert parts == {"host": "h", "port": "2", "dbname": "b", "user": "u", "password": "p"}


def test_password_needs_an_environment_variable_and_says_which(monkeypatch):
    monkeypatch.delenv("XERLANE_TEST_PW", raising=False)
    with pytest.raises(RuntimeError, match="XERLANE_TEST_PW"):
        db.password("XERLANE_TEST_PW")
    monkeypatch.setenv("XERLANE_TEST_PW", "from-env")
    assert db.password("XERLANE_TEST_PW") == "from-env"


def test_dotenv_fills_gaps_and_never_overrides_the_environment(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("# comment\n# XERLANE_T_THREE=commented-out\n=no-name\n  \nXERLANE_T_ONE=from-file\nXERLANE_T_TWO=file-two\nnot a pair\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XERLANE_T_ONE", raising=False)
    monkeypatch.setenv("XERLANE_T_TWO", "from-env")
    monkeypatch.setattr(db, "ENV_FILE", tmp_path / "missing.env")
    try:
        assert db.password("XERLANE_T_ONE") == "from-file"
        assert db.password("XERLANE_T_TWO") == "from-env"
        assert "XERLANE_T_THREE" not in os.environ and "# XERLANE_T_THREE" not in os.environ and "" not in os.environ
    finally:
        os.environ.pop("XERLANE_T_ONE", None)


def test_explicit_dsn_variables_win_over_built_ones(monkeypatch):
    monkeypatch.setenv("XERLANE_DSN", "host=x dbname=y user=z password=w")
    monkeypatch.setenv("XERLANE_ADMIN_DSN", "host=x dbname=y user=a password=w")
    assert db.default_dsn() == "host=x dbname=y user=z password=w"
    assert db.default_admin_dsn() == "host=x dbname=y user=a password=w"


def test_built_dsns_use_the_documented_port_and_roles(monkeypatch):
    monkeypatch.delenv("XERLANE_DSN", raising=False)
    monkeypatch.delenv("XERLANE_ADMIN_DSN", raising=False)
    monkeypatch.delenv("XERLANE_PG_PORT", raising=False)
    monkeypatch.setenv("XERLANE_APP_PASSWORD", "pw1")
    monkeypatch.setenv("XERLANE_ADMIN_PASSWORD", "pw2")
    app, admin = (psycopg.conninfo.conninfo_to_dict(d) for d in (db.default_dsn(), db.default_admin_dsn()))
    assert (app["user"], app["port"], app["password"]) == ("xerlane_app", "5435", "pw1")
    assert (admin["user"], admin["port"], admin["password"]) == ("postgres", "5435", "pw2")


def test_wipe_empties_every_table(conn, xer, admin_dsn):
    from xerlane.load import load_file
    path, _ = xer(tasks=30, defects=["bad_number"])
    load_file(conn, "t", path)
    db.wipe(admin_dsn)
    with psycopg.connect(admin_dsn) as c:
        for t in db.DATA_TABLES:
            assert c.execute(f"select count(*) from xerlane.{t}").fetchone()[0] == 0, t


def test_tenant_tx_refuses_a_connection_that_is_already_in_a_transaction(conn):
    conn.execute("select 1")
    with pytest.raises(RuntimeError):
        with db.tenant_tx(conn, "t"):
            pass
    conn.rollback()


def test_built_dsns_follow_the_host_port_and_database_variables(monkeypatch):
    for var in ("XERLANE_DSN", "XERLANE_ADMIN_DSN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XERLANE_PG_HOST", "db.example.test")
    monkeypatch.setenv("XERLANE_PG_PORT", "6543")
    monkeypatch.setenv("XERLANE_PG_DB", "other")
    monkeypatch.setenv("XERLANE_APP_PASSWORD", "a")
    monkeypatch.setenv("XERLANE_ADMIN_PASSWORD", "b")
    monkeypatch.setenv("XERLANE_OWNER_PASSWORD", "c")
    got = {name: psycopg.conninfo.conninfo_to_dict(fn()) for name, fn in (("app", db.default_dsn), ("admin", db.default_admin_dsn), ("owner", db.default_owner_dsn))}
    for parts in got.values():
        assert (parts["host"], parts["port"], parts["dbname"]) == ("db.example.test", "6543", "other")
    assert [got[k]["user"] for k in ("app", "admin", "owner")] == ["xerlane_app", "postgres", "xerlane_owner"]
    assert [got[k]["password"] for k in ("app", "admin", "owner")] == ["a", "b", "c"]


def test_the_owner_dsn_follows_an_explicit_app_dsn(monkeypatch):
    monkeypatch.setenv("XERLANE_DSN", "host=h port=1 dbname=worker user=xerlane_app password=p")
    monkeypatch.setenv("XERLANE_OWNER_PASSWORD", "o")
    assert psycopg.conninfo.conninfo_to_dict(db.default_owner_dsn()) == {"host": "h", "port": "1", "dbname": "worker", "user": "xerlane_owner", "password": "o"}


def test_init_db_on_a_fresh_server_state_creates_the_roles_with_the_given_passwords_and_a_working_schema(admin_dsn):
    """Roles and schema names are parameters, so the creation path (a role that does not exist yet) can be run for real without touching the roles other tests use."""
    tag = os.getpid()                                                       # unique per process: roles are shared by the whole server, two test runs at once must not collide
    name, owner, app, schema = f"xl_roles_{tag}", f"xl_probe_owner_{tag}", f"xl_probe_app_{tag}", "xl_probe"
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')
        for r in (owner, app):
            c.execute(f'drop role if exists "{r}"')
        c.execute(f'create database "{name}"')
    dsn = db.retarget(admin_dsn, dbname=name)
    try:
        app_pw, owner_pw, other_pw = (secrets.token_hex(8) for _ in range(3))          # generated per run, nothing to commit
        db.init_db(dsn, app_pw, owner_pw, schema=schema, app_role=app, owner_role=owner)
        with psycopg.connect(db.retarget(dsn, user=app, password=app_pw)) as c:
            assert c.execute("select count(*) from task").fetchone() == (0,), "the app role's search_path points at the new schema"
            assert c.execute("select rolsuper or rolbypassrls from pg_roles where rolname = current_user").fetchone() == (False,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"create table {schema}.x (a int)")
            c.rollback()
            c.execute("select set_config('app.tenant_id', 't', true)")
            c.execute("insert into load_run (tenant_id, proj_id, kind, source_label, source_sha256) values ('t', 1, 'initial', 'x', 'y')")
            assert c.execute("select run_id from load_run").fetchone() == (1,), "the app role can use the sequences behind the generated ids"
            c.rollback()
        with psycopg.connect(db.retarget(dsn, user=owner, password=owner_pw)) as c:
            assert c.execute("select relforcerowsecurity from pg_class where relname = 'task'").fetchone() == (True,)
            assert c.execute("select tableowner from pg_tables where tablename = 'task'").fetchone() == (owner,), "the tables belong to the owner role, not to the administrator who ran init"
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(db.retarget(dsn, user=app, password=other_pw), connect_timeout=5)
        db.init_db(dsn, other_pw, other_pw, schema=schema, app_role=app, owner_role=owner)                 # a second run keeps the first passwords
        psycopg.connect(db.retarget(dsn, user=app, password=app_pw)).close()
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as c:
            c.execute(f'drop database if exists "{name}" with (force)')
            for r in (owner, app):
                c.execute(f'drop role if exists "{r}"')


class FakeConn:
    """Stands in for a connection so connect()'s own behaviour can be seen: what it asks the driver for, and that it hangs up on a role it refuses."""
    def __init__(self, role_row):
        self.role_row, self.closed, self.rolled_back = role_row, False, False

    def execute(self, *_):
        return self

    def fetchone(self):
        return self.role_row

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_connect_asks_for_a_ten_second_timeout_and_returns_the_connection_for_a_safe_role(monkeypatch):
    seen = {}
    fake = FakeConn(("xerlane_app", False, False))
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: seen.update(dsn=dsn, **kw) or fake)
    assert db.connect("host=h dbname=d") is fake
    assert seen == {"dsn": "host=h dbname=d", "connect_timeout": 10} and fake.rolled_back and not fake.closed


@pytest.mark.parametrize("row", [("x", True, False), ("x", False, True), ("x", True, True)])
def test_connect_hangs_up_on_a_role_that_would_bypass_row_security(monkeypatch, row):
    fake = FakeConn(row)
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: fake)
    with pytest.raises(db.UnsafeRole, match="x"):
        db.connect("host=h")
    assert fake.closed


def test_connect_without_a_dsn_uses_the_default_one(monkeypatch):
    seen = {}
    monkeypatch.setenv("XERLANE_DSN", "host=default-host dbname=d user=u password=p")
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: seen.update(dsn=dsn) or FakeConn(("u", False, False)))
    db.connect()
    assert seen["dsn"] == "host=default-host dbname=d user=u password=p"
