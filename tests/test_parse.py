"""Parser: layout, typing, and a named problem for everything it does not understand."""
from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal

import pytest

from xerlane.parse import FATAL_CODES, convert, parse_bytes, parse_file, parse_lines, parse_stream
from xerlane.schema import INT, NUM, TEXT, TS

HDR = "ERMHDR\t99.0\t2026-11-06\tProject\tsample\tgenerator\tinvented\tnone\tUSD"
CAL = ["%T\tCALENDAR", "%F\tclndr_id\tclndr_name\tclndr_type\tday_hr_cnt\tweek_hr_cnt\tdefault_flag", "%R\t1\tStandard\tBASE\t8\t40\tY"]


def codes(result):
    return [p.code for p in result.problems]


def test_minimal_file_parses_clean():
    r = parse_lines([HDR, *CAL, "%E"])
    assert not r.problems and r.ended and not r.fatal
    row = r.block("CALENDAR").rows[0]
    assert row.line == 4 and row.ok
    assert row.values["clndr_id"] == 1 and row.values["day_hr_cnt"] == Decimal(8) and row.values["clndr_name"] == "Standard"


def test_header_fields_named_by_position():
    r = parse_lines([HDR, "%E"])
    assert r.header_info == {"version": "99.0", "export_date": "2026-11-06", "user": "generator", "currency": "USD"}


def test_short_header_gives_empty_header_fields_not_an_error():
    r = parse_lines(["ERMHDR\t1", "%E"])
    assert r.header_info["currency"] == "" and not r.fatal


@pytest.mark.parametrize("first,why", [("", "empty first line"), ("hello", "text"), ("ERMHDRX\t1", "marker is not exactly ERMHDR"), ("ï»¿ERMHDR\t1", "byte order mark")])
def test_first_line_must_be_ermhdr(first, why):
    r = parse_lines([first, "%E"])
    assert "NOT_XER" in codes(r), why
    assert r.fatal and r.blocks == []


def test_empty_file_is_fatal_and_named():
    r = parse_bytes(b"")
    assert codes(r) == ["EMPTY_FILE"] and r.fatal


def test_missing_end_marker_is_fatal_with_last_line_number():
    r = parse_lines([HDR, *CAL])
    p = [p for p in r.problems if p.code == "MISSING_END"]
    assert len(p) == 1 and p[0].line == 4 and r.fatal and not r.ended


def test_data_after_end_marker_is_reported():
    r = parse_lines([HDR, *CAL, "%E", "%R\t1", "   ", ""])
    assert "TRAILING_DATA" in codes(r)
    assert not r.fatal


def test_lf_only_and_crlf_give_the_same_rows():
    lines = [HDR, *CAL, "%E"]
    a = parse_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))
    b = parse_bytes(("\n".join(lines) + "\n").encode("cp1252"))
    assert [r.raw for r in a.block("CALENDAR").rows] == [r.raw for r in b.block("CALENDAR").rows]
    assert not a.problems and not b.problems


def test_missing_final_newline_still_parses():
    r = parse_bytes(("\r\n".join([HDR, *CAL, "%E"])).encode("cp1252"))
    assert r.ended and not r.problems


def test_wrong_field_count_is_a_row_problem_with_the_row_kept():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_name", "%R\t1", "%R\t2\tb\textra", "%R\t3\tc", "%E"])
    rows = r.block("CALENDAR").rows
    assert [row.ok for row in rows] == [False, False, True]
    assert rows[0].problems[0].code == "WRONG_FIELD_COUNT" and rows[0].problems[0].line == 4
    assert rows[0].raw == ("1",) and rows[1].raw == ("2", "b", "extra")


def test_unknown_table_is_read_and_reported_not_dropped():
    r = parse_lines([HDR, "%T\tFOO", "%F\ta\tb", "%R\t1\t2", "%R\t3\t4", "%E"])
    b = r.block("FOO")
    assert not b.modeled and len(b.rows) == 2 and b.rows[0].values is None
    p = [p for p in r.problems if p.code == "UNMODELED_TABLE"]
    assert len(p) == 1 and p[0].line == 2 and "2 rows" in p[0].message and p[0].severity == "info"


def test_row_before_any_table():
    r = parse_lines([HDR, "%R\t1", "%E"])
    assert codes(r) == ["ROW_OUTSIDE_TABLE"] and r.problems[0].line == 2


def test_fields_before_any_table():
    assert "FIELDS_OUTSIDE_TABLE" in codes(parse_lines([HDR, "%F\ta", "%E"]))


