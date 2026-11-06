"""Earned value report: schedule progress from the XER tables joined to the budget and actual cost of the cost export, by task, by cost code and by model element.

Definitions (docs/cost.md has the reasoning and the limits):
  BAC  budget at completion      sum of the budget of the cost lines tied to the task
  PV   planned value             BAC times the share of the planned dates that has passed at the as-of date (straight line between target start and target end)
  EV   earned value              BAC times the percent complete of the task (actual finish recorded = 1; no actual start = 0; else 1 - remaining / target duration)
  AC   actual cost               sum of the actual of the cost lines tied to the task
  CV = EV - AC, SV = EV - PV, CPI = EV / AC, SPI = EV / PV, EAC = BAC / CPI (the usual "CPI holds" forecast; nothing when CPI is not defined)
Money is Decimal throughout and shown to two places. A ratio whose denominator is zero is None, never 0 and never infinity.
Cost lines with no task code, or whose task code no longer matches a task, are not hidden: they are reported as `unallocated` and `unmatched` with their budget and
actual, counted in the project totals for BAC and AC, and given no PV or EV (there is no schedule to take progress from).
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from .db import tenant_tx

CENT = Decimal("0.01")
ZERO = Decimal(0)


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(num: Decimal, den: Decimal) -> Decimal | None:
    return None if den == 0 else (num / den).quantize(CENT, rounding=ROUND_HALF_UP)


def percent_complete(act_start: datetime | None, act_end: datetime | None, target_hr: Decimal | None, remain_hr: Decimal | None) -> Decimal:
    """0 to 1. A recorded actual finish is 1 and no recorded actual start is 0, whatever the remaining duration says; in between 1 - remaining / target."""
    if act_end is not None:
        return Decimal(1)
    if act_start is None:
        return ZERO
    if not target_hr or target_hr <= 0 or remain_hr is None:
        return ZERO
    return min(Decimal(1), max(ZERO, Decimal(1) - remain_hr / target_hr))


def percent_planned(as_of: datetime, start: datetime | None, end: datetime | None) -> Decimal:
    """0 to 1: the share of the span between target start and target end that is behind the as-of date. A zero-length task (a milestone) is 1 from its date on."""
    if start is None or end is None:
        return ZERO
    if as_of >= end:
        return Decimal(1)
    if as_of <= start:
        return ZERO
    return Decimal(str((as_of - start).total_seconds() / (end - start).total_seconds()))


@dataclass
class Line:
    key: str
    name: str
    bac: Decimal = ZERO
    pv: Decimal = ZERO
    ev: Decimal = ZERO
    ac: Decimal = ZERO
    forecast: Decimal = ZERO
    pct_done: Decimal | None = None
    pct_planned: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cv(self) -> Decimal:
        return money(self.ev - self.ac)

    @property
    def sv(self) -> Decimal:
        return money(self.ev - self.pv)

    @property
    def cpi(self) -> Decimal | None:
        return ratio(self.ev, self.ac)

    @property
    def spi(self) -> Decimal | None:
        return ratio(self.ev, self.pv)

    @property
    def eac(self) -> Decimal | None:
        cpi = ratio(self.ev, self.ac)
        return None if cpi is None or cpi == 0 else money(self.bac / cpi)


@dataclass
class Report:
    tenant: str
    proj_id: int
    project: str
    as_of: datetime
    tasks: list[Line]
    totals: Line
    unallocated: Line
    unmatched: Line
    by_code: list[dict[str, Any]]
    elements: list[dict[str, Any]]
    notes: list[str]


class ReportError(ValueError):
    pass


def build_report(conn: psycopg.Connection, tenant: str, proj_id: int, as_of: datetime | None = None) -> Report:
    with tenant_tx(conn, tenant) as cur:
        cur.execute("SELECT proj_short_name, last_recalc_date FROM project WHERE tenant_id = %s AND proj_id = %s", (tenant, proj_id))
        row = cur.fetchone()
        if row is None:
            raise ReportError(f"project {proj_id} is not loaded for tenant {tenant!r}")
        short, recalc = row
        when = as_of or recalc
        if when is None:
            raise ReportError("the project has no last recalculation date; pass an as-of date")
        cur.execute(TASK_SQL, {"tenant": tenant, "proj": proj_id})
        task_rows = cur.fetchall()
        cur.execute("SELECT coalesce(sum(budget), 0), coalesce(sum(actual), 0), coalesce(sum(forecast), 0), count(*) FROM cost_line WHERE tenant_id = %s AND proj_id = %s AND task_code IS NULL", (tenant, proj_id))
        un_bac, un_ac, un_fc, un_n = cur.fetchone()
        cur.execute(NO_TASK_SQL, {"tenant": tenant, "proj": proj_id})
        nm_bac, nm_ac, nm_fc, nm_n = cur.fetchone()
        cur.execute(CODE_SQL, {"tenant": tenant, "proj": proj_id})
        code_rows = cur.fetchall()
        cur.execute(ELEMENT_SQL, {"tenant": tenant, "proj": proj_id})
        element_rows = cur.fetchall()
    tasks: list[Line] = []
    for task_code, name, wbs, a_start, a_end, t_start, t_end, tgt, rem, bac, ac, fc in task_rows:
        done = percent_complete(a_start, a_end, tgt, rem)
        planned = percent_planned(when, t_start, t_end)
        tasks.append(Line(task_code, name or "", bac, bac * planned, bac * done, ac, fc, done, planned, {"wbs": wbs}))
    un = Line("(no task code)", f"{un_n} cost line(s) with no task code", un_bac, ZERO, ZERO, un_ac, un_fc)
    nm = Line("(task not found)", f"{nm_n} cost line(s) whose task code matches no task", nm_bac, ZERO, ZERO, nm_ac, nm_fc)
    totals = Line("TOTAL", short)
    for part in (*tasks, un, nm):
        totals.bac += part.bac
        totals.pv += part.pv
        totals.ev += part.ev
        totals.ac += part.ac
        totals.forecast += part.forecast
    by_task = {t.key: t for t in tasks}
    elements = [{"external_id": ext, "name": nm_, "category": cat, "task_code": code, "bac": money(by_task[code].bac), "ev": money(by_task[code].ev), "ac": money(by_task[code].ac)}
                for ext, nm_, cat, code in element_rows if code in by_task]
    by_code = [{"cost_code": c, "budget": b, "forecast": f, "actual": a, "committed": cm, "invoiced": inv, "over": cm > b} for c, b, f, a, cm, inv in code_rows]
    notes = []
    if un_n:
        notes.append(f"{un_n} cost line(s) have no task code: counted in BAC and AC, given no PV or EV.")
    if nm_n:
        notes.append(f"{nm_n} cost line(s) name a task code that no task has now: counted in BAC and AC, given no PV or EV.")
    if not tasks:
        notes.append("no task has cost lines: load a cost export with task codes (xerlane load-cost).")
    return Report(tenant, proj_id, short, when, tasks, totals, un, nm, by_code, elements, notes)


# The three statements below are what the report asks the database. docs/query-tuning.md shows their plans and why cost_schema.sql adds no secondary index.
TASK_SQL = """
SELECT t.task_code, t.task_name, t.wbs_id, t.act_start_date, t.act_end_date, t.target_start_date, t.target_end_date, t.target_drtn_hr_cnt, t.remain_drtn_hr_cnt,
       c.bac, c.ac, c.fc
