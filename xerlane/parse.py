"""Streaming XER parser.

Reads a file line by line. Returns typed rows for the tables xerlane models and raw rows for the rest. Nothing is dropped without a trace: every line that is not
understood becomes a named, line-numbered Problem on the result; a row that cannot be typed keeps its raw text and carries its own problems so the loader can quarantine it.

The layout (ERMHDR line, %T table, %F fields, %R row, %E end, tab delimited, cp1252, CRLF) is taken from third-party sources, see docs/vendor-contract/SOURCE.md.
What those sources do not settle is listed in docs/NOT_CONFIRMED.md; each cautious choice below names its item.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, BinaryIO, Iterable, Iterator

from .schema import BY_NAME, INT, MAX_ID, NUM, TS, Table

ENCODING = "cp1252"
INT_RE = re.compile(r"-?[0-9]{1,19}")
NUM_RE = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,30})?")
TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?: ([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?)?")

FATAL_CODES = frozenset({"EMPTY_FILE", "NOT_XER", "MISSING_END"})


@dataclass(frozen=True)
class Problem:
    code: str
    severity: str                       # error | warning | info
    message: str
    line: int | None = None
    table: str | None = None

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line is not None else "file"
        return f"{self.code} ({where}{', ' + self.table if self.table else ''}): {self.message}"


@dataclass
class Row:
    table: str
    line: int
    raw: tuple[str, ...]                          # the fields exactly as written, marker removed
    values: dict[str, Any] | None = None          # typed values of the modeled columns; None for an unmodeled table
    extra: dict[str, str] = field(default_factory=dict)      # non-empty fields the schema does not model
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Block:
    name: str
    line: int
    modeled: bool
    fields: list[str] | None = None
    fields_line: int | None = None
    rows: list[Row] = field(default_factory=list)
    duplicate: bool = False


@dataclass
class ParseResult:
    path: str
    sha256: str
    size: int
    line_count: int = 0
    header: list[str] | None = None
    blocks: list[Block] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    ended: bool = False

    @property
    def fatal(self) -> bool:
        return any(p.code in FATAL_CODES for p in self.problems)

    def block(self, name: str) -> Block | None:
        for b in self.blocks:
            if b.name == name and not b.duplicate:
                return b
        return None

    def all_rows(self) -> Iterator[Row]:
        for b in self.blocks:
            yield from b.rows

    def row_count(self, name: str) -> int:
        return sum(len(b.rows) for b in self.blocks if b.name == name)

    @property
    def header_info(self) -> dict[str, str]:
        """Only the four header fields that a loaded source names (docs/NOT_CONFIRMED.md item 8)."""
        h = self.header or []
        pick = lambda i: h[i] if i < len(h) else ""          # noqa: E731
        return {"version": pick(0), "export_date": pick(1), "user": pick(4), "currency": pick(7)}


def convert(kind: str, text: str) -> tuple[Any, str | None]:
    """Typed value of one field, or (None, reason) when the text does not fit the kind. An empty field is NULL."""
    if text == "":
        return None, None
    if kind == INT:
        if INT_RE.fullmatch(text) is None:
            return None, "not a whole number"
        n = int(text)
        if abs(n) > MAX_ID:
            return None, "whole number too large"
        return n, None
    if kind == NUM:
        if NUM_RE.fullmatch(text) is None:
            return None, "not a plain number (digits, optional minus, optional dot)"
        return Decimal(text), None
    if kind == TS:
        m = TS_RE.fullmatch(text)
        if m is None:
            return None, "not a date of the form YYYY-MM-DD HH:MM"
        y, mo, d, h, mi, s = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0)), None
        except ValueError:
            return None, "not a real calendar date or time"
    return text, None


def _type_row(table: Table, fields: list[str], raw: tuple[str, ...], row: Row) -> None:
    index = {name: i for i, name in enumerate(fields)}
    values: dict[str, Any] = {}
    for col in table.cols:
        i = index.get(col.name)
        text = raw[i] if i is not None and i < len(raw) else ""
        value, why = convert(col.kind, text)
        if why:
            row.problems.append(Problem("BAD_VALUE", "error", f"{col.name}={text!r}: {why}", row.line, table.name))
        elif value is None and col.required:
            row.problems.append(Problem("MISSING_VALUE", "error", f"{col.name} is empty or has no column", row.line, table.name))
        values[col.name] = value
    known = set(table.col_names)
    row.extra = {name: raw[i] for name, i in index.items() if name not in known and i < len(raw) and raw[i] != ""}
    row.values = values


def _decode(raw: bytes, line_no: int, problems: list[Problem]) -> str:
    try:
        return raw.decode(ENCODING)
    except UnicodeDecodeError as exc:
        problems.append(Problem("BAD_ENCODING", "error", f"byte 0x{raw[exc.start]:02X} at offset {exc.start} is not defined in {ENCODING}", line_no))
        return raw.decode(ENCODING, errors="replace")


class _Parser:
    def __init__(self, result: ParseResult) -> None:
        self.r = result
        self.block: Block | None = None
        self.names: set[str] = set()

    def problem(self, code: str, message: str, line: int | None, severity: str = "error", table: str | None = None) -> None:
        self.r.problems.append(Problem(code, severity, message, line, table))

    def feed(self, line_no: int, raw: bytes) -> bool:
        """Handle one physical line. Returns False when the parse must stop (fatal first line)."""
        self.r.line_count = line_no
        enc_problems: list[Problem] = []
        text = _decode(raw, line_no, enc_problems)
        if not (text.startswith("%R\t") and self.block is not None and not self.r.ended):
            self.r.problems.extend(enc_problems)            # a row keeps its own encoding problems; every other line reports them on the file
        if line_no == 1:
            if not (text == "ERMHDR" or text.startswith("ERMHDR\t")):
                self.problem("NOT_XER", f"first line must start with ERMHDR, found {text[:30]!r}" + (" (a byte order mark?)" if text.startswith("ï»¿") else ""), 1)
                return False                                       # no-mutate: None is falsy too, the caller only tests `not`
            self.r.header = text.split("\t")[1:]
            return True
        if self.r.ended:
            if text.strip():
                self.problem("TRAILING_DATA", f"data after the %E end marker: {text[:40]!r}", line_no)
            else:
                self.problem("BLANK_LINE", "blank line after the end marker", line_no, "info")
            return True
        parts = text.split("\t")
        marker = parts[0]
        if marker == "%T":
            self.table_line(line_no, parts)
        elif marker == "%F":
            self.fields_line(line_no, parts)
        elif marker == "%R":
            self.row_line(line_no, parts, enc_problems)
        elif marker == "%E":
            self.r.ended = True
            if len(parts) > 1 and any(parts[1:]):
                self.problem("END_LINE_FORMAT", "text after the %E marker on the same line", line_no, "warning")
        elif marker == "ERMHDR":
            self.problem("DUPLICATE_HEADER", "a second ERMHDR line", line_no)
        elif text == "":
            self.problem("BLANK_LINE", "blank line", line_no, "warning", self.block.name if self.block else None)
        elif marker.startswith("%"):
            self.problem("UNKNOWN_MARKER", f"unknown record marker {marker[:10]!r}", line_no, table=self.block.name if self.block else None)
        else:
            self.problem("STRAY_LINE", f"line does not start with a record marker: {text[:40]!r}", line_no, table=self.block.name if self.block else None)
        return True

    def table_line(self, line_no: int, parts: list[str]) -> None:
        name = parts[1] if len(parts) > 1 else ""
        if len(parts) != 2 or not name:
            self.problem("TABLE_LINE_FORMAT", "a %T line needs exactly one table name", line_no)
        block = Block(name, line_no, modeled=name in BY_NAME)
        if name in self.names:
            block.duplicate = True
            self.problem("DUPLICATE_TABLE", f"table {name} appears again; its rows are quarantined", line_no, table=name)
        self.names.add(name)
        self.r.blocks.append(block)
        self.block = block

    def fields_line(self, line_no: int, parts: list[str]) -> None:
        b = self.block
        if b is None:
            self.problem("FIELDS_OUTSIDE_TABLE", "%F line before any %T line", line_no)
            return
        if b.fields is not None:
            self.problem("DUPLICATE_FIELD_LIST", f"second %F line for table {b.name}", line_no, table=b.name)
            return
        names = parts[1:]
        if len(set(names)) != len(names):
            dup = sorted({n for n in names if names.count(n) > 1})
            self.problem("DUPLICATE_FIELD", f"field names repeated: {', '.join(dup)}", line_no, table=b.name)
        b.fields, b.fields_line = names, line_no
        if b.modeled:
            missing = [c.name for c in BY_NAME[b.name].cols if c.required and c.name not in names]
            if missing:
                self.problem("MISSING_COLUMN", f"required columns not in the field list: {', '.join(missing)}", line_no, table=b.name)

    def row_line(self, line_no: int, parts: list[str], enc_problems: list[Problem]) -> None:
        b = self.block
        if b is None:
            self.problem("ROW_OUTSIDE_TABLE", "%R line before any %T line", line_no)
            return
        raw = tuple(parts[1:])
        row = Row(b.name, line_no, raw)
        row.problems.extend(enc_problems)
        if b.duplicate:
            row.problems.append(Problem("DUPLICATE_TABLE", "error", f"row of a repeated {b.name} block", line_no, b.name))
        if b.fields is None:
            row.problems.append(Problem("NO_FIELD_LIST", "error", f"row before any %F line in table {b.name}", line_no, b.name))
        else:
            if len(raw) != len(b.fields):
                row.problems.append(Problem("WRONG_FIELD_COUNT", "error", f"{len(raw)} fields, the %F line has {len(b.fields)}", line_no, b.name))
            if b.modeled:
                _type_row(BY_NAME[b.name], b.fields, raw, row)
        b.rows.append(row)

    def finish(self) -> None:
        if self.r.line_count == 0:
            self.problem("EMPTY_FILE", "the file has no lines", None)
            return
        if not self.r.ended:
            self.problem("MISSING_END", "no %E end marker: the file is truncated or incomplete", self.r.line_count)
        for b in self.r.blocks:
            if b.fields is None and not b.rows:
                self.problem("EMPTY_TABLE_BLOCK", f"table {b.name} has no %F line", b.line, "warning", b.name)
            if not b.modeled:
                self.problem("UNMODELED_TABLE", f"table {b.name} is not modeled; {len(b.rows)} rows read and not loaded", b.line, "info", b.name)


def parse_stream(fh: BinaryIO, path: str = "<stream>") -> ParseResult:
    """Parse a binary stream line by line; the SHA-256 and size are taken over the same bytes."""
    digest = hashlib.sha256()
    result = ParseResult(path, "", 0)
    parser = _Parser(result)
    stop = False
    for line_no, raw in enumerate(fh, start=1):
        digest.update(raw)
        result.size += len(raw)
        if stop:
            continue
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if not parser.feed(line_no, raw):
            stop = True
    result.sha256 = digest.hexdigest()
    if not stop:
        parser.finish()
    return result


def parse_file(path: str) -> ParseResult:
    with open(path, "rb") as fh:
        return parse_stream(fh, str(path))


def parse_bytes(data: bytes, path: str = "<bytes>") -> ParseResult:
    import io
    return parse_stream(io.BytesIO(data), path)


def parse_lines(lines: Iterable[str], path: str = "<lines>") -> ParseResult:
    return parse_bytes(("\r\n".join(lines) + "\r\n").encode(ENCODING), path)
