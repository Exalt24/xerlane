"""The cost commands as real processes, the sample file generator, and a property test over random cost files."""
from __future__ import annotations

import csv
import io
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import make_sample_cost as gen_cost
from conftest import TENANT
from test_cost import COMMIT_HEAD, COST_HEAD, commit_rec, cost_rec, csv_bytes, q_of, rows_of
from xerlane import cost, db, evm
from xerlane.db import tenant_tx
from xerlane.load import load_file

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str):
    p = subprocess.run([sys.executable, "-m", "xerlane", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture()
def files(clean, xer, tmp_path):
    plan, _ = xer(tasks=60, name="plan.xer")
    assert run("load", plan, "--tenant", TENANT)[0] == 0
    out = tmp_path / "gen"
    assert gen_cost.main(["--xer", plan, "--seed", "5", "--out-dir", str(out)]) == 0
    return plan, out / "cost.csv", out / "commitments.csv"


@pytest.mark.db
def test_load_reconcile_and_report_of_a_clean_pair_exit_zero(files, tmp_path):
    _, cost_file, commit_file = files
    code, out, _ = run("load-cost", str(cost_file), "--kind", "cost", "--tenant", TENANT)
    assert code == 0 and "LOADED" in out and "added" in out and "sha256" in out
    assert run("load-cost", str(cost_file), "--kind", "cost", "--tenant", TENANT)[1].count("UNCHANGED") == 1
    code, out, _ = run("load-cost", str(commit_file), "--kind", "commitment", "--tenant", TENANT)
    assert code == 0 and "LOADED" in out
    for f, kind in ((cost_file, "cost"), (commit_file, "commitment")):
        code, out, _ = run("reconcile-cost", str(f), "--kind", kind, "--tenant", TENANT)
        assert code == 0 and "RECONCILED" in out
    code, out, err = run("report", "--tenant", TENANT, "--proj-id", "1010")
    assert code == 0 and out.startswith("project    XL-01 (1010)") and "BAC" in out and "CPI" in out, err
    page = tmp_path / "r.html"
    code, out, _ = run("report", "--tenant", TENANT, "--proj-id", "1010", "--format", "html", "--out", str(page), "--as-of", "2026-02-01")
    assert code == 0 and "wrote" in out and page.read_text(encoding="utf-8").startswith("<!doctype html>")
    csv_out = tmp_path / "r.csv"
    assert run("report", "--tenant", TENANT, "--proj-id", "1010", "--format", "csv", "--out", str(csv_out))[0] == 0
    assert list(csv.reader(csv_out.open(encoding="utf-8")))[0][0] == "task_code"


@pytest.mark.db
def test_exit_codes_for_each_outcome(files, tmp_path):
    plan, _, _ = files
    bad = tmp_path / "bad"
    gen_cost.main(["--xer", plan, "--seed", "5", "--defect", "bad_amount", "--defect", "over_invoiced", "--out-dir", str(bad)])
    code, out, _ = run("load-cost", str(bad / "cost.csv"), "--kind", "cost", "--tenant", TENANT)
    assert code == 1 and "quarantine 1 row(s)" in out and "BAD_VALUE" in out and "LOADED" in out
    code, out, _ = run("load-cost", str(bad / "commitments.csv"), "--kind", "commitment", "--tenant", TENANT)
    assert code == 1 and "OVER_INVOICED" in out
    code, out, _ = run("reconcile-cost", str(bad / "cost.csv"), "--kind", "cost", "--tenant", TENANT)
    assert code == 0 and "1 set aside" in out
    junk = tmp_path / "junk.csv"
    junk.write_bytes(b"not,an,export\n")
    code, out, _ = run("load-cost", str(junk), "--kind", "cost", "--tenant", TENANT)
    assert code == 2 and "REJECTED" in out and "missing required" in out
    code, out, err = run("load-cost", str(tmp_path / "missing.csv"), "--kind", "cost", "--tenant", TENANT)
    assert code == 2 and "cannot read" in err
    code, out, _ = run("reconcile-cost", str(junk), "--kind", "cost", "--tenant", TENANT)
    assert code == 1 and "FILE_UNREADABLE" in out
    code, out, err = run("report", "--tenant", TENANT, "--proj-id", "424242")
    assert code == 2 and "not loaded" in err
    assert run("load-cost", str(bad / "cost.csv"), "--kind", "other", "--tenant", TENANT)[0] == 2


@pytest.mark.db
def test_a_file_that_empties_a_project_fails_and_the_flag_lets_it(files, tmp_path):
    _, cost_file, _ = files
    assert run("load-cost", str(cost_file), "--kind", "cost", "--tenant", TENANT)[0] == 0
    one = tmp_path / "one.csv"
    one.write_bytes(csv_bytes(COST_HEAD, [cost_rec(line_id="1", task_code="A1000", budget="x")]))
    code, out, _ = run("load-cost", str(one), "--kind", "cost", "--tenant", TENANT)
    assert code == 2 and "FAILED" in out and "allow_empty" in out
    code, out, _ = run("load-cost", str(one), "--kind", "cost", "--tenant", TENANT, "--allow-empty")
    assert code == 1 and "removed" in out


def test_the_sample_generator_is_deterministic_and_every_defect_lands_in_the_quarantine(clean, conn, xer, tmp_path):
    plan, _ = xer(tasks=80)
    load_file(conn, TENANT, plan)
    a, b = gen_cost.make(plan, 3, []), gen_cost.make(plan, 3, [])
    assert a == b and gen_cost.make(plan, 4, []) != a
    expect = {"bad_amount": "BAD_VALUE", "unknown_task_code": "UNKNOWN_TASK_CODE", "unknown_project": "UNKNOWN_PROJECT", "dup_id": "DUP_ID", "wrong_field_count": "WRONG_FIELD_COUNT",
              "bad_period": "BAD_VALUE", "over_invoiced": "OVER_INVOICED", "bad_status": "BAD_VALUE", "bad_date": "BAD_VALUE"}
    assert set(expect) == set(gen_cost.DEFECTS)
    for defect, code in expect.items():
        rows_cost, rows_commit = gen_cost.make(plan, 3, [defect])
        rows, kind = (rows_commit, "commitment") if defect in ("over_invoiced", "bad_status", "bad_date") else (rows_cost, "cost")
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(rows)
        r = cost.load_bytes(conn, TENANT, kind, buf.getvalue().encode(), f"{defect}.csv")
        assert code in {q.code for q in r.quarantined}, (defect, [(q.code, q.reason) for q in r.quarantined])
    clean_cost, clean_commit = a
    for rows, kind in ((clean_cost, "cost"), (clean_commit, "commitment")):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(rows)
        r = cost.load_bytes(conn, TENANT, kind, buf.getvalue().encode(), "clean.csv")
        assert r.quarantined == [] and r.status in ("loaded", "unchanged")


def test_generator_cli_lists_defects_and_needs_a_file(capsys):
    assert gen_cost.main(["--list-defects"]) == 0
    assert "over_invoiced" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        gen_cost.main([])
    with pytest.raises(SystemExit):
        gen_cost.make("none", 1, ["nope"])


# ---- property: random cost files ----------------------------------------------------------------------------------------------------------------------

amount = st.one_of(st.integers(-10**6, 10**7).map(lambda n: f"{n / 100:.2f}"), st.sampled_from(["", "0", "12.3456", "1,000.00", "abc", "1e3", "-0.01", "NaN"]))
cost_rows = st.fixed_dictionaries({
    "line_id": st.one_of(st.integers(0, 30).map(str), st.sampled_from(["", "-1", "x"])),
    "proj_id": st.sampled_from(["1010", "1010", "1010", "77", ""]),
    "cost_code": st.sampled_from(["03-100", "05-200", " ", "x" * 5]),
    "description": st.text(alphabet="abc é,\"\n", max_size=8),
    "task_code": st.sampled_from(["", "A1000", "A1010", "NOPE"]),
    "period": st.sampled_from(["", "2026-03", "2026-13"]),
    "budget": amount, "forecast": amount, "actual": amount})

DB_SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])


