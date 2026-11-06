"""Record docs/demo.gif from a REAL run: every command below is executed, its output is captured, and the captured text is what the frames show.

Needs the PostgreSQL container (python tools/pg.py up), Python Playwright with Chromium, and ffmpeg. Not part of the test suite.

    python tools/record_demo.py            writes docs/demo.gif and docs/demo-transcript.txt

The transcript is the same text as plain UTF-8, so the GIF can be checked against it. Frames are screenshots of a styled HTML page, a terminal look-alike that reveals
the captured lines step by step; the command lines shown after the prompt are the commands that were run (the Python path is shortened to `python`).
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT = ROOT / "out"
COLS, ROWS = 98, 27
TENANT = "demo"

DAMAGE = "update xerlane.taskrsrc set target_cost = target_cost + 0.01 where taskrsrc_id = 81000"
RESTORE = DAMAGE.replace("+ 0.01", "- 0.01")

# run first and not shown, so the first frame of the GIF already carries a result: the file the first load reads
PRE = [PY, "tools/make_sample_xer.py", "--seed", "7", "--tasks", "400", "--out", "out/plan.xer"]

# (act, shown command, argv actually run)
STEPS = [
    (1, f"python -m xerlane load out/plan.xer --tenant {TENANT}", [PY, "-m", "xerlane", "load", "out/plan.xer", "--tenant", TENANT]),
    (1, f"python -m xerlane reconcile out/plan.xer --tenant {TENANT}", [PY, "-m", "xerlane", "reconcile", "out/plan.xer", "--tenant", TENANT]),
    (2, "python tools/make_sample_xer.py --seed 7 --tasks 400 --defect cycle --defect orphan_assignment --defect unknown_pred_type --out out/plan2.xer",
     [PY, "tools/make_sample_xer.py", "--seed", "7", "--tasks", "400", "--defect", "cycle", "--defect", "orphan_assignment", "--defect", "unknown_pred_type", "--out", "out/plan2.xer"]),
    (2, f"python -m xerlane load out/plan2.xer --tenant {TENANT}", [PY, "-m", "xerlane", "load", "out/plan2.xer", "--tenant", TENANT]),
    (2, f"python -m xerlane reconcile out/plan2.xer --tenant {TENANT}", [PY, "-m", "xerlane", "reconcile", "out/plan2.xer", "--tenant", TENANT]),
    (3, f'docker exec xerlane-pg psql -U postgres -d xerlane -c "{DAMAGE}"',
     ["docker", "exec", "xerlane-pg", "psql", "-U", "postgres", "-d", "xerlane", "-c", DAMAGE]),
    (3, f"python -m xerlane reconcile out/plan2.xer --tenant {TENANT}", [PY, "-m", "xerlane", "reconcile", "out/plan2.xer", "--tenant", TENANT]),
]


def run(argv: list[str]) -> tuple[list[str], int]:
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180)
    text = (p.stdout + p.stderr).replace("\r\n", "\n").rstrip("\n")
    return (text.split("\n") if text else []), p.returncode


def colour(line: str) -> str:
    esc = html.escape(line)
    if re.search(r"RECONCILED|^result +(LOADED|UPDATED|UNCHANGED)|^project .*(LOADED|UPDATED)", line):
        return f'<span class="ok">{esc}</span>'
    if re.search(r"DIFFERENCE|REJECTED|\[error\]", line):
        return f'<span class="bad">{esc}</span>'
    if re.search(r"^quarantine|^  line \d+ ", line):
        return f'<span class="warn">{esc}</span>'
    if line.lstrip().startswith(("note:", "exit code")):
        return f'<span class="dim">{esc}</span>'
    return esc


PAGE = """<!doctype html><meta charset="utf-8"><style>
html,body{{margin:0;background:#0d1117}}
#t{{width:800px;height:{h}px;box-sizing:border-box;padding:10px 12px;overflow:hidden;background:#0d1117;color:#c9d1d9;
font:13px/{lh}px Consolas,'Cascadia Mono','DejaVu Sans Mono',monospace;white-space:pre-wrap;word-break:break-all}}
.p{{color:#7ee787}} .c{{color:#f0f6fc;font-weight:bold}} .ok{{color:#7ee787}} .bad{{color:#ff7b72;font-weight:bold}} .warn{{color:#e3b341}} .dim{{color:#8b949e}}
.bar{{color:#8b949e;margin-bottom:6px}}
</style><div id="t"><div class="bar">xerlane demo, invented data, tenant "{tenant}"</div>{body}</div>"""


def frame_html(lines: list[str]) -> str:
    lh = 18
    return PAGE.format(h=lh * (ROWS + 2) + 20, lh=lh, tenant=TENANT, body="\n".join(lines[-ROWS:]))


def main() -> int:
    from playwright.sync_api import sync_playwright
    sys.path.insert(0, str(ROOT))
    from xerlane import db
    OUT.mkdir(exist_ok=True)
    db.init_db(db.default_admin_dsn())
    db.wipe(db.default_admin_dsn())
    run(PRE)
    frames: list[tuple[str, float]] = []
    transcript: list[str] = []
    act_lines: list[str] = []
    current_act = 0
    shots = Path(tempfile.mkdtemp(prefix="xerlane_gif_"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 600})

        def snap(seconds: float) -> None:
            page.set_content(frame_html(act_lines))
            path = shots / f"f{len(frames):03d}.png"
            page.locator("#t").screenshot(path=str(path))
            frames.append((str(path), seconds))

        for act, shown, argv in STEPS:
            if act != current_act:
                act_lines, current_act = [], act
                transcript.append("")
            lines, code = run(argv)
            if code == 0 and "DIFFERENCE" in "\n".join(lines):
                code = 1
            act_lines.append(f'<span class="p">$</span> <span class="c">{html.escape(shown)}</span>')
            transcript.append(f"$ {shown}")
            if shown != STEPS[0][1]:                                 # frame 0 already carries the first command's output
                snap(0.8)
            for ln in lines:
                act_lines.append(colour(ln))
                transcript.append(ln)
            if argv[1:3] == ["-m", "xerlane"] and code:
                act_lines.append(colour(f"exit code {code}"))
                transcript.append(f"exit code {code}")
            snap(min(5.0, 1.4 + 0.13 * len(lines)))
        browser.close()
    run(["docker", "exec", "xerlane-pg", "psql", "-U", "postgres", "-d", "xerlane", "-c", RESTORE])
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "demo-transcript.txt").write_text("\n".join(transcript).lstrip("\n") + "\n", encoding="utf-8")
    listing = shots / "list.txt"
    with open(listing, "w", encoding="utf-8") as fh:
        for path, secs in frames:
            fh.write(f"file '{Path(path).as_posix()}'\nduration {secs}\n")
        fh.write(f"file '{Path(frames[-1][0]).as_posix()}'\n")
    gif = docs / "demo.gif"
    vf = "fps=8,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=48[p];[b][p]paletteuse=dither=none"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-vf", vf, "-loop", "0", str(gif)], check=True, timeout=300)
    shutil.rmtree(shots, ignore_errors=True)
    print(f"wrote {gif} ({gif.stat().st_size // 1024} KB, {len(frames)} frames) and docs/demo-transcript.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
