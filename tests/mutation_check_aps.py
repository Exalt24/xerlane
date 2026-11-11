# LIFESPAN: permanent (the repo's own mutation check for the APS connector, run by hand and in CI)
"""Mutation check for xerlane/aps.py: remove or flip one guard at a time and require a failing test for each. Same method as tests/mutation_check.py, kept in its own
file so it does not depend on that one's internals.

The mutants are derived from the syntax tree, so a renamed variable or a new branch changes the mutant set instead of breaking a hand-written table:

  comparison operators flipped (== != < <= > >= is is not in not in), `and` and `or` swapped, a `not` removed, an if / while / assert / conditional test negated,
  an `if` that ends in raise / return / continue / break replaced by `pass` (the guard removed), a call statement replaced by `pass`, `return x` turned into `return None`,
  integer constants moved by one, True and False swapped, + and - swapped, and the upper case reason and kind codes (DUP_OBJECT_ID, OBJECT_ADDED ...) emptied.

Each mutant is written into a COPY of the repository and the tests run there, so the working tree is never touched. A mutant is KILLED when pytest fails (or hangs past the
timeout) and SURVIVED when the suite stays green. A survivor means a line no test pins: strengthen a test, do not delete the mutant. A line that cannot matter is marked
`# no-mutate: <why>` in the source.

    python tests/mutation_check_aps.py                    all mutants of xerlane/aps.py, 4 workers
    python tests/mutation_check_aps.py --list             show the mutants without running them
    python tests/mutation_check_aps.py --part 1/3         a third of them
    python tests/mutation_check_aps.py --ids ID,ID        rerun only these (after a test was added) and merge the outcome into the recorded results
    python tests/mutation_check_aps.py --only ID          one mutant by hand, with the diff and what pytest printed
    python tests/mutation_check_aps.py --summary          totals from the recorded results; exit 1 if anything survived or the source changed since

The tests need PostgreSQL (python tools/pg.py up). Every worker sets XERLANE_APS_TEST_DB so it gets a database of its own and workers never trample each other.
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
RESULTS = ROOT / ".mutation" / "results_aps.json"
TARGET = "xerlane/aps.py"
COPY = ("xerlane", "tools", "tests", "docs", "pytest.ini", "LICENSE", "pyproject.toml", "requirements.txt")
TESTS = ["tests/test_aps_map.py", "tests/test_aps_client.py", "tests/test_aps_db.py", "tests/test_aps_property.py"]     # the quick ones first: -x stops at the first failure
TIMEOUT = 240

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}
CODE = re.compile(r"[A-Z][A-Z_]{3,}")
EXITS = (ast.Raise, ast.Return, ast.Continue, ast.Break)


def _skipped_lines(src: str) -> set[int]:
    return {i for i, ln in enumerate(src.splitlines(), 1) if re.search(r"#\s*no-mutate", ln)}


def _protected(tree: ast.AST) -> set[int]:
    """ids of nodes that must not be mutated: docstrings, f-string parts, annotations, dict keys and subscript keys, the __main__ guard."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            out.add(id(n.body[0].value))
        if isinstance(n, ast.JoinedStr):
            out.update(id(v) for v in ast.walk(n))
        if isinstance(n, ast.arg) and n.annotation is not None:
            out.update(id(v) for v in ast.walk(n.annotation))
        if isinstance(n, ast.FunctionDef) and n.returns is not None:
            out.update(id(v) for v in ast.walk(n.returns))
        if isinstance(n, ast.AnnAssign):
            out.update(id(v) for v in ast.walk(n.annotation))
        if isinstance(n, ast.Dict):
            out.update(id(k) for k in n.keys if k is not None)
        if isinstance(n, ast.Subscript):
            out.update(id(v) for v in ast.walk(n.slice))
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
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        elif k in ("drop_guard", "drop_call"):
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
    new = _Apply(idx, kind).visit(copy.deepcopy(ast.parse(src)))
    ast.fix_missing_locations(new)
    return ast.unparse(new)


def mutants() -> list[dict]:
    src = (ROOT / TARGET).read_text(encoding="utf-8")
    base = ast.unparse(ast.parse(src))
    out = []
    for idx, kind, line in sites(src):
        if mutate(src, idx, kind) == base:
            continue                                                 # a mutant identical to the original is no mutant
        out.append({"id": f"aps:{line}:{kind}#{idx}", "idx": idx, "kind": kind, "line": line})
    return out


# ---- running ------------------------------------------------------------------------------------------------------------------------------------

def _env(worker: int) -> dict[str, str]:
    sys.path.insert(0, str(ROOT))
    from xerlane import db
    passwords = {v: db.password(v) for v in ("XERLANE_ADMIN_PASSWORD", "XERLANE_OWNER_PASSWORD", "XERLANE_APP_PASSWORD")}      # the copy has no .env of its own
    return {**os.environ, **passwords, "PYTHONHASHSEED": "0", "XERLANE_APS_TEST_DB": f"xl_mut_aps_{worker}"}


