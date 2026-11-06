"""Command line: python -m xerlane init-db | load | validate | reconcile | diff | load-cost | reconcile-cost | report | add-user | aps.

Exit codes: 0 clean; 1 the command ran and found something (quarantined rows, validation errors, a reconcile difference, a diff with changes); 2 the file was
rejected, a project failed to load, or the command could not run (bad arguments, no database, unsafe role).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg

from datetime import datetime

from . import cost, db, evm, users
from .load import LoadResult, diff_file, load_file, make_plan
from .parse import parse_file
from .reconcile import reconcile
from .validate import Finding, severity_counts, validate_db, validate_parse
from .db import tenant_tx

SHOW = 25
WIDTH = 98
LABELS = {"PROJWBS": "wbs", "TASK": "tasks", "TASKPRED": "links", "TASKRSRC": "assigns"}


def _clip(text: str) -> str:
    return text if len(text) <= WIDTH else text[: WIDTH - 3] + "..."


def _print_findings(findings: list[Finding], limit: int = SHOW) -> None:
    shown = [f for f in findings if f.severity != "info"]
    for f in shown[:limit]:
        print(_clip(f"  {f}"))
    if len(shown) > limit:
        print(f"  ... and {len(shown) - limit} more")
    for f in findings:
        if f.severity == "info":
            print(f"  note: {f.message}")


def _summary(findings: list[Finding]) -> str:
    c = severity_counts(findings)
    return f"{c.get('error', 0)} errors, {c.get('warning', 0)} warnings, {c.get('info', 0)} notes"


def cmd_init(args) -> int:
    db.init_db(args.admin_dsn or db.default_admin_dsn())
    print("schema ready: roles xerlane_owner and xerlane_app, tables with forced row level security")
    return 0


def _print_load(r: LoadResult) -> None:
    size = Path(r.path).stat().st_size if Path(r.path).exists() else 0
    print(f"file       {r.label}  {size:,} bytes  {sum(r.rows_read.values())} rows  sha256 {r.sha256[:16]}")
    print(f"checked    {_summary(r.findings)}")
    _print_findings(r.findings)
    if r.status == "rejected":
        print(f"REJECTED   nothing was written: {r.reason}")
        return
    for p in r.projects:
        counts = " ".join(f"{LABELS[k]} {v}" for k, v in p.counts.items() if k in LABELS)
        print(_clip(f"project    {p.short_name} ({p.proj_id})  {p.status.upper()}  {counts}  quarantined {p.quarantined}"))
        if p.error:
            print(f"           rolled back: {p.error}")
        if p.changes:
            kinds: dict[str, int] = {}
            for c in p.changes:
                kinds[c.kind] = kinds.get(c.kind, 0) + 1
            print("           change report: " + ", ".join(f"{k.lower()} {v}" for k, v in sorted(kinds.items())))
            for c in p.changes[:SHOW]:
                print(_clip(f"             {c}"))
            if len(p.changes) > SHOW:
                print(f"             ... and {len(p.changes) - SHOW} more")
    if r.quarantined:
        print(f"quarantine {len(r.quarantined)} row(s) set aside with their reason (select from the quarantine table):")
        for q in r.quarantined[:SHOW]:
            print(_clip(f"  line {q.line} {q.table}: {q.code}: {q.reason}"))
        if len(r.quarantined) > SHOW:
            print(f"  ... and {len(r.quarantined) - SHOW} more")
    print(f"result     {r.status.upper()} in {r.seconds:.2f} s")


def cmd_load(args) -> int:
    with db.connect(args.dsn) as conn:
        r = load_file(conn, args.tenant, args.file, args.label)
    _print_load(r)
    return r.exit_code


def cmd_validate(args) -> int:
    pr = parse_file(args.file)
    findings, _ = validate_parse(pr)
    print(f"file       {args.file}  {sum(len(b.rows) for b in pr.blocks)} rows")
    print(f"before load  {_summary(findings)}")
    _print_findings(findings)
    bad = any(f.severity == "error" for f in findings)
    if args.tenant and not pr.fatal:
        plan = make_plan(pr)
        proj_ids = [r.values["proj_id"] for r in plan.accepted["PROJECT"]]
        with db.connect(args.dsn) as conn, tenant_tx(conn, args.tenant) as cur:
            after = validate_db(cur, args.tenant, proj_ids)
        print(f"after load   {_summary(after)} (stored data of {len(proj_ids)} project(s))")
        _print_findings(after)
        bad = bad or any(f.severity == "error" for f in after)
    return 1 if bad else 0


def cmd_reconcile(args) -> int:
    with db.connect(args.dsn) as conn:
        r = reconcile(conn, args.tenant, args.file, sample=args.sample)
    print(f"file       {args.file}  sha256 {r.sha256[:16]}")
    print(f"{'table':10} {'file rows':>10} {'stored':>8} {'quarantined':>12} {'sampled':>8} {'sums':>5}")
    for ln in r.lines:
        print(f"{ln.table:10} {ln.file_rows:>10} {ln.stored:>8} {ln.quarantined:>12} {ln.sampled:>8} {ln.sums_checked:>5}")
    for n in r.notes:
        print(f"note: {n}")
    for f in r.findings[:SHOW]:
        print(f"DIFFERENCE {f}")
    if len(r.findings) > SHOW:
        print(f"... and {len(r.findings) - SHOW} more")
    print("result     " + ("RECONCILED: counts, sums and sampled rows match the file" if r.ok else f"{len(r.findings)} DIFFERENCE(S) FOUND"))
    return 0 if r.ok else 1


def cmd_diff(args) -> int:
    with db.connect(args.dsn) as conn:
        out = diff_file(conn, args.tenant, args.file)
    changed = False
    for pid, (existed, changes) in out.items():
        if not existed:
            print(f"project {pid}: not stored yet, a load would add it")
            changed = True
            continue
        print(f"project {pid}: {len(changes)} change(s)")
        for c in changes[:SHOW]:
            print(f"  {c}")
        if len(changes) > SHOW:
            print(f"  ... and {len(changes) - SHOW} more")
        changed = changed or bool(changes)
    return 1 if changed else 0


def cmd_load_cost(args) -> int:
    with db.connect(args.dsn) as conn:
        r = cost.load_file(conn, args.tenant, args.kind, args.file, args.label, allow_empty=args.allow_empty)
    print(f"file       {r.label}  {args.kind} export  sha256 {r.sha256[:16]}  {r.rows_read} rows")
    if r.status == "rejected":
        print(f"REJECTED   nothing was written: {r.reason}")
        return r.exit_code
    if r.status == "failed":
        print(f"FAILED     rolled back, nothing was written: {r.reason}")
        return r.exit_code
    for pid, c in sorted(r.projects.items()):
        print(f"project    {pid}  added {c['added']}  changed {c['changed']}  removed {c['removed']}")
    if r.quarantined:
        print(f"quarantine {len(r.quarantined)} row(s) set aside with their reason (select from the quarantine table):")
        for q in r.quarantined[:SHOW]:
            print(_clip(f"  line {q.line}: {q.code}: {q.reason}"))
        if len(r.quarantined) > SHOW:
            print(f"  ... and {len(r.quarantined) - SHOW} more")
    print(f"result     {r.status.upper()}  stored {r.stored}  set aside {len(r.quarantined)}  in {r.seconds:.2f} s")
    return r.exit_code


def cmd_reconcile_cost(args) -> int:
    with db.connect(args.dsn) as conn:
        r = cost.reconcile_file(conn, args.tenant, args.kind, args.file, args.label)
    print(f"file       {r.label}  {args.kind} export  {r.file_rows} rows, {r.stored} stored, {r.quarantined} set aside")
    for name, total in r.sums.items():
        print(f"sum        {name:10} {total:,}")
    for f in r.findings[:SHOW]:
        print(f"DIFFERENCE {f}")
    if len(r.findings) > SHOW:
        print(f"... and {len(r.findings) - SHOW} more")
    print("result     " + ("RECONCILED: counts, sums and every stored row match the file" if r.ok else f"{len(r.findings)} DIFFERENCE(S) FOUND"))
    return 0 if r.ok else 1


def cmd_report(args) -> int:
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with db.connect(args.dsn) as conn:
        rep = evm.build_report(conn, args.tenant, args.proj_id, as_of)
    body = {"text": evm.render_text, "csv": evm.render_csv, "html": evm.render_html}[args.format](rep)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8", newline="")
        print(f"wrote {args.out} ({args.format}, {len(rep.tasks)} task(s))")
    else:
        print(body)
    return 0


def cmd_add_user(args) -> int:
    password = os.environ.get("XERLANE_NEW_PASSWORD") or getpass.getpass("password: ")
    with db.connect(args.dsn) as conn:
        uid = users.add_user(conn, args.tenant, args.email, password, args.role)
    print(f"user {uid} created: {args.email.strip().lower()} ({args.role}) for tenant {args.tenant}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="xerlane", description="Parse, load, validate and reconcile Primavera XER schedules in PostgreSQL.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, tenant=True) -> None:
        p.add_argument("file")
        if tenant:
            p.add_argument("--tenant", required=True, help="client id; row level security shows this client's rows only")
        p.add_argument("--dsn", help="connection string for the app role (default: XERLANE_DSN)")

    p = sub.add_parser("init-db", help="create roles, schema and tables (administrator connection)")
    p.add_argument("--admin-dsn", help="default: XERLANE_ADMIN_DSN")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("load", help="parse, validate and load a file")
    common(p)
    p.add_argument("--label", help="name recorded for the file (default: its file name)")
    p.set_defaults(fn=cmd_load)
    p = sub.add_parser("validate", help="check a file; with --tenant also the stored data of its projects")
    common(p, tenant=False)
    p.add_argument("--tenant")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("reconcile", help="prove the stored data equals the file")
    common(p)
    p.add_argument("--sample", type=int, default=300, help="rows per table compared field by field (default 300)")
    p.set_defaults(fn=cmd_reconcile)
    p = sub.add_parser("diff", help="what loading the file would change in the stored project")
    common(p)
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("load-cost", help="load a cost or commitment export (CSV) into the tenant")
    common(p)
    p.add_argument("--kind", required=True, choices=sorted(cost.KINDS))
    p.add_argument("--label", help="name recorded for the file (default: its file name)")
    p.add_argument("--allow-empty", action="store_true", help="let a file with no accepted row for a stored project remove that project's rows")
    p.set_defaults(fn=cmd_load_cost)
    p = sub.add_parser("reconcile-cost", help="prove the stored cost or commitment rows equal the file")
    common(p)
    p.add_argument("--kind", required=True, choices=sorted(cost.KINDS))
    p.add_argument("--label")
    p.set_defaults(fn=cmd_reconcile_cost)
    p = sub.add_parser("report", help="earned value report for one project (needs the schedule and a cost export)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--proj-id", type=int, required=True)
    p.add_argument("--as-of", help="date or date and time, ISO form (default: the project's last recalculation date)")
    p.add_argument("--format", choices=("text", "csv", "html"), default="text")
    p.add_argument("--out", help="write to a file instead of the terminal")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("add-user", help="create a web app user for a tenant (password from XERLANE_NEW_PASSWORD or a prompt)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=users.ROLES, default="viewer")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_add_user)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "aps":
        from .aps import main as aps_main                 # the APS connector has its own sub commands and exit codes, see docs/aps.md
        return aps_main(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (psycopg.Error, db.UnsafeRole, OSError, ValueError, evm.ReportError, users.UserError) as exc:
        print(f"xerlane: {type(exc).__name__}: {exc}".splitlines()[0], file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
