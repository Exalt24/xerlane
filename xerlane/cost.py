"""Cost and commitment exports: CSV files loaded into PostgreSQL per tenant, with the same safety properties as the XER loader.

Two file kinds, each a header row and one row per line (column order does not matter, extra columns are ignored, names are matched without case):

  cost        line_id, proj_id, cost_code, budget, forecast, actual   and optional   description, task_code, period (YYYY-MM)
  commitment  commitment_id, proj_id, vendor, cost_code, committed, invoiced, status (OPEN, CLOSED or VOID)   and optional   description, issued (YYYY-MM-DD)

The layouts are ours (docs/cost.md says why): no vendor publishes one layout for "a cost export". What a load does: decode and parse the file (an unreadable file, a missing
required column or a repeated column name rejects it and writes nothing), run the row rules and set aside every row that fails one (the quarantine table, with the
rule code, the reason and the raw fields, never dropped), then in ONE transaction bring the stored rows of every project in the file in line with the accepted rows
(insert, update and delete only what differs), write the quarantine and a run row. Loading the same file again changes nothing. A file is a full snapshot of each
project it names: a project whose accepted rows would leave a stored project with none is refused unless allow_empty is set.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .db import tenant_tx

MAX_ID = 2**63 - 1
MAX_TEXT = 2048                      # no-mutate: a tunable limit, the tests are written relative to it
MAX_FILE_BYTES = 256 * 1024 * 1024   # no-mutate: a tunable limit, the test passes its own cap
AMOUNT_RE = re.compile(r"[+-]?[0-9]{1,13}(\.[0-9]{1,4})?")          # no thousands separators, no exponent, no NaN: numeric(18,4) holds 14 digits before the point
ID_RE = re.compile(r"[0-9]{1,19}")
PERIOD_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STATUSES = ("OPEN", "CLOSED", "VOID")
LABEL_PREFIX = "cost:"               # keeps the quarantine rows of a cost file apart from those of an XER file with the same name


@dataclass(frozen=True)
class Kind:
    name: str                        # cost | commitment
    table: str                       # postgres table, also the table_name in the quarantine
    quarantine_name: str             # COST | COMMITMENT
    key: str                         # id column
    required: tuple[str, ...]
    optional: tuple[str, ...]
    amounts: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return self.required + self.optional


COST = Kind("cost", "cost_line", "COST", "line_id", ("line_id", "proj_id", "cost_code", "budget", "forecast", "actual"), ("description", "task_code", "period"), ("budget", "forecast", "actual"))
COMMITMENT = Kind("commitment", "commitment", "COMMITMENT", "commitment_id", ("commitment_id", "proj_id", "vendor", "cost_code", "committed", "invoiced", "status"), ("description", "issued"), ("committed", "invoiced"))
KINDS = {k.name: k for k in (COST, COMMITMENT)}
# column order of the stored rows, after tenant_id; the order of the diff and of the INSERT
STORED = {"cost": ("proj_id", "line_id", "cost_code", "description", "task_code", "period", "budget", "forecast", "actual"),
          "commitment": ("proj_id", "commitment_id", "vendor", "cost_code", "description", "committed", "invoiced", "status", "issued")}


class CostFileError(ValueError):
    """The file cannot be read as this kind of export at all (nothing is written)."""


@dataclass(frozen=True)
class CostRow:
    line: int                        # physical line of the file where the record starts
    raw: tuple[str, ...]
    values: dict[str, Any]           # parsed, only when the row passed the field rules
    code: str | None = None          # reason code when the row failed
    reason: str | None = None


@dataclass
class Parsed:
    kind: Kind
    sha256: str
    rows: list[CostRow]
    header: tuple[str, ...]


@dataclass(frozen=True)
class SetAside:
    line: int
    proj_id: int | None
    code: str
    reason: str
    raw: tuple[str, ...]


@dataclass
class CostLoadResult:
    label: str
    kind: str
    sha256: str
    status: str                      # loaded | unchanged | rejected | failed
    reason: str = ""
    rows_read: int = 0
    stored: int = 0
    projects: dict[int, dict[str, int]] = field(default_factory=dict)       # proj_id -> added / changed / removed
    quarantined: list[SetAside] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        if self.status in ("rejected", "failed"):
            return 2
        return 1 if self.quarantined else 0


def decode(data: bytes) -> str:
    """UTF-8 (with or without a byte order mark), else Windows-1252, which is what Excel writes on Windows. Anything else is refused."""
    if b"\x00" in data:
        raise CostFileError("the file holds NUL bytes; it is not a text export")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise CostFileError(f"the file is neither UTF-8 nor Windows-1252 text ({exc.reason} at byte {exc.start})") from exc


def _amount(text: str) -> Decimal | None:
    s = text.strip()
    if not AMOUNT_RE.fullmatch(s):
        return None
    return Decimal(s)


def _whole(text: str) -> int | None:
    s = text.strip()
    if not ID_RE.fullmatch(s):
        return None
    n = int(s)
    return n if n <= MAX_ID else None


def _real_date(text: str) -> date | None:
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _text_problem(name: str, value: str) -> str | None:
    if len(value) > MAX_TEXT:
        return f"{name} is longer than {MAX_TEXT} characters"
    if any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        return f"{name} holds a control character"
    return None


def check_row(kind: Kind, record: dict[str, str]) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """Field rules for one row: the parsed values, or (code, reason) for the first rule it breaks. No database needed."""
    for name in kind.required:
        if not record[name].strip() and not (kind.name == "cost" and name in kind.amounts):       # a blank amount in a cost export means none, see below
            return None, ("MISSING_VALUE", f"{name} is empty")
    for name in kind.columns:
        problem = _text_problem(name, record.get(name, ""))
        if problem:
            return None, ("BAD_VALUE", problem)
    out: dict[str, Any] = {}
    for name in (kind.key, "proj_id"):
        n = _whole(record[name])
        if n is None:
            return None, ("BAD_VALUE", f"{name} must be a whole number from 0 to {MAX_ID}, got {record[name]!r}")
        out[name] = n
    for name in kind.amounts:
        text = record[name]
        if not text.strip() and kind.name == "cost":
            out[name] = Decimal(0)                      # a blank amount in a cost export means none; the commitment amounts have no such default
            continue
        d = _amount(text)
        if d is None:
            return None, ("BAD_VALUE", f"{name} must be a plain number with at most 4 decimals (no thousands separator, no exponent), got {text!r}")
        out[name] = d
    if kind.name == "commitment":
        for name in ("committed", "invoiced"):
            if out[name] < 0:
                return None, ("OUT_OF_RANGE", f"{name} must not be negative, got {out[name]}")
        status = record["status"].strip().upper()
        if status not in STATUSES:
            return None, ("BAD_VALUE", f"status must be one of {', '.join(STATUSES)}, got {record['status']!r}")
        out["status"] = status
        issued = record.get("issued", "").strip()
        if issued:
            d = _real_date(issued)
            if d is None:
                return None, ("BAD_VALUE", f"issued must be a real date as YYYY-MM-DD, got {issued!r}")
            out["issued"] = d
        else:
            out["issued"] = None
        out["vendor"] = record["vendor"].strip()
    else:
        period = record.get("period", "").strip()
        if period and not PERIOD_RE.fullmatch(period):
            return None, ("BAD_VALUE", f"period must be YYYY-MM, got {period!r}")
        out["period"] = period or None
        out["task_code"] = record.get("task_code", "").strip() or None
    out["cost_code"] = record["cost_code"].strip()
    out["description"] = record.get("description", "").strip() or None
    return out, None


def parse_bytes(kind: Kind, data: bytes) -> Parsed:
    """Decode and read a whole file into rows with their field-rule verdicts. Raises CostFileError when the file cannot be an export of this kind."""
    if len(data) > MAX_FILE_BYTES:
        raise CostFileError(f"the file is larger than {MAX_FILE_BYTES} bytes")
    text = decode(data)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header_row = next(reader)
    except StopIteration:
        raise CostFileError("the file is empty") from None
    except csv.Error as exc:
        raise CostFileError(f"the header line is not valid CSV: {exc}") from exc
    header = tuple(h.strip().lower() for h in header_row)
    if len(set(header)) != len(header):
        dup = sorted({h for h in header if header.count(h) > 1})
        raise CostFileError(f"column name repeated in the header: {', '.join(dup)}")
    missing = [c for c in kind.required if c not in header]
    if missing:
        raise CostFileError(f"{kind.name} export is missing required column(s): {', '.join(missing)}")
    index = {h: i for i, h in enumerate(header)}
    rows: list[CostRow] = []
    last = reader.line_num
    while True:
        try:
            rec = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise CostFileError(f"line {reader.line_num}: not valid CSV: {exc}") from exc
        start, last = last + 1, reader.line_num
        if not rec:
            continue                                      # a blank line between rows
        raw = tuple(rec)
        if len(rec) != len(header):
            rows.append(CostRow(start, raw, {}, "WRONG_FIELD_COUNT", f"{len(rec)} fields where the header has {len(header)}"))
            continue
        record = {name: rec[index[name]] for name in kind.columns if name in index}
        for name in kind.columns:
            record.setdefault(name, "")
        values, problem = check_row(kind, record)
        if problem:
            rows.append(CostRow(start, raw, {}, problem[0], problem[1]))
        else:
            rows.append(CostRow(start, raw, values or {}))
    return Parsed(kind, hashlib.sha256(data).hexdigest(), rows, header)


def _key(kind: Kind, values: dict[str, Any]) -> tuple[int, int]:
    return values["proj_id"], values[kind.key]


def _proj_of(kind: Kind, row: CostRow, header: tuple[str, ...]) -> int | None:
    if row.values:
        return row.values["proj_id"]
    if "proj_id" in header and header.index("proj_id") < len(row.raw):
        return _whole(row.raw[header.index("proj_id")])
    return None


@dataclass
class Plan:
    accepted: list[dict[str, Any]]
    aside: list[SetAside]


def make_plan(parsed: Parsed, projects: dict[int, set[str]]) -> Plan:
    """Apply the rules that need the stored schedule (`projects`: proj_id -> task codes) and the ones that look across rows. A duplicate id sets aside every copy."""
    kind = parsed.kind
    counts: dict[tuple[int, int], int] = {}
    for r in parsed.rows:
        if not r.code:
            counts[_key(kind, r.values)] = counts.get(_key(kind, r.values), 0) + 1
    accepted: list[dict[str, Any]] = []
    aside: list[SetAside] = []
    for r in parsed.rows:
        proj = _proj_of(kind, r, parsed.header)
        if r.code:
            aside.append(SetAside(r.line, proj, r.code, r.reason or "", r.raw))
            continue
        v = r.values
        problem: tuple[str, str] | None = None
        if counts[_key(kind, v)] > 1:
            problem = ("DUP_ID", f"{kind.key} {v[kind.key]} appears more than once for project {v['proj_id']}; no way to tell which line is right")
        elif v["proj_id"] not in projects:
            problem = ("UNKNOWN_PROJECT", f"project {v['proj_id']} is not loaded for this tenant; load its schedule first")
        elif kind.name == "cost" and v["task_code"] is not None and v["task_code"] not in projects[v["proj_id"]]:
            problem = ("UNKNOWN_TASK_CODE", f"task code {v['task_code']!r} is not a task of project {v['proj_id']}")
        elif kind.name == "commitment" and v["invoiced"] > v["committed"]:
            problem = ("OVER_INVOICED", f"invoiced {v['invoiced']} is more than committed {v['committed']}: a change order is missing or an amount was mistyped")
        if problem:
            aside.append(SetAside(r.line, v["proj_id"], problem[0], problem[1], r.raw))
        else:
            accepted.append(v)
    return Plan(accepted, aside)


def _load_projects(cur, tenant: str) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    cur.execute("SELECT proj_id FROM project WHERE tenant_id = %s", (tenant,))
    for (pid,) in cur.fetchall():
        out[pid] = set()
    cur.execute("SELECT proj_id, task_code FROM task WHERE tenant_id = %s", (tenant,))
    for pid, code in cur.fetchall():
        out.setdefault(pid, set()).add(code)
    return out


def _stored(cur, tenant: str, kind: Kind, proj_ids: list[int]) -> dict[tuple[int, int], dict[str, Any]]:
    cols = STORED[kind.name]
    cur.execute(f"SELECT {', '.join(cols)} FROM {kind.table} WHERE tenant_id = %s AND proj_id = ANY(%s)", (tenant, proj_ids))
    have = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        have[_key(kind, d)] = d
    return have


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a[c] == b[c] for c in a)


def _write_diff(cur, tenant: str, kind: Kind, want: dict[tuple[int, int], dict[str, Any]], have: dict[tuple[int, int], dict[str, Any]]) -> dict[int, dict[str, int]]:
    cols = STORED[kind.name]
    per: dict[int, dict[str, int]] = {}

    def bump(pid: int, what: str) -> None:
        per.setdefault(pid, {"added": 0, "changed": 0, "removed": 0})[what] += 1

    marks = ", ".join(["%s"] * (len(cols) + 1))
    insert = f"INSERT INTO {kind.table} (tenant_id, {', '.join(cols)}) VALUES ({marks})"
    sets = ", ".join(f"{c} = %s" for c in cols if c not in ("proj_id", kind.key))
    update = f"UPDATE {kind.table} SET {sets} WHERE tenant_id = %s AND proj_id = %s AND {kind.key} = %s"
    ins, upd, dele = [], [], []
    for k, v in want.items():
        old = have.get(k)
        if old is None:
            ins.append((tenant, *[v.get(c) for c in cols]))
            bump(k[0], "added")
        elif not _same({c: v.get(c) for c in cols}, old):
            upd.append((*[v.get(c) for c in cols if c not in ("proj_id", kind.key)], tenant, k[0], k[1]))
            bump(k[0], "changed")
    for k in have:
        if k not in want:
            dele.append((tenant, k[0], k[1]))
            bump(k[0], "removed")
    if dele:
        cur.executemany(f"DELETE FROM {kind.table} WHERE tenant_id = %s AND proj_id = %s AND {kind.key} = %s", dele)
    if upd:
        cur.executemany(update, upd)
    if ins:
        cur.executemany(insert, ins)
    return per


def _sync_quarantine(cur, tenant: str, kind: Kind, label: str, sha: str, aside: list[SetAside]) -> None:
    cur.execute("DELETE FROM quarantine WHERE tenant_id = %s AND source_label = %s AND table_name = %s", (tenant, LABEL_PREFIX + label, kind.quarantine_name))
    cur.executemany("INSERT INTO quarantine (tenant_id, proj_id, source_label, source_sha256, table_name, line_no, reason_code, reason, raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(tenant, a.proj_id, LABEL_PREFIX + label, sha, kind.quarantine_name, a.line, a.code, a.reason, Jsonb(list(a.raw))) for a in aside])


def load_bytes(conn: psycopg.Connection, tenant: str, kind_name: str, data: bytes, label: str, *, allow_empty: bool = False, started: float | None = None) -> CostLoadResult:
    kind = KINDS[kind_name]
    t0 = time.monotonic() if started is None else started
    sha = hashlib.sha256(data).hexdigest()
    try:
        parsed = parse_bytes(kind, data)
    except CostFileError as exc:
        return CostLoadResult(label, kind.name, sha, "rejected", str(exc), seconds=time.monotonic() - t0)
    result = CostLoadResult(label, kind.name, sha, "unchanged", rows_read=len(parsed.rows))
    try:
        with tenant_tx(conn, tenant) as cur:
            plan = make_plan(parsed, _load_projects(cur, tenant))
            result.quarantined = plan.aside
            want = {_key(kind, v): v for v in plan.accepted}
            proj_ids = sorted({a.proj_id for a in plan.aside if a.proj_id is not None} | {k[0] for k in want})
            have = _stored(cur, tenant, kind, proj_ids)
            for pid in proj_ids:
                lost = [k for k in have if k[0] == pid]
                if lost and not any(k[0] == pid for k in want) and not allow_empty:
                    raise _EmptyProject(f"the file leaves project {pid} with no accepted row while {len(lost)} are stored; nothing was changed (allow_empty overrides)")
            result.projects = _write_diff(cur, tenant, kind, want, have)
            _sync_quarantine(cur, tenant, kind, label, sha, plan.aside)
            result.stored = len(want)
            if result.projects:
                counts = {"rows": len(want), "quarantined": len(plan.aside), **{k: sum(p[k] for p in result.projects.values()) for k in ("added", "changed", "removed")}}
                cur.execute("INSERT INTO cost_load_run (tenant_id, kind, source_label, source_sha256, counts) VALUES (%s,%s,%s,%s,%s)", (tenant, kind.name, label, sha, Jsonb(counts)))
                result.status = "loaded"
    except _EmptyProject as exc:
        result.status, result.reason = "failed", str(exc)
        result.projects = {}
    except psycopg.Error as exc:
        result.status, result.reason = "failed", f"{type(exc).__name__}: {exc}".splitlines()[0]
        result.projects = {}
    result.seconds = time.monotonic() - t0
    return result


class _EmptyProject(RuntimeError):
    pass


def load_file(conn: psycopg.Connection, tenant: str, kind_name: str, path: str, label: str | None = None, *, allow_empty: bool = False) -> CostLoadResult:
    started = time.monotonic()
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return load_bytes(conn, tenant, kind_name, data, label or p.name, allow_empty=allow_empty, started=started)


# ---- reconcile ------------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


@dataclass
class CostReconcile:
    label: str
    kind: str
    file_rows: int = 0
    stored: int = 0
    quarantined: int = 0
    sums: dict[str, Decimal] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _file_dec(text: str) -> Decimal | None:
    """An amount as the reconcile reads it: blank is zero, anything that is not a finite number is None. It does not share the loader's pattern on purpose."""
    s = text.strip()
    if not s:
        return Decimal(0)
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _own_reader(kind: Kind, data: bytes) -> tuple[list[tuple[int, dict[str, str]]], int]:
    """A second, deliberately plain reader: the csv module and nothing else from this file. Returns (line number, record by column) for every row with the right field count, and the row count."""
    reader = csv.reader(io.StringIO(decode(data), newline=""))
    header = [h.strip().lower() for h in next(reader)]
    missing = [c for c in kind.required if c not in header]
    if missing:
        raise CostFileError(f"missing required column(s): {', '.join(missing)}")
    out, total, last = [], 0, reader.line_num
    for rec in reader:
        start, last = last + 1, reader.line_num
        if not rec:
            continue
        total += 1
        if len(rec) == len(header):
            out.append((start, dict(zip(header, rec))))
    return out, total


