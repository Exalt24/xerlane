"""Earned value report: the formulas on numbers worked out by hand, the joins, and the rendering."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal

import pytest

from conftest import TENANT
from xerlane import cost, evm

D = Decimal
T0 = datetime(2026, 3, 2, 0, 0)       # Monday


# ---- pure functions -----------------------------------------------------------------------------------------------------------------------------------

def test_percent_complete():
    s, e = datetime(2026, 3, 2), datetime(2026, 3, 9)
    assert evm.percent_complete(None, None, D(40), D(40)) == 0
    assert evm.percent_complete(s, None, D(40), D(40)) == 0
    assert evm.percent_complete(s, None, D(40), D(10)) == D("0.75")
    assert evm.percent_complete(s, None, D(40), D(0)) == 1                       # active, nothing remaining
    assert evm.percent_complete(s, None, D(40), D(50)) == 0                      # remaining above target never goes below zero
    assert evm.percent_complete(s, e, D(40), D(40)) == 1                         # a recorded finish wins over the remaining duration
    assert evm.percent_complete(None, e, D(40), D(40)) == 1
    assert evm.percent_complete(s, None, D(0), D(0)) == 0                        # a milestone that has started but not finished earns nothing
    assert evm.percent_complete(s, None, None, D(5)) == 0 and evm.percent_complete(s, None, D(40), None) == 0


def test_percent_planned():
    s, e = datetime(2026, 3, 2), datetime(2026, 3, 12)
    assert evm.percent_planned(datetime(2026, 3, 1), s, e) == 0
    assert evm.percent_planned(s, s, e) == 0
    assert evm.percent_planned(datetime(2026, 3, 7), s, e) == D("0.5")
    assert evm.percent_planned(datetime(2026, 3, 4), s, e) == D("0.2")
    assert evm.percent_planned(e, s, e) == 1 and evm.percent_planned(datetime(2027, 1, 1), s, e) == 1
    assert evm.percent_planned(datetime(2026, 3, 5), None, e) == 0 and evm.percent_planned(datetime(2026, 3, 5), s, None) == 0
    assert evm.percent_planned(s, s, s) == 1 and evm.percent_planned(datetime(2026, 3, 1), s, s) == 0      # a milestone


def test_ratio_and_money_round_half_up_and_refuse_a_zero_denominator():
    assert evm.ratio(D(1), D(0)) is None and evm.ratio(D(0), D(0)) is None
    assert evm.ratio(D(0), D(5)) == D("0.00")
    assert evm.ratio(D(2), D(3)) == D("0.67") and evm.ratio(D("0.125"), D(1)) == D("0.13")
    assert evm.money(D("2.345")) == D("2.35") and evm.money(D("-2.345")) == D("-2.35")


def test_line_formulas():
    x = evm.Line("A", "n", bac=D(1000), pv=D(500), ev=D(400), ac=D(500))
    assert (x.cv, x.sv, x.cpi, x.spi, x.eac) == (D("-100.00"), D("-100.00"), D("0.80"), D("0.80"), D("1250.00"))
    zero = evm.Line("A", "n", bac=D(1000), pv=D(0), ev=D(0), ac=D(0))
    assert zero.cpi is None and zero.spi is None and zero.eac is None
    nothing_earned = evm.Line("A", "n", bac=D(1000), pv=D(10), ev=D(0), ac=D(5))
    assert nothing_earned.cpi == 0 and nothing_earned.eac is None              # CPI zero: no forecast, never a division by zero


# ---- a schedule worked out by hand --------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def world(conn, admin):
    """Project 7 at as-of 2026-03-12 00:00, which is the midpoint of the 20 day span 03-02 to 03-22. Four tasks:
       T1 finished           target 03-02..03-22  actual finish 03-20     -> done 1, planned 0.5
       T2 half done          target 03-02..03-22  actual start, 40h of 80h remaining 40 -> done 0.5, planned 0.5
       T3 not started        target 03-12..03-22  -> done 0, planned 0
       T4 started late       target 03-02..03-12  actual start, target 100, remaining 100 -> done 0, planned 1
    """
    admin.execute("insert into calendar (tenant_id, clndr_id) values (%s, 1)", (TENANT,))
    admin.execute("insert into project (tenant_id, proj_id, proj_short_name, last_recalc_date, clndr_id) values (%s, 7, 'P7', '2026-03-12 00:00', 1)", (TENANT,))
    admin.execute("insert into projwbs (tenant_id, wbs_id, proj_id, wbs_short_name) values (%s, 1, 7, 'W')", (TENANT,))
    rows = [(1, "T1", "2026-03-02", "2026-03-22", "2026-03-02", "2026-03-20", 80, 0), (2, "T2", "2026-03-02", "2026-03-22", "2026-03-02", None, 80, 40),
            (3, "T3", "2026-03-12", "2026-03-22", None, None, 80, 80), (4, "T4", "2026-03-02", "2026-03-12", "2026-03-03", None, 100, 100)]
    for tid, code, ts, te, as_, ae, tgt, rem in rows:
        admin.execute("insert into task (tenant_id, task_id, proj_id, wbs_id, clndr_id, task_code, task_name, target_start_date, target_end_date, act_start_date, act_end_date, "
                      "target_drtn_hr_cnt, remain_drtn_hr_cnt) values (%s,%s,7,1,1,%s,%s,%s,%s,%s,%s,%s,%s)", (TENANT, tid, code, f"name {code}", ts, te, as_, ae, tgt, rem))
    head = "line_id,proj_id,cost_code,task_code,budget,forecast,actual"
    lines = ["1,7,03-100,T1,1000,1000,1200", "2,7,03-200,T1,500,500,0",                    # T1: BAC 1500, AC 1200
             "3,7,03-100,T2,2000,2100,900",                                                # T2
             "4,7,03-300,T3,3000,3000,0",                                                  # T3
             "5,7,03-300,T4,400,400,100",                                                  # T4
             "6,7,01-000,,100,100,50"]                                                     # no task code
    r = cost.load_bytes(conn, TENANT, "cost", ((head + "\n") + "\n".join(lines) + "\n").encode(), "c.csv")
    assert r.status == "loaded" and not r.quarantined
    c = cost.load_bytes(conn, TENANT, "commitment", ("commitment_id,proj_id,vendor,cost_code,committed,invoiced,status\n"
                                                      "1,7,V1,03-100,2500,1000,OPEN\n2,7,V2,03-100,900,900,VOID\n3,7,V3,03-300,2000,100,OPEN\n").encode(), "m.csv")
    assert c.status == "loaded"
    return conn


@pytest.mark.db
def test_task_figures_are_the_hand_worked_ones(world):
    rep = evm.build_report(world, TENANT, 7)
    assert rep.as_of == datetime(2026, 3, 12) and rep.project == "P7"
    by = {t.key: t for t in rep.tasks}
    assert [(k, v.bac, v.pv, v.ev, v.ac) for k, v in by.items()] == [
        ("T1", D(1500), D(750), D(1500), D(1200)),
        ("T2", D(2000), D(1000), D(1000), D(900)),
        ("T3", D(3000), D(0), D(0), D(0)),
        ("T4", D(400), D(400), D(0), D(100))]
    assert by["T1"].pct_done == 1 and by["T2"].pct_done == D("0.5") and by["T4"].pct_planned == 1
    assert by["T2"].forecast == D(2100) and by["T2"].cpi == D("1.11") and by["T2"].spi == D("1.00")


@pytest.mark.db
def test_totals_include_the_lines_without_a_task_in_bac_and_ac_only(world):
    rep = evm.build_report(world, TENANT, 7)
    t = rep.totals
    assert (t.bac, t.pv, t.ev, t.ac) == (D(7000), D(2150), D(2500), D(2250))
    assert (t.cv, t.sv) == (D("250.00"), D("350.00")) and t.cpi == D("1.11") and t.spi == D("1.16")
    assert rep.unallocated.bac == 100 and rep.unallocated.ac == 50 and rep.unallocated.ev == 0
    assert rep.unmatched.bac == 0
    assert any("no task code" in n for n in rep.notes)


@pytest.mark.db
def test_as_of_moves_the_planned_value_and_nothing_else(world):
    early = evm.build_report(world, TENANT, 7, datetime(2026, 3, 2))
    assert early.totals.pv == 0 and early.totals.ev == 2500 and early.totals.ac == 2250
    late = evm.build_report(world, TENANT, 7, datetime(2026, 4, 1))
    assert late.totals.pv == 1500 + 2000 + 3000 + 400


@pytest.mark.db
def test_cost_codes_show_commitments_without_voided_ones_and_flag_over_commitment(world):
    rep = evm.build_report(world, TENANT, 7)
    rows = {c["cost_code"]: c for c in rep.by_code}
    assert list(rows) == ["01-000", "03-100", "03-200", "03-300"]
    assert (rows["03-100"]["budget"], rows["03-100"]["actual"], rows["03-100"]["committed"], rows["03-100"]["invoiced"]) == (D(3000), D(2100), D(2500), D(1000))
    assert rows["03-100"]["over"] is False and rows["03-300"]["over"] is False
    assert rows["03-200"]["committed"] == 0
    cost.load_bytes(world, TENANT, "commitment", b"commitment_id,proj_id,vendor,cost_code,committed,invoiced,status\n1,7,V1,03-100,3000.01,0,OPEN\n", "m.csv", allow_empty=True)
    assert {c["cost_code"]: c["over"] for c in evm.build_report(world, TENANT, 7).by_code}["03-100"] is True


@pytest.mark.db
def test_cost_lines_whose_task_vanished_are_reported_not_hidden(world, admin):
    admin.execute("delete from task where task_code = 'T4'")
    rep = evm.build_report(world, TENANT, 7)
    assert [t.key for t in rep.tasks] == ["T1", "T2", "T3"]
    assert rep.unmatched.bac == 400 and rep.unmatched.ac == 100 and rep.unmatched.ev == 0
    assert rep.totals.bac == 7000 and rep.totals.ac == 2250
    assert any("no task has now" in n for n in rep.notes)


@pytest.mark.db
def test_model_elements_join_through_the_task_code(world, admin):
    for oid, ext, name, code in [(1, "ext-1", "Wall", "T1"), (2, "ext-2", "Slab", "T2"), (3, "ext-3", "Orphan", "NOPE"), (4, None, "Loose", None)]:
        admin.execute("insert into aps_object (tenant_id, model_urn, view_guid, object_id, external_id, name, task_code, content_hash) values (%s,'u','v',%s,%s,%s,%s,%s)",
                      (TENANT, oid, ext, name, code, "0" * 64))
    rep = evm.build_report(world, TENANT, 7)
    assert [(e["external_id"], e["task_code"], e["bac"], e["ev"], e["ac"]) for e in rep.elements] == [("ext-1", "T1", D(1500), D(1500), D(1200)), ("ext-2", "T2", D(2000), D(1000), D(900))]


@pytest.mark.db
def test_a_project_without_cost_lines_says_so_and_an_unknown_project_is_an_error(world, xer):
    from xerlane.load import load_file
    path, _ = xer(tasks=20)
    load_file(world, "globex", path)
    empty = evm.build_report(world, "globex", 1010)
    assert any("no task has cost lines" in n for n in empty.notes) and empty.tasks == [] and empty.totals.bac == 0 and empty.totals.cpi is None
    with pytest.raises(evm.ReportError, match="not loaded"):
        evm.build_report(world, TENANT, 8)
    with pytest.raises(evm.ReportError, match="not loaded"):
        evm.build_report(world, "globex", 7)


@pytest.mark.db
def test_a_project_with_no_recalc_date_needs_an_as_of(world, admin):
    admin.execute("update project set last_recalc_date = null")
    with pytest.raises(evm.ReportError, match="as-of"):
        evm.build_report(world, TENANT, 7)
    assert evm.build_report(world, TENANT, 7, T0).as_of == T0


@pytest.mark.db
def test_the_report_of_one_tenant_never_includes_another(world, xer, conn):
    from xerlane.load import load_file
    path, _ = xer(tasks=20)
    load_file(conn, "globex", path)
    cost.load_bytes(conn, "globex", "cost", b"line_id,proj_id,cost_code,task_code,budget,forecast,actual\n1,1010,01-000,A1000,999999,0,0\n", "g.csv")
    mine = evm.build_report(conn, TENANT, 7)
    assert mine.totals.bac == 7000
    theirs = evm.build_report(conn, "globex", 1010)
    assert theirs.totals.bac == 999999 and theirs.project != "P7"


# ---- rendering ----------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.db
def test_text_report_names_the_totals_and_the_worst_tasks_first(world):
    out = evm.render_text(evm.build_report(world, TENANT, 7))
    lines = out.splitlines()
    assert lines[0].startswith("project    P7 (7)") and "as of 2026-03-12 00:00" in lines[0]
    assert "BAC 7,000.00" in lines[1] and "EV 2,500.00" in lines[1]
    assert "CPI 1.11" in lines[2] and "SPI 1.16" in lines[2] and "EAC" in lines[2]
    table = [ln.split()[0] for ln in lines if ln[:1] == "T"]
    assert table == ["T4", "T3", "T2", "T1"]                       # sorted by cost variance, lowest first: T4 -100, T3 0, T2 +100, T1 +300
    assert "committed above budget on 0" in out


@pytest.mark.db
def test_csv_report_round_trips_and_totals_match(world):
    rows = list(csv.reader(io.StringIO(evm.render_csv(evm.build_report(world, TENANT, 7)))))
    assert rows[0][:6] == ["task_code", "task_name", "bac", "pv", "ev", "ac"]
    by = {r[0]: r for r in rows[1:]}
    assert by["T2"][2:6] == ["2000.00", "1000.00", "1000.00", "900.00"] and by["T2"][8] == "1.11"
    assert by["TOTAL"][2] == "7000.00" and by["(no task code)"][2] == "100.00" and by["T3"][8] == "n/a"


@pytest.mark.db
def test_html_report_is_one_escaped_self_contained_page(world, admin):
    admin.execute("update task set task_name = '<script>alert(1)</script> & co' where task_code = 'T1'")
    page = evm.render_html(evm.build_report(world, TENANT, 7))
    assert page.startswith("<!doctype html>") and "<script" not in page.replace("&lt;script", "") and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page
    assert "http://" not in page and "https://" not in page and "src=" not in page
    assert "<h1>P7</h1>" in page and "2,500.00" in page
