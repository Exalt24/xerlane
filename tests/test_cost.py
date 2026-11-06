"""Cost and commitment exports: the row rules, the file rules, the load (idempotence, quarantine, tenants, rollback), reconcile and its damage control."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import psycopg
import pytest

from conftest import TENANT
from xerlane import cost
from xerlane.db import tenant_tx
from xerlane.load import load_file

COST_HEAD = "line_id,proj_id,cost_code,description,task_code,period,budget,forecast,actual"
COMMIT_HEAD = "commitment_id,proj_id,vendor,cost_code,description,committed,invoiced,status,issued"


def cost_rec(**over):
    rec = {"line_id": "1", "proj_id": "1010", "cost_code": "03-300", "description": "Slab", "task_code": "A1000", "period": "2026-03", "budget": "100.50", "forecast": "110", "actual": "50"}
    rec.update(over)
    return rec


def commit_rec(**over):
    rec = {"commitment_id": "1", "proj_id": "1010", "vendor": "Harbor Steel", "cost_code": "03-300", "description": "Rebar", "committed": "500", "invoiced": "200", "status": "OPEN", "issued": "2026-02-01"}
    rec.update(over)
    return rec


def csv_bytes(head: str, recs: list[dict], newline: str = "\r\n") -> bytes:
    cols = head.split(",")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=newline)
    w.writerow(cols)
    for r in recs:
        w.writerow([r.get(c, "") for c in cols])
    return buf.getvalue().encode("utf-8")


# ---- row rules, no database -------------------------------------------------------------------------------------------------------------------------

def check(kind, rec):
    return cost.check_row(kind, rec)


def test_a_good_cost_row_is_parsed_with_exact_decimals():
    v, problem = check(cost.COST, cost_rec())
    assert problem is None
    assert v["budget"] == Decimal("100.50") and v["forecast"] == Decimal("110") and v["actual"] == Decimal("50")
    assert v["line_id"] == 1 and v["proj_id"] == 1010 and v["task_code"] == "A1000" and v["period"] == "2026-03" and v["cost_code"] == "03-300"


def test_blank_amounts_in_a_cost_row_mean_zero_but_not_in_a_commitment():
    v, problem = check(cost.COST, cost_rec(budget="", forecast=" ", actual=""))
    assert problem is None and v["budget"] == v["forecast"] == v["actual"] == 0
    v, problem = check(cost.COMMITMENT, commit_rec(committed=""))
    assert v is None and problem == ("MISSING_VALUE", "committed is empty")


@pytest.mark.parametrize("field,value", [("budget", "1,250.00"), ("budget", "1e5"), ("budget", "NaN"), ("budget", "Infinity"), ("budget", "12.34567"), ("budget", "$5"), ("budget", "5."),
                                         ("budget", ".5"), ("budget", "12345678901234"), ("actual", "--5"), ("forecast", "1 000")])
def test_amounts_must_be_plain_numbers(field, value):
    v, problem = check(cost.COST, cost_rec(**{field: value}))
    assert v is None and problem[0] == "BAD_VALUE" and field in problem[1]


def test_largest_amount_that_fits_and_negative_credits_are_accepted():
    v, problem = check(cost.COST, cost_rec(budget="9999999999999.9999", actual="-250.25"))
    assert problem is None and v["budget"] == Decimal("9999999999999.9999") and v["actual"] == Decimal("-250.25")


@pytest.mark.parametrize("field,value", [("line_id", "-1"), ("line_id", "1.5"), ("line_id", "abc"), ("proj_id", "9223372036854775808"), ("proj_id", " "), ("line_id", "")])
def test_ids_must_be_whole_numbers_in_range(field, value):
    v, problem = check(cost.COST, cost_rec(**{field: value}))
    assert v is None and problem[0] in ("BAD_VALUE", "MISSING_VALUE")


def test_largest_id_is_accepted():
    v, problem = check(cost.COST, cost_rec(line_id="9223372036854775807"))
    assert problem is None and v["line_id"] == 2**63 - 1


def test_required_text_missing_is_named():
    v, problem = check(cost.COST, cost_rec(cost_code="  "))
    assert v is None and problem == ("MISSING_VALUE", "cost_code is empty")
    v, problem = check(cost.COMMITMENT, commit_rec(vendor=""))
    assert problem == ("MISSING_VALUE", "vendor is empty")


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "26-03", "2026-3", "2026-03-01", "March"])
def test_period_must_be_a_month(period):
    v, problem = check(cost.COST, cost_rec(period=period))
    assert v is None and problem[0] == "BAD_VALUE" and "period" in problem[1]


def test_period_and_task_code_are_optional():
    v, problem = check(cost.COST, cost_rec(period="", task_code=" "))
    assert problem is None and v["period"] is None and v["task_code"] is None


def test_text_length_and_control_characters():
    v, problem = check(cost.COST, cost_rec(description="x" * cost.MAX_TEXT))
    assert problem is None
    v, problem = check(cost.COST, cost_rec(description="x" * (cost.MAX_TEXT + 1)))
    assert problem[0] == "BAD_VALUE" and "longer" in problem[1]
    v, problem = check(cost.COST, cost_rec(description="a\x07b"))
    assert problem[0] == "BAD_VALUE" and "control" in problem[1]
    v, problem = check(cost.COST, cost_rec(description="line one\nline two\ttabbed"))
    assert problem is None


def test_commitment_rules():
    v, problem = check(cost.COMMITMENT, commit_rec(status="open"))
    assert problem is None and v["status"] == "OPEN" and v["issued"].isoformat() == "2026-02-01"
    v, problem = check(cost.COMMITMENT, commit_rec(issued=""))
    assert problem is None and v["issued"] is None
    assert check(cost.COMMITMENT, commit_rec(committed="-1"))[1][0] == "OUT_OF_RANGE"
    assert check(cost.COMMITMENT, commit_rec(invoiced="-0.01"))[1][0] == "OUT_OF_RANGE"
    assert check(cost.COMMITMENT, commit_rec(status="PENDING"))[1][0] == "BAD_VALUE"
    for bad in ("2026-02-30", "2026-2-3", "02/03/2026", "2026-13-01", "20260201"):
        assert check(cost.COMMITMENT, commit_rec(issued=bad))[1][0] == "BAD_VALUE", bad
    assert check(cost.COMMITMENT, commit_rec(committed="0", invoiced="0"))[1] is None


# ---- file rules, no database ------------------------------------------------------------------------------------------------------------------------

def parse(data: bytes, kind=cost.COST):
    return cost.parse_bytes(kind, data)


def test_parse_reads_rows_with_their_line_numbers_including_quoted_newlines():
    data = ('\r\n'.join([COST_HEAD, '1,1010,03-300,"two\r\nlines",A1000,2026-03,10,10,5', '', '2,1010,03-300,plain,,,1,1,0']) + '\r\n').encode()
    rows = parse(data).rows
    assert [r.line for r in rows] == [2, 5]
    assert rows[0].values["description"] == "two\r\nlines" and rows[1].values["line_id"] == 2


def test_parse_accepts_bom_cp1252_either_line_ending_and_any_column_order():
    head = "ACTUAL, Budget ,forecast,cost_code,PROJ_ID,line_id"
    for newline in ("\n", "\r\n"):
        data = (head + newline + "5,10,10,03-300,1010,7" + newline).encode("utf-8")
        for payload in (data, b"\xef\xbb\xbf" + data):
            r = parse(payload).rows[0]
            assert r.code is None and r.values["actual"] == 5 and r.values["line_id"] == 7
    r = parse(("line_id,proj_id,cost_code,description,budget,forecast,actual\n1,1010,03-300,Bétonnière é,1,1,1\n").encode("cp1252")).rows[0]
    assert r.values["description"] == "Bétonnière é"


def test_extra_columns_are_ignored():
    r = parse((COST_HEAD + ",notes\n1,1010,03-300,d,,,1,1,1,whatever\n").encode()).rows[0]
    assert r.code is None


@pytest.mark.parametrize("data,why", [(b"", "empty"), (b"line_id,proj_id\n1,2\n", "missing required"), (b"a,a,cost_code\n", "repeated"), (b"line_id,proj_id,cost_code,budget,forecast,actual\x00\n", "NUL"),
                                      (b"line_id,proj_id\xff\x81,cost_code\n", "neither"), (b'line_id,"proj_id\n', "not valid CSV")])
def test_unreadable_files_are_rejected(data, why):
    with pytest.raises(cost.CostFileError) as e:
        parse(data)
    assert why.split()[0].lower() in str(e.value).lower() or why == "empty"


def test_header_only_file_is_readable_and_has_no_rows():
    assert parse((COST_HEAD + "\n").encode()).rows == []


def test_an_unterminated_quote_rejects_the_file_because_no_row_boundary_can_be_trusted_after_it():
    data = (COST_HEAD + '\n1,1010,03-300,"unterminated,A1000,,1,1,1\n2,1010,03-300,x,,,1,1,1\n').encode()
    with pytest.raises(cost.CostFileError, match="line 3: not valid CSV"):
        parse(data)


def test_wrong_field_count_is_a_row_problem_not_a_file_problem():
    data = (COST_HEAD + "\n1,1010,03-300\n2,1010,03-300,d,,,1,1,1\n").encode()
    rows = parse(data).rows
    assert [r.code for r in rows] == ["WRONG_FIELD_COUNT", None]
    assert "3 fields where the header has 9" in rows[0].reason


def test_the_size_cap(monkeypatch):
    monkeypatch.setattr(cost, "MAX_FILE_BYTES", 40)
    with pytest.raises(cost.CostFileError, match="larger"):
        parse((COST_HEAD + "\n").encode() + b"1,1010,03-300,d,,,1,1,1\n")
    monkeypatch.setattr(cost, "MAX_FILE_BYTES", 10**6)
    parse((COST_HEAD + "\n").encode())


def test_decode_prefers_utf8_over_cp1252():
    assert cost.decode("é".encode("utf-8")) == "é"
    assert cost.decode("é".encode("cp1252")) == "é"
    assert cost.decode(b"\xef\xbb\xbfabc") == "abc"


# ---- the load -----------------------------------------------------------------------------------------------------------------------------------------



@pytest.fixture()
def sched(conn, xer):
    """A tenant with a loaded schedule: project 1010, task codes A1000, A1010, ..."""
    path, _ = xer(tasks=40)
    load_file(conn, TENANT, path)
    return conn


def load(conn, kind, recs, head=None, tenant=TENANT, label="f.csv", **kw):
    head = head or (COST_HEAD if kind == "cost" else COMMIT_HEAD)
    return cost.load_bytes(conn, tenant, kind, csv_bytes(head, recs), label, **kw)


def rows_of(conn, table, tenant=TENANT):
    with tenant_tx(conn, tenant) as cur:
        cur.execute(f"select * from {table} where tenant_id = %s order by proj_id, 3", (tenant,))
        return cur.fetchall()


def run_count(conn, tenant=TENANT):
    with tenant_tx(conn, tenant) as cur:
        cur.execute("select count(*) from cost_load_run")
        return cur.fetchone()[0]


def q_of(conn, tenant=TENANT):
    with tenant_tx(conn, tenant) as cur:
        cur.execute("select table_name, line_no, reason_code, raw, proj_id, source_label from quarantine where tenant_id = %s order by 1, 2", (tenant,))
        return cur.fetchall()


@pytest.mark.db
def test_load_stores_rows_and_a_second_load_changes_nothing(sched):
    recs = [cost_rec(line_id=str(i), task_code=f"A{1000 + 10 * i}", budget=str(100 * i)) for i in range(5)]
    r = load(sched, "cost", recs)
    assert r.status == "loaded" and r.stored == 5 and r.projects == {1010: {"added": 5, "changed": 0, "removed": 0}} and r.exit_code == 0 and r.quarantined == []
    assert len(rows_of(sched, "cost_line")) == 5 and run_count(sched) == 1
    before = rows_of(sched, "cost_line")
    again = load(sched, "cost", recs)
    assert again.status == "unchanged" and again.projects == {} and run_count(sched) == 1 and rows_of(sched, "cost_line") == before


@pytest.mark.db
def test_a_changed_removed_and_added_line_are_each_counted(sched):
    recs = [cost_rec(line_id=str(i), task_code=f"A{1000 + 10 * i}") for i in range(4)]
    load(sched, "cost", recs)
    recs[1]["actual"] = "77.77"                                  # changed
    del recs[2]                                                  # removed
    recs.append(cost_rec(line_id="9", task_code="A1000"))        # added
    r = load(sched, "cost", recs)
    assert r.status == "loaded" and r.projects == {1010: {"added": 1, "changed": 1, "removed": 1}}
    assert run_count(sched) == 2
    stored = {row[2]: row for row in rows_of(sched, "cost_line")}
    assert sorted(stored) == [0, 1, 3, 9] and stored[1][-1] == Decimal("77.7700")


@pytest.mark.db
def test_each_field_change_is_noticed(sched):
    base = cost_rec()
    load(sched, "cost", [base])
    for name, value in [("cost_code", "09-000"), ("description", "other"), ("task_code", "A1010"), ("period", "2026-04"), ("budget", "1"), ("forecast", "1"), ("actual", "1")]:
        r = load(sched, "cost", [{**base, name: value}])
        assert r.status == "loaded" and r.projects[1010]["changed"] == 1, name
        r = load(sched, "cost", [base])
        assert r.projects[1010]["changed"] == 1, name


@pytest.mark.db
def test_commitments_load_the_same_way(sched):
    recs = [commit_rec(commitment_id=str(i), committed=str(500 + i)) for i in range(3)]
    r = load(sched, "commitment", recs)
    assert r.status == "loaded" and r.stored == 3
    row = rows_of(sched, "commitment")[0]
    assert row[3] == "Harbor Steel" and row[6] == Decimal("500.0000") and row[8] == "OPEN" and str(row[9]) == "2026-02-01"
    assert load(sched, "commitment", recs).status == "unchanged"
    recs[0]["status"] = "closed"
    assert load(sched, "commitment", recs).projects[1010]["changed"] == 1


@pytest.mark.db
@pytest.mark.parametrize("kind,recs,code,fragment", [
    ("cost", [cost_rec(budget="1,5")], "BAD_VALUE", "budget"),
    ("cost", [cost_rec(proj_id="99999")], "UNKNOWN_PROJECT", "99999"),
    ("cost", [cost_rec(task_code="NOPE-1")], "UNKNOWN_TASK_CODE", "NOPE-1"),
    ("cost", [cost_rec(line_id="3"), cost_rec(line_id="3", budget="9")], "DUP_ID", "line_id 3"),
    ("commitment", [commit_rec(committed="100", invoiced="100.01")], "OVER_INVOICED", "100.01"),
    ("commitment", [commit_rec(status="PENDING")], "BAD_VALUE", "status"),
    ("commitment", [commit_rec(committed="-5")], "OUT_OF_RANGE", "committed"),
])
def test_each_rule_sets_the_row_aside_with_its_reason_and_the_good_rows_still_load(sched, kind, recs, code, fragment):
    good = cost_rec(line_id="100", task_code="A1010") if kind == "cost" else commit_rec(commitment_id="100")
    r = load(sched, kind, recs + [good])
    assert r.status == "loaded" and r.exit_code == 1 and r.stored == 1
    assert {q.code for q in r.quarantined} == {code} and all(fragment in q.reason for q in r.quarantined)
    q = q_of(sched)
    assert len(q) == len(recs) and q[0][0] == kind.upper() and q[0][2] == code and q[0][5] == "cost:f.csv"
    assert len(rows_of(sched, "cost_line" if kind == "cost" else "commitment")) == 1


@pytest.mark.db
def test_a_duplicate_id_sets_every_copy_aside_and_none_is_stored(sched):
    r = load(sched, "cost", [cost_rec(line_id="3"), cost_rec(line_id="3", budget="9"), cost_rec(line_id="4")])
    assert r.stored == 1 and [q.line for q in r.quarantined] == [2, 3]
    assert [row[2] for row in rows_of(sched, "cost_line")] == [4]


@pytest.mark.db
def test_quarantine_keeps_the_raw_fields_the_line_and_the_project(sched):
    data = (COST_HEAD + "\n1,1010,03-300,ok,A1000,,1,1,1\n2,1010,03-300,bad,A1010,,x,1,1\n3,1010\n").encode()
    r = cost.load_bytes(sched, TENANT, "cost", data, "f.csv")
    assert [(q.line, q.code, q.proj_id) for q in r.quarantined] == [(3, "BAD_VALUE", 1010), (4, "WRONG_FIELD_COUNT", 1010)]
    q = q_of(sched)
    assert q[0][3] == ["2", "1010", "03-300", "bad", "A1010", "", "x", "1", "1"] and q[1][3] == ["3", "1010"]


@pytest.mark.db
def test_fixing_the_file_and_loading_again_clears_the_quarantine_and_stores_the_row(sched):
    load(sched, "cost", [cost_rec(line_id="1"), cost_rec(line_id="2", budget="oops")])
    assert len(q_of(sched)) == 1
    again = load(sched, "cost", [cost_rec(line_id="1"), cost_rec(line_id="2", budget="5")])
    assert again.status == "loaded" and again.projects[1010]["added"] == 1
    assert q_of(sched) == [] and len(rows_of(sched, "cost_line")) == 2


@pytest.mark.db
def test_quarantine_is_stable_when_the_same_bad_file_is_loaded_twice(sched):
    recs = [cost_rec(line_id="1"), cost_rec(line_id="2", budget="oops")]
    load(sched, "cost", recs)
    first = q_of(sched)
    again = load(sched, "cost", recs)
    assert again.status == "unchanged" and q_of(sched) == first and run_count(sched) == 1


@pytest.mark.db
def test_cost_and_commitment_quarantine_do_not_trample_each_other_or_an_xer_file_of_the_same_name(sched, xer):
    load(sched, "cost", [cost_rec(budget="oops")], label="same.csv")
    load(sched, "commitment", [commit_rec(status="NOPE")], label="same.csv")
    assert [q[0] for q in q_of(sched)] == ["COMMITMENT", "COST"]
    path, planted = xer(tasks=40, defects=["dup_task_code"], name="same.csv")
    load_file(sched, TENANT, path, "same.csv")
    names = [q[0] for q in q_of(sched)]
    assert "COST" in names and "COMMITMENT" in names and "TASK" in names


@pytest.mark.db
def test_a_file_that_would_empty_a_stored_project_is_refused_unless_allowed(sched):
    load(sched, "cost", [cost_rec(line_id="1"), cost_rec(line_id="2")])
    r = load(sched, "cost", [cost_rec(line_id="1", budget="bad")])
    assert r.status == "failed" and "no accepted row" in r.reason and r.exit_code == 2 and len(rows_of(sched, "cost_line")) == 2
    assert len(q_of(sched)) == 0                                  # nothing at all was written, the quarantine included
    r = load(sched, "cost", [cost_rec(line_id="1", budget="bad")], allow_empty=True)
    assert r.status == "loaded" and r.projects[1010]["removed"] == 2 and rows_of(sched, "cost_line") == []


@pytest.mark.db
def test_an_empty_export_with_a_header_only_empties_nothing_because_it_names_no_project(sched):
    load(sched, "cost", [cost_rec()])
    r = load(sched, "cost", [])
    assert r.status == "unchanged" and len(rows_of(sched, "cost_line")) == 1


@pytest.mark.db
def test_other_projects_in_the_file_load_while_one_is_refused_as_a_whole(sched, xer):
    path, _ = xer(tasks=40, projects=2, name="two.xer", id_base=2000)
    load_file(sched, TENANT, path)
    load(sched, "cost", [cost_rec(line_id="1"), cost_rec(line_id="2", proj_id="2010", task_code="A1000")])
    r = load(sched, "cost", [cost_rec(line_id="1", budget="bad"), cost_rec(line_id="2", proj_id="2010", task_code="A1000", actual="9")])
    assert r.status == "failed" and len(rows_of(sched, "cost_line")) == 2
    assert [row[-1] for row in rows_of(sched, "cost_line")] == [Decimal("50.0000"), Decimal("50.0000")]


@pytest.mark.db
def test_a_rejected_file_writes_nothing_and_says_why(sched):
    load(sched, "cost", [cost_rec()])
    r = cost.load_bytes(sched, TENANT, "cost", b"line_id,proj_id\n1,2\n", "bad.csv")
    assert r.status == "rejected" and "missing required" in r.reason and r.exit_code == 2
    assert len(rows_of(sched, "cost_line")) == 1 and q_of(sched) == []


@pytest.mark.db
def test_a_database_error_rolls_the_whole_load_back(sched, monkeypatch):
    def boom(*a, **k):
        raise psycopg.errors.OperationalError("simulated outage")
    monkeypatch.setattr(cost, "_sync_quarantine", boom)
    r = load(sched, "cost", [cost_rec()])
    assert r.status == "failed" and "simulated outage" in r.reason and r.projects == {}
    monkeypatch.undo()
    assert rows_of(sched, "cost_line") == [] and run_count(sched) == 0


@pytest.mark.db
def test_tenants_never_see_or_clash_with_each_other(sched, xer):
    path, _ = xer(tasks=40, name="b.xer")
    load_file(sched, "globex", path)
    load(sched, "cost", [cost_rec(line_id="1", budget="111")])
    load(sched, "cost", [cost_rec(line_id="1", budget="222")], tenant="globex")
    assert [r[-3] for r in rows_of(sched, "cost_line")] == [Decimal("111.0000")]
    assert [r[-3] for r in rows_of(sched, "cost_line", "globex")] == [Decimal("222.0000")]
    with tenant_tx(sched, "third") as cur:
        for t in ("cost_line", "commitment", "cost_load_run"):
            cur.execute(f"select count(*) from {t}")
            assert cur.fetchone()[0] == 0
    assert sched.execute("select count(*) from cost_line").fetchone()[0] == 0          # no tenant setting at all


@pytest.mark.db
def test_a_cost_line_cannot_name_a_task_of_another_tenant(sched, xer):
    path, _ = xer(tasks=40, name="b.xer", seed=99)
    load_file(sched, "globex", path)
    r = load(sched, "cost", [cost_rec(proj_id="1010", task_code="A1000")], tenant="initech")
    assert [q.code for q in r.quarantined] == ["UNKNOWN_PROJECT"]


# ---- reconcile -----------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def loaded(sched):
    recs = [cost_rec(line_id=str(i), task_code=f"A{1000 + 10 * i}", budget=f"{100 + i}.25", actual=f"{i}.5") for i in range(8)]
    recs.append(cost_rec(line_id="50", budget="oops"))
    data = csv_bytes(COST_HEAD, recs)
    cost.load_bytes(sched, TENANT, "cost", data, "c.csv")
    return sched, data


def rec(conn, data, kind="cost"):
    return cost.reconcile_bytes(conn, TENANT, kind, data, "c.csv")


@pytest.mark.db
def test_reconcile_passes_on_a_clean_load_and_reports_the_sums(loaded):
    conn, data = loaded
    r = rec(conn, data)
    assert r.ok and r.file_rows == 9 and r.stored == 8 and r.quarantined == 1
    assert r.sums["budget"] == sum((Decimal(f"{100 + i}.25") for i in range(8)), Decimal(0))


@pytest.mark.db
@pytest.mark.parametrize("sql,check", [
    ("update cost_line set actual = actual + 0.01 where line_id = 3", "FIELD"),
    ("update cost_line set budget = budget + 0.01 where line_id = 3", "SUM"),
    ("update cost_line set cost_code = 'XX' where line_id = 3", "FIELD"),
    ("update cost_line set task_code = 'A1000' where line_id = 3", "FIELD"),
    ("update cost_line set description = 'x' where line_id = 3", "FIELD"),
    ("update cost_line set period = '2030-01' where line_id = 3", "FIELD"),
    ("delete from cost_line where line_id = 3", "MISSING_STORED"),
    ("delete from cost_line where line_id = 3", "COUNT"),
    ("delete from quarantine", "COUNT"),
    ("insert into cost_line (tenant_id, proj_id, line_id, cost_code, budget, forecast, actual) values ('acme', 1010, 77, 'ZZ', 1, 1, 1)", "EXTRA_STORED"),
    ("insert into cost_line (tenant_id, proj_id, line_id, cost_code, budget, forecast, actual) values ('acme', 1010, 50, 'ZZ', 1, 1, 1)", "EXTRA_STORED"),
])
def test_reconcile_finds_each_kind_of_damage(loaded, admin, sql, check):
    conn, data = loaded
    assert rec(conn, data).ok                                    # the control: it says "match" before the damage
    admin.execute(sql)
    r = rec(conn, data)
    assert not r.ok and check in {f.check for f in r.findings}, [str(f) for f in r.findings]


@pytest.mark.db
def test_reconcile_of_commitments_checks_status_vendor_and_date(sched, admin):
    recs = [commit_rec(commitment_id=str(i)) for i in range(4)]
    data = csv_bytes(COMMIT_HEAD, recs)
    cost.load_bytes(sched, TENANT, "commitment", data, "c.csv")
    assert cost.reconcile_bytes(sched, TENANT, "commitment", data, "c.csv").ok
    for sql in ("update commitment set status = 'CLOSED' where commitment_id = 1", "update commitment set vendor = 'Other' where commitment_id = 1",
                "update commitment set issued = '2030-01-01' where commitment_id = 1", "update commitment set issued = null where commitment_id = 1",
                "update commitment set invoiced = invoiced + 1 where commitment_id = 1"):
        admin.execute(sql)
        assert not cost.reconcile_bytes(sched, TENANT, "commitment", data, "c.csv").ok, sql
        cost.load_bytes(sched, TENANT, "commitment", data, "c.csv")
        assert cost.reconcile_bytes(sched, TENANT, "commitment", data, "c.csv").ok


@pytest.mark.db
def test_reconcile_of_another_file_is_a_difference_and_an_unreadable_file_is_named(loaded):
    conn, data = loaded
    other = csv_bytes(COST_HEAD, [cost_rec(line_id="1", budget="5")])
    assert not rec(conn, other).ok
    r = rec(conn, b"")
    assert [f.check for f in r.findings] == ["FILE_UNREADABLE"]


@pytest.mark.db
def test_reconcile_sees_only_its_own_tenant(loaded):
    conn, data = loaded
    r = cost.reconcile_bytes(conn, "globex", "cost", data, "c.csv")
    assert not r.ok and r.stored == 0


@pytest.mark.db
def test_reconcile_file_reads_the_path(sched, tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(csv_bytes(COST_HEAD, [cost_rec()]))
    cost.load_file(sched, TENANT, "cost", str(p))
    assert cost.reconcile_file(sched, TENANT, "cost", str(p)).ok
    with pytest.raises(ValueError, match="cannot read"):
        cost.load_file(sched, TENANT, "cost", str(tmp_path / "missing.csv"))
