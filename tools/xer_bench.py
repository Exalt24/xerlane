"""Time the Rust tokenizer and the Python parser on the benchmark file (seed 11, 20000 tasks, about 6 MB and 65,686 rows).

    python tools/xer_bench.py [--runs 7] [--rust-bin PATH]

Three timings are printed, and they measure different amounts of work, so read them side by side and not as a race:

  python parse_file     xerlane/parse.py end to end: line splitting, cp1252 decoding, the SHA-256 of the bytes, the structure, and the typed values (numbers, dates) of the
                        seven modeled tables. This is what the loader calls.
  rust tokenize         rust/xer-tokenizer in one process, called repeatedly on bytes already in memory (its own --bench): line splitting, decoding, structure. No hash,
                        no typing, and the file read is not counted.
  rust process          the whole binary started fresh: start up, read the file, tokenize, print the tokens as JSON. The JSON goes to a file.

Nothing here is a claim that Rust is the better loader. The Python parser does more.
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_sample_xer as gen  # noqa: E402
from xerlane.parse import parse_file  # noqa: E402

EXE = "xer-tokenizer.exe" if sys.platform == "win32" else "xer-tokenizer"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--rust-bin")
    a = ap.parse_args()
    exe = Path(a.rust_bin) if a.rust_bin else ROOT / "rust" / "xer-tokenizer" / "target" / "release" / EXE
    if not exe.is_file():
        print(f"no binary at {exe}; run: cd rust/xer-tokenizer && cargo build --release", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="xer-bench-") as tmp:
        path = Path(tmp) / "bench.xer"
        data, _ = gen.make(11, 20000)
        path.write_bytes(data)
        print(f"file: seed 11, 20000 tasks, {len(data):,} bytes ({len(data) / 1e6:.1f} MB), {data.count(b'%R'):,} rows")
        py: list[float] = []
        for _ in range(a.runs):
            t0 = time.perf_counter()
            r = parse_file(str(path))
            py.append((time.perf_counter() - t0) * 1000)
        print(f"python parse_file    {a.runs} runs: min {min(py):.0f} ms, median {statistics.median(py):.0f} ms, max {max(py):.0f} ms  ({sum(len(b.rows) for b in r.blocks):,} rows)")
        out = subprocess.run([str(exe), "--bench", str(a.runs * 3), str(path)], capture_output=True, text=True, check=True, timeout=300).stdout.strip()
        print(f"rust tokenize        {out}")
        wall: list[float] = []
        sink = Path(tmp) / "tokens.json"
        for _ in range(a.runs):
            t0 = time.perf_counter()
            with sink.open("wb") as fh:
                subprocess.run([str(exe), str(path)], stdout=fh, check=False, timeout=300)
            wall.append((time.perf_counter() - t0) * 1000)
        print(f"rust process         {a.runs} runs: min {min(wall):.0f} ms, median {statistics.median(wall):.0f} ms, max {max(wall):.0f} ms  (writes {sink.stat().st_size / 1e6:.1f} MB of JSON)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
