"""Property tests: random schedules, random damage to the file, and the invariants that must hold whatever the damage.

Invariants checked below
  parse      never raises on any bytes; every problem points at a real line; a rendered file parses back to exactly the rows that were rendered
  accounting every row of a modeled table in the file is either stored or quarantined (counted three ways: by the file, by the loader, by the independent reconcile)
  idempotent loading the same file twice changes nothing
  converge   loading v1 then v2 leaves the same project data as loading v2 alone
  defects    a planted defect is always reported under its code, and a file-level defect always rejects the file and writes nothing
  tenants    two tenants with the same ids never see each other, whatever the tenant ids look like
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import make_sample_xer as gen
from conftest import snapshot
from xerlane import db
from xerlane.db import tenant_tx
from xerlane.load import load_file
from xerlane.parse import parse_bytes
from xerlane.reconcile import reconcile

DB_SETTINGS = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
PURE_SETTINGS = settings(max_examples=200, deadline=None)

worlds = st.fixed_dictionaries({"seed": st.integers(0, 10**6), "tasks": st.integers(3, 40), "projects": st.integers(1, 3)})
JUNK = [b"", b"abc", b"-1", b"999999999", b"1.5.5", b"2026-02-30 00:00", b"x\ty", b"\x81", b"12", b"0", b"PR_ZZ", b"1,5"]
OPS = ["delete", "dup", "field", "swap", "truncate", "drop_end", "blank"]
ops_strategy = st.lists(st.tuples(st.sampled_from(OPS), st.integers(0, 10**6), st.integers(0, 10**6), st.sampled_from(JUNK)), min_size=0, max_size=4)
PROJECT_TABLES = ("project", "projwbs", "task", "taskpred", "taskrsrc")


def mutate(data: bytes, ops) -> bytes:
    lines = data.split(b"\r\n")
    for op, a, b, junk in ops:
        if len(lines) < 3:
            break
        i = 1 + a % (len(lines) - 1)
        if op == "delete":
            del lines[i]
        elif op == "dup":
            lines.insert(i, lines[i])
        elif op == "swap":
            j = 1 + b % (len(lines) - 1)
            lines[i], lines[j] = lines[j], lines[i]
        elif op == "field":
            parts = lines[i].split(b"\t")
            if parts[0] == b"%R" and len(parts) > 1:
                parts[1 + b % (len(parts) - 1)] = junk
                lines[i] = b"\t".join(parts)
        elif op == "truncate":
            joined = b"\r\n".join(lines)
            lines = joined[: max(1, a % len(joined))].split(b"\r\n")
        elif op == "drop_end":
            lines = [x for x in lines if x != b"%E"]
        elif op == "blank":
            lines.insert(i, b"")
    return b"\r\n".join(lines)


def count_modeled_rows(data: bytes) -> int:
    """A deliberately naive count of %R lines under the seven modeled tables, independent of the parser and of reconcile."""
    names = {b"CALENDAR", b"RSRC", b"PROJECT", b"PROJWBS", b"TASK", b"TASKPRED", b"TASKRSRC"}
    cur, n, ended = None, 0, False
    for ln in data.split(b"\n")[1:]:
        ln = ln.rstrip(b"\r")
        if ended:
            break
        if ln.startswith(b"%T\t"):
            cur = ln.split(b"\t")[1] if ln.count(b"\t") == 1 else None
        elif ln == b"%E" or ln.startswith(b"%E\t"):
            ended = True
        elif (ln == b"%R" or ln.startswith(b"%R\t")) and cur in names:
            n += 1
    return n


class Session:
    """A clean database, an app connection and an administrator connection for one Hypothesis example."""
    def __init__(self, admin_dsn: str) -> None:
        import psycopg
        db.wipe(admin_dsn)
        self.conn = db.connect()
        self.admin = psycopg.connect(admin_dsn, autocommit=True)
        self.admin.execute("set search_path = xerlane")
        self.tmp = tempfile.TemporaryDirectory()

    def file(self, data: bytes, name: str = "plan.xer") -> str:
        p = Path(self.tmp.name) / name
        p.write_bytes(data)
        return str(p)

    def close(self) -> None:
        self.conn.close()
        self.admin.close()
        self.tmp.cleanup()


def stored_total(s: Session, tenant: str) -> int:
    return sum(len(v) for k, v in snapshot(s.admin, tenant).items())


# ---- pure parser properties -----------------------------------------------------------------------------------------------------------------------

@PURE_SETTINGS
@given(st.binary(max_size=3000))
def test_parser_never_raises_and_problem_lines_exist(data):
    r = parse_bytes(data)
    total = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    for p in r.problems:
        assert p.line is None or 1 <= p.line <= max(total, 1)
    for row in r.all_rows():
        assert 1 <= row.line <= max(total, 1)
        for p in row.problems:
            assert p.line == row.line


@PURE_SETTINGS
@given(worlds)
def test_rendered_file_parses_back_to_exactly_the_rendered_rows(w):
    tables = gen.build_tables(w["seed"], w["tasks"], w["projects"])
    r = parse_bytes(gen.render(tables))
    assert not r.fatal and not [p for p in r.problems if p.severity != "info"]
    for t in tables:
        got = [list(row.raw) for row in r.block(t.name).rows]
        assert got == t.rows, t.name
        assert r.block(t.name).fields == t.fields


@PURE_SETTINGS
@given(worlds, ops_strategy)
def test_every_percent_r_line_becomes_a_row_whatever_the_damage(w, ops):
    data = mutate(gen.make(w["seed"], w["tasks"], w["projects"])[0], ops)
    r = parse_bytes(data)
    expected = count_modeled_rows(data)
    got = sum(len(b.rows) for b in r.blocks if b.modeled)
    if r.header is not None:
        assert got == expected


# ---- database properties --------------------------------------------------------------------------------------------------------------------------

@pytest.mark.db
@DB_SETTINGS
@given(worlds, ops_strategy)
def test_nothing_is_lost_reconcile_passes_and_a_reload_changes_nothing(admin_dsn, w, ops):
    s = Session(admin_dsn)
    try:
        data = mutate(gen.make(w["seed"], w["tasks"], w["projects"])[0], ops)
        path = s.file(data)
        r = load_file(s.conn, "t1", path)
        if r.status == "rejected":
            assert stored_total(s, "t1") == 0, "a rejected file must leave nothing behind"
            return
        rec = reconcile(s.conn, "t1", path, sample=10_000)
        assert rec.ok, [str(f) for f in rec.findings][:3]
        assert sum(ln.file_rows for ln in rec.lines) == count_modeled_rows(data)
        for ln in rec.lines:
            assert ln.file_rows == ln.stored + ln.quarantined
        assert len(r.quarantined) == sum(ln.quarantined for ln in rec.lines)
        before = snapshot(s.admin, "t1")
        again = load_file(s.conn, "t1", path)
        assert again.status == "unchanged" and snapshot(s.admin, "t1") == before
    finally:
        s.close()


@pytest.mark.db
@DB_SETTINGS
@given(worlds, ops_strategy, ops_strategy)
def test_loading_v1_then_v2_equals_loading_v2_alone(admin_dsn, w, ops1, ops2):
    s = Session(admin_dsn)
    try:
        base = gen.make(w["seed"], w["tasks"], w["projects"])[0]
        v1, v2 = mutate(base, ops1), mutate(base, ops2)
        r1 = load_file(s.conn, "inc", s.file(v1, "plan.xer"))
        before = snapshot(s.admin, "inc")
        r2 = load_file(s.conn, "inc", s.file(v2, "plan.xer"))
        if r2.status == "rejected":
            assert snapshot(s.admin, "inc") == before, "a rejected newer file must not change what is stored"
            return
        fresh = load_file(s.conn, "fresh", s.file(v2, "plan.xer"))
        assume(r1.status != "rejected")
        assume({p.proj_id for p in r1.projects} == {p.proj_id for p in fresh.projects})          # a project missing from v2 is kept, by design
        a, b = snapshot(s.admin, "inc"), snapshot(s.admin, "fresh")
        for t in PROJECT_TABLES:
            assert a[t] == b[t], t
        assert a["quarantine"] == b["quarantine"]
        for t in ("calendar", "rsrc"):
            assert set(b[t]) <= set(a[t]), "global rows are only added or updated, never removed by a later file"
        rec = reconcile(s.conn, "inc", s.file(v2, "plan.xer"), sample=10_000)
        assert {x.table for x in rec.findings} <= {"CALENDAR", "RSRC"}, "only a global row kept from v1 and quarantined in v2 may differ"
    finally:
        s.close()


@pytest.mark.db
@DB_SETTINGS
@given(worlds, st.sampled_from(sorted(gen.DEFECTS)))
def test_a_planted_defect_is_always_reported_under_its_code(admin_dsn, w, kind):
    s = Session(admin_dsn)
    try:
        tasks = max(w["tasks"], 12)
        data, planted = gen.make(w["seed"], tasks, w["projects"], defects=[kind])
        r = load_file(s.conn, "t1", s.file(data))
        expect, fatal = set(planted[0].expect), gen.DEFECTS[kind][2]
        if fatal:
            assert r.status == "rejected" and stored_total(s, "t1") == 0
            assert expect <= {f.rule for f in r.findings}
            return
        codes = {f.rule for f in r.findings if f.severity == "error"} | {q.code for q in r.quarantined}
        assert expect <= codes, f"{kind}: expected {expect}, reported {codes}"
        assert reconcile(s.conn, "t1", s.file(data), sample=10_000).ok
        assert r.exit_code == 1
    finally:
        s.close()


tenant_ids = st.text(alphabet="abAB01%_'\"; -", min_size=1, max_size=6).filter(lambda t: t.strip() != "")


@pytest.mark.db
@DB_SETTINGS
@given(worlds, worlds, tenant_ids, tenant_ids)
def test_tenants_never_see_each_other(admin_dsn, wa, wb, ta, tb):
    assume(ta != tb)
    s = Session(admin_dsn)
    try:
        # the same ids in both tenants whenever the two worlds share a seed: identical keys must not collide or leak
        load_file(s.conn, ta, s.file(gen.make(wa["seed"], wa["tasks"], wa["projects"])[0], "a.xer"))
        load_file(s.conn, tb, s.file(gen.make(wb["seed"], wb["tasks"], wb["projects"], defects=["bad_number"] if wb["tasks"] > 8 else [])[0], "b.xer"))
        for mine, other in ((ta, tb), (tb, ta)):
            for table in ("calendar", "rsrc", "project", "projwbs", "task", "taskpred", "taskrsrc", "quarantine", "load_run"):
                with tenant_tx(s.conn, mine) as cur:
                    cur.execute(f"select count(*), count(*) filter (where tenant_id <> %s) from {table}", (mine,))
                    seen, foreign = cur.fetchone()
                want = s.admin.execute(f"select count(*) from {table} where tenant_id = %s", (mine,)).fetchone()[0]
                assert foreign == 0 and seen == want, (table, mine, other)
        with s.conn.transaction():                                  # and with no setting at all, nothing is visible
            assert s.conn.execute("select count(*) from task").fetchone()[0] == 0
    finally:
        s.close()
