"""Wait until PostgreSQL answers a real query with the administrator password from the environment. Used by CI after the service container starts.

    python tools/wait_for_pg.py [seconds]
"""
from __future__ import annotations

import os
import sys
import time

import psycopg


def main() -> int:
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    end = time.monotonic() + limit
    last = ""
    while time.monotonic() < end:
        try:
            with psycopg.connect(host=os.environ.get("XERLANE_PG_HOST", "localhost"), port=int(os.environ.get("XERLANE_PG_PORT", "5435")), dbname="xerlane",
                                 user="postgres", password=os.environ["XERLANE_ADMIN_PASSWORD"], connect_timeout=3) as conn:
                if conn.execute("select 1").fetchone() == (1,):
                    print("postgres answers")
                    return 0
        except psycopg.Error as exc:
            last = str(exc).splitlines()[0]
        time.sleep(0.5)
    print(f"postgres did not answer within {limit:.0f} s: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
