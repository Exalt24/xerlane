# Cost and commitment exports, and the earned value report

The schedule says when work happens. The money says what it costs. `xerlane/cost.py` loads two kinds of CSV export into the same PostgreSQL schema as the schedule, with the same safety properties (a tenant id in every key, forced row level security, a quarantine, an idempotent reload, a reconcile), and `xerlane/evm.py` joins the two into an earned value report.

## Why the layouts are ours

No vendor publishes one layout for "a cost export". Every cost system writes its own columns. These two layouts are the smallest ones that carry what the report needs; a real export is mapped to them by a small script before it is loaded. Column order does not matter, names are matched without case, extra columns are ignored, and a required column that is missing rejects the whole file.

| Kind | Required columns | Optional columns |
|---|---|---|
| `cost` | `line_id`, `proj_id`, `cost_code`, `budget`, `forecast`, `actual` | `description`, `task_code`, `period` (YYYY-MM) |
| `commitment` | `commitment_id`, `proj_id`, `vendor`, `cost_code`, `committed`, `invoiced`, `status` (OPEN, CLOSED or VOID) | `description`, `issued` (YYYY-MM-DD) |

A cost line is tied to a schedule activity by its `task_code`. A line with no task code is allowed (site overhead has no activity).

## What a load does

1. Decode the file: UTF-8 with or without a byte order mark, else Windows-1252 (what Excel writes). NUL bytes, bytes that are neither, an empty file, a repeated column name, a missing required column or a quote that never closes reject the file and write nothing.
2. Check each row. The first rule a row breaks sets it aside in the `quarantine` table with its rule code, reason, line number and raw fields:

| Code | When |
|---|---|
| `WRONG_FIELD_COUNT` | the row has more or fewer fields than the header |
| `MISSING_VALUE` | a required text or id field is empty (a blank amount in a cost export means zero) |
| `BAD_VALUE` | an id is not a whole number from 0 to 2**63-1; an amount is not a plain number with at most 4 decimals (no thousands separator, no exponent, no `NaN`); a period is not YYYY-MM; a status is not one of the three; a date is not a real date; text is over 2048 characters or holds a control character |
| `OUT_OF_RANGE` | a commitment amount is negative (cost lines may be negative: credits and transfers) |
| `DUP_ID` | the same id appears twice for one project; every copy is set aside, because there is no way to tell which line is right |
| `UNKNOWN_PROJECT` | the project is not loaded for this tenant yet |
| `UNKNOWN_TASK_CODE` | a cost line names a task code the project does not have |
| `OVER_INVOICED` | a commitment is invoiced for more than it was committed for: a change order is missing or an amount was mistyped, and loading it would make the committed against budget view wrong |

3. In one transaction, bring the stored rows of every project in the file in line with the accepted rows (insert, update and delete only what differs), write the quarantine and one `cost_load_run` row. A file is a full snapshot of each project it names. Loading the same file again changes nothing, writes no run row and leaves the quarantine as it was.
4. A file that would leave a stored project with no accepted row at all is refused and nothing is changed, because that is what a wrong or empty export looks like; `--allow-empty` overrides it.

Quarantine rows of a cost file carry a `cost:` prefix on their file label, so an XER file and a cost file with the same name never overwrite each other's rows.

## Reconcile

`python -m xerlane reconcile-cost FILE --kind cost --tenant T` reads the file again with a second, plainer reader and the database with its own queries, and names every difference: the file's row count against stored plus set aside, every stored row against its line field by field, the sums of the stored amounts against the sums of the file rows that were not set aside, and stored rows of a named project that the file does not hold. `tests/test_cost.py` damages the stored data on purpose (a cent, a cost code, a deleted row, an extra row, a deleted quarantine row) and requires reconcile to fail with the right check name after first requiring it to pass.

## The report

`python -m xerlane report --tenant T --proj-id N [--as-of 2026-03-12] [--format text|csv|html] [--out FILE]`

| Term | Meaning here |
|---|---|
| BAC | the budget of the cost lines tied to the task |
| PV | BAC times the share of the span between target start and target end that has passed at the as-of date (a straight line on the clock, not on working time; a task of no length counts as planned from its date) |
| EV | BAC times the percent complete: 100 percent when an actual finish is recorded, 0 when no actual start is recorded, otherwise 1 minus remaining over target duration |
| AC | the actual of the cost lines tied to the task |
| CV, SV | EV minus AC, EV minus PV |
| CPI, SPI | EV over AC, EV over PV; empty, never zero or infinite, when the denominator is zero |
| EAC | BAC over CPI, the usual forecast when cost performance continues; empty when CPI is empty or zero |

Cost lines with no task code, and lines whose task code no longer matches a task, are reported on their own lines with their budget and actual; they count in the project BAC and AC and get no PV or EV, because there is no schedule to take progress from. The report also gives the cost codes with budget, actual, committed (voided commitments left out) and invoiced, flags a code committed above its budget, and lists the model elements joined to a task through the Autodesk connector's task code.

Money is `Decimal` in Python and `numeric` in PostgreSQL; nothing passes through a float. `tests/test_evm.py` checks the formulas on a four task schedule worked out on paper (every expected number is derived in the test's docstring and comments), and the web app's SQL (`web/server/utils/ev.ts`) is checked against the same numbers.

## Limits

- The cost layouts are invented for this repository. No real cost export or client system was read.
- Percent complete is duration based and PV is straight line on the calendar clock. Real earned value practice has more methods (weighted steps, units, 0/100) and this loader models none of them.
- A cost line joins a task by task code, which is unique only inside one project. Tasks are matched inside the project of the line.
- Currency is not modeled: amounts are plain numbers in one currency per tenant.
