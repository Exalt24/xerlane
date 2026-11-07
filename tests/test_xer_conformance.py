"""The conformance script's own parts, tested without the Rust binary: the comparison must see a difference, and the corpus must reach every structural problem code.

The run against the real Rust binary is `python tools/xer_conformance.py` (CI does it in the rust job).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import xer_conformance as conf

ROOT = Path(__file__).resolve().parent.parent


def test_differences_is_empty_for_equal_values():
    a = {"x": [1, {"y": "z"}], "n": None}
    assert conf.differences(a, {"x": [1, {"y": "z"}], "n": None}) == []


@pytest.mark.parametrize(
    "a,b,fragment",
    [
        ({"x": 1}, {"x": 2}, "$.x: python 1, rust 2"),
        ({"x": [1, 2]}, {"x": [1]}, "python has 2 items, rust has 1"),
        ({"x": [{"y": "a"}]}, {"x": [{"y": "b"}]}, "$.x[0].y"),
        ({"x": 1}, {}, "present on one side only"),
        ({"x": 1}, {"x": True}, "python 1, rust True"),
        ({"x": None}, {"x": 0}, "python None, rust 0"),
    ],
)
def test_differences_names_where_two_values_differ(a, b, fragment):
    found = conf.differences(a, b)
    assert found and fragment in found[0]


def test_python_tokens_has_the_shape_the_rust_binary_prints():
    data, _ = conf.gen.make(1, 5, 1)
    tokens, fatal = conf.python_tokens(data)
    assert fatal is False
    assert set(tokens) == {"size", "line_count", "ended", "header", "problems", "blocks"}
    assert set(tokens["blocks"][0]) == {"name", "line", "duplicate", "fields_line", "fields", "rows"}
    assert set(tokens["blocks"][0]["rows"][0]) == {"line", "raw", "problems"}
    assert tokens["size"] == len(data) and tokens["ended"] is True


def test_python_tokens_leaves_out_the_problems_that_need_the_schema():
    data, _ = conf.gen.make(3, 30, 1, ["bad_number", "bad_date"])
    tokens, _ = conf.python_tokens(data)
    codes = {p["code"] for b in tokens["blocks"] for r in b["rows"] for p in r["problems"]} | {p["code"] for p in tokens["problems"]}
    assert codes.isdisjoint(conf.SCHEMA_ONLY_ROW_CODES | conf.SCHEMA_ONLY_FILE_CODES)


def test_python_fatal_flag_follows_the_three_fatal_codes():
    assert conf.python_tokens(b"")[1] is True
    assert conf.python_tokens(b"hello\r\n")[1] is True
    assert conf.python_tokens(b"ERMHDR\t1\r\n")[1] is True
    assert conf.python_tokens(b"ERMHDR\t1\r\n%E\r\n")[1] is False


def test_the_list_of_structural_codes_is_every_code_parse_py_writes_minus_the_schema_ones():
    source = (ROOT / "xerlane" / "parse.py").read_text(encoding="utf-8")
    written = set(re.findall(r'(?:problem\(|Problem\()"([A-Z_]+)"', source))
    assert written - conf.SCHEMA_ONLY_FILE_CODES - conf.SCHEMA_ONLY_ROW_CODES == set(conf.STRUCTURAL_CODES)


def test_the_corpus_reaches_every_structural_code_and_every_group():
    corpus = conf.build_corpus(fuzz=120, benchmark=False)
    assert {g for g, _, _ in corpus} >= {"generated", "planted defect", "edge case", "damaged"}
    seen: set[str] = set()
    for _, _, data in corpus:
        tokens, _ = conf.python_tokens(data)
        seen |= {p["code"] for p in tokens["problems"]}
        seen |= {p["code"] for b in tokens["blocks"] for r in b["rows"] for p in r["problems"]}
    assert set(conf.STRUCTURAL_CODES) <= seen, sorted(set(conf.STRUCTURAL_CODES) - seen)


def test_the_corpus_is_the_same_every_time():
    a = conf.build_corpus(fuzz=40, benchmark=False)
    b = conf.build_corpus(fuzz=40, benchmark=False)
    assert a == b
