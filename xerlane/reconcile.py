"""Reconcile a stored schedule against the XER file it came from.

Independent of the loader on purpose: this module has its own small reader of the file (it does not import parse.py or load.py) and its own value comparison, so a
mistake in the loader's parsing or typing cannot also hide in the check. It only reads. Per modeled table it proves:

  * counts:   rows in the file == rows stored for the file's projects + rows quarantined for this exact file (matched by SHA-256);
  * sums:     every numeric column adds up to the same total (stored rows + the numeric values still held in quarantined rows);
  * rows:     a seeded sample of rows matches the stored row field by field, extra columns included;
  * quarantine: every quarantined row is still exactly the text of that line of the file.

Any difference is a named Finding. The CLI exits nonzero when there is one.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from .db import tenant_tx
from .schema import BY_NAME, INT, NUM, TABLES, TS, Table

_INT = re.compile(r"-?[0-9]+")
_NUM = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?")


@dataclass(frozen=True)
class ReconFinding:
    check: str                      # COUNT | SUM | ROW | QUARANTINE | FILE
    table: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.check} {self.table or 'file'}: {self.message}"


@dataclass
class TableLine:
    table: str
    file_rows: int
    stored: int
    quarantined: int
    sampled: int
    sums_checked: int


@dataclass
class ReconResult:
    path: str
    sha256: str
    findings: list[ReconFinding] = field(default_factory=list)
    lines: list[TableLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass
class _FileTable:
    fields: list[str] = field(default_factory=list)
    rows: list[tuple[int, list[str]]] = field(default_factory=list)       # (line number, fields without the marker)


def _read_file(path: str) -> tuple[str, dict[str, _FileTable], bool, bool]:
    """sha256, tables by name, whether the first line is ERMHDR, whether a %E line was seen. Rows of a repeated table are appended to the first block."""
    with open(path, "rb") as fh:
        data = fh.read()
    sha = hashlib.sha256(data).hexdigest()
    lines = data.split(b"\n")                                   # a final newline leaves one empty last element; an empty line is ignored below
    tables: dict[str, _FileTable] = {}
    cur: _FileTable | None = None
    has_header = has_end = False
    for n, raw in enumerate(lines, start=1):
        text = raw.rstrip(b"\r").decode("cp1252", errors="replace")
        parts = text.split("\t")
        if n == 1:
            has_header = parts[0] == "ERMHDR"
            continue
        if has_end:
            continue
        if parts[0] == "%T" and len(parts) > 1:
            cur = tables.setdefault(parts[1], _FileTable())
        elif parts[0] == "%F" and cur is not None and not cur.fields:
            cur.fields = parts[1:]
        elif parts[0] == "%R" and cur is not None:
            cur.rows.append((n, parts[1:]))
        elif parts[0] == "%E":
            has_end = True
    return sha, tables, has_header, has_end


def _field(ft: _FileTable, row: list[str], name: str) -> str | None:
    try:
        i = ft.fields.index(name)
    except ValueError:
        return None
    return row[i] if i < len(row) else None


def _as_int(text: str | None) -> int | None:
    return int(text) if text is not None and _INT.fullmatch(text) else None


def _as_num(text: str | None) -> Decimal | None:
    return Decimal(text) if text is not None and _NUM.fullmatch(text) else None


def _as_ts(text: str | None) -> datetime | None:
    if text is None or _TS.fullmatch(text) is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _show(value: Any) -> str:
    """A stored value as text: a number or a date as itself, anything else quoted."""
    return str(value) if isinstance(value, (Decimal, datetime, int)) else repr(value)


def _same(kind: str, text: str | None, stored: Any) -> bool:
    """Does the stored value say what the file text says?"""
    if text is None or text == "":
        return stored is None
    if stored is None:
        return False
    if kind == INT:
        return _as_int(text) == stored
    if kind == NUM:
        try:
            return _as_num(text) == Decimal(stored)
        except InvalidOperation:
            return False
    if kind == TS:
        return _as_ts(text) == stored
    return text == stored


def reconcile(conn: psycopg.Connection, tenant: str, path: str, sample: int = 300, seed: int = 0) -> ReconResult:
    sha, ftables, has_header, has_end = _read_file(path)
    res = ReconResult(path, sha)
    if not has_header:
        res.findings.append(ReconFinding("FILE", None, "the file does not start with ERMHDR, so it cannot have been loaded"))
        return res
    if not has_end:
        res.findings.append(ReconFinding("FILE", None, "the file has no %E end marker, a loader must have refused it"))
    for name, ft in ftables.items():
        if name not in BY_NAME:
            res.notes.append(f"{name}: {len(ft.rows)} rows in the file, table not modeled, not loaded")
    rng = random.Random(seed)
    with tenant_tx(conn, tenant) as cur:
        pf = ftables.get("PROJECT")
        proj_ids = sorted({p for _, r in (pf.rows if pf else []) if (p := _as_int(_field(pf, r, "proj_id"))) is not None})
        cur.execute("SELECT table_name, line_no, raw FROM quarantine WHERE tenant_id = %s AND source_sha256 = %s", (tenant, sha))
        quarantine: dict[tuple[str, int], list[str]] = {(r[0], r[1]): list(r[2]) for r in cur.fetchall()}
        for t in TABLES:
            ft = ftables.get(t.name, _FileTable())
            by_line = {ln: row for ln, row in ft.rows}
            q_lines = {ln for (tb, ln) in quarantine if tb == t.name}
            _check_quarantine(res, t, ft, by_line, quarantine)
            keys = [k for _, r in ft.rows if (k := _as_int(_field(ft, r, t.key))) is not None]
            stored_where, args = _scope(t, tenant, proj_ids, keys)
            cur.execute(f"SELECT count(*) FROM {t.pg} WHERE {stored_where}", args)
            stored = cur.fetchone()[0]
            n_file, n_q = len(ft.rows), len(q_lines & set(by_line))
            if n_file != stored + n_q:
                res.findings.append(ReconFinding("COUNT", t.name, f"file has {n_file} rows, database holds {stored} and quarantine holds {n_q} for this file (sum {stored + n_q})"))
            checked = _check_sums(res, cur, t, ft, quarantine, stored_where, args)
            sampled = _check_rows(res, cur, t, ft, q_lines, tenant, rng, sample)
            res.lines.append(TableLine(t.name, n_file, stored, n_q, sampled, checked))
    return res


def _scope(t: Table, tenant: str, proj_ids: list[int], keys: list[int]) -> tuple[str, list[Any]]:
    if t.scope == "project":
        return "tenant_id = %s AND proj_id = ANY(%s)", [tenant, proj_ids]
    return f"tenant_id = %s AND {t.key} = ANY(%s)", [tenant, keys]


def _check_quarantine(res: ReconResult, t: Table, ft: _FileTable, by_line: dict[int, list[str]], quarantine: dict[tuple[str, int], list[str]]) -> None:
    for (tb, ln), raw in quarantine.items():
        if tb != t.name:
            continue
        if ln not in by_line:
            res.findings.append(ReconFinding("QUARANTINE", t.name, f"quarantine row for line {ln} but that line is not a row of the file"))
        elif by_line[ln] != raw:
            res.findings.append(ReconFinding("QUARANTINE", t.name, f"quarantined row for line {ln} no longer matches the file text"))


def _check_sums(res: ReconResult, cur, t: Table, ft: _FileTable, quarantine: dict[tuple[str, int], list[str]], where: str, args: list[Any]) -> int:
    cols = [c.name for c in t.cols if c.kind == NUM]
    if not cols:
        return 0
    cur.execute(f"SELECT {', '.join(f'COALESCE(SUM({c}), 0)' for c in cols)} FROM {t.pg} WHERE {where}", args)
    stored = dict(zip(cols, cur.fetchone()))
    held = {c: Decimal(0) for c in cols}                                                  # numeric values still in quarantined rows
    total = {c: Decimal(0) for c in cols}
    quarantined_lines = {ln for (tb, ln) in quarantine if tb == t.name}
    for ln, row in ft.rows:
        for c in cols:
            v = _as_num(_field(ft, row, c))
            if v is not None:
                total[c] += v
                if ln in quarantined_lines:
                    held[c] += v
    # A second file row with the same key as a stored one is quarantined (counted in `held`); a stored row appears once in the file. For a global table
    # only rows whose key is in the file were summed from the database, so the two sides cover the same keys.
    for c in cols:
        if total[c] != Decimal(stored[c]) + held[c]:
            res.findings.append(ReconFinding("SUM", t.name, f"{c}: file adds up to {total[c]}, database {stored[c]} + quarantine {held[c]} = {Decimal(stored[c]) + held[c]}"))
    return len(cols)


def _check_rows(res: ReconResult, cur, t: Table, ft: _FileTable, q_lines: set[int], tenant: str, rng: random.Random, sample: int) -> int:
    candidates = [(ln, row) for ln, row in ft.rows if ln not in q_lines and _as_int(_field(ft, row, t.key)) is not None]
    seen: dict[int, int] = {}
    for ln, row in candidates:                                       # a key repeated in the file keeps its first line: the loader keeps the first
        seen.setdefault(_as_int(_field(ft, row, t.key)), ln)
    candidates = [(ln, row) for ln, row in candidates if seen[_as_int(_field(ft, row, t.key))] == ln]
    picked = candidates if len(candidates) <= sample else rng.sample(candidates, sample)
    cols = list(t.col_names) + ["extra"]
    keys = [_as_int(_field(ft, row, t.key)) for _, row in picked]
    cur.execute(f"SELECT {', '.join(cols)} FROM {t.pg} WHERE tenant_id = %s AND {t.key} = ANY(%s)", (tenant, keys))
    stored = {rec[cols.index(t.key)]: dict(zip(cols, rec)) for rec in cur.fetchall()}
    known = set(t.col_names)
    for ln, row in picked:
        key = _as_int(_field(ft, row, t.key))
        s = stored.get(key)
        if s is None:
            res.findings.append(ReconFinding("ROW", t.name, f"line {ln}: {t.key} {key} is in the file and in neither the table nor the quarantine"))
            continue
        for c in t.cols:
            if not _same(c.kind, _field(ft, row, c.name), s[c.name]):
                res.findings.append(ReconFinding("ROW", t.name, f"line {ln}: {t.key} {key} column {c.name}: file {_field(ft, row, c.name)!r}, database {_show(s[c.name])}"))
        extra_file = {n: v for n, v in zip(ft.fields, row) if n not in known and v != ""}
        if extra_file != s["extra"]:
            res.findings.append(ReconFinding("ROW", t.name, f"line {ln}: {t.key} {key} unmodeled columns differ: file {extra_file!r}, database {s['extra']!r}"))
    return len(picked)
