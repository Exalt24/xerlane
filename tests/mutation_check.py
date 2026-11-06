# LIFESPAN: permanent (the repo's own mutation check, run in CI and by hand)
"""Mutation check: remove or flip one guard at a time and require a failing test for each.

The mutants are derived from the syntax tree of each module, so a renamed variable or a new branch changes the mutant set instead of breaking a hand-written table:

  comparison operators flipped (== != < <= > >= is is not in not in), `and` and `or` swapped, a `not` removed, an if / while / assert / conditional test negated,
  an `if` that ends in raise / return / continue / break replaced by `pass` (the guard removed), a call statement replaced by `pass`, `return x` turned into `return None`,
  integer constants moved by one, True and False swapped, + and - swapped, and the upper case rule codes (DUP_KEY, CYCLE ...) emptied.

Each mutant is written into a COPY of the repository and the tests run there, so the working tree is never touched. A mutant is KILLED when pytest fails (or hangs past the
timeout) and SURVIVED when the suite stays green. A survivor means a line no test pins: strengthen a test, do not delete the mutant. A line that cannot matter is marked
`# no-mutate: <why>` in the source.

    python tests/mutation_check.py --module parse                 all mutants of xerlane/parse.py
    python tests/mutation_check.py --module load --part 1/3       a third of them (for runs that must stay short)
    python tests/mutation_check.py --list --module validate       show the mutants without running them
    python tests/mutation_check.py --summary                      totals from the recorded results; exit 1 if anything survived

Stage 1 runs the tests that cover the module; a survivor is run again against the whole suite before it is reported. DB tests need PostgreSQL; each worker gets its own
database (xl_mut_N) on the same server so workers do not trample each other.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / ".mutation" / "results.json"
COPY = ("xerlane", "tools", "tests", "docs", "pytest.ini", "LICENSE", "pyproject.toml", "requirements.txt")

# module -> (pytest arguments for stage 1, timeout seconds)
TARGETS = {
    "parse": (["-m", "not db", "tests/test_parse.py", "tests/test_validate.py", "tests/test_validate_more.py", "tests/test_sample_and_docs.py", "tests/test_property.py"], 120),
    "validate": (["tests/test_validate.py", "tests/test_validate_more.py", "tests/test_validate_db.py", "tests/test_parse.py", "tests/test_sample_and_docs.py"], 120),
    "load": (["tests/test_load.py", "tests/test_load_more.py", "tests/test_defects.py", "tests/test_tenant.py"], 300),
    "reconcile": (["tests/test_reconcile_unit.py", "tests/test_reconcile.py", "tests/test_tenant.py"], 300),
    "db": (["tests/test_db.py", "tests/test_tenant.py", "tests/test_load.py", "tests/test_cli.py"], 300),
    "cli": (["tests/test_cli.py"], 300),
}
DB_MODULES = {"validate", "load", "reconcile", "db", "cli"}          # the other modules are covered by tests that never open a connection
FILES = {"parse": "xerlane/parse.py", "validate": "xerlane/validate.py", "load": "xerlane/load.py", "reconcile": "xerlane/reconcile.py", "db": "xerlane/db.py", "cli": "xerlane/cli.py"}
DEFAULT_MODULES = ("parse", "validate", "load", "reconcile", "db")           # cli.py is output formatting, covered by test_cli and test_cli_unit, and not mutated here

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}
CODE = re.compile(r"[A-Z][A-Z_]{3,}")
EXITS = (ast.Raise, ast.Return, ast.Continue, ast.Break)


def _skipped_lines(src: str) -> set[int]:
    return {i for i, ln in enumerate(src.splitlines(), 1) if re.search(r"#\s*no-mutate", ln)}


def _protected(tree: ast.AST) -> set[int]:
    """ids of nodes that must not be mutated: docstrings, f-string parts, annotations, dict keys and subscript keys, __all__."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            out.add(id(n.body[0].value))
        if isinstance(n, ast.JoinedStr):
            out.update(id(v) for v in ast.walk(n))
        if isinstance(n, ast.arg) and n.annotation is not None:
            out.update(id(v) for v in ast.walk(n.annotation))
        if isinstance(n, (ast.FunctionDef,)) and n.returns is not None:
            out.update(id(v) for v in ast.walk(n.returns))
        if isinstance(n, ast.AnnAssign):
            out.update(id(v) for v in ast.walk(n.annotation))
        if isinstance(n, ast.Dict):
            out.update(id(k) for k in n.keys if k is not None)
        if isinstance(n, ast.Subscript):
            out.update(id(v) for v in ast.walk(n.slice))
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets):
            out.update(id(v) for v in ast.walk(n))
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__":
            out.update(id(v) for v in ast.walk(n))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "field":
            out.update(id(v) for v in ast.walk(n))
    return out