def reconcile_bytes(conn: psycopg.Connection, tenant: str, kind_name: str, data: bytes, label: str) -> CostReconcile:
    """Read the file again with a second reader and the database with its own queries, and name every difference. Changes nothing.

    Checks: every row of the file is stored or set aside (counts); every stored row equals its line of the file field by field; the amounts of the stored rows add up
    to the amounts of the file rows that are not set aside; no row of a project named in the file is stored that the file does not hold."""
    kind = KINDS[kind_name]
    rep = CostReconcile(label, kind.name)
    try:
        lines, total = _own_reader(kind, data)
    except (CostFileError, StopIteration, csv.Error) as exc:
        rep.findings.append(Finding("FILE_UNREADABLE", str(exc) or "empty file"))
        return rep
    rep.file_rows = total
    with tenant_tx(conn, tenant) as cur:
        cur.execute("SELECT line_no FROM quarantine WHERE tenant_id = %s AND source_label = %s AND table_name = %s", (tenant, LABEL_PREFIX + label, kind.quarantine_name))
        aside_lines = {r[0] for r in cur.fetchall()}
        rep.quarantined = len(aside_lines)
        by_key: dict[tuple[int, int], tuple[int, dict[str, str]]] = {}
        file_projects: set[int] = set()
        for ln, rec in lines:
            pid, rid = _whole(rec.get("proj_id", "")), _whole(rec.get(kind.key, ""))
            if pid is not None:
                file_projects.add(pid)
            if ln in aside_lines or pid is None or rid is None:
                continue
            by_key[(pid, rid)] = (ln, rec)
        stored = _stored(cur, tenant, kind, sorted(file_projects)) if file_projects else {}
    rep.stored = len(stored)
    if total != rep.stored + rep.quarantined:
        rep.findings.append(Finding("COUNT", f"file has {total} rows, {rep.stored} are stored and {rep.quarantined} set aside"))
    for k, row in stored.items():
        if k not in by_key:
            rep.findings.append(Finding("EXTRA_STORED", f"{kind.key} {k[1]} of project {k[0]} is stored but its line is not in the file or was set aside"))
    for k, (ln, rec) in by_key.items():
        row = stored.get(k)
        if row is None:
            rep.findings.append(Finding("MISSING_STORED", f"line {ln}: {kind.key} {k[1]} of project {k[0]} is in the file, not set aside, and not stored"))
            continue
        for name in kind.amounts:
            raw = rec[name].strip()
            want = Decimal(raw) if raw else Decimal(0)
            if row[name] != want:
                rep.findings.append(Finding("FIELD", f"line {ln} {name}: file {want}, stored {row[name]}"))
        for name in ("cost_code", "vendor"):
            if name in row and row[name] != rec.get(name, "").strip():
                rep.findings.append(Finding("FIELD", f"line {ln} {name}: file {rec.get(name, '').strip()!r}, stored {row[name]!r}"))
        if "status" in row and row["status"] != rec.get("status", "").strip().upper():
            rep.findings.append(Finding("FIELD", f"line {ln} status: file {rec.get('status', '').strip()!r}, stored {row['status']!r}"))
        for name in ("task_code", "description", "period"):
            if name in row and (row[name] or "") != rec.get(name, "").strip():
                rep.findings.append(Finding("FIELD", f"line {ln} {name}: file {rec.get(name, '').strip()!r}, stored {row[name]!r}"))
        if kind.name == "commitment" and (str(row["issued"]) if row["issued"] else "") != rec.get("issued", "").strip():
            rep.findings.append(Finding("FIELD", f"line {ln} issued: file {rec.get('issued', '').strip()!r}, stored {row['issued']}"))
    for name in kind.amounts:
        file_sum = sum((d for d in (_file_dec(rec[name]) for _, rec in by_key.values()) if d is not None), Decimal(0))
        db_sum = sum((r[name] for k, r in stored.items() if k in by_key), Decimal(0))
        rep.sums[name] = db_sum
        if file_sum != db_sum:
            rep.findings.append(Finding("SUM", f"{name}: file rows add up to {file_sum}, stored rows to {db_sum}"))
    return rep


def reconcile_file(conn: psycopg.Connection, tenant: str, kind_name: str, path: str, label: str | None = None) -> CostReconcile:
    p = Path(path)
    return reconcile_bytes(conn, tenant, kind_name, p.read_bytes(), label or p.name)
