"""Reconcile's own reader and comparison helpers, tested without a database."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from xerlane import reconcile as rc
from xerlane.schema import INT, NUM, TEXT, TS


def write(tmp_path, lines, newline="\r\n", name="a.xer"):
    p = tmp_path / name
    p.write_bytes((newline.join(lines) + newline).encode("cp1252"))
    return str(p)


HDR = "ERMHDR\t1\t2026-11-06"


def test_reader_returns_tables_fields_and_numbered_rows(tmp_path):
    sha, tables, has_header, has_end = rc._read_file(write(tmp_path, [HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_name", "%R\t1\ta", "%R\t2\tb", "%T\tRSRC", "%F\trsrc_id", "%R\t9", "%E"]))
    assert has_header and has_end and len(sha) == 64
    assert tables["CALENDAR"].fields == ["clndr_id", "clndr_name"]
    assert tables["CALENDAR"].rows == [(4, ["1", "a"]), (5, ["2", "b"])]
    assert tables["RSRC"].rows == [(8, ["9"])]


def test_reader_ignores_everything_after_the_end_marker(tmp_path):
    _, tables, _, has_end = rc._read_file(write(tmp_path, [HDR, "%T\tCALENDAR", "%F\tclndr_id", "%R\t1", "%E", "%R\t2", "%T\tRSRC", "%F\trsrc_id", "%R\t3"]))
    assert has_end and tables["CALENDAR"].rows == [(4, ["1"])] and "RSRC" not in tables


def test_reader_reports_a_missing_end_and_a_missing_header(tmp_path):
    assert rc._read_file(write(tmp_path, [HDR, "%T\tCALENDAR", "%F\tclndr_id", "%R\t1"]))[2:] == (True, False)
    assert rc._read_file(write(tmp_path, ["junk", "%E"], name="b.xer"))[2:] == (False, True)
    assert rc._read_file(write(tmp_path, ["ERMHDRX", "%E"], name="c.xer"))[2] is False


def test_reader_keeps_the_first_field_list_and_appends_rows_of_a_repeated_table(tmp_path):
    _, tables, _, _ = rc._read_file(write(tmp_path, [HDR, "%T\tCALENDAR", "%F\tclndr_id", "%R\t1", "%F\tother", "%T\tCALENDAR", "%F\tzzz", "%R\t2", "%E"]))
    assert tables["CALENDAR"].fields == ["clndr_id"] and [r for _, r in tables["CALENDAR"].rows] == [["1"], ["2"]]


def test_reader_skips_a_table_line_with_no_name_and_rows_before_any_table(tmp_path):
    _, tables, _, _ = rc._read_file(write(tmp_path, [HDR, "%R\t5", "%T", "%R\t6", "%E"]))
    assert tables == {}


def test_reader_accepts_lf_only_and_no_final_newline(tmp_path):
    p = tmp_path / "lf.xer"
    p.write_bytes(("\n".join([HDR, "%T\tCALENDAR", "%F\tclndr_id", "%R\t1", "%E"])).encode("cp1252"))
    _, tables, has_header, has_end = rc._read_file(str(p))
    assert has_header and has_end and tables["CALENDAR"].rows == [(4, ["1"])]


def test_reader_decodes_cp1252_and_keeps_undecodable_bytes_visible(tmp_path):
    p = tmp_path / "e.xer"
    p.write_bytes(b"\r\n".join([HDR.encode(), b"%T\tCALENDAR", b"%F\tclndr_id\tclndr_name", b"%R\t1\tstra\xdfe", b"%R\t2\tbad\x81", b"%E"]) + b"\r\n")
    _, tables, _, _ = rc._read_file(str(p))
    assert tables["CALENDAR"].rows[0][1][1] == "straße" and "�" in tables["CALENDAR"].rows[1][1][1]


def test_field_returns_none_for_a_missing_column_and_for_a_short_row():
    ft = rc._FileTable(fields=["a", "b", "c"])
    assert rc._field(ft, ["1", "2", "3"], "b") == "2"
    assert rc._field(ft, ["1"], "c") is None
    assert rc._field(ft, ["1", "2", "3"], "zzz") is None


@pytest.mark.parametrize("text,expected", [("12", 12), ("-3", -3), ("", None), (None, None), ("1.5", None), ("x", None), (" 4", None), ("4 ", None), ("+4", None)])
def test_as_int(text, expected):
    assert rc._as_int(text) == expected


@pytest.mark.parametrize("text,expected", [("1.50", Decimal("1.50")), ("-0.25", Decimal("-0.25")), ("7", Decimal(7)), ("", None), (None, None), ("1,5", None), ("1e3", None), (".5", None), ("5.", None), ("x", None)])
def test_as_num(text, expected):
    assert rc._as_num(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2026-01-05 07:00", datetime(2026, 1, 5, 7, 0)), ("2026-01-05 07:00:09", datetime(2026, 1, 5, 7, 0, 9)), ("2026-01-05", datetime(2026, 1, 5)),
    ("2026-13-05", None), ("2026-01-05 25:00", None), ("2026-01-05T07:00", None), ("", None), (None, None), ("5 January", None)])
def test_as_ts(text, expected):
    assert rc._as_ts(text) == expected


@pytest.mark.parametrize("kind,text,stored,same", [
    (INT, "5", 5, True), (INT, "5", 6, False), (INT, "x", 5, False), (INT, "", None, True), (INT, "5", None, False), (INT, "", 5, False),
    (NUM, "1.50", Decimal("1.5"), True), (NUM, "1.50", Decimal("1.51"), False), (NUM, "1,5", Decimal("1.5"), False), (NUM, "", None, True), (NUM, "0", None, False), (NUM, "1", "x", False),
    (TS, "2026-01-05 07:00", datetime(2026, 1, 5, 7, 0), True), (TS, "2026-01-05 07:00", datetime(2026, 1, 5, 7, 1), False), (TS, "2026-01-05", datetime(2026, 1, 5), True),
    (TS, "bad", datetime(2026, 1, 5), False), (TS, "", None, True),
    (TEXT, "abc", "abc", True), (TEXT, "abc", "abd", False), (TEXT, "abc", "abc ", False), (TEXT, "", None, True), (TEXT, "", "", False), (TEXT, None, None, True), (TEXT, "x", None, False),
])
def test_same(kind, text, stored, same):
    assert rc._same(kind, text, stored) is same


def test_show_prints_numbers_and_dates_as_themselves_and_text_quoted():
    assert rc._show(Decimal("4526.49")) == "4526.49"
    assert rc._show(datetime(2026, 1, 5, 7, 0)) == "2026-01-05 07:00:00"
    assert rc._show(7) == "7"
    assert rc._show("abc") == "'abc'"
    assert rc._show(None) == "None"


def test_a_finding_prints_its_check_table_and_message():
    assert str(rc.ReconFinding("SUM", "TASK", "x differs")) == "SUM TASK: x differs"
    assert str(rc.ReconFinding("FILE", None, "bad")) == "FILE file: bad"
    r = rc.ReconResult("p", "s")
    assert r.ok
    r.findings.append(rc.ReconFinding("ROW", "TASK", "m"))
    assert not r.ok


def test_a_finding_is_immutable():
    from dataclasses import FrozenInstanceError
    f = rc.ReconFinding("SUM", "TASK", "m")
    with pytest.raises(FrozenInstanceError):
        f.check = "ROW"