def preorder(node: ast.AST):
    """Nodes in the order NodeTransformer visits them, so a site index found here names the same node when the mutation is applied."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from preorder(child)


def sites(src: str) -> list[tuple[int, str, int]]:
    """(walk index, kind, line) for every mutable site, in a stable order."""
    tree = ast.parse(src)
    skip, prot = _skipped_lines(src), _protected(tree)
    out: list[tuple[int, str, int]] = []
    for idx, n in enumerate(preorder(tree)):
        line = getattr(n, "lineno", 0)
        if line in skip or id(n) in prot:
            continue
        if isinstance(n, ast.Compare):
            out += [(idx, f"cmp{k}", line) for k, op in enumerate(n.ops) if type(op) in CMP_SWAP]
        elif isinstance(n, ast.BoolOp):
            out.append((idx, "boolop", line))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.append((idx, "not", line))
        elif isinstance(n, (ast.If, ast.While)):
            out.append((idx, "negate", line))
            if isinstance(n, ast.If) and not n.orelse and n.body and isinstance(n.body[-1], EXITS):
                out.append((idx, "drop_guard", line))
        elif isinstance(n, (ast.Assert, ast.IfExp)):
            out.append((idx, "negate", line))
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            out.append((idx, "drop_call", line))
        elif isinstance(n, ast.Return) and n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None):
            out.append((idx, "ret_none", line))
        elif isinstance(n, ast.Constant):
            if isinstance(n.value, bool):
                out.append((idx, "bool", line))
            elif isinstance(n.value, int):
                out.append((idx, "int", line))
            elif isinstance(n.value, str) and CODE.fullmatch(n.value):
                out.append((idx, "code", line))
        elif isinstance(n, ast.BinOp) and type(n.op) in ARITH_SWAP:
            out.append((idx, "arith", line))
    return out


class _Apply(ast.NodeTransformer):
    def __init__(self, target_idx: int, kind: str) -> None:
        self.target, self.kind, self.i = target_idx, kind, -1

    def visit(self, node):
        self.i += 1
        here = self.i
        node = self.generic_visit(node)
        if here != self.target:
            return node
        k = self.kind
        if k.startswith("cmp"):
            pos = int(k[3:])
            node.ops[pos] = CMP_SWAP[type(node.ops[pos])]()
        elif k == "boolop":
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        elif k == "not":
            return node.operand
        elif k == "negate":
            if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        elif k == "drop_guard" or k == "drop_call":
            return ast.copy_location(ast.Pass(), node)
        elif k == "ret_none":
            node.value = ast.Constant(value=None)
        elif k == "bool":
            node.value = not node.value
        elif k == "int":
            node.value = node.value + 1
        elif k == "code":
            node.value = ""
        elif k == "arith":
            node.op = ARITH_SWAP[type(node.op)]()
        return node


def mutate(src: str, idx: int, kind: str) -> str:
    tree = ast.parse(src)
    new = _Apply(idx, kind).visit(copy.deepcopy(tree))
    ast.fix_missing_locations(new)
    return ast.unparse(new)


def mutants(module: str) -> list[dict]:
    src = (ROOT / FILES[module]).read_text(encoding="utf-8")
    base = ast.unparse(ast.parse(src))
    out = []
    for idx, kind, line in sites(src):
        text = mutate(src, idx, kind)
        if text == base:
            continue                                                 # a mutant identical to the original is no mutant
        out.append({"id": f"{module}:{line}:{kind}#{idx}", "module": module, "idx": idx, "kind": kind, "line": line})
    return out


# ---- running ------------------------------------------------------------------------------------------------------------------------------------

def _dsns(worker: int, module: str) -> dict[str, str]:
    from xerlane import db
    name = f"xl_mut_{module}_{worker}"          # one database per module and worker, so two runs at once never drop each other's
    return {"XERLANE_ADMIN_DSN": db.retarget(db.default_admin_dsn(), dbname=name), "XERLANE_DSN": db.retarget(db.default_dsn(), dbname=name), "_NAME": name}


def _prepare_db(worker: int, module: str) -> dict[str, str]:
    import psycopg
    from xerlane import db
    env = _dsns(worker, module)
    with psycopg.connect(db.default_admin_dsn(), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{env["_NAME"]}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{env["_NAME"]}"')
    db.init_db(env["XERLANE_ADMIN_DSN"])
    return env


def _drop_db(worker: int, module: str) -> None:
    import psycopg
    from xerlane import db
    with psycopg.connect(db.default_admin_dsn(), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{_dsns(worker, module)["_NAME"]}" WITH (FORCE)')


def _copy_repo(dest: Path) -> None:
    for name in COPY:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", "out"))
        elif src.exists():
            shutil.copy2(src, dest / name)


def _pytest_run(work: Path, args: list[str], timeout: int, env: dict[str, str]) -> tuple[bool, str]:
    """(tests passed, the tail of what pytest printed). A hang past the timeout counts as a failure."""
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *args], cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout} s"
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def _pytest(work: Path, args: list[str], timeout: int, env: dict[str, str]) -> bool:
    """True when the tests PASS."""
    return _pytest_run(work, args, timeout, env)[0]


def _run_worker(worker: int, batch: list[dict], module: str, full_suite: bool) -> list[dict]:
    needs_db = module in DB_MODULES or full_suite
    env_extra = _prepare_db(worker, module) if needs_db else {}
    from xerlane import db
    passwords = {v: db.password(v) for v in ("XERLANE_ADMIN_PASSWORD", "XERLANE_OWNER_PASSWORD", "XERLANE_APP_PASSWORD")}      # the copy has no .env of its own
    env = {**os.environ, **passwords, **{k: v for k, v in env_extra.items() if not k.startswith("_")}, "PYTHONHASHSEED": "0"}
    args, timeout = TARGETS[module]
    results: list[dict] = []
    work = Path(tempfile.mkdtemp(prefix=f"xl_mut_{worker}_"))
    try:
        _copy_repo(work)
        target = work / FILES[module]
        original = target.read_text(encoding="utf-8")
        ok, tail = _pytest_run(work, args, timeout, env)
        assert ok, f"stage 1 tests are not green on the unmutated {module}:" + chr(10) + tail
        for m in batch:
            target.write_text(mutate(original, m["idx"], m["kind"]), encoding="utf-8")
            passed = _pytest(work, args, timeout, env)
            if passed and full_suite:
                passed = _pytest(work, [], 900, env)
            results.append({**m, "status": "SURVIVED" if passed else "KILLED"})
            print(f"  {results[-1]['status']:8} {m['id']}", flush=True)
        target.write_text(original, encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if needs_db:
            _drop_db(worker, module)
    return results


def only(module: str, mutant_id: str) -> int:
    """Run one mutant by hand and print what pytest says, so a survivor can be understood instead of guessed at."""
    import difflib
    m = next((x for x in mutants(module) if x["id"] == mutant_id), None)
    if m is None:
        print(f"no mutant {mutant_id} in {module}; --list shows them")
        return 2
    from xerlane import db
    env_extra = _prepare_db(99, module + "_only") if module in DB_MODULES else {}
    passwords = {v: db.password(v) for v in ("XERLANE_ADMIN_PASSWORD", "XERLANE_OWNER_PASSWORD", "XERLANE_APP_PASSWORD")}
    env = {**os.environ, **passwords, **{k: v for k, v in env_extra.items() if not k.startswith("_")}}
    work = Path(tempfile.mkdtemp(prefix="xl_mut_only_"))
    try:
        _copy_repo(work)
        target = work / FILES[module]
        original = target.read_text(encoding="utf-8")
        target.write_text(mutate(original, m["idx"], m["kind"]), encoding="utf-8")
        diff = list(difflib.unified_diff(ast.unparse(ast.parse(original)).splitlines(), target.read_text(encoding="utf-8").splitlines(), lineterm="", n=0))
        print(chr(10).join(diff[2:12]))
        args, timeout = TARGETS[module]
        p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *args], cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        print(p.stdout[-3000:])
        print("KILLED" if p.returncode else "SURVIVED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if module in DB_MODULES:
            _drop_db(99, module + "_only")


def _load() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}


def _save(data: dict) -> None:
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


def summary() -> int:
    data = _load()
    if not data:
        print("no recorded results; run with --module first")
        return 1
    total = killed = 0
    survivors = []
    for module in sorted(data):
        rs = list(data[module].values())
        k = sum(1 for r in rs if r["status"] == "KILLED")
        total, killed = total + len(rs), killed + k
        survivors += [r for r in rs if r["status"] != "KILLED"]
        print(f"{module:10} {len(rs):4} mutants  {k:4} killed  {len(rs) - k:3} survived")
    print(f"{'total':10} {total:4} mutants  {killed:4} killed  {total - killed:3} survived")
    for s in survivors:
        print(f"  SURVIVOR {s['id']}")
    stale = [m for m in FILES if m in data and {x["id"] for x in mutants(m)} != set(data[m])]
    if stale:
        print(f"results are out of date for: {', '.join(stale)} (the source changed since they were recorded; rerun)")
    return 1 if survivors or stale else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", choices=sorted(FILES))
    ap.add_argument("--part", help="i/n: run the i-th of n slices of the mutants")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-full-suite", action="store_true", help="skip the whole-suite rerun of survivors")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--ids", help="run only these mutant ids (comma separated) and merge the outcome into the recorded results; used to rerun survivors after a test was added")
    ap.add_argument("--only", help="run just the mutant with this id (as printed by --list) and show the pytest output of the stage 1 run, for working out why one survived")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    if a.summary:
        return summary()
    if not a.module:
        ap.error("--module is required")
    ms = mutants(a.module)
    if a.ids:
        wanted = set(a.ids.split(","))
        ms = [m for m in ms if m["id"] in wanted]
    if a.part:
        i, n = (int(x) for x in a.part.split("/"))
        ms = [m for k, m in enumerate(ms) if k % n == i - 1]
    if a.only:
        return only(a.module, a.only)
    if a.list:
        for m in ms:
            print(m["id"])
        print(f"{len(ms)} mutants")
        return 0
    print(f"{a.module}: {len(ms)} mutants, {a.jobs} workers")
    batches = [ms[w::a.jobs] for w in range(a.jobs)]
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(_run_worker, w, b, a.module, not a.no_full_suite) for w, b in enumerate(batches) if b]
        for f in futs:
            results += f.result()
    data = _load()
    current = {m["id"] for m in mutants(a.module)}
    rec = {k: v for k, v in data.get(a.module, {}).items() if k in current}          # drop what an older version of the source recorded
    data[a.module] = rec
    for r in results:
        rec[r["id"]] = {k: r[k] for k in ("id", "kind", "line", "status")}
    _save(data)
    survived = [r for r in results if r["status"] != "KILLED"]
    print(f"{a.module}: {len(results)} run, {len(results) - len(survived)} killed, {len(survived)} survived")
    src = (ROOT / FILES[a.module]).read_text(encoding="utf-8").splitlines()
    for r in survived:
        print(f"  SURVIVED {r['id']}: {src[r['line'] - 1].strip()[:110]}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