FROM (SELECT task_code, sum(budget) AS bac, sum(actual) AS ac, sum(forecast) AS fc
      FROM cost_line WHERE tenant_id = %(tenant)s AND proj_id = %(proj)s AND task_code IS NOT NULL GROUP BY task_code) c
JOIN task t ON t.tenant_id = %(tenant)s AND t.proj_id = %(proj)s AND t.task_code = c.task_code
ORDER BY t.task_code
"""

NO_TASK_SQL = """
SELECT coalesce(sum(c.budget), 0), coalesce(sum(c.actual), 0), coalesce(sum(c.forecast), 0), count(*)
FROM cost_line c
WHERE c.tenant_id = %(tenant)s AND c.proj_id = %(proj)s AND c.task_code IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM task t WHERE t.tenant_id = c.tenant_id AND t.proj_id = c.proj_id AND t.task_code = c.task_code)
"""

CODE_SQL = """
SELECT k.cost_code, k.budget, k.forecast, k.actual, coalesce(m.committed, 0), coalesce(m.invoiced, 0)
FROM (SELECT cost_code, sum(budget) AS budget, sum(forecast) AS forecast, sum(actual) AS actual
      FROM cost_line WHERE tenant_id = %(tenant)s AND proj_id = %(proj)s GROUP BY cost_code) k
