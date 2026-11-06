"""Load a parsed XER file into Postgres, one transaction per project.

What a load does, in order: parse the file; stop if it is truncated or not an XER at all (nothing is written); run the validation rules and set aside every row that
fails them (the quarantine, with the reason, never dropped); then for each project in the file open ONE transaction, set the tenant, bring the stored project in line with
the file (insert, update, delete only what differs), write the quarantine and the change report, run the validation rules again over what is stored, and commit. Any
error before the commit rolls the whole project back. Loading the same file again changes nothing.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .db import tenant_tx
from .parse import FATAL_CODES, ParseResult, Row, parse_file
from .schema import BY_NAME, GLOBAL_TABLES, PROJECT_TABLES, TABLES, Table
from .validate import Analysis, Finding, analyze, problem_findings, validate_db, world_from_parse

FLOAT_FIELDS = ("total_float_hr_cnt", "free_float_hr_cnt")
FINISH_FIELDS = ("early_end_date", "target_end_date")


class PostLoadError(RuntimeError):
    """The stored data failed validation right after the writes; the project transaction is rolled back."""


@dataclass(frozen=True)
class Change:
    kind: str                  # TASK_ADDED | TASK_REMOVED | FINISH_MOVED | FLOAT_CHANGED
    task_id: int
    task_code: str | None
    field: str | None = None
    old: str | None = None
    new: str | None = None

    def __str__(self) -> str:
        if self.field:
            return f"{self.kind} {self.task_code} (task {self.task_id}) {self.field}: {self.old} -> {self.new}"
        return f"{self.kind} {self.task_code} (task {self.task_id})"


@dataclass(frozen=True)
class QRow:
    table: str
    line: int
    proj_id: int | None
    code: str
    reason: str
    raw: tuple[str, ...]


@dataclass
class Plan:
    """What a parsed file turns into: rows to store, rows to set aside."""
    accepted: dict[str, list[Row]]
    quarantined: list[QRow]
    analysis: Analysis
    findings: list[Finding]


@dataclass
class ProjectResult:
    proj_id: int
    short_name: str
    status: str                               # loaded | updated | unchanged | failed
    counts: dict[str, int] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    quarantined: int = 0
    error: str | None = None


@dataclass
class LoadResult:
    path: str
    label: str
    sha256: str
    status: str                               # loaded | unchanged | partial | failed | rejected
    reason: str = ""
    projects: list[ProjectResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    quarantined: list[QRow] = field(default_factory=list)
    rows_read: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        if self.status in ("rejected", "failed", "partial"):
            return 2
        return 1 if self.quarantined or any(f.severity == "error" for f in self.findings) else 0


def make_plan(pr: ParseResult) -> Plan:
    findings, analysis = _validate(pr)
    accepted: dict[str, list[Row]] = {t.name: [] for t in TABLES}
    quarantined: list[QRow] = []
    for block in pr.blocks:
        if not block.modeled:
            continue
        for row in block.rows:
            reason = None
            if row.problems:
                reason = (row.problems[0].code, "; ".join(p.message for p in row.problems))
            elif (row.table, row.line) in analysis.rejected:
                reason = analysis.rejected[(row.table, row.line)]
            if reason is None:
                accepted[row.table].append(row)
            else:
                proj = row.values.get("proj_id") if row.values else None
                quarantined.append(QRow(row.table, row.line, proj, reason[0], reason[1], row.raw))
    quarantined.sort(key=lambda q: q.line)          # no-mutate: blocks are read in file order, so the list is already sorted; the sort makes that a guarantee instead of an accident
    return Plan(accepted, quarantined, analysis, findings)


def _validate(pr: ParseResult):
    analysis = analyze(world_from_parse(pr))
    return problem_findings(pr) + analysis.findings, analysis


def _dbrow(row: Row) -> dict[str, Any]:
    d = dict(row.values or {})
    d["extra"] = dict(row.extra)
    return d


def desired_state(plan: Plan, proj_id: int) -> dict[str, dict[Any, dict[str, Any]]]:
    out: dict[str, dict[Any, dict[str, Any]]] = {}
    for t in TABLES:
        rows = plan.accepted[t.name]
        if t.scope == "project":
            rows = [r for r in rows if r.values["proj_id"] == proj_id]
        out[t.name] = {r.values[t.key]: _dbrow(r) for r in rows}
    return out


def _read_existing(cur, tenant: str, t: Table, proj_id: int, keys: list[Any]) -> dict[Any, dict[str, Any]]:
    cols = list(t.col_names) + ["extra"]
    if t.scope == "project":
        cur.execute(f"SELECT {', '.join(cols)} FROM {t.pg} WHERE tenant_id = %s AND proj_id = %s", (tenant, proj_id))
    else:
        cur.execute(f"SELECT {', '.join(cols)} FROM {t.pg} WHERE tenant_id = %s AND {t.key} = ANY(%s)", (tenant, keys))
    return {rec[cols.index(t.key)]: dict(zip(cols, rec)) for rec in cur.fetchall()}


@dataclass
class TableDiff:
    insert: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    delete: list[Any] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.insert or self.update or self.delete)


def diff_tables(t: Table, want: dict[Any, dict[str, Any]], have: dict[Any, dict[str, Any]]) -> TableDiff:
    d = TableDiff()
    for k, row in want.items():
        old = have.get(k)
        if old is None:
            d.insert.append(row)
        elif old != row:
            d.update.append(row)
    if t.scope == "project":
        d.delete = [k for k in have if k not in want]
    return d


def _fmt(x: Any) -> str | None:
    if x is None:
        return None
    return f"{x:%Y-%m-%d %H:%M}" if hasattr(x, "strftime") else str(x)


def task_changes(want: dict[Any, dict[str, Any]], have: dict[Any, dict[str, Any]]) -> list[Change]:
    """The change report between two states of one project's tasks."""
    out: list[Change] = []
    for tid in sorted(set(want) | set(have)):
        new, old = want.get(tid), have.get(tid)
        if old is None:
            out.append(Change("TASK_ADDED", tid, new["task_code"]))
        elif new is None:
            out.append(Change("TASK_REMOVED", tid, old["task_code"]))
        else:
            for f in FINISH_FIELDS:
                if new[f] != old[f]:
                    out.append(Change("FINISH_MOVED", tid, new["task_code"], f, _fmt(old[f]), _fmt(new[f])))
            for f in FLOAT_FIELDS:
                if new[f] != old[f]:
                    out.append(Change("FLOAT_CHANGED", tid, new["task_code"], f, _fmt(old[f]), _fmt(new[f])))
    return out


