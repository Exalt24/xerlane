"""Start or remove the PostgreSQL 17 container the tests and the demo use.

    python tools/pg.py up      start it (or reuse it) on port 5435 and wait until it accepts connections
    python tools/pg.py down    remove it

The first `up` writes .env (git-ignored) with three random passwords, one per database role. xerlane reads them from there or from the same environment variables.
"""
from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

NAME = "xerlane-pg"
PORT = 5435
IMAGE = "postgres:17"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
VARS = ("XERLANE_ADMIN_PASSWORD", "XERLANE_OWNER_PASSWORD", "XERLANE_APP_PASSWORD")


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip()
    return out


def ensure_env() -> dict[str, str]:
    env = _read_env()
    if all(env.get(v) for v in VARS):
        return env
    env.update({v: secrets.token_urlsafe(18) for v in VARS if not env.get(v)})
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return env


def up(timeout: float = 90.0) -> None:
    had_env = all(_read_env().get(v) for v in VARS)
    env = ensure_env()
    state = _docker("inspect", "-f", "{{.State.Running}}", NAME)
    if state.returncode == 0 and not had_env:
        _docker("rm", "-f", "-v", NAME)                  # a container whose password this checkout does not know is replaced
        state = _docker("inspect", "-f", "{{.State.Running}}", NAME)
    if state.returncode != 0:
        run = _docker("run", "-d", "--name", NAME, "-e", f"POSTGRES_PASSWORD={env['XERLANE_ADMIN_PASSWORD']}", "-e", "POSTGRES_DB=xerlane", "-p", f"{PORT}:5432", IMAGE)
        if run.returncode != 0:
            raise SystemExit(f"docker run failed: {run.stderr.strip()}")
    elif state.stdout.strip() != "true":
        _docker("start", NAME)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        # pg_isready answers before the server accepts the real database, so ask for a query result
        probe = _docker("exec", NAME, "psql", "-U", "postgres", "-d", "xerlane", "-tAc", "select 1")
        if probe.returncode == 0 and probe.stdout.strip() == "1":
            return
        time.sleep(0.3)
    raise SystemExit("postgres did not become ready in time")


def down() -> None:
    _docker("rm", "-f", "-v", NAME)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "up"
    if cmd == "up":
        up()
        print(f"{NAME} ready on localhost:{PORT}")
    elif cmd == "down":
        down()
        print(f"{NAME} removed")
    else:
        raise SystemExit(__doc__)
