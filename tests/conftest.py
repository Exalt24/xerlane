"""Shared fixtures. Tests that need PostgreSQL FAIL when it is not there; they never skip.

Start the database with `python tools/pg.py up` (Docker, port 5435). CI starts a service container on the same port.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from xerlane import db  # noqa: E402

import make_sample_xer as gen  # noqa: E402


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    try:
        dsn = db.default_admin_dsn()
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("select 1")
    except psycopg.Error as exc:
        pytest.fail(f"PostgreSQL is not reachable ({exc}). Start it with: python tools/pg.py up", pytrace=False)
    except RuntimeError as exc:
        pytest.fail(f"{exc}", pytrace=False)
    db.init_db(dsn)
    return dsn


@pytest.fixture()
def clean(admin_dsn):
    """An empty database for one test."""
    db.wipe(admin_dsn)
    return admin_dsn


@pytest.fixture()
def conn(clean):
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def admin(clean):
    """Superuser connection, used only to damage data on purpose and to look past row level security."""
    c = psycopg.connect(clean, autocommit=True)
    c.execute("set search_path = xerlane")
    yield c
    c.close()


@pytest.fixture()
def xer(tmp_path):
    """make(seed, tasks, projects, defects) -> (path, planted)"""
    def _make(seed: int = 3, tasks: int = 120, projects: int = 1, defects: list[str] | None = None, name: str = "plan.xer", id_base: int = 1000):
        data, planted = gen.make(seed, tasks, projects, defects or [], id_base)
        path = tmp_path / name
        path.write_bytes(data)
        return str(path), planted
    return _make


def snapshot(admin_conn, tenant: str) -> dict[str, list[tuple]]:
    """Every data table of one tenant as sorted rows without the tenant id and the generated run ids; for comparing two states."""
    out: dict[str, list[tuple]] = {}
    for t in ("calendar", "rsrc", "project", "projwbs", "task", "taskpred", "taskrsrc"):
        rows = admin_conn.execute(f"select * from {t} where tenant_id = %s order by 2", (tenant,)).fetchall()
        out[t] = [tuple(str(x) for x in r[1:]) for r in rows]
    q = admin_conn.execute("select table_name, line_no, reason_code, reason, raw::text from quarantine where tenant_id = %s order by 1, 2", (tenant,)).fetchall()
    out["quarantine"] = [tuple(str(x) for x in r) for r in q]
    return out


TENANT = "acme"
os.environ.setdefault("PYTHONHASHSEED", "0")