def _write(cur, tenant: str, t: Table, d: TableDiff) -> None:
    cols = list(t.col_names)
    if d.delete:
        cur.execute(f"DELETE FROM {t.pg} WHERE tenant_id = %s AND {t.key} = ANY(%s)", (tenant, d.delete))
    if d.update:
        sets = ", ".join(f"{c} = %s" for c in cols if c != t.key) + ", extra = %s"
        cur.executemany(f"UPDATE {t.pg} SET {sets} WHERE tenant_id = %s AND {t.key} = %s",
                        [[r[c] for c in cols if c != t.key] + [Jsonb(r["extra"]), tenant, r[t.key]] for r in d.update])
    if d.insert:
        marks = ", ".join(["%s"] * (len(cols) + 2))
        cur.executemany(f"INSERT INTO {t.pg} (tenant_id, {', '.join(cols)}, extra) VALUES ({marks})",
                        [[tenant] + [r[c] for c in cols] + [Jsonb(r["extra"])] for r in d.insert])


def _sync_quarantine(cur, tenant: str, proj_id: int, label: str, sha: str, rows: list[QRow]) -> bool:
    """Make the stored quarantine for this project and file label equal to `rows`. Returns True when the set of quarantined rows (ignoring the file hash) changed."""
    scope = "tenant_id = %s AND (proj_id = %s OR (proj_id IS NULL AND source_label = %s))"
    cur.execute(f"SELECT table_name, line_no, reason_code, reason, raw, source_sha256, source_label FROM quarantine WHERE {scope}", (tenant, proj_id, label))
    stored = cur.fetchall()
    have = sorted((r[0], r[1], r[2], r[3], tuple(r[4])) for r in stored)
    want = sorted((q.table, q.line, q.code, q.reason, tuple(q.raw)) for q in rows)
    if have == want and all(r[5] == sha and r[6] == label for r in stored):
        return False                                                         # no-mutate: None is falsy too, the caller only tests the truth of it
    cur.execute(f"DELETE FROM quarantine WHERE {scope}", (tenant, proj_id, label))
    cur.executemany("INSERT INTO quarantine (tenant_id, proj_id, source_label, source_sha256, table_name, line_no, reason_code, reason, raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(tenant, q.proj_id, label, sha, q.table, q.line, q.code, q.reason, Jsonb(list(q.raw))) for q in rows])
    return have != want


def load_project(conn: psycopg.Connection, tenant: str, plan: Plan, proj_id: int, short_name: str, label: str, sha: str) -> ProjectResult:
    want = desired_state(plan, proj_id)
    loaded = set(_project_ids(plan))
    mine = [q for q in plan.quarantined if q.proj_id == proj_id or q.proj_id not in loaded]       # rows of another loaded project belong to that project's transaction
    res = ProjectResult(proj_id, short_name, "failed")
    with tenant_tx(conn, tenant) as cur:
        have = {t.name: _read_existing(cur, tenant, t, proj_id, list(want[t.name])) for t in TABLES}
        existed = proj_id in have["PROJECT"]
        diffs = {t.name: diff_tables(t, want[t.name], have[t.name]) for t in TABLES}
        for t in TABLES:                                                  # parents first for inserts and updates
            _write(cur, tenant, t, TableDiff(diffs[t.name].insert, diffs[t.name].update, []))
        for t in reversed(PROJECT_TABLES):                                # children first for deletes
            _write(cur, tenant, t, TableDiff([], [], diffs[t.name].delete))
        scoped = [QRow(q.table, q.line, proj_id if q.proj_id == proj_id else None, q.code, q.reason, q.raw) for q in mine]
        q_changed = _sync_quarantine(cur, tenant, proj_id, label, sha, scoped)
        data_changed = any(not d.empty for d in diffs.values())
        res.counts = {t.name: len(want[t.name]) for t in TABLES if t.scope == "project"}
        res.quarantined = len(scoped)
        if existed and data_changed:
            res.changes = task_changes(want["TASK"], have["TASK"])
        if not existed:
            res.status = "loaded"
        elif data_changed or q_changed:
            res.status = "updated"
        else:
            res.status = "unchanged"
        if res.status != "unchanged":
            cur.execute("INSERT INTO load_run (tenant_id, proj_id, kind, source_label, source_sha256, counts) VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id",
                        (tenant, proj_id, "update" if existed else "initial", label, sha, Jsonb({**res.counts, "quarantined": res.quarantined})))
            run_id = cur.fetchone()[0]
            if res.changes:
                cur.executemany("INSERT INTO change_event (tenant_id, run_id, kind, task_id, task_code, field, old_value, new_value) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                [(tenant, run_id, c.kind, c.task_id, c.task_code, c.field, c.old, c.new) for c in res.changes])
        after = [f for f in validate_db(cur, tenant, [proj_id]) if f.severity == "error"]
        if after:
            raise PostLoadError(f"{len(after)} validation finding(s) on the stored data, first: {after[0]}")
    return res


def _project_ids(plan: Plan) -> list[int]:
    return [r.values["proj_id"] for r in plan.accepted["PROJECT"]]


def load_file(conn: psycopg.Connection, tenant: str, path: str, label: str | None = None) -> LoadResult:
    started = time.perf_counter()
    pr = parse_file(path)
    return load_parsed(conn, tenant, pr, label or os.path.basename(path), started)


def load_parsed(conn: psycopg.Connection, tenant: str, pr: ParseResult, label: str, started: float | None = None) -> LoadResult:
    started = time.perf_counter() if started is None else started
    result = LoadResult(pr.path, label, pr.sha256, "rejected")
    for b in pr.blocks:
        result.rows_read[b.name] = result.rows_read.get(b.name, 0) + len(b.rows)
    if pr.fatal:
        result.findings = problem_findings(pr)
        result.reason = next(p.message for p in pr.problems if p.code in FATAL_CODES)
        result.seconds = time.perf_counter() - started
        return result
    plan = make_plan(pr)
    result.findings = plan.findings
    result.quarantined = plan.quarantined
    if not plan.accepted["PROJECT"]:
        result.reason = "no PROJECT row survived validation, so there is nothing to attach the schedule to"
        result.seconds = time.perf_counter() - started
        return result
    for prow in plan.accepted["PROJECT"]:
        pid, name = prow.values["proj_id"], prow.values["proj_short_name"]
        try:
            result.projects.append(load_project(conn, tenant, plan, pid, name, label, pr.sha256))
        except (psycopg.Error, PostLoadError) as exc:
            result.projects.append(ProjectResult(pid, name, "failed", error=f"{type(exc).__name__}: {exc}".splitlines()[0]))
    states = {p.status for p in result.projects}
    if states == {"failed"}:
        result.status = "failed"
    elif "failed" in states:
        result.status = "partial"
    elif states == {"unchanged"}:
        result.status = "unchanged"
    else:
        result.status = "loaded"
    result.seconds = time.perf_counter() - started
    return result


def diff_file(conn: psycopg.Connection, tenant: str, path: str) -> dict[int, tuple[bool, list[Change]]]:
    """What loading `path` would change, per project, without writing anything. Maps proj_id to (already stored?, changes)."""
    pr = parse_file(path)
    if pr.fatal:
        raise ValueError("; ".join(p.message for p in pr.problems if p.code in FATAL_CODES))
    plan = make_plan(pr)
    out: dict[int, tuple[bool, list[Change]]] = {}
    with tenant_tx(conn, tenant) as cur:
        for prow in plan.accepted["PROJECT"]:
            pid = prow.values["proj_id"]
            want = desired_state(plan, pid)
            have_tasks = _read_existing(cur, tenant, BY_NAME["TASK"], pid, [])
            existed = bool(_read_existing(cur, tenant, BY_NAME["PROJECT"], pid, []))
            out[pid] = (existed, task_changes(want["TASK"], have_tasks) if existed else [])
    return out


__all__ = ["load_file", "load_parsed", "diff_file", "make_plan", "LoadResult", "ProjectResult", "Change", "QRow", "PostLoadError", "GLOBAL_TABLES"]
