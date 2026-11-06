# Wiring the APS connector into the rest of xerlane

The connector was added as new files only, because other work was editing the existing modules. Nothing below is applied yet. Apply 1 and 2 TOGETHER (see the warning), the rest independently.

## 1. xerlane/db.py: create the APS tables in init_db

Read the second schema file next to the first:

```python
    ddl = resources.files("xerlane").joinpath("schema.sql").read_text(encoding="utf-8")
    aps_ddl = resources.files("xerlane").joinpath("aps_schema.sql").read_text(encoding="utf-8")
```

and run it right after the first, BEFORE the two GRANT lines (the existing `GRANT ... ON ALL TABLES` and `ON ALL SEQUENCES` then cover the new tables, the view and their sequences). The view reads `task`, so it has to come after `schema.sql`:

```python
        conn.execute(ddl)
        conn.execute(aps_ddl)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, app))
```

`xerlane.aps.init_schema(admin_dsn)` does the same thing on its own (DDL plus grants) and stays useful for a database that was initialised before this change; it is safe to run after the wiring.

## 2. xerlane/db.py: wipe the APS tables

In `DATA_TABLES`, put the four new tables first (children before parents; `wipe` truncates them all in one statement):

```python
DATA_TABLES = ("aps_quarantine", "aps_change_event", "aps_load_run", "aps_object", "quarantine", "change_event", "load_run", "taskrsrc", "taskpred", "task", "projwbs", "project", "rsrc", "calendar")
```

**Warning:** change 2 without change 1 breaks the whole existing suite, because `wipe` would TRUNCATE tables that `init_db` never created. Apply both in one edit. After both, `tests/aps_dbfix.py` still works (its own extra TRUNCATE of the APS tables is harmless).

## 3. pyproject.toml: ship the SQL file

```toml
[tool.setuptools.package-data]
xerlane = ["schema.sql", "aps_schema.sql"]
```

Without this an installed (non editable) copy has no `aps_schema.sql` and `init_db` and `init_schema` fail with a missing file.

## 4. xerlane/cli.py: an `aps` command

The module already runs as `python -m xerlane.aps sync|reconcile|init-schema`. To also reach it as `python -m xerlane aps ...`, make `main` hand the arguments over before the parser sees them:

```python
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "aps":
        from .aps import main as aps_main                 # the APS connector has its own sub commands and exit codes, see docs/aps.md
        return aps_main(argv[1:])
    args = build_parser().parse_args(argv)
    ...
```

and add `| aps` to the usage line of the module docstring. The exit codes follow the same convention (0 clean, 1 ran and found something, 2 could not run).

## 5. README.md

Under "What it does" add one short paragraph:

> **Autodesk Platform Services.** `python -m xerlane.aps sync` pulls a translated model's object properties through the Model Derivative API and loads them into the same schema: idempotent, a change log, a quarantine, row level security per tenant, and a join to the XER tasks by `task_code` with a reconcile report in both directions. It was written from the vendor documentation and tested against a stand-in server; it has not been run against a live Autodesk account. See [docs/aps.md](docs/aps.md).

Under "Tests" add the command `python -m pytest tests/test_aps_*.py` and `python tests/mutation_check_aps.py`. Keep the "never run against a live account" sentence wherever the connector is mentioned.

## 6. CI (.github/workflows/ci.yml)

`python -m pytest -q` in the existing job already collects `tests/test_aps_*.py`, because they sit in `tests/`. They create their own database on the service container, so no new service is needed.

The mutation check takes far longer than the 15 minute job limit, so give it its own job (same service container and passwords as `python-root`), on pushes to main only:

```yaml
  aps-mutation:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      XERLANE_ADMIN_PASSWORD: ci-admin-${{ github.run_id }}-${{ github.run_attempt }}
      XERLANE_OWNER_PASSWORD: ci-owner-${{ github.run_id }}-${{ github.run_attempt }}
      XERLANE_APP_PASSWORD: ci-app-${{ github.run_id }}-${{ github.run_attempt }}
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: ci-admin-${{ github.run_id }}-${{ github.run_attempt }}
          POSTGRES_DB: xerlane
        ports:
          - 5435:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d xerlane"
          --health-interval 2s
          --health-timeout 5s
          --health-retries 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.13"
          cache: pip
      - run: pip install -r requirements.txt
      - run: python tools/wait_for_pg.py
      - run: python tests/mutation_check_aps.py --jobs 2
```

The script exits 1 when any mutant survives. (Use the same pinned action SHAs as the existing job; the ones above are copied from it.)

## 7. Nothing else needs to change

`tests/conftest.py`, `tests/mutation_check.py` and the XER modules are untouched by the connector, and the APS tests do not import from them except `xerlane.db`, `xerlane.load.load_file` (one integration test) and the `xer` fixture in `conftest.py`.
