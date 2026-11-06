"""Validation of a schedule: the same rules run on the parsed file before a load and on the database after it.

A rule never edits anything. It returns Findings, and a Rejection for every row that must not be loaded; the loader turns each rejection into a quarantine row with
the rule's code as the reason. Rules are applied until nothing new is rejected, so a task whose WBS was rejected is rejected too, and so are its relationships.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .parse import ParseResult
from .schema import BY_NAME, DATE_PAIRS, INT, NUM, PRED_TYPES, TABLES


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    table: str | None
    key: str | None
    line: int | None
    message: str

    def __str__(self) -> str:
        at = (f"{self.table} {self.key}" if self.key else self.table) if self.table else "file"
        if self.line is not None:
            at += f" (line {self.line})"
        return f"[{self.severity}] {self.rule} {at}: {self.message}"


@dataclass
class WRow:
    table: str
    ref: Any                  # line number for rows from a file, key for rows from the database
    line: int | None
    v: dict[str, Any]


@dataclass
class Analysis:
    findings: list[Finding] = field(default_factory=list)
    rejected: dict[tuple[str, Any], tuple[str, str]] = field(default_factory=dict)       # (table, ref) -> (rule code, message)

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


World = dict[str, list[WRow]]


def world_from_parse(pr: ParseResult) -> World:
    """Rows of the modeled tables that parsed without a problem. Rows with a parse problem are quarantined before validation ever sees them."""
    world: World = {t.name: [] for t in TABLES}
    for block in pr.blocks:
        for row in block.rows:                       # a row of an unmodeled table has no values and a row of a repeated table carries a problem, so neither gets in
            if row.ok and row.values is not None:
                world[block.name].append(WRow(block.name, row.line, row.line, row.values))
    return world


def problem_findings(pr: ParseResult) -> list[Finding]:
    out = [Finding(p.code, p.severity, p.table, None, p.line, p.message) for p in pr.problems]
    for row in pr.all_rows():
        for p in row.problems:
            out.append(Finding(p.code, p.severity, row.table, None, p.line, p.message))
    return out


def _key(table: str, v: dict[str, Any]) -> str:
    k = v.get(BY_NAME[table].key)
    return "?" if k is None else str(k)


def analyze(world: World) -> Analysis:
    a = Analysis()

    def reject(row: WRow, rule: str, message: str) -> None:
        ident = (row.table, row.ref)
        if ident in a.rejected:
            return
        a.rejected[ident] = (rule, message)
        a.findings.append(Finding(rule, "error", row.table, _key(row.table, row.v), row.line, message))

    def alive(table: str) -> list[WRow]:
        return [r for r in world.get(table, []) if (table, r.ref) not in a.rejected]

    # 1. Rules that need only the row itself.
    for t in TABLES:
        seen: dict[Any, WRow] = {}
        for r in world.get(t.name, []):
            k = r.v.get(t.key)
            if k in seen:
                first = seen[k]
                reject(r, "DUP_KEY", f"{t.key} {k} already used at {_where(first)}")
                continue
            seen[k] = r
            for c in t.cols:
                x = r.v.get(c.name)
                if x is None:
                    continue
                if c.kind == INT and c.name.endswith("_id") and x <= 0:
                    reject(r, "OUT_OF_RANGE", f"{c.name}={x} must be positive")
                    break
                if c.kind == NUM and ((c.lo is not None and x < c.lo) or (c.hi is not None and x > c.hi)):
                    reject(r, "OUT_OF_RANGE", f"{c.name}={x} is outside {c.lo} to {c.hi}")
                    break
    for r in world.get("TASK", []):
        for lo, hi in DATE_PAIRS:
            s, e = r.v.get(lo), r.v.get(hi)
            if s is not None and e is not None and e < s:
                reject(r, "FINISH_BEFORE_START", f"{hi} {e:%Y-%m-%d %H:%M} is before {lo} {s:%Y-%m-%d %H:%M}")
                break
    for r in world.get("TASKPRED", []):
        if r.v["pred_type"] not in PRED_TYPES:
            reject(r, "UNKNOWN_PRED_TYPE", f"pred_type {r.v['pred_type']!r} is not one of {', '.join(PRED_TYPES)}")
        elif r.v["proj_id"] != r.v["pred_proj_id"]:
            reject(r, "CROSS_PROJECT_PRED", f"relationship between projects {r.v['proj_id']} and {r.v['pred_proj_id']} is not supported")

    # 2. Rules about references, repeated until nothing new is rejected.
    while True:
        before = len(a.rejected)
        cals = {r.v["clndr_id"] for r in alive("CALENDAR")}
        rsrcs = {r.v["rsrc_id"] for r in alive("RSRC")}
        projs = {r.v["proj_id"] for r in alive("PROJECT")}
        for r in alive("RSRC"):
            if r.v["clndr_id"] is not None and r.v["clndr_id"] not in cals:
                reject(r, "MISSING_CALENDAR", f"clndr_id {r.v['clndr_id']} is not a calendar in the file")
        for r in alive("PROJECT"):
            if r.v["clndr_id"] is not None and r.v["clndr_id"] not in cals:
                reject(r, "MISSING_CALENDAR", f"clndr_id {r.v['clndr_id']} is not a calendar in the file")
        for r in alive("PROJWBS"):
            if r.v["proj_id"] not in projs:
                reject(r, "MISSING_PROJECT", f"proj_id {r.v['proj_id']} is not a project in the file")
        wbs_proj = {r.v["wbs_id"]: r.v["proj_id"] for r in alive("PROJWBS")}
        for r in alive("PROJWBS"):
            p = r.v["parent_wbs_id"]
            if p is not None and wbs_proj.get(p) != r.v["proj_id"]:
                reject(r, "MISSING_WBS_PARENT", f"parent_wbs_id {p} is not a WBS element of project {r.v['proj_id']}")
        _wbs_cycles(alive("PROJWBS"), reject)
        wbs_proj = {r.v["wbs_id"]: r.v["proj_id"] for r in alive("PROJWBS")}
        for r in alive("TASK"):
            if r.v["proj_id"] not in projs:
                reject(r, "MISSING_PROJECT", f"proj_id {r.v['proj_id']} is not a project in the file")
            elif wbs_proj.get(r.v["wbs_id"]) != r.v["proj_id"]:
                reject(r, "MISSING_WBS", f"wbs_id {r.v['wbs_id']} is not a WBS element of project {r.v['proj_id']}")
            elif r.v["clndr_id"] not in cals:
                reject(r, "MISSING_CALENDAR", f"clndr_id {r.v['clndr_id']} is not a calendar in the file")
        codes: dict[tuple[Any, str], WRow] = {}
        for r in alive("TASK"):
            ck = (r.v["proj_id"], r.v["task_code"])
            if ck in codes:
                reject(r, "DUP_TASK_CODE", f"task_code {r.v['task_code']!r} already used by task {codes[ck].v['task_id']} at {_where(codes[ck])}")
            else:
                codes[ck] = r
        task_proj = {r.v["task_id"]: r.v["proj_id"] for r in alive("TASK")}
        for r in alive("TASKPRED"):
            if r.v["proj_id"] not in projs:
                reject(r, "MISSING_PROJECT", f"proj_id {r.v['proj_id']} is not a project in the file")
            elif task_proj.get(r.v["task_id"]) != r.v["proj_id"]:
                reject(r, "MISSING_TASK", f"task_id {r.v['task_id']} is not a task of project {r.v['proj_id']}")
            elif task_proj.get(r.v["pred_task_id"]) != r.v["pred_proj_id"]:
                reject(r, "MISSING_TASK", f"pred_task_id {r.v['pred_task_id']} is not a task of project {r.v['pred_proj_id']}")
        for r in alive("TASKRSRC"):
            if r.v["proj_id"] not in projs:
                reject(r, "MISSING_PROJECT", f"proj_id {r.v['proj_id']} is not a project in the file")
            elif task_proj.get(r.v["task_id"]) != r.v["proj_id"]:
                reject(r, "ORPHAN_ASSIGNMENT", f"task_id {r.v['task_id']} is not a task of project {r.v['proj_id']}")
            elif r.v["rsrc_id"] is not None and r.v["rsrc_id"] not in rsrcs:
                reject(r, "ORPHAN_ASSIGNMENT", f"rsrc_id {r.v['rsrc_id']} is not a resource in the file")
        if len(a.rejected) == before:
            break

    # 3. Task cycles. Removing every back edge found by one depth first walk leaves a graph with no cycle. (WBS loops are cut inside the loop above, because a rejected WBS element takes its tasks with it.)
    _task_cycles(alive("TASK"), alive("TASKPRED"), reject)
    return a


def _where(r: WRow) -> str:
    return f"line {r.line}" if r.line is not None else f"key {r.ref}"


def _wbs_cycles(rows: list[WRow], reject) -> None:
    by_id = {r.v["wbs_id"]: r for r in rows}
    walking: set[Any] = set()                       # on the current walk
    finished: set[Any] = set()
    for start in by_id:
        path: list[Any] = []
        cur = start
        while cur is not None and cur in by_id and cur not in walking and cur not in finished:
            walking.add(cur)
            path.append(cur)
            cur = by_id[cur].v["parent_wbs_id"]
        if cur in walking:
            loop = path[path.index(cur):] + [cur]
            reject(by_id[path[-1]], "WBS_CYCLE", "WBS parents form a loop: " + " -> ".join(str(x) for x in loop))
        walking.difference_update(path)
        finished.update(path)


def _task_cycles(tasks: list[WRow], preds: list[WRow], reject) -> None:
    code = {r.v["task_id"]: r.v["task_code"] for r in tasks}
    out: dict[Any, list[WRow]] = {}
    for p in preds:
        out.setdefault(p.v["pred_task_id"], []).append(p)       # edge: predecessor -> successor
    seen: set[Any] = set()                                      # on the stack or finished
    for root in code:
        if root not in seen:
            _walk(root, out, code, seen, reject)


def _walk(root: Any, out: dict[Any, list[WRow]], code: dict[Any, str], seen: set[Any], reject) -> None:
    """Iterative depth first walk (a schedule can be thousands of tasks deep). The stack is the path from the root to the task being looked at."""
    seen.add(root)
    stack: list[tuple[Any, int]] = [(root, 0)]
    on_stack = {root}
    while stack:
        node, i = stack[-1]
        edges = out.get(node, [])
        if i == len(edges):
            on_stack.discard(stack.pop()[0])
            continue
        stack[-1] = (node, i + 1)
        e = edges[i]
        nxt = e.v["task_id"]
        if nxt not in seen:
            seen.add(nxt)
            on_stack.add(nxt)
            stack.append((nxt, 0))
        elif nxt in on_stack:
            path = [n for n, _ in stack]
            loop = path[path.index(nxt):] + [nxt]
            reject(e, "CYCLE", "dependency cycle: " + " -> ".join(str(code.get(x, x)) for x in loop))


def validate_parse(pr: ParseResult) -> tuple[list[Finding], Analysis]:
    """Every parse problem plus the rule findings of the rows that parsed cleanly."""
    analysis = analyze(world_from_parse(pr))
    return problem_findings(pr) + analysis.findings, analysis


def world_from_db(cur, tenant: str, proj_ids: Iterable[int] | None = None) -> World:
    """Rows of one tenant (and, when given, of the named projects plus every global row) as a World. `cur` must already be inside that tenant's transaction."""
    world: World = {t.name: [] for t in TABLES}
    wanted = None if proj_ids is None else list(proj_ids)
    for t in TABLES:
        cols = ", ".join(t.col_names)
        if t.scope == "project" and wanted is not None:
            col = "proj_id"
            cur.execute(f"SELECT {cols} FROM {t.pg} WHERE tenant_id = %s AND {col} = ANY(%s) ORDER BY {t.key}", (tenant, wanted))
        else:
            cur.execute(f"SELECT {cols} FROM {t.pg} WHERE tenant_id = %s ORDER BY {t.key}", (tenant,))
        for rec in cur.fetchall():
            v = dict(zip(t.col_names, rec))
            world[t.name].append(WRow(t.name, v[t.key], None, v))
    return world


def validate_db(cur, tenant: str, proj_ids: Iterable[int] | None = None) -> list[Finding]:
    """The same rules over what is stored. A clean load leaves no finding here; any finding is a defect in the loader or in someone's direct edit."""
    return analyze(world_from_db(cur, tenant, proj_ids)).findings


def severity_counts(findings: Iterable[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out


__all__ = ["Finding", "WRow", "Analysis", "analyze", "validate_parse", "validate_db", "world_from_parse", "world_from_db", "problem_findings", "severity_counts"]
