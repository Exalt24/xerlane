# Working rules for coding agents in this repository

Read this first. These are the rules the code and the tests already hold; a change that breaks one is not done.

## Before you say a task is done

1. `python tools/pg.py up` starts the PostgreSQL the tests need. A test that needs it fails when it is missing and never skips.
2. `python -m pytest` runs the whole Python suite. `cd web && npm run build && npm test && npm run typecheck` runs the web app.
3. Every new guard, rule or branch gets a test that fails without it. A test is first shown to pass, then the damage it should catch is planted and it must fail.
4. For a module with a mutation check, run it: `python tests/mutation_check.py --module cost` (parse, validate, load, reconcile, db, cost and evm are the modules; `tests/mutation_check_aps.py` does the Autodesk connector). A survivor means a line no test pins: add the test, never delete the mutant.
5. Run the real thing, not only mocks: the real PostgreSQL, the built web server, a real browser for anything a person clicks, and read what it shows.

## Rules the data model depends on

- A `tenant_id` is part of every key and every table has forced row level security. A new table gets both, and goes into `DATA_TABLES` in `xerlane/db.py` and into the tenant tests in `tests/test_tenant.py`.
- The tenant of a web request comes from the session row the server found, never from the request. Every route checks on the server; a page check is only for comfort.
- Money is `Decimal` in Python and `numeric` in PostgreSQL. Nothing passes through a float.
- A row that fails a rule is set aside with its reason, never dropped and never guessed. Loading the same file twice changes nothing.
- A reconcile that says "match" is only trusted after it has been seen to say "differ": keep the damage tests.

## Text and docs

- No em or en dashes in any text file (a test checks the whole repository). Plain, short sentences.
- A number in a README or doc comes from a run, with the command beside it. When behavior changes, change `docs/cost.md`, `docs/web.md` or `docs/aps.md` in the same commit, and keep the Limits sections true.
- Where documentation did not settle a question, say so in the code and in the doc; do not guess quietly.

## Commits

- One plain sentence on what changed, then a line of why. No lists, no marketing words.
