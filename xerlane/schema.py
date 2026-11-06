"""The tables and columns xerlane models.

Table and column NAMES come from Oracle's XER Import/Export Data Map Guide (docs/vendor-contract/oracle-xer-data-map-v23-excerpt.txt).
Column KINDS are inferred from the column name suffix, the way the two open-source readers listed in docs/vendor-contract/SOURCE.md do; Oracle publishes no types
(docs/NOT_CONFIRMED.md item 1). Key columns and the relationships between tables follow the fields Oracle lists (TASKPRED.task_id and pred_task_id point at TASK, and so on).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

INT, NUM, TS, TEXT = "int", "num", "ts", "text"

PRED_TYPES = ("PR_FS", "PR_SS", "PR_FF", "PR_SF")          # codes from MPXJ only, see NOT_CONFIRMED item 5
BIGGEST = Decimal("100000000")                               # 1e8 hours or units: far past any real schedule, close enough to catch a shifted decimal point
MAX_ID = 2**63 - 1


@dataclass(frozen=True)
class Col:
    name: str
    kind: str
    required: bool = False                # an empty value in a required column is a MISSING_VALUE
    lo: Decimal | None = None             # inclusive bounds for numeric columns
    hi: Decimal | None = None


@dataclass(frozen=True)
class Table:
    name: str                              # XER table name, e.g. TASK
    pg: str                                # Postgres table name, lower case
    key: str                               # single-column primary key (together with tenant_id)
    cols: tuple[Col, ...]
    scope: str                             # "project" (has proj_id, replaced as a unit) or "global" (per tenant)

    @property
    def col_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.cols)

    def col(self, name: str) -> Col:
        for c in self.cols:
            if c.name == name:
                return c
        raise KeyError(name)


def _hours(name: str, *, signed: bool = False) -> Col:
    return Col(name, NUM, lo=-BIGGEST if signed else Decimal(0), hi=BIGGEST)


def _money(name: str) -> Col:
    return Col(name, NUM, lo=Decimal(0), hi=BIGGEST)


CALENDAR = Table("CALENDAR", "calendar", "clndr_id", (
    Col("clndr_id", INT, True), Col("clndr_name", TEXT), Col("clndr_type", TEXT),
    _hours("day_hr_cnt"), _hours("week_hr_cnt"), Col("default_flag", TEXT),
), "global")

RSRC = Table("RSRC", "rsrc", "rsrc_id", (
    Col("rsrc_id", INT, True), Col("rsrc_name", TEXT), Col("rsrc_short_name", TEXT), Col("rsrc_type", TEXT), Col("clndr_id", INT),
), "global")

PROJECT = Table("PROJECT", "project", "proj_id", (
    Col("proj_id", INT, True), Col("proj_short_name", TEXT, True), Col("plan_start_date", TS), Col("plan_end_date", TS),
    Col("last_recalc_date", TS), Col("clndr_id", INT),
), "project")

PROJWBS = Table("PROJWBS", "projwbs", "wbs_id", (
    Col("wbs_id", INT, True), Col("proj_id", INT, True), Col("parent_wbs_id", INT), Col("wbs_short_name", TEXT, True),
    Col("wbs_name", TEXT), Col("proj_node_flag", TEXT), Col("seq_num", INT),
), "project")

TASK = Table("TASK", "task", "task_id", (
    Col("task_id", INT, True), Col("proj_id", INT, True), Col("wbs_id", INT, True), Col("clndr_id", INT, True),
    Col("task_code", TEXT, True), Col("task_name", TEXT), Col("task_type", TEXT), Col("status_code", TEXT),
    _hours("target_drtn_hr_cnt"), _hours("remain_drtn_hr_cnt"),
    Col("target_start_date", TS), Col("target_end_date", TS), Col("early_start_date", TS), Col("early_end_date", TS),
    Col("late_start_date", TS), Col("late_end_date", TS), Col("act_start_date", TS), Col("act_end_date", TS),
    _hours("total_float_hr_cnt", signed=True), _hours("free_float_hr_cnt", signed=True),
), "project")

TASKPRED = Table("TASKPRED", "taskpred", "task_pred_id", (
    Col("task_pred_id", INT, True), Col("task_id", INT, True), Col("pred_task_id", INT, True), Col("proj_id", INT, True),
    Col("pred_proj_id", INT, True), Col("pred_type", TEXT, True), _hours("lag_hr_cnt", signed=True),
), "project")

TASKRSRC = Table("TASKRSRC", "taskrsrc", "taskrsrc_id", (
    Col("taskrsrc_id", INT, True), Col("task_id", INT, True), Col("rsrc_id", INT), Col("proj_id", INT, True),
    _money("target_cost"), _money("target_qty"), _money("remain_cost"), _money("remain_qty"),
    _money("act_reg_cost"), _money("act_reg_qty"), _money("act_ot_cost"), _money("act_ot_qty"),
), "project")

# Dependency order: a table appears after every table it points at.
TABLES: tuple[Table, ...] = (CALENDAR, RSRC, PROJECT, PROJWBS, TASK, TASKPRED, TASKRSRC)
BY_NAME: dict[str, Table] = {t.name: t for t in TABLES}
GLOBAL_TABLES = tuple(t for t in TABLES if t.scope == "global")
PROJECT_TABLES = tuple(t for t in TABLES if t.scope == "project")

# Columns whose sums reconcile.py checks: every numeric column.
NUMERIC_COLS: dict[str, tuple[str, ...]] = {t.name: tuple(c.name for c in t.cols if c.kind == NUM) for t in TABLES}

DATE_PAIRS = (("target_start_date", "target_end_date"), ("early_start_date", "early_end_date"),
              ("late_start_date", "late_end_date"), ("act_start_date", "act_end_date"))