def _drop_db(env: dict[str, str]) -> None:
    import psycopg
    from xerlane import db
    with psycopg.connect(db.default_admin_dsn(), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{env["XERLANE_APS_TEST_DB"]}" WITH (FORCE)')


def _copy_repo(dest: Path) -> None:
    for name in COPY:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", "out"))
        elif src.exists():
            shutil.copy2(src, dest / name)


def _pytest_run(work: Path, env: dict[str, str]) -> tuple[bool, str]:
    """(tests passed, the tail of what pytest printed). A hang past the timeout counts as a failure."""
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS], cwd=work, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT} s"
    return p.returncode == 0, (p.stdout + p.stderr)[-2500:]


def _run_worker(worker: int, batch: list[dict]) -> list[dict]:
    env = _env(worker)
    results: list[dict] = []
    work = Path(tempfile.mkdtemp(prefix=f"xl_mutaps_{worker}_"))
    try:
        _copy_repo(work)
        target = work / TARGET
        original = target.read_text(encoding="utf-8")
        ok, tail = _pytest_run(work, env)
        assert ok, "the APS tests are not green on the unmutated source:" + chr(10) + tail
        for m in batch:
            target.write_text(mutate(original, m["idx"], m["kind"]), encoding="utf-8")
            passed, _ = _pytest_run(work, env)
            results.append({**m, "status": "SURVIVED" if passed else "KILLED"})
            print(f"  {results[-1]['status']:8} {m['id']}", flush=True)
        target.write_text(original, encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _drop_db(env)
    return results


def only(mutant_id: str) -> int:
    """Run one mutant by hand and print the diff and what pytest says, so a survivor can be understood instead of guessed at."""
    import difflib
    m = next((x for x in mutants() if x["id"] == mutant_id), None)
    if m is None:
        print(f"no mutant {mutant_id}; --list shows them")
        return 2
    env = _env(99)
    work = Path(tempfile.mkdtemp(prefix="xl_mutaps_only_"))
    try:
        _copy_repo(work)
        target = work / TARGET
        original = target.read_text(encoding="utf-8")
        target.write_text(mutate(original, m["idx"], m["kind"]), encoding="utf-8")
        diff = list(difflib.unified_diff(ast.unparse(ast.parse(original)).splitlines(), target.read_text(encoding="utf-8").splitlines(), lineterm="", n=0))
        print(chr(10).join(diff[2:12]))
        passed, tail = _pytest_run(work, env)
        print(tail)
        print("SURVIVED" if passed else "KILLED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _drop_db(env)


def _load() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}


def _save(data: dict) -> None:
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


def summary() -> int:
    data = _load()
    if not data:
        print("no recorded results; run without arguments first")
        return 1
    survivors = [r for r in data.values() if r["status"] != "KILLED"]
    print(f"aps        {len(data):4} mutants  {len(data) - len(survivors):4} killed  {len(survivors):3} survived")
    for s in survivors:
        print(f"  SURVIVOR {s['id']}")
    stale = {x["id"] for x in mutants()} != set(data)
    if stale:
        print("results are out of date (xerlane/aps.py changed since they were recorded; rerun)")
    return 1 if survivors or stale else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", help="i/n: run the i-th of n slices of the mutants")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--ids", help="run only these mutant ids (comma separated) and merge the outcome into the recorded results")
    ap.add_argument("--only", help="run just this mutant and show the diff and the pytest output")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    if a.summary:
        return summary()
    if a.only:
        return only(a.only)
    ms = mutants()
    if a.ids:
        wanted = set(a.ids.split(","))
        ms = [m for m in ms if m["id"] in wanted]
    if a.part:
        i, n = (int(x) for x in a.part.split("/"))
        ms = [m for k, m in enumerate(ms) if k % n == i - 1]
    if a.list:
        for m in ms:
            print(m["id"])
        print(f"{len(ms)} mutants")
        return 0
    print(f"aps: {len(ms)} mutants, {a.jobs} workers")
    batches = [ms[w::a.jobs] for w in range(a.jobs)]
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(_run_worker, w, b) for w, b in enumerate(batches) if b]
        for f in futs:
            results += f.result()
    current = {m["id"] for m in mutants()}
    rec = {k: v for k, v in _load().items() if k in current}          # drop what an older version of the source recorded
    for r in results:
        rec[r["id"]] = {k: r[k] for k in ("id", "kind", "line", "status")}
    _save(rec)
    survived = [r for r in results if r["status"] != "KILLED"]
    print(f"aps: {len(results)} run, {len(results) - len(survived)} killed, {len(survived)} survived")
    src = (ROOT / TARGET).read_text(encoding="utf-8").splitlines()
    for r in survived:
        print(f"  SURVIVED {r['id']}: {src[r['line'] - 1].strip()[:110]}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