@pytest.mark.db
@given(recs=st.lists(cost_rows, max_size=25), data=st.data())
@DB_SETTINGS
def test_every_row_is_stored_or_set_aside_a_reload_changes_nothing_and_reconcile_agrees(admin_dsn, recs, data):
    db.wipe(admin_dsn)
    conn = db.connect()
    try:
        from conftest import gen
        plan_bytes, _ = gen.make(11, 30, 1, [])
        from xerlane.parse import parse_bytes
        from xerlane.load import load_parsed
        load_parsed(conn, TENANT, parse_bytes(plan_bytes), "p.xer")
        body = csv_bytes(COST_HEAD, recs)
        first = cost.load_bytes(conn, TENANT, "cost", body, "r.csv", allow_empty=True)
        assert first.status in ("loaded", "unchanged")
        assert first.stored + len(first.quarantined) == first.rows_read == len(recs)
        stored = rows_of(conn, "cost_line")
        assert len(stored) == first.stored
        again = cost.load_bytes(conn, TENANT, "cost", body, "r.csv", allow_empty=True)
        assert again.status == "unchanged" and rows_of(conn, "cost_line") == stored
        shuffled = data.draw(st.permutations(recs))
        third = cost.load_bytes(conn, TENANT, "cost", csv_bytes(COST_HEAD, list(shuffled)), "r.csv", allow_empty=True)
        assert len(rows_of(conn, "cost_line")) == first.stored
        assert {(q.code) for q in third.quarantined} == {q.code for q in first.quarantined} and len(third.quarantined) == len(first.quarantined)
        r = cost.reconcile_bytes(conn, TENANT, "cost", csv_bytes(COST_HEAD, list(shuffled)), "r.csv")
        assert r.ok, [str(f) for f in r.findings]
        if first.stored:
            rep = evm.build_report(conn, TENANT, 1010)
            t = rep.totals
            assert t.bac == sum((row[-3] for row in stored if row[1] == 1010), 0)
    finally:
        conn.close()