def test_row_before_field_list():
    r = parse_lines([HDR, "%T\tCALENDAR", "%R\t1\tx", "%E"])
    assert r.block("CALENDAR").rows[0].problems[0].code == "NO_FIELD_LIST"


def test_second_field_list_is_reported_and_first_wins():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id", "%F\tother", "%R\t1", "%E"])
    assert "DUPLICATE_FIELD_LIST" in codes(r)
    assert r.block("CALENDAR").fields == ["clndr_id"]


def test_duplicate_field_name():
    assert "DUPLICATE_FIELD" in codes(parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_id", "%R\t1\t1", "%E"]))


def test_repeated_table_block_rows_are_flagged():
    r = parse_lines([HDR, *CAL[:3], "%T\tCALENDAR", "%F\tclndr_id", "%R\t9", "%E"])
    assert "DUPLICATE_TABLE" in codes(r)
    dup = r.blocks[1]
    assert dup.duplicate and dup.rows[0].problems[0].code == "DUPLICATE_TABLE"
    assert r.block("CALENDAR") is r.blocks[0]


def test_table_line_needs_exactly_one_name():
    assert "TABLE_LINE_FORMAT" in codes(parse_lines([HDR, "%T", "%E"]))
    assert "TABLE_LINE_FORMAT" in codes(parse_lines([HDR, "%T\tA\tB", "%E"]))


def test_required_column_missing_from_the_field_list():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_name", "%R\tx", "%E"])
    assert "MISSING_COLUMN" in codes(r)
    row = r.block("CALENDAR").rows[0]
    assert [p.code for p in row.problems] == ["MISSING_VALUE"] and "clndr_id" in row.problems[0].message


def test_stray_line_unknown_marker_and_blank_line_are_named_and_not_merged():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_name", "%R\t1\ta", "continuation text", "%X\twhat", "", "%R\t2\tb", "%E"])
    assert sorted(codes(r)) == ["BLANK_LINE", "STRAY_LINE", "UNKNOWN_MARKER"]
    assert [row.raw for row in r.block("CALENDAR").rows] == [("1", "a"), ("2", "b")]
    assert {p.code: p.line for p in r.problems} == {"STRAY_LINE": 5, "UNKNOWN_MARKER": 6, "BLANK_LINE": 7}


def test_second_header_line():
    assert "DUPLICATE_HEADER" in codes(parse_lines([HDR, HDR, "%E"]))


def test_end_line_with_text():
    assert "END_LINE_FORMAT" in codes(parse_lines([HDR, "%E\tjunk"]))


def test_cp1252_text_decodes():
    b = ("\r\n".join([HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_name", "%R\t1\tstraße €", "%E"]) + "\r\n").encode("cp1252")
    r = parse_bytes(b)
    assert not r.problems and r.block("CALENDAR").rows[0].values["clndr_name"] == "straße €"


def test_undefined_cp1252_byte_is_a_named_encoding_problem_on_the_row():
    raw = b"\r\n".join([HDR.encode(), b"%T\tCALENDAR", b"%F\tclndr_id\tclndr_name", b"%R\t1\tbad\x81byte", b"%E"]) + b"\r\n"
    r = parse_bytes(raw)
    row = r.block("CALENDAR").rows[0]
    assert [p.code for p in row.problems] == ["BAD_ENCODING"] and row.problems[0].line == 4
    assert not any(p.code == "BAD_ENCODING" for p in r.problems)          # reported once, on the row


def test_undefined_byte_outside_a_row_is_reported_on_the_file():
    raw = b"\r\n".join([HDR.encode(), b"%T\tCAL\x81", b"%E"]) + b"\r\n"
    assert "BAD_ENCODING" in codes(parse_bytes(raw))


def test_unmodeled_columns_are_kept_in_extra_and_empty_ones_are_not():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tzeta\talpha\tbeta", "%R\t1\tZ\t\tB", "%E"])
    row = r.block("CALENDAR").rows[0]
    assert row.extra == {"zeta": "Z", "beta": "B"}


def test_sha256_and_size_cover_the_raw_bytes():
    import hashlib
    data = ("\r\n".join([HDR, *CAL, "%E"]) + "\r\n").encode("cp1252")
    r = parse_bytes(data)
    assert r.sha256 == hashlib.sha256(data).hexdigest() and r.size == len(data)


def test_parse_file_matches_parse_bytes(tmp_path):
    data = ("\r\n".join([HDR, *CAL, "%E"]) + "\r\n").encode("cp1252")
    p = tmp_path / "a.xer"
    p.write_bytes(data)
    a, b = parse_file(str(p)), parse_bytes(data)
    assert a.sha256 == b.sha256 and [r.raw for r in a.all_rows()] == [r.raw for r in b.all_rows()]


def test_stream_reads_line_by_line_and_never_asks_for_the_whole_file():
    class Spy(io.RawIOBase):
        def __init__(self, data):
            self.lines, self.calls = io.BytesIO(data), []

        def readable(self):
            return True

        def __iter__(self):
            return iter(self.lines)

        def read(self, *a):
            self.calls.append("read")
            return b""

        def readall(self):
            self.calls.append("readall")
            return b""
    data = ("\r\n".join([HDR, *CAL, "%E"]) + "\r\n").encode("cp1252")
    spy = Spy(data)
    r = parse_stream(spy)
    assert r.ended and spy.calls == []


def test_a_fatal_first_line_stops_reading_but_still_hashes_the_whole_file():
    import hashlib
    data = b"nothing\r\n" + b"x" * 5000
    r = parse_bytes(data)
    assert r.fatal and r.sha256 == hashlib.sha256(data).hexdigest() and r.size == len(data)


@pytest.mark.parametrize("kind,text,expected", [
    (INT, "42", 42), (INT, "-7", -7), (INT, "", None), (NUM, "1.50", Decimal("1.50")), (NUM, "-0.25", Decimal("-0.25")), (NUM, "", None),
    (TS, "2026-01-05 07:00", datetime(2026, 1, 5, 7, 0)), (TS, "2026-01-05", datetime(2026, 1, 5)), (TS, "2026-01-05 07:00:09", datetime(2026, 1, 5, 7, 0, 9)),
    (TEXT, "anything at all", "anything at all"), (TEXT, "", None),
])
def test_convert_accepts(kind, text, expected):
    assert convert(kind, text) == (expected, None)


@pytest.mark.parametrize("kind,text", [
    (INT, "1.0"), (INT, "1e3"), (INT, " 4"), (INT, "4 "), (INT, "abc"), (INT, "99999999999999999999"), (INT, "+3"),
    (NUM, "1,5"), (NUM, "1.2.3"), (NUM, "1e5"), (NUM, ".5"), (NUM, "5."), (NUM, "NaN"), (NUM, "Infinity"), (NUM, " 1"),
    (TS, "2026-13-01 00:00"), (TS, "2026-02-30"), (TS, "2026-01-05 25:00"), (TS, "05/01/2026"), (TS, "2026-1-5"), (TS, "2026-01-05T07:00"),
])
def test_convert_rejects_and_says_why(kind, text):
    value, why = convert(kind, text)
    assert value is None and why


def test_bad_values_are_row_problems_naming_column_and_text():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tday_hr_cnt", "%R\t1\t8,5", "%R\tx\t8", "%E"])
    a, b = r.block("CALENDAR").rows
    assert a.problems[0].code == "BAD_VALUE" and "day_hr_cnt='8,5'" in a.problems[0].message
    assert b.problems[0].code == "BAD_VALUE" and "clndr_id='x'" in b.problems[0].message


def test_empty_required_value_is_missing_value():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tclndr_name", "%R\t\tn", "%E"])
    assert r.block("CALENDAR").rows[0].problems[0].code == "MISSING_VALUE"


def test_every_fatal_code_is_one_the_parser_can_produce():
    produced = {
        "EMPTY_FILE": parse_bytes(b""), "NOT_XER": parse_lines(["x"]), "MISSING_END": parse_lines([HDR]),
    }
    for code, result in produced.items():
        assert code in FATAL_CODES and code in codes(result)
    assert FATAL_CODES == set(produced)


def test_row_count_counts_every_block_of_a_name():
    r = parse_lines([HDR, *CAL, "%T\tCALENDAR", "%F\tclndr_id", "%R\t9", "%E"])
    assert r.row_count("CALENDAR") == 2


# ---- tests added after the mutation check found lines nothing pinned ------------------------------------------------------------------------------

def test_a_problem_is_immutable_and_prints_where_it_happened():
    from dataclasses import FrozenInstanceError
    from xerlane.parse import Problem
    p = Problem("X", "error", "msg", 7, "TASK")
    with pytest.raises(FrozenInstanceError):
        p.code = "Y"
    assert str(p) == "X (line 7, TASK): msg"
    assert str(Problem("X", "error", "msg")) == "X (file): msg"
    assert str(Problem("X", "error", "msg", 3)) == "X (line 3): msg"


def test_a_whole_number_is_accepted_up_to_the_biggest_bigint_and_refused_past_it():
    assert convert(INT, "9223372036854775807") == (9223372036854775807, None)
    assert convert(INT, "-9223372036854775807") == (-9223372036854775807, None)
    value, why = convert(INT, "9223372036854775808")
    assert value is None and "too large" in why
    value, why = convert(INT, "9999999999999999999")
    assert value is None and "too large" in why


def test_a_file_that_is_not_xer_is_not_read_any_further():
    r = parse_lines(["junk", "%T\tCALENDAR", "%F\tclndr_id", "%R\t1", "%E"])
    assert codes(r) == ["NOT_XER"] and r.blocks == [] and not r.ended


def test_a_table_with_no_field_list_and_no_rows_is_a_warning_with_its_line():
    r = parse_lines([HDR, "%T\tCALENDAR", "%E"])
    p = [p for p in r.problems if p.code == "EMPTY_TABLE_BLOCK"]
    assert len(p) == 1 and p[0].severity == "warning" and p[0].line == 2 and p[0].table == "CALENDAR"


def test_a_table_with_rows_but_no_field_list_is_not_also_called_empty():
    r = parse_lines([HDR, "%T\tCALENDAR", "%R\t1", "%E"])
    assert "EMPTY_TABLE_BLOCK" not in codes(r)


def test_end_marker_followed_by_only_a_tab_is_clean_and_followed_by_text_is_not():
    assert codes(parse_lines([HDR, "%E\t"])) == []
    assert codes(parse_lines([HDR, "%E\t\t"])) == []
    assert codes(parse_lines([HDR, "%E\tjunk"])) == ["END_LINE_FORMAT"]
    assert parse_lines([HDR, "%E\tjunk"]).problems[0].severity == "warning"


def test_duplicate_field_message_names_only_the_repeated_names():
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\tb\ta\ta\tc\tb", "%R\t1\t2\t3\t4\t5\t6", "%E"])
    p = [p for p in r.problems if p.code == "DUPLICATE_FIELD"]
    assert len(p) == 1 and p[0].message == "field names repeated: a, b"
    r = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id\ta\tb", "%R\t1\t2\t3", "%E"])
    assert "DUPLICATE_FIELD" not in codes(r)


def test_blank_and_trailing_lines_after_the_end_marker_are_told_apart():
    blank = parse_lines([HDR, "%E", "", "  ", "x"])
    by_line = {p.line: (p.code, p.severity) for p in blank.problems}
    assert by_line == {3: ("BLANK_LINE", "info"), 4: ("BLANK_LINE", "info"), 5: ("TRAILING_DATA", "error")}, "a whitespace only line is blank"


def test_every_line_after_the_end_marker_is_judged_not_only_the_first():
    r = parse_lines([HDR, "%E", "first", "second", "", "fourth"])
    assert [p.line for p in r.problems] == [3, 4, 5, 6]


def test_problems_outside_a_table_carry_no_table_and_inside_carry_its_name():
    inside = parse_lines([HDR, "%T\tCALENDAR", "%F\tclndr_id", "stray", "%X", "", "%E"])
    assert {p.code: p.table for p in inside.problems} == {"STRAY_LINE": "CALENDAR", "UNKNOWN_MARKER": "CALENDAR", "BLANK_LINE": "CALENDAR"}
    outside = parse_lines([HDR, "stray", "%X", "", "%E"])
    assert {p.code: p.table for p in outside.problems} == {"STRAY_LINE": None, "UNKNOWN_MARKER": None, "BLANK_LINE": None}


def test_blank_line_inside_a_table_is_a_warning_and_after_the_end_an_info():
    assert parse_lines([HDR, "%T\tCALENDAR", "", "%E"]).problems[0].severity == "warning"
    assert parse_lines([HDR, "%E", ""]).problems[0].severity == "info"


def test_first_line_message_shows_what_was_found_and_hints_at_a_byte_order_mark():
    plain = parse_lines(["hello world", "%E"]).problems[0]
    assert plain.line == 1 and "'hello world'" in plain.message and "byte order mark" not in plain.message
    bom = parse_bytes(b"\xef\xbb\xbfERMHDR\t1\r\n%E\r\n").problems[0]
    assert "byte order mark" in bom.message
