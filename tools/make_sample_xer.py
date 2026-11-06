"""Write an invented XER schedule, optionally with planted defects.

Self-contained on purpose: it imports nothing from xerlane, so a bug in the parser cannot hide a bug here. Every value is made up. The layout is the one described in
docs/vendor-contract/SOURCE.md (ERMHDR line, %T table, %F fields, %R rows, %E end, tab delimited, cp1252, CRLF). Text codes such as task_type are invented words, because no
page I could load lists Primavera's own codes; the relationship codes PR_FS, PR_SS, PR_FF and PR_SF are the ones MPXJ documents.

    python tools/make_sample_xer.py --seed 7 --tasks 400 --out out/sample.xer
    python tools/make_sample_xer.py --seed 7 --tasks 400 --defect cycle --defect bad_number --out out/bad.xer --manifest out/bad.json
    python tools/make_sample_xer.py --list-defects
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PRED_CODES = ("PR_FS", "PR_SS", "PR_FF", "PR_SF")
EXPORT_DATE = "2026-11-06"

# kind -> (what it plants, parse or rule codes xerlane must report for it, True when the whole file must be rejected)
DEFECTS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "missing_pred_target": ("a predecessor link that points at a task which is not in the file", ("MISSING_TASK",), False),
    "cycle": ("two or three relationships that make a dependency loop", ("CYCLE",), False),
    "finish_before_start": ("a task whose planned finish is before its planned start", ("FINISH_BEFORE_START",), False),
    "dup_task_code": ("two tasks of one project with the same task code", ("DUP_TASK_CODE",), False),
    "truncated": ("the file cut off in the middle of a row", ("MISSING_END",), True),
    "missing_end": ("the %E end marker removed", ("MISSING_END",), True),
    "bad_number": ("a numeric field holding text that is not a number", ("BAD_VALUE",), False),
    "bad_date": ("a date field that is not a real date", ("BAD_VALUE",), False),
    "wrong_field_count": ("a TASK row with one field too few", ("WRONG_FIELD_COUNT",), False),
    "orphan_assignment": ("a resource assignment on a task that does not exist", ("ORPHAN_ASSIGNMENT",), False),
    "missing_calendar": ("a task that names a calendar which is not in the file", ("MISSING_CALENDAR",), False),
    "unknown_pred_type": ("a relationship type that is not PR_FS, PR_SS, PR_FF or PR_SF", ("UNKNOWN_PRED_TYPE",), False),
    "out_of_range": ("a negative planned duration", ("OUT_OF_RANGE",), False),
    "stray_line": ("a line with no record marker in the middle of a table", ("STRAY_LINE",), False),
    "dup_key": ("two tasks with the same task_id", ("DUP_KEY",), False),
}


@dataclass
class Planted:
    kind: str
    table: str | None
    key: str | None
    expect: tuple[str, ...]
    fatal: bool
    line: int | None = None


@dataclass
class Tbl:
    name: str
    fields: list[str]
    rows: list[list[str]] = field(default_factory=list)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M")


def _money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Clock:
    """Working hours to dates: 8 hour days, Monday to Friday, from a start date. Invented, not a claim about how Primavera counts."""

    def __init__(self, start: datetime) -> None:
        self.start = start

    def at(self, hours: float) -> datetime:
        days, rem = divmod(int(round(hours)), 8)
        weeks, wd = divmod(days, 5)
        return self.start + timedelta(days=weeks * 7 + wd, hours=rem)


def build_tables(seed: int, tasks: int = 300, projects: int = 1, id_base: int = 1000, progress_hours: float = 40.0) -> list[Tbl]:
    rng = random.Random(seed)
    cal = Tbl("CALENDAR", ["clndr_id", "clndr_name", "clndr_type", "day_hr_cnt", "week_hr_cnt", "default_flag"])
    for i, (nm, d, w) in enumerate([("Standard five day", 8, 40), ("Six day", 8, 48), ("Round the clock", 24, 168)]):
        cal.rows.append([str(id_base + i), nm, "BASE", str(d), str(w), "Y" if i == 0 else "N"])
    cal_ids = [r[0] for r in cal.rows]

    rs = Tbl("RSRC", ["rsrc_id", "rsrc_name", "rsrc_short_name", "rsrc_type", "clndr_id"])
    rates: dict[str, Decimal] = {}
    for i in range(12):
        rid = str(id_base + 500 + i)
        kind = ["LABOR", "NONLABOR", "MATERIAL"][i % 3]
        rates[rid] = Decimal(rng.randint(30, 140)) + Decimal(rng.randint(0, 99)) / 100
        rs.rows.append([rid, f"Crew {chr(65 + i)} équipe" if i == 0 else f"Crew {chr(65 + i)}", f"C{i:02d}", kind, cal_ids[0]])

    proj = Tbl("PROJECT", ["proj_id", "proj_short_name", "plan_start_date", "plan_end_date", "last_recalc_date", "clndr_id", "guid"])
    wbs = Tbl("PROJWBS", ["wbs_id", "proj_id", "parent_wbs_id", "wbs_short_name", "wbs_name", "proj_node_flag", "seq_num", "guid"])
    task = Tbl("TASK", ["task_id", "proj_id", "wbs_id", "clndr_id", "task_code", "task_name", "task_type", "status_code", "target_drtn_hr_cnt", "remain_drtn_hr_cnt",
                        "target_start_date", "target_end_date", "early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date", "act_end_date",
                        "total_float_hr_cnt", "free_float_hr_cnt", "priority_type", "guid"])
    pred = Tbl("TASKPRED", ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_proj_id", "pred_type", "lag_hr_cnt"])
    asg = Tbl("TASKRSRC", ["taskrsrc_id", "task_id", "rsrc_id", "proj_id", "target_cost", "target_qty", "remain_cost", "remain_qty", "act_reg_cost", "act_reg_qty", "act_ot_cost", "act_ot_qty"])
    cur = Tbl("CURRTYPE", ["curr_id", "curr_short_name", "decimal_symbol", "digit_group_symbol"])
    cur.rows.append(["1", "USD", ".", ","])

    next_wbs = id_base + 100
    next_task = id_base + 1000
    next_pred = id_base + 40000
    next_asg = id_base + 80000
    per = max(1, tasks // projects)
    for pi in range(projects):
        pid = id_base + 10 + pi
        start = datetime(2026, 1, 5, 7, 0) + timedelta(days=7 * pi)
        clock = Clock(start)
        root = next_wbs
        next_wbs += 1
        wbs.rows.append([str(root), str(pid), "", f"XL{pi + 1}", f"Invented project {pi + 1}", "Y", "0", f"g{root}"])
        leaves: list[int] = []
        for a in range(rng.randint(3, 5)):
            wa = next_wbs
            next_wbs += 1
            wbs.rows.append([str(wa), str(pid), str(root), f"XL{pi + 1}.{a + 1}", f"Area {a + 1}", "N", str(a), f"g{wa}"])
            for b in range(rng.randint(3, 5)):
                wb = next_wbs
                next_wbs += 1
                wbs.rows.append([str(wb), str(pid), str(wa), f"XL{pi + 1}.{a + 1}.{b + 1}", f"Package {a + 1}.{b + 1}", "N", str(b), f"g{wb}"])
                leaves.append(wb)
        ids = [next_task + i for i in range(per)]
        next_task += per
        dur = {t: (0 if rng.random() < 0.06 else rng.choice([8, 16, 24, 40, 56, 80, 120])) for t in ids}
        links: list[tuple[int, int, str, int]] = []                  # (pred task, successor task, type, lag hours)
        for i, t in enumerate(ids):
            if i == 0:
                continue
            for p in rng.sample(ids[max(0, i - 25):i], k=min(i, rng.choice([1, 1, 2, 3]), len(ids[max(0, i - 25):i]))):
                links.append((p, t, rng.choices(PRED_CODES, weights=[70, 15, 10, 5])[0], rng.choice([0, 0, 0, 8, 16, -8])))
        es: dict[int, float] = {}
        ef: dict[int, float] = {}
        incoming: dict[int, list[tuple[int, str, int]]] = {}
        for p, t, ty, lag in links:
            incoming.setdefault(t, []).append((p, ty, lag))
        for t in ids:                                                 # ids are in topological order by construction
            lo = 0.0
            for p, ty, lag in incoming.get(t, []):
                if ty == "PR_FS":
                    lo = max(lo, ef[p] + lag)
                elif ty == "PR_SS":
                    lo = max(lo, es[p] + lag)
                elif ty == "PR_FF":
                    lo = max(lo, ef[p] + lag - dur[t])
                else:
                    lo = max(lo, es[p] + lag - dur[t])
            es[t] = max(0.0, lo)
            ef[t] = es[t] + dur[t]
        finish = max(ef.values())
        succ: dict[int, list[tuple[int, str, int]]] = {}
        for p, t, ty, lag in links:
            succ.setdefault(p, []).append((t, ty, lag))
        ls: dict[int, float] = {}
        lf: dict[int, float] = {}
        for t in reversed(ids):
            hi = finish
            for s, ty, lag in succ.get(t, []):
                if ty == "PR_FS":
                    hi = min(hi, ls[s] - lag)
                elif ty == "PR_SS":
                    hi = min(hi, ls[s] - lag + dur[t])
                elif ty == "PR_FF":
                    hi = min(hi, lf[s] - lag)
                else:
                    hi = min(hi, lf[s] - lag + dur[t])
            lf[t] = hi
            ls[t] = hi - dur[t]
        proj.rows.append([str(pid), f"XL-{pi + 1:02d}", _ts(clock.at(0)), _ts(clock.at(finish)), _ts(datetime(2026, 11, 6, 6, 0)), cal_ids[0], f"p{pid}"])
        for i, t in enumerate(ids):
            started = es[t] < progress_hours and rng.random() < 0.8
            done = started and ef[t] < progress_hours * 0.6
            status = "COMPLETED" if done else "IN_PROGRESS" if started else "NOT_STARTED"
            tf = ls[t] - es[t]
            fs_gaps = [es[s] - ef[t] - lag for s, ty, lag in succ.get(t, []) if ty == "PR_FS"]
            ff = min(fs_gaps) if fs_gaps else finish - ef[t]
            task.rows.append([
                str(t), str(pid), str(rng.choice(leaves)), cal_ids[0], f"A{1000 + i * 10}", f"Activity {i + 1} {rng.choice(['pour', 'frame', 'inspect', 'fit out', 'commission', 'schütten'])}",
                "MILESTONE" if dur[t] == 0 else "TASK", status, str(dur[t]), "0" if done else str(dur[t]),
                _ts(clock.at(es[t])), _ts(clock.at(ef[t])), _ts(clock.at(es[t])), _ts(clock.at(ef[t])), _ts(clock.at(ls[t])), _ts(clock.at(lf[t])),
                _ts(clock.at(es[t])) if started else "", _ts(clock.at(ef[t])) if done else "", f"{tf:g}", f"{max(0.0, ff):g}", "NORMAL", f"t{t}"])
        for p, t, ty, lag in links:
            pred.rows.append([str(next_pred), str(t), str(p), str(pid), str(pid), ty, str(lag)])
            next_pred += 1
        for t in ids:
            if rng.random() < 0.4:
                for rid in rng.sample(sorted(rates), k=rng.choice([1, 1, 2])):
                    qty = Decimal(dur[t]) * Decimal(rng.choice([1, 1, 2]))
                    cost = qty * rates[rid]
                    done_part = Decimal("0.5") if es[t] < progress_hours else Decimal(0)
                    asg.rows.append([str(next_asg), str(t), rid, str(pid), _money(cost), str(qty), _money(cost * (1 - done_part)), str(qty * (1 - done_part)),
                                     _money(cost * done_part), str(qty * done_part), "0.00", "0"])
                    next_asg += 1
    if not asg.rows:                                            # a tiny schedule can draw no assignment at all; keep one so every table has a row
        first = task.rows[0]
        asg.rows.append([str(next_asg), first[0], next(iter(rates)), first[1], "10.00", "8", "10.00", "8", "0.00", "0", "0.00", "0"])
    return [cal, rs, cur, proj, wbs, task, pred, asg]


def plant(tables: list[Tbl], kinds: list[str], seed: int) -> list[Planted]:
    """Apply defects in place. Returns what was planted so a test can look for it."""
    rng = random.Random(seed * 7919 + 1)
    by = {t.name: t for t in tables}
    out: list[Planted] = []
    for kind in kinds:
        text, expect, fatal = DEFECTS[kind]
        table = key = None
        T, P, A = by["TASK"], by["TASKPRED"], by["TASKRSRC"]
        ti = {f: i for i, f in enumerate(T.fields)}
        pi = {f: i for i, f in enumerate(P.fields)}
        if kind == "missing_pred_target":
            r = rng.choice(P.rows)
            r[pi["pred_task_id"]] = "999999"
            table, key = "TASKPRED", r[0]
        elif kind == "cycle":
            e = rng.choice(P.rows)                                                   # edge: pred a precedes task b; the new edge makes b precede a
            new_id = str(max(int(r[0]) for r in P.rows) + 1)
            P.rows.append([new_id, e[pi["pred_task_id"]], e[pi["task_id"]], e[pi["proj_id"]], e[pi["proj_id"]], "PR_FS", "0"])
            table, key = "TASKPRED", new_id
        elif kind == "finish_before_start":
            r = rng.choice(T.rows)
            r[ti["target_start_date"]], r[ti["target_end_date"]] = "2026-09-01 07:00", "2026-03-01 07:00"
            table, key = "TASK", r[0]
        elif kind == "dup_task_code":
            a, b = rng.sample([r for r in T.rows if r[ti["proj_id"]] == T.rows[0][ti["proj_id"]]], 2)
            b[ti["task_code"]] = a[ti["task_code"]]
            table, key = "TASK", b[0]
        elif kind == "bad_number":
            r = rng.choice(T.rows)
            r[ti["total_float_hr_cnt"]] = "1.2.3"
            table, key = "TASK", r[0]
        elif kind == "bad_date":
            r = rng.choice(T.rows)
            r[ti["early_start_date"]] = "2026-13-45 99:99"
            table, key = "TASK", r[0]
        elif kind == "wrong_field_count":
            r = rng.choice(T.rows)
            del r[-1]
            table, key = "TASK", r[0]
        elif kind == "orphan_assignment":
            r = rng.choice(A.rows)
            r[1] = "888888"
            table, key = "TASKRSRC", r[0]
        elif kind == "missing_calendar":
            r = rng.choice(T.rows)
            r[ti["clndr_id"]] = "777777"
            table, key = "TASK", r[0]
        elif kind == "unknown_pred_type":
            r = rng.choice(P.rows)
            r[pi["pred_type"]] = "PR_XX"
            table, key = "TASKPRED", r[0]
        elif kind == "out_of_range":
            r = rng.choice(T.rows)
            r[ti["target_drtn_hr_cnt"]] = "-16"
            table, key = "TASK", r[0]
        elif kind == "stray_line":
            r = rng.choice(T.rows)
            r.append("\x00STRAY")                                                   # marker; the writer turns this into a bare line
            table, key = "TASK", r[0]
        elif kind == "dup_key":
            a, b = rng.sample(T.rows, 2)
            b[0] = a[0]
            b[ti["task_code"]] = b[ti["task_code"]] + "X"
            table, key = "TASK", a[0]
        out.append(Planted(kind, table, key, expect, fatal))
    return out


def render(tables: list[Tbl], planted: list[Planted] | None = None, *, header_date: str = EXPORT_DATE) -> bytes:
    """Text of the file as cp1252 bytes with CRLF line ends. Fills in the line number of each planted defect."""
    lines = ["\t".join(["ERMHDR", "99.0", header_date, "Project", "sample", "generator", "invented", "none", "USD"])]
    where: dict[tuple[str, str], int] = {}
    for t in tables:
        lines.append(f"%T\t{t.name}")
        lines.append("\t".join(["%F"] + t.fields))
        for r in t.rows:
            if r and r[-1] == "\x00STRAY":
                lines.append("this line has no marker")
                where[(t.name, r[0])] = len(lines)
                continue
            lines.append("\t".join(["%R"] + r))
            where[(t.name, r[0])] = len(lines)
    lines.append("%E")
    for p in planted or []:
        if p.table and (p.table, p.key or "") in where:
            p.line = where[(p.table, p.key or "")]
    return ("\r\n".join(lines) + "\r\n").encode("cp1252")


def make(seed: int = 1, tasks: int = 300, projects: int = 1, defects: list[str] | None = None, id_base: int = 1000, progress_hours: float = 40.0) -> tuple[bytes, list[Planted]]:
    tables = build_tables(seed, tasks, projects, id_base, progress_hours)
    planted = plant(tables, list(defects or []), seed)
    data = render(tables, planted)
    for p in planted:
        if p.kind == "truncated":
            data = data[: int(len(data) * 0.6)]
            p.line = data.count(b"\n") + 1
        elif p.kind == "missing_end":
            data = data.replace(b"%E\r\n", b"")
            p.line = data.count(b"\n") + 1
    return data, planted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tasks", type=int, default=300)
    ap.add_argument("--projects", type=int, default=1)
    ap.add_argument("--progress-hours", type=float, default=40.0, help="working hours of the plan that have been worked at the data date (default 40: almost nothing started)")
    ap.add_argument("--defect", action="append", default=[], choices=sorted(DEFECTS))
    ap.add_argument("--out", default="out/sample.xer")
    ap.add_argument("--manifest", help="write the planted defects as JSON")
    ap.add_argument("--list-defects", action="store_true")
    a = ap.parse_args(argv)
    if a.list_defects:
        for k, (text, expect, fatal) in DEFECTS.items():
            print(f"{k:22} {text}  -> {', '.join(expect)}{'  (file rejected)' if fatal else ''}")
        return 0
    data, planted = make(a.seed, a.tasks, a.projects, a.defect, progress_hours=a.progress_hours)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {out} ({len(data):,} bytes, {data.count(b'%R')} rows, {len(planted)} planted defect(s))")
    for p in planted:
        print(f"  planted {p.kind}: {p.table} {p.key} line {p.line} -> expect {', '.join(p.expect)}")
    if a.manifest:
        Path(a.manifest).write_text(json.dumps([p.__dict__ for p in planted], indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
