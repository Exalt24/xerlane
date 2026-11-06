"""Fixtures for the APS tests that need PostgreSQL. They fail, never skip, when the database is not there (same rule as conftest.py).

The APS tests run in their OWN database (xerlane_aps_test, or the name in XERLANE_APS_TEST_DB), created at the start of the session and dropped at the end, so a run
of them never wipes the tables of the XER tests running at the same time on the same server, and each mutation worker gets a database of its own.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from xerlane import aps, db

APS_TABLES = ("aps_change_event", "aps_load_run", "aps_quarantine", "aps_object")
TENANT = "acme"


@pytest.fixture(scope="session")
def aps_admin_dsn():
    name = os.environ.get("XERLANE_APS_TEST_DB", "xerlane_aps_test")
    try:
        base = db.default_admin_dsn()
        with psycopg.connect(base, autocommit=True, connect_timeout=5) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error as exc:
        pytest.fail(f"PostgreSQL is not reachable ({exc}). Start it with: python tools/pg.py up", pytrace=False)
    except RuntimeError as exc:
        pytest.fail(f"{exc}", pytrace=False)
    dsn = db.retarget(base, dbname=name)
    db.init_db(dsn)
    aps.init_schema(dsn)
    yield dsn
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def aps_app_dsn(aps_admin_dsn):
    return db.retarget(db.default_dsn(), dbname=conninfo_to_dict(aps_admin_dsn)["dbname"])


@pytest.fixture()
def aps_clean(aps_admin_dsn):
    db.wipe(aps_admin_dsn)
    with psycopg.connect(aps_admin_dsn) as c:
        c.execute(f"TRUNCATE {', '.join('xerlane.' + t for t in APS_TABLES)} RESTART IDENTITY")
    return aps_admin_dsn


@pytest.fixture()
def aconn(aps_clean, aps_app_dsn):
    """A connection as the app role (no superuser, no BYPASSRLS) on the APS test database."""
    c = db.connect(aps_app_dsn)
    yield c
    c.close()


@pytest.fixture()
def aadmin(aps_clean):
    """Superuser connection, used to look past row level security and to damage data on purpose."""
    c = psycopg.connect(aps_clean, autocommit=True)
    c.execute("set search_path = xerlane")
    yield c
    c.close()


def table_state(admin_conn, tenant: str) -> dict[str, list[tuple]]:
    """All four APS tables of one tenant as sorted rows, generated ids included, for proving that a second run changed nothing."""
    out = {}
    for t in APS_TABLES:
        rows = admin_conn.execute(f"select * from {t} where tenant_id = %s order by 2", (tenant,)).fetchall()
        out[t] = [tuple(str(x) for x in r) for r in rows]
    return out
