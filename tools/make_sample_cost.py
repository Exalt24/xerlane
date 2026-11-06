"""Write an invented cost export and an invented commitment export that fit a sample XER file, optionally with planted defects.

Every value is made up. The budget of a task comes from the cost of its resource assignments in the XER file, so the money and the schedule describe one project; the
actual cost follows how far the task has got, with noise, so some tasks run over.

    python tools/make_sample_cost.py --xer out/plan.xer --seed 7 --out-dir out
    python tools/make_sample_cost.py --xer out/plan.xer --defect bad_amount --defect over_invoiced --out-dir out
    python tools/make_sample_cost.py --list-defects

Writes cost.csv and commitments.csv into --out-dir.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xerlane.parse import parse_file  # noqa: E402

VENDORS = ("Harbor Steel", "Delta Concrete", "Northline Electric", "Quarry & Sons", "Brightwater Mechanical", "Oak Street Formwork")

DEFECTS = {
    "bad_amount": "a budget written with a thousands separator",
    "unknown_task_code": "a cost line that names a task code the schedule does not have",
    "unknown_project": "a cost line for a project that is not loaded",
    "dup_id": "two cost lines with the same line id",
    "wrong_field_count": "a cost line with a field missing",
    "bad_period": "a period that is not a month",
    "over_invoiced": "a commitment invoiced for more than it was committed for",
    "bad_status": "a commitment with a status that is not OPEN, CLOSED or VOID",
    "bad_date": "a commitment issued on a day that does not exist",
}


def money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def make(xer: str, seed: int, defects: list[str]) -> tuple[list[list[str]], list[list[str]]]:
    for d in defects:
        if d not in DEFECTS:
            raise SystemExit(f"unknown defect {d!r}; --list-defects shows them")
    rng = random.Random(seed)
    pr = parse_file(xer)
    tasks = [r.values for r in pr.block("TASK").rows if r.values] if pr.block("TASK") else []
    budgets: dict[int, Decimal] = {}
    for r in (pr.block("TASKRSRC").rows if pr.block("TASKRSRC") else []):
        if r.values:
            budgets[r.values["task_id"]] = budgets.get(r.values["task_id"], Decimal(0)) + Decimal(str(r.values["target_cost"] or 0))
    cost = [["line_id", "proj_id", "cost_code", "description", "task_code", "period", "budget", "forecast", "actual"]]
    codes: dict[tuple[int, str], Decimal] = {}
    line = 1
    for t in tasks:
        if t["task_id"] not in budgets and rng.random() < 0.5:
            continue
        budget = budgets.get(t["task_id"]) or Decimal(rng.randint(2, 90)) * 100
        if t["act_start_date"] is None:
            actual = Decimal(0)
        else:
            done = Decimal(1) if t["act_end_date"] is not None else (Decimal(1) - Decimal(str(t["remain_drtn_hr_cnt"] or 0)) / Decimal(str(t["target_drtn_hr_cnt"] or 1)))
            actual = budget * done * Decimal(str(round(rng.uniform(0.85, 1.25), 3)))
        code = f"{rng.choice([3, 5, 9, 16, 26]):02d}-{rng.choice([100, 200, 300, 400]):03d}"
        forecast = budget if actual <= budget else actual
        month = (t["target_start_date"] or t["early_start_date"]).strftime("%Y-%m") if (t["target_start_date"] or t["early_start_date"]) else ""
        cost.append([str(line), str(t["proj_id"]), code, f"{t['task_name']}", t["task_code"], month, money(budget), money(forecast), money(actual)])
        codes[(t["proj_id"], code)] = codes.get((t["proj_id"], code), Decimal(0)) + budget
        line += 1
    proj_ids = sorted({t["proj_id"] for t in tasks})
    for pid in proj_ids:                                                 # project overhead with no task
        cost.append([str(line), str(pid), "01-000", "Site overhead", "", "", money(Decimal(rng.randint(20, 80)) * 1000), money(Decimal(rng.randint(20, 80)) * 1000), money(Decimal(rng.randint(5, 20)) * 1000)])
        line += 1
    commit = [["commitment_id", "proj_id", "vendor", "cost_code", "description", "committed", "invoiced", "status", "issued"]]
    cid = 1
    for (pid, code), total in sorted(codes.items()):
        for _ in range(rng.randint(1, 2)):
            committed = total * Decimal(str(round(rng.uniform(0.2, 0.7), 3)))
            invoiced = committed * Decimal(str(round(rng.uniform(0, 0.9), 3)))
            issued = date(2026, 1, 5) + timedelta(days=rng.randint(0, 200))
            commit.append([str(cid), str(pid), rng.choice(VENDORS), code, f"Package {code}", money(committed), money(invoiced), rng.choice(["OPEN", "OPEN", "OPEN", "CLOSED"]), issued.isoformat()])
            cid += 1
    for d in defects:
        if d in ("bad_amount", "unknown_task_code", "unknown_project", "dup_id", "wrong_field_count", "bad_period") and len(cost) > 6:
            i = rng.randint(1, len(cost) - 2)
            if d == "bad_amount":
                cost[i][6] = "1,250.00"
            elif d == "unknown_task_code":
                cost[i][4] = "NOPE-1"
            elif d == "unknown_project":
                cost[i][1] = "99999"
            elif d == "dup_id":
                cost[i][0] = cost[i + 1][0]
            elif d == "wrong_field_count":
                cost[i] = cost[i][:-1]
            else:
                cost[i][5] = "2026-13"
        elif d in ("over_invoiced", "bad_status", "bad_date") and len(commit) > 3:
            i = rng.randint(1, len(commit) - 1)
            if d == "over_invoiced":
                commit[i][6] = money(Decimal(commit[i][5]) + 500)
            elif d == "bad_status":
                commit[i][7] = "PENDING"
            else:
                commit[i][8] = "2026-02-30"
    return cost, commit


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--xer")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--defect", action="append", default=[])
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--list-defects", action="store_true")
    args = ap.parse_args(argv)
    if args.list_defects:
        for k, v in DEFECTS.items():
            print(f"{k:20} {v}")
        return 0
    if not args.xer:
        ap.error("--xer is required")
    cost, commit = make(args.xer, args.seed, args.defect)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("cost.csv", cost), ("commitments.csv", commit)):
        with (out / name).open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, lineterminator="\r\n").writerows(rows)
        print(f"wrote {out / name} ({len(rows) - 1} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