LEFT JOIN (SELECT cost_code, sum(committed) AS committed, sum(invoiced) AS invoiced
           FROM commitment WHERE tenant_id = %(tenant)s AND proj_id = %(proj)s AND status <> 'VOID' GROUP BY cost_code) m ON m.cost_code = k.cost_code
ORDER BY k.cost_code
"""

ELEMENT_SQL = """
SELECT o.external_id, o.name, o.category, o.task_code
FROM aps_object_task o WHERE o.tenant_id = %(tenant)s AND o.proj_id = %(proj)s
ORDER BY o.task_code, o.external_id NULLS LAST, o.object_id
"""


# ---- rendering ------------------------------------------------------------------------------------------------------------------------------------

def _fmt(x: Decimal | None) -> str:
    return "n/a" if x is None else f"{money(x):,}" if abs(x) >= 1 or x == 0 else f"{x}"


def _ratio(x: Decimal | None) -> str:
    return "n/a" if x is None else f"{x}"


def render_text(rep: Report, limit: int = 15) -> str:
    t = rep.totals
    out = [f"project    {rep.project} ({rep.proj_id})  tenant {rep.tenant}  as of {rep.as_of:%Y-%m-%d %H:%M}",
           f"BAC {_fmt(t.bac)}   PV {_fmt(t.pv)}   EV {_fmt(t.ev)}   AC {_fmt(t.ac)}",
           f"CV {_fmt(t.cv)}   SV {_fmt(t.sv)}   CPI {_ratio(t.cpi)}   SPI {_ratio(t.spi)}   EAC {_fmt(t.eac)}"]
    out += [f"note       {n}" for n in rep.notes]
    worst = sorted(rep.tasks, key=lambda x: x.cv)[:limit]
    if worst:
        out.append("")
        out.append(f"{'task':10} {'BAC':>14} {'PV':>14} {'EV':>14} {'AC':>14} {'CPI':>6} {'SPI':>6}  (the {len(worst)} furthest over cost)")
        for x in worst:
            out.append(f"{x.key:10} {_fmt(x.bac):>14} {_fmt(x.pv):>14} {_fmt(x.ev):>14} {_fmt(x.ac):>14} {_ratio(x.cpi):>6} {_ratio(x.spi):>6}")
    over = [c for c in rep.by_code if c["over"]]
    out.append("")
    out.append(f"cost codes {len(rep.by_code)}, committed above budget on {len(over)}" + (": " + ", ".join(c["cost_code"] for c in over[:limit]) if over else ""))
    out.append(f"model      {len(rep.elements)} element(s) joined to a task through its task code")
    return "\n".join(out)


def render_csv(rep: Report) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["task_code", "task_name", "bac", "pv", "ev", "ac", "cv", "sv", "cpi", "spi", "eac", "pct_done", "pct_planned"])
    for x in [*rep.tasks, rep.unallocated, rep.unmatched, rep.totals]:
        w.writerow([x.key, x.name, money(x.bac), money(x.pv), money(x.ev), money(x.ac), x.cv, x.sv, _ratio(x.cpi), _ratio(x.spi), "" if x.eac is None else x.eac,
                    "" if x.pct_done is None else x.pct_done.quantize(CENT), "" if x.pct_planned is None else x.pct_planned.quantize(CENT)])
    return buf.getvalue()


def render_html(rep: Report) -> str:
    """One self-contained page: no script, no external file. The bars compare PV, EV and AC of the whole project."""
    t = rep.totals
    top = max(t.pv, t.ev, t.ac, Decimal(1))
    bars = "".join(f'<div class="bar"><span>{lab}</span><i style="width:{float(val / top * 100):.1f}%" class="{cls}"></i><b>{_fmt(val)}</b></div>'
                   for lab, val, cls in (("PV", t.pv, "pv"), ("EV", t.ev, "ev"), ("AC", t.ac, "ac")))
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(x.key)}</td><td>{esc(x.name)}</td><td>{_fmt(x.bac)}</td><td>{_fmt(x.pv)}</td><td>{_fmt(x.ev)}</td><td>{_fmt(x.ac)}</td><td>{_ratio(x.cpi)}</td><td>{_ratio(x.spi)}</td></tr>"
                   for x in rep.tasks)
    codes = "".join(f"<tr><td>{esc(c['cost_code'])}</td><td>{_fmt(c['budget'])}</td><td>{_fmt(c['actual'])}</td><td>{_fmt(c['committed'])}</td><td>{_fmt(c['invoiced'])}</td><td>{'over' if c['over'] else ''}</td></tr>" for c in rep.by_code)
    elems = "".join(f"<tr><td>{esc(str(e['external_id'] or ''))}</td><td>{esc(str(e['name'] or ''))}</td><td>{esc(e['task_code'])}</td><td>{_fmt(e['bac'])}</td><td>{_fmt(e['ev'])}</td></tr>" for e in rep.elements)
    notes = "".join(f"<li>{esc(n)}</li>" for n in rep.notes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Earned value, {esc(rep.project)}</title>
<style>body{{font:14px system-ui,sans-serif;margin:2rem;color:#1b1f23}}table{{border-collapse:collapse;margin:1rem 0}}td,th{{border:1px solid #c8ccd0;padding:.25rem .6rem;text-align:right}}
td:first-child,td:nth-child(2),th:first-child,th:nth-child(2){{text-align:left}}.bar{{display:flex;align-items:center;gap:.6rem;margin:.2rem 0}}.bar span{{width:2rem}}
.bar i{{display:block;height:1rem}}.pv{{background:#8a94a0}}.ev{{background:#2f7d4f}}.ac{{background:#b5483a}}</style></head><body>
<h1>{esc(rep.project)}</h1><p>Tenant {esc(rep.tenant)}, project {rep.proj_id}, as of {rep.as_of:%Y-%m-%d %H:%M}.</p>
<div>{bars}</div>
<p>BAC {_fmt(t.bac)}. CV {_fmt(t.cv)}, SV {_fmt(t.sv)}, CPI {_ratio(t.cpi)}, SPI {_ratio(t.spi)}, EAC {_fmt(t.eac)}.</p><ul>{notes}</ul>
<h2>By task</h2><table><tr><th>Task</th><th>Name</th><th>BAC</th><th>PV</th><th>EV</th><th>AC</th><th>CPI</th><th>SPI</th></tr>{rows}</table>
<h2>By cost code</h2><table><tr><th>Cost code</th><th>Budget</th><th>Actual</th><th>Committed</th><th>Invoiced</th><th></th></tr>{codes}</table>
<h2>Model elements</h2><table><tr><th>External id</th><th>Name</th><th>Task</th><th>BAC</th><th>EV</th></tr>{elems}</table>
</body></html>
"""
