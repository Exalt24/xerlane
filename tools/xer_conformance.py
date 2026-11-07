"""Check that the Rust tokenizer and the Python parser read every XER file the same way.

    python tools/xer_conformance.py                     build the Rust binary if needed, then compare over the whole corpus
    python tools/xer_conformance.py --rust-bin PATH     use a binary you already built
    python tools/xer_conformance.py --fuzz 600          more damaged files (default 300)
    python tools/xer_conformance.py --no-benchmark      skip the 6 MB benchmark file
    python tools/xer_conformance.py --keep DIR          write every file that differs into DIR

Exit code 0 when no file differs, 1 when any does, 2 when the Rust binary cannot be built or run.

The Python side is xerlane/parse.py itself, not a copy. The Rust side is rust/xer-tokenizer, which does the structural half of the parser only. So what is compared is
that structural half: for each file the size, the line count, the header fields, whether %E was seen, every table (name, line, field list and its line, whether it repeats an
earlier table), every row (line number and the fields exactly as written), and every problem the structure produces (code, severity, line, table), in order. The
problems that need the schema (BAD_VALUE, MISSING_VALUE, MISSING_COLUMN, UNMODELED_TABLE) are left out on the Python side because the Rust crate does not know the
schema. The Rust exit code must also agree with the Python parser's `fatal`.

The corpus: generated schedules (several seeds, sizes and project counts), each of the 15 planted defects alone and all of them together, the 6 MB benchmark file,
hand written edge cases, every .xer file found in the repository, and seeded random damage to generated files (a deleted or repeated line, a changed delimiter, a stray
byte that cp1252 does not define, a cut file, and so on).
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_sample_xer as gen  # noqa: E402
from xerlane.parse import parse_bytes  # noqa: E402

CRATE = ROOT / "rust" / "xer-tokenizer"
EXE = "xer-tokenizer.exe" if sys.platform == "win32" else "xer-tokenizer"
SCHEMA_ONLY_FILE_CODES = {"MISSING_COLUMN", "UNMODELED_TABLE"}
SCHEMA_ONLY_ROW_CODES = {"BAD_VALUE", "MISSING_VALUE"}
# Every structural code parse.py can produce. The run prints how many files exercised each, so a code no file reaches is visible.
STRUCTURAL_CODES = (
    "EMPTY_FILE", "NOT_XER", "MISSING_END", "TRAILING_DATA", "BLANK_LINE", "END_LINE_FORMAT", "DUPLICATE_HEADER", "UNKNOWN_MARKER", "STRAY_LINE",
    "TABLE_LINE_FORMAT", "DUPLICATE_TABLE", "FIELDS_OUTSIDE_TABLE", "DUPLICATE_FIELD_LIST", "DUPLICATE_FIELD", "ROW_OUTSIDE_TABLE", "NO_FIELD_LIST",
    "WRONG_FIELD_COUNT", "BAD_ENCODING", "EMPTY_TABLE_BLOCK",
)


def _problem(p) -> dict:
    return {"code": p.code, "severity": p.severity, "line": p.line, "table": p.table}


def python_tokens(data: bytes) -> tuple[dict, bool]:
    """What xerlane/parse.py read from these bytes, in the shape the Rust binary prints, and its `fatal` flag."""
    r = parse_bytes(data)
    return {
        "size": r.size,
        "line_count": r.line_count,
        "ended": r.ended,
        "header": r.header,
        "problems": [_problem(p) for p in r.problems if p.code not in SCHEMA_ONLY_FILE_CODES],
        "blocks": [
            {
                "name": b.name,
                "line": b.line,
                "duplicate": b.duplicate,
                "fields_line": b.fields_line,
                "fields": b.fields,
                "rows": [
                    {"line": row.line, "raw": list(row.raw), "problems": [_problem(p) for p in row.problems if p.code not in SCHEMA_ONLY_ROW_CODES]}
                    for row in b.rows
                ],
            }
            for b in r.blocks
        ],
    }, r.fatal


def differences(a, b, path: str = "$", limit: int = 6) -> list[str]:
    """Where two parsed JSON values differ, as short readable lines. Stops after `limit`."""
    out: list[str] = []

    def walk(x, y, where: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(set(x) | set(y)):
                if key not in x or key not in y:
                    out.append(f"{where}.{key}: present on one side only")
                else:
                    walk(x[key], y[key], f"{where}.{key}")
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                out.append(f"{where}: python has {len(x)} items, rust has {len(y)}")
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{where}[{i}]")
        elif x != y or type(x) is not type(y):
            out.append(f"{where}: python {x!r}, rust {y!r}")

    walk(a, b, path)
    return out


# --- the corpus ---------------------------------------------------------------------------------------------------------------------------------------------------

HEADER = b"ERMHDR\t99.0\t2026-11-06\tProject\r\n"
TABLE_T = b"%T\tTASK\r\n%F\ttask_id\ttask_code\r\n%R\t1\tA\r\n"

EDGE_CASES: dict[str, bytes] = {
    "empty file": b"",
    "one blank line": b"\r\n",
    "header only": b"ERMHDR\r\n",
    "header only, no line end": b"ERMHDR",
    "header with tabs": b"ERMHDR\t\t\t\r\n%E\r\n",
    "no marker on line one": b"%T\tTASK\r\n%E\r\n",
    "byte order mark": b"\xef\xbb\xbfERMHDR\t1\r\n%E\r\n",
    "ermhdr lowercase": b"ermhdr\t1\r\n%E\r\n",
    "ermhdr with a space": b"ERMHDR 1\r\n%E\r\n",
    "lf line ends": b"ERMHDR\t1\n%T\tTASK\n%F\ta\tb\n%R\t1\t2\n%E\n",
    "mixed line ends": b"ERMHDR\t1\r\n%T\tTASK\n%F\ta\tb\r\n%R\t1\t2\n%E\r\n",
    "two carriage returns": b"ERMHDR\t1\n%T\tT\n%F\ta\n%R\tx\r\r\n%E\n",
    "lone carriage return inside a field": b"ERMHDR\t1\n%T\tT\n%F\ta\n%R\tx\ry\n%E\n",
    "no final line end": b"ERMHDR\t1\r\n%T\tT\r\n%F\ta\r\n%R\t1\r\n%E",
    "missing end marker": HEADER + TABLE_T,
    "end marker with text": HEADER + TABLE_T + b"%E\tjunk\r\n",
    "end marker with empty tabs": HEADER + TABLE_T + b"%E\t\t\r\n",
    "end marker twice": HEADER + TABLE_T + b"%E\r\n%E\r\n",
    "text after the end": HEADER + TABLE_T + b"%E\r\nmore text\r\n",
    "blank lines after the end": HEADER + TABLE_T + b"%E\r\n\r\n   \r\n\t\r\n",
    "unicode whitespace after the end": HEADER + TABLE_T + b"%E\r\n\xa0\r\n\x1c\x1d\x1e\x1f\r\n\x85\r\n",
    "blank line inside a table": HEADER + b"%T\tT\r\n%F\ta\r\n\r\n%R\t1\r\n%E\r\n",
    "blank line before any table": HEADER + b"\r\n%T\tT\r\n%E\r\n",
    "whitespace line inside a table": HEADER + b"%T\tT\r\n%F\ta\r\n  \r\n%E\r\n",
    "table with no name": HEADER + b"%T\r\n%F\ta\r\n%R\t1\r\n%E\r\n",
    "table with an empty name": HEADER + b"%T\t\r\n%F\ta\r\n%E\r\n",
    "table with two names": HEADER + b"%T\tA\tB\r\n%F\ta\r\n%E\r\n",
    "two tables with no name": HEADER + b"%T\r\n%F\ta\r\n%T\r\n%F\ta\r\n%R\t1\r\n%E\r\n",
    "repeated table": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t1\r\n%T\tT\r\n%F\ta\r\n%R\t2\r\n%R\t3\r\n%E\r\n",
    "repeated table three times": HEADER + b"%T\tT\r\n%T\tT\r\n%T\tT\r\n%R\t1\r\n%E\r\n",
    "table with no field list and no rows": HEADER + b"%T\tT\r\n%T\tU\r\n%F\ta\r\n%E\r\n",
    "row before the field list": HEADER + b"%T\tT\r\n%R\t1\r\n%F\ta\r\n%E\r\n",
    "field list outside a table": HEADER + b"%F\ta\r\n%R\t1\r\n%E\r\n",
    "row outside a table": HEADER + b"%R\t1\r\n%E\r\n",
    "second field list": HEADER + b"%T\tT\r\n%F\ta\r\n%F\tb\r\n%R\t1\r\n%E\r\n",
    "repeated field names": HEADER + b"%T\tT\r\n%F\ta\tb\ta\tb\tc\r\n%R\t1\t2\t3\t4\t5\r\n%E\r\n",
    "empty field list": HEADER + b"%T\tT\r\n%F\r\n%R\r\n%E\r\n",
    "field list of one empty name": HEADER + b"%T\tT\r\n%F\t\r\n%R\t\r\n%E\r\n",
    "row with no tab": HEADER + b"%T\tT\r\n%F\ta\r\n%R\r\n%E\r\n",
    "row with too many fields": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t1\t2\t3\r\n%E\r\n",
    "row with too few fields": HEADER + b"%T\tT\r\n%F\ta\tb\tc\r\n%R\t1\r\n%E\r\n",
    "row with empty fields": HEADER + b"%T\tT\r\n%F\ta\tb\tc\r\n%R\t\t\t\r\n%E\r\n",
    "second header line": HEADER + b"ERMHDR\t2\r\n%T\tT\r\n%E\r\n",
    "header after the end": HEADER + b"%E\r\nERMHDR\t2\r\n",
    "unknown marker": HEADER + b"%T\tT\r\n%F\ta\r\n%Q\t1\r\n%E\r\n",
    "unknown marker before any table": HEADER + b"%Q\t1\r\n%E\r\n",
    "long unknown marker": HEADER + b"%ABCDEFGHIJKLMNOP\t1\r\n%E\r\n",
    "percent sign alone": HEADER + b"%\t1\r\n%E\r\n",
    "stray line in a table": HEADER + b"%T\tT\r\n%F\ta\r\nno marker here\r\n%R\t1\r\n%E\r\n",
    "stray line before any table": HEADER + b"stray\r\n%E\r\n",
    "marker prefix is not a marker": HEADER + b"%TASK\tx\r\n%Fa\r\n%Rx\r\n%E\r\n",
    "spaces instead of tabs": HEADER + b"%T TASK\r\n%F a b\r\n%R 1 2\r\n%E\r\n",
    "cp1252 text": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t\x80\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x8b\x8c\x8e\r\n%R\t\x91\x92\x93\x94\x95\x96\x97\x98\x99\x9a\x9b\x9c\x9e\x9f\r\n%R\t\xa0\xe9\xff\r\n%E\r\n",
    "undefined byte in a row": HEADER + b"%T\tT\r\n%F\ta\r\n%R\tab\x81cd\r\n%R\t\x8d\x8f\x90\x9d\r\n%E\r\n",
    "undefined byte in a table line": HEADER + b"%T\tT\x81\r\n%F\ta\r\n%E\r\n",
    "undefined byte in a field list": HEADER + b"%T\tT\r\n%F\ta\x8d\r\n%R\t1\r\n%E\r\n",
    "undefined byte in the header": b"ERMHDR\t1\x81\r\n%E\r\n",
    "undefined byte on line one": b"\x81ERMHDR\r\n%E\r\n",
    "undefined byte after the end": HEADER + b"%E\r\n\x81\r\n",
    "undefined byte in a stray line": HEADER + b"stray \x90 text\r\n%E\r\n",
    "undefined byte in a blank-looking line": HEADER + b"%T\tT\r\n\x9d\r\n%E\r\n",
    "undefined byte in a row outside a table": HEADER + b"%R\t\x81\r\n%E\r\n",
    "undefined byte in a row after the end": HEADER + b"%E\r\n%R\t\x81\r\n",
    "undefined byte in a duplicate table row": HEADER + b"%T\tT\r\n%F\ta\r\n%T\tT\r\n%F\ta\r\n%R\t\x81\r\n%E\r\n",
    "row marker without a tab and a bad byte": HEADER + b"%T\tT\r\n%F\ta\r\n%R\x81\r\n%E\r\n",
    "null byte": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t\x00\r\n%E\r\n",
    "quotes and backslashes": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t\"q\"\\\\\r\n%E\r\n",
    "control characters in a field": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t\x01\x02\x1f\x7f\r\n%E\r\n",
    "a very long field": HEADER + b"%T\tT\r\n%F\ta\r\n%R\t" + b"x" * 100_000 + b"\r\n%E\r\n",
    "ten thousand empty fields": HEADER + b"%T\tT\r\n%F" + b"\t" * 10_000 + b"\r\n%R" + b"\t" * 10_000 + b"\r\n%E\r\n",
    "only line ends": b"\n\n\n",
    "only carriage returns": b"\r\r\r",
    "the first line is a row": b"%R\tx\r\n%E\r\n",
    "header, table, end, nothing else": b"ERMHDR\t1\r\n%T\tT\r\n%E\r\n",
}

FUZZ_MARKER_LINES = [
    b"%T\tTASK", b"%T", b"%T\tA\tB", b"%T\t", b"%F\tx\tx", b"%F", b"%R\ta", b"%R", b"%R\t\t", b"%E", b"%E\tz", b"ERMHDR", b"ERMHDR\t1", b"%Z\t1",
    b"", b"\t", b"  ", b"junk", b"\x1c", b"\xa0", b"\x81", b"%%", b"%R\t\x81", b"ermhdr",
]
UNDEFINED_BYTES = [0x81, 0x8D, 0x8F, 0x90, 0x9D]


def fuzz_once(rng: random.Random, base: bytes) -> bytes:
    data = base
    for _ in range(rng.randint(1, 4)):
        lines = data.split(b"\n")
        kind = rng.randrange(16)
        i = rng.randrange(len(lines)) if lines else 0
        if kind == 0 and len(lines) > 1:
            del lines[i]
        elif kind == 1:
            lines.insert(i, lines[i])
        elif kind == 2 and len(lines) > 1:
            j = rng.randrange(len(lines))
            lines[i], lines[j] = lines[j], lines[i]
        elif kind == 3:
            lines[i] = lines[i].replace(b"\t", b" ", 1)
        elif kind == 4:
            lines[i] = lines[i].removesuffix(b"\r")
        elif kind == 5:
            lines[i] = lines[i] + b"\r"
        elif kind == 6:
            pos = rng.randint(0, len(lines[i]))
            lines[i] = lines[i][:pos] + bytes([rng.choice(UNDEFINED_BYTES)]) + lines[i][pos:]
        elif kind == 7:
            pos = rng.randint(0, len(lines[i]))
            lines[i] = lines[i][:pos] + bytes([rng.randint(0x80, 0xFF)]) + lines[i][pos:]
        elif kind == 8:
            lines.insert(i, rng.choice(FUZZ_MARKER_LINES) + (b"\r" if rng.random() < 0.8 else b""))
        elif kind == 9:
            lines[i] = lines[i].replace(b"\t", b"\t\t", 1)
        elif kind == 10:
            lines[i] = lines[i].replace(b"\t", b"", 1)
        elif kind == 11:
            lines[i] = rng.choice([b"%T", b"%F", b"%R", b"%E"]) + lines[i][2:]
        data = b"\n".join(lines)
        if kind == 12 and data:
            data = data[: rng.randrange(len(data))]
        elif kind == 13:
            data = data.removesuffix(b"\n").removesuffix(b"\r")
        elif kind == 14:
            data = b"\xef\xbb\xbf" + data
        elif kind == 15:
            data = data.replace(b"\r\n", b"\n")
    return data


def build_corpus(fuzz: int, benchmark: bool) -> list[tuple[str, str, bytes]]:
    """(group, name, bytes) for every file to compare."""
    corpus: list[tuple[str, str, bytes]] = []
    for seed in (1, 2, 3, 7, 11, 23, 42):
        for tasks, projects in ((1, 1), (5, 1), (40, 1), (120, 2), (400, 3)):
            data, _ = gen.make(seed, tasks, projects)
            corpus.append(("generated", f"seed {seed}, {tasks} tasks, {projects} project(s)", data))
    kinds = sorted(gen.DEFECTS)
    for kind in kinds:
        for seed in (3, 7, 19):
            data, _ = gen.make(seed, 60, 1, [kind])
            corpus.append(("planted defect", f"{kind}, seed {seed}", data))
    for seed in (5, 6):
        data, _ = gen.make(seed, 80, 2, [k for k in kinds if k not in ("truncated", "missing_end")])
        corpus.append(("planted defect", f"all but the two cut-off kinds, seed {seed}", data))
        data, _ = gen.make(seed, 80, 2, kinds)
        corpus.append(("planted defect", f"all fifteen, seed {seed}", data))
    if benchmark:
        data, _ = gen.make(11, 20000)
        corpus.append(("benchmark", "seed 11, 20000 tasks", data))
    for name, data in EDGE_CASES.items():
        corpus.append(("edge case", name, data))
    skip = {".venv", "node_modules", ".git", "target", ".mutation", ".hypothesis", ".pytest_cache"}
    for path in sorted(ROOT.rglob("*.xer")):
        if skip.intersection(path.relative_to(ROOT).parts):
            continue
        corpus.append(("file in the repository", str(path.relative_to(ROOT)), path.read_bytes()))
    rng = random.Random(20261107)
    bases = [gen.make(s, t, p, d)[0] for s, t, p, d in ((1, 6, 1, []), (2, 15, 1, []), (3, 30, 2, ["cycle", "stray_line"]), (4, 10, 1, ["wrong_field_count"]))]
    for n in range(fuzz):
        corpus.append(("damaged", f"damaged {n:04d}", fuzz_once(rng, rng.choice(bases))))
    return corpus


# --- running the Rust side ------------------------------------------------------------------------------------------------------------------------------------------

def find_or_build(rust_bin: str | None) -> Path:
    if rust_bin:
        return Path(rust_bin)
    exe = CRATE / "target" / "release" / EXE
    try:
        subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=CRATE, check=True, timeout=540)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"cannot build the Rust tokenizer: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return exe


def rust_tokens(exe: Path, path: Path) -> tuple[dict, int]:
    proc = subprocess.run([str(exe), str(path)], capture_output=True, timeout=120)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"{exe.name} exited {proc.returncode}: {proc.stderr.decode(errors='replace')[:300]}")
    return json.loads(proc.stdout), proc.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rust-bin", help="path to a built xer-tokenizer binary (default: cargo build --release in rust/xer-tokenizer)")
    ap.add_argument("--fuzz", type=int, default=300, help="how many randomly damaged files to add (default 300)")
    ap.add_argument("--no-benchmark", action="store_true", help="leave out the 6 MB, 65,686 row file")
    ap.add_argument("--keep", help="write each file that differs into this folder")
    a = ap.parse_args(argv)

    exe = find_or_build(a.rust_bin)
    if not exe.is_file():
        print(f"no binary at {exe}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    corpus = build_corpus(a.fuzz, not a.no_benchmark)
    keep = Path(a.keep) if a.keep else None
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[int]] = {}
    failures: list[tuple[str, str, list[str]]] = []
    rows_total = 0
    seen: collections.Counter = collections.Counter()
    with tempfile.TemporaryDirectory(prefix="xer-conformance-") as tmp:
        for n, (group, name, data) in enumerate(corpus):
            path = Path(tmp) / f"case{n:05d}.xer"
            path.write_bytes(data)
            py, py_fatal = python_tokens(data)
            rs, code = rust_tokens(exe, path)
            diffs = differences(py, rs)
            if (code == 1) != py_fatal:
                diffs.append(f"exit code: python fatal {py_fatal}, rust exit {code}")
            codes = {p["code"] for p in py["problems"]} | {p["code"] for b in py["blocks"] for row in b["rows"] for p in row["problems"]}
            seen.update(codes)
            stats = groups.setdefault(group, [0, 0, 0])
            stats[0] += 1
            stats[1] += sum(len(b["rows"]) for b in py["blocks"])
            rows_total += sum(len(b["rows"]) for b in py["blocks"])
            if diffs:
                stats[2] += 1
                failures.append((group, name, diffs))
                if keep:
                    (keep / f"differs{n:05d}.xer").write_bytes(data)
    print(f"{'group':26}{'files':>7}{'rows':>10}{'differ':>8}")
    for group, (files, rows, bad) in groups.items():
        print(f"{group:26}{files:>7}{rows:>10,}{bad:>8}")
    print(f"{'all':26}{len(corpus):>7}{rows_total:>10,}{len(failures):>8}   ({time.perf_counter() - started:.1f} s, binary {exe})")
    print("files that reach each structural code: " + ", ".join(f"{c} {seen[c]}" for c in STRUCTURAL_CODES))
    unreached = [c for c in STRUCTURAL_CODES if not seen[c]]
    if unreached:
        print(f"WARNING: no file in the corpus reaches {', '.join(unreached)}")
    for group, name, diffs in failures[:25]:
        print(f"DIFFERS [{group}] {name}")
        for line in diffs:
            print(f"    {line}")
    if len(failures) > 25:
        print(f"... and {len(failures) - 25} more files that differ")
    if failures:
        print(f"CONFORMANCE FAILED: {len(failures)} of {len(corpus)} files read differently by the Rust tokenizer and the Python parser")
        return 1
    print(f"CONFORMANCE OK: {len(corpus)} files, {rows_total:,} rows, table, field, row and problem output identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
