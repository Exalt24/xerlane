"""The generator, the vendor contract and the repo's own text rules."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import make_sample_xer as gen
from xerlane.parse import parse_bytes
from xerlane.schema import TABLES
from xerlane.validate import validate_parse

ROOT = Path(__file__).resolve().parent.parent


def test_generator_is_deterministic():
    assert gen.make(5, 100)[0] == gen.make(5, 100)[0]
    assert gen.make(5, 100)[0] != gen.make(6, 100)[0]


@pytest.mark.parametrize("seed", range(1, 9))
@pytest.mark.parametrize("projects", [1, 3])
def test_a_clean_generated_file_has_no_error_in_any_seed(seed, projects):
    findings, analysis = validate_parse(parse_bytes(gen.make(seed, 200, projects)[0]))
    assert [str(f) for f in findings if f.severity == "error"] == []
    assert analysis.rejected == {}


def test_generated_file_has_the_documented_shape():
    data, _ = gen.make(2, 300)
    assert data.startswith(b"ERMHDR\t") and data.endswith(b"%E\r\n") and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    r = parse_bytes(data)
    assert r.header_info["currency"] == "USD" and r.row_count("TASK") == 300
    assert {b.name for b in r.blocks} >= {t.name for t in TABLES}
    types = {row.values["pred_type"] for row in r.block("TASKPRED").rows}
    assert types == {"PR_FS", "PR_SS", "PR_FF", "PR_SF"}, "all four relationship types are generated"
    assert any(row.values["lag_hr_cnt"] < 0 for row in r.block("TASKPRED").rows) and any(row.values["lag_hr_cnt"] > 0 for row in r.block("TASKPRED").rows)
    assert any(ord(ch) > 127 for ch in data.decode("cp1252")), "non-ASCII text exercises the encoding"


def test_generator_scales_to_several_hundred_tasks_with_a_wbs_tree():
    r = parse_bytes(gen.make(1, 600, 1)[0])
    assert r.row_count("TASK") == 600
    parents = {row.values["wbs_id"]: row.values["parent_wbs_id"] for row in r.block("PROJWBS").rows}
    roots = [w for w, p in parents.items() if p is None]
    assert len(roots) == 1 and all(p is None or p in parents for p in parents.values())
    depth = lambda w: 0 if parents[w] is None else 1 + depth(parents[w])          # noqa: E731
    assert max(depth(w) for w in parents) == 2


@pytest.mark.parametrize("kind", sorted(gen.DEFECTS))
def test_planted_line_numbers_point_at_the_planted_row(kind):
    data, planted = gen.make(4, 120, defects=[kind])
    p = planted[0]
    lines = data.decode("cp1252").split("\r\n")
    if kind in ("truncated", "missing_end"):
        assert p.line == data.count(b"\n") + 1
    elif kind == "stray_line":
        assert lines[p.line - 1] == "this line has no marker"
    elif kind == "cycle":
        assert lines[p.line - 1].split("\t")[1] == p.key
    elif kind == "dup_key":
        assert lines[p.line - 1].split("\t")[1] == p.key
    else:
        assert lines[p.line - 1].split("\t")[1] == p.key, kind


def test_generator_has_no_import_of_xerlane():
    src = (ROOT / "tools" / "make_sample_xer.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+xerlane", src, re.M), "the generator must stay independent of the parser it tests"


# ---- the vendor contract ------------------------------------------------------------------------------------------------------------------------

def _oracle_sections() -> dict[str, str]:
    text = (ROOT / "docs" / "vendor-contract" / "oracle-xer-data-map-v23-excerpt.txt").read_text(encoding="utf-8")
    parts = re.split(r"^([A-Z]+) - \(", text, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_modeled_table_and_column_is_in_oracles_data_map():
    sections = _oracle_sections()
    missing = []
    for t in TABLES:
        assert t.name in sections, f"{t.name} is not in the Oracle excerpt"
        words = set(re.findall(r"[a-z][a-z0-9_]*", sections[t.name]))
        missing += [f"{t.name}.{c.name}" for c in t.cols if c.name not in words]
    assert missing == []


def test_the_extra_columns_the_generator_writes_are_oracle_columns_too():
    sections = _oracle_sections()
    for table, col in (("TASK", "priority_type"), ("TASK", "guid"), ("PROJECT", "guid"), ("PROJWBS", "guid")):
        assert col in set(re.findall(r"[a-z][a-z0-9_]*", sections[table])), (table, col)


def test_every_url_in_source_md_has_a_quote_or_excerpt_in_the_folder():
    folder = ROOT / "docs" / "vendor-contract"
    source = (folder / "SOURCE.md").read_text(encoding="utf-8")
    urls = set(re.findall(r"https://[^\s|`)]+", source))
    assert len(urls) >= 8
    for name in ("oracle-quotes.txt", "oracle-xer-data-map-v23-excerpt.txt", "third-party-layout-facts.txt"):
        assert (folder / name).stat().st_size > 500
    evidence = "".join((folder / n).read_text(encoding="utf-8") for n in ("oracle-quotes.txt", "oracle-xer-data-map-v23-excerpt.txt", "third-party-layout-facts.txt"))
    for u in urls:
        if "importing_and_exporting_projects.htm" in u:
            continue                                     # a frame shell that held no facts, listed for honesty only
        assert u in evidence, f"{u} is cited in SOURCE.md but no evidence file carries it"


def test_every_number_in_not_confirmed_is_unique_and_sequential():
    text = (ROOT / "docs" / "NOT_CONFIRMED.md").read_text(encoding="utf-8")
    nums = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)]
    assert nums == list(range(1, len(nums) + 1)) and len(nums) >= 12


def test_the_codes_the_parser_does_not_trust_are_all_named_in_not_confirmed():
    text = (ROOT / "docs" / "NOT_CONFIRMED.md").read_text(encoding="utf-8")
    for code in ("BAD_VALUE", "STRAY_LINE", "UNKNOWN_PRED_TYPE", "WRONG_FIELD_COUNT", "UNMODELED_TABLE"):
        assert code in text


# ---- text rules for the whole repository -----------------------------------------------------------------------------------------------------------

TEXT_SUFFIXES = {".py", ".md", ".txt", ".sql", ".yml", ".yaml", ".toml", ".ini", ".json", ".cfg", ".ts", ".vue", ".css"}


def _repo_text_files():
    skip = {".venv", ".git", ".pytest_cache", ".hypothesis", "__pycache__", "out", ".mutation", "node_modules", ".nuxt", ".output", ".nitro"}       # the last four are the web app's installed packages and build output
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and (p.suffix in TEXT_SUFFIXES or p.name in ("LICENSE", ".gitignore")):
            yield p


def test_no_em_or_en_dash_anywhere_in_the_repository():
    bad = []
    for p in _repo_text_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search("[" + "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015)) + "]", text):
            bad.append(str(p.relative_to(ROOT)))
    assert bad == []


def test_no_ai_attribution_in_repository_text():
    pat = re.compile(r"co-authored-by|generated with|claude code|anthropic", re.I)
    this = Path(__file__).resolve()
    bad = [str(p.relative_to(ROOT)) for p in _repo_text_files() if p.resolve() != this and pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert bad == []


def test_the_licence_is_mit():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Copyright (c) 2026" in text
