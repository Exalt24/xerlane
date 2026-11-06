"""Connections, schema set up and the tenant setting.

Two Postgres roles, so row level security is real and not an accident of being a superuser:
  xerlane_owner  owns the tables (RLS is FORCED, so it is held to the policy too)
  xerlane_app    the role the loader uses: no superuser, no BYPASSRLS, only table privileges
`init_db` is run once with an administrator connection; everything else uses the app role.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SCHEMA = "xerlane"
APP_ROLE = "xerlane_app"
OWNER_ROLE = "xerlane_owner"
DEFAULT_PORT = 5435
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
INIT_LOCK = 74160001                    # no-mutate: any number does, it only has to be the same in every call (two init-db runs at once take turns on the schema and table creation)
DATA_TABLES = ("app_session", "app_user", "login_attempt", "cost_load_run", "cost_line", "commitment", "aps_quarantine", "aps_change_event", "aps_load_run", "aps_object", "quarantine", "change_event", "load_run", "taskrsrc", "taskpred", "task", "projwbs", "project", "rsrc", "calendar")


class UnsafeRole(RuntimeError):
    """The connection's role would skip row level security."""


def _dotenv() -> None:
    """Read KEY=VALUE lines from a .env in the working folder or the repo root. Real environment variables win. tools/pg.py writes this file."""
    for path in (Path.cwd() / ".env", ENV_FILE):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def password(var: str) -> str:
    _dotenv()
    value = os.environ.get(var)
    if not value:
        raise RuntimeError(f"{var} is not set: run `python tools/pg.py up`, which writes .env, or export it")
    return value


def retarget(dsn: str, **changes: str) -> str:
    """The same connection string with some parts (user, password, dbname, port, host) replaced."""
    parts = conninfo_to_dict(dsn)
    parts.update(changes)
    return make_conninfo(**parts)


def _base(user: str, pw: str) -> str:
    return make_conninfo(host=os.environ.get("XERLANE_PG_HOST", "localhost"), port=os.environ.get("XERLANE_PG_PORT", str(DEFAULT_PORT)),
                         dbname=os.environ.get("XERLANE_PG_DB", "xerlane"), user=user, password=pw)


def default_dsn() -> str:
    return os.environ.get("XERLANE_DSN") or _base(APP_ROLE, password("XERLANE_APP_PASSWORD"))


def default_admin_dsn() -> str:
    return os.environ.get("XERLANE_ADMIN_DSN") or _base("postgres", password("XERLANE_ADMIN_PASSWORD"))


def default_owner_dsn() -> str:
    """The table owner role on the same server and database as the app role (tests use it to show that FORCE row level security binds the owner too)."""
    return retarget(os.environ.get("XERLANE_DSN") or _base(APP_ROLE, ""), user=OWNER_ROLE, password=password("XERLANE_OWNER_PASSWORD"))


def init_db(admin_dsn: str, app_password: str | None = None, owner_password: str | None = None, *, schema: str = SCHEMA, app_role: str = APP_ROLE, owner_role: str = OWNER_ROLE) -> None:
    """Create the roles, the schema and the tables. Safe to run again. The role passwords come from XERLANE_APP_PASSWORD and XERLANE_OWNER_PASSWORD unless given."""
    app_password = app_password or password("XERLANE_APP_PASSWORD")
    owner_password = owner_password or password("XERLANE_OWNER_PASSWORD")
    ddl = resources.files("xerlane").joinpath("schema.sql").read_text(encoding="utf-8")
    aps_ddl = resources.files("xerlane").joinpath("aps_schema.sql").read_text(encoding="utf-8")
    cost_ddl = resources.files("xerlane").joinpath("cost_schema.sql").read_text(encoding="utf-8")
    web_ddl = resources.files("xerlane").joinpath("web_schema.sql").read_text(encoding="utf-8")
    sch, owner, app = sql.Identifier(schema), sql.Identifier(owner_role), sql.Identifier(app_role)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (INIT_LOCK,))          # released when this connection closes
        for role, pw in ((owner_role, owner_password), (app_role, app_password)):
            try:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(sql.Identifier(role), sql.Literal(pw)))
                conn.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(sql.Identifier(role), sch))
            except psycopg.errors.DuplicateObject:
                pass                                                      # a role that exists keeps its password and settings, whether it was made earlier or by another init a moment ago
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(sch, owner))
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (INIT_LOCK,))
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(owner))
        conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sch))
        conn.execute(ddl)
        conn.execute(aps_ddl)
        conn.execute(cost_ddl)
        conn.execute(web_ddl)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, app))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sch, app))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sch, app))


def wipe(admin_dsn: str) -> None:
    """Empty every table (tests and demos). Administrator connection."""
    with psycopg.connect(admin_dsn) as conn:                       # the with block commits
        conn.execute(f"TRUNCATE {', '.join(SCHEMA + '.' + t for t in DATA_TABLES)} RESTART IDENTITY")


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a connection as the app role and refuse a role that would bypass row level security."""
    conn = psycopg.connect(dsn or default_dsn(), connect_timeout=10)
    row = conn.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    conn.rollback()
    if row is None or row[1] or row[2]:
        conn.close()
        raise UnsafeRole(f"role {row[0] if row else '?'} is a superuser or bypasses row level security; connect as {APP_ROLE}")
    return conn


@contextmanager
def tenant_tx(conn: psycopg.Connection, tenant: str) -> Iterator[psycopg.Cursor]:
    """One transaction in which row level security shows only `tenant`. Commits on exit, rolls back on an exception."""
    if not tenant or not tenant.strip():
        raise ValueError("tenant id must not be empty")
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("tenant_tx needs a connection with no open transaction (a nested block would not commit)")
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        yield cur
