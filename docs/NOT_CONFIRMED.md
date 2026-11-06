# Not confirmed

What no loaded page settles. xerlane does not rely on any item here as a fact: where the code has to do something, it takes the cautious route listed beside the item, and says so out loud (a named problem or a
named finding) instead of guessing.

## Not in any Oracle page that I could load

Oracle's data map and dictionary give table and column names and what the columns mean. They do not give the file syntax. The syntax is taken from PRONOM, xer-reader and MPXJ (see `vendor-contract/SOURCE.md`).
Those three agree on: the `ERMHDR` first line, tab delimiters, `%T` `%F` `%R` `%E` markers, cp1252 text, CRLF line ends, `YYYY-MM-DD HH:MM` dates, and an empty field being null. A file in a different dialect is
rejected or quarantined, not read by guesswork.

## Not confirmed by anything I loaded

1. **Column data types.** Neither Oracle nor the readers publish a type per column. xerlane assigns each modeled column a type from its name, following the suffix convention both readers use (`_date`, `_id`, `_cnt`, `_cost`, `_qty`, `_flag`). A value that does not fit its column's type becomes a named BAD_VALUE problem and the row is quarantined.
2. **Line breaks inside text fields (notes, memos).** MPXJ treats a record whose first token is empty as a continuation and treats two 0x7F bytes in notes as a line break; xer-reader mentions neither. xerlane models no memo table. A physical line that does not start with a record marker is reported as STRAY_LINE and never merged into the row above. A 0x7F byte inside a value is kept as it is.
3. **Quote escaping.** MPXJ turns a doubled quote into a single quote; nothing else says so. xerlane keeps every value exactly as written.
4. **Decimal and digit grouping symbols.** MPXJ reads them from the default currency row of CURRTYPE. xerlane accepts only a plain `-1234.56` shape (digits, optional minus, optional dot and digits); anything else is a BAD_VALUE.
5. **Relationship codes.** Oracle names the four types in words only. The codes `PR_FS`, `PR_SS`, `PR_FF`, `PR_SF` come from MPXJ alone, which also notes real files with suffixed forms such as `PR_FF1`. xerlane accepts exactly the four; any other value quarantines the row with reason UNKNOWN_PRED_TYPE.
6. **Units of the `*_hr_cnt` fields.** The names say hours; no Oracle page states the unit. xerlane stores them as plain numbers and never converts.
7. **Time zone of dates.** Not stated anywhere. xerlane stores `timestamp without time zone` and never shifts a value.
8. **Header fields beyond version (0), export date (1), user (4) and currency (7).** xer-reader names those four; the other fields of the `ERMHDR` line are stored as text and not interpreted.
9. **Whether `proj_id` is stable between exports.** Oracle calls it "Unique ID". xerlane identifies a project as (tenant, `proj_id`); a file whose project was re-created with a new id loads as a new project.
10. **A row with fewer or more fields than the `%F` line.** MPXJ silently ignores surplus fields; no page says whether a short row is legal. xerlane treats both as WRONG_FIELD_COUNT and quarantines the row.
11. **Which tables are mandatory.** xer-reader requires CALENDAR, CURRTYPE, PROJECT, PROJWBS; Oracle says nothing. xerlane requires the header and a PROJECT table, and quarantines whatever then fails its references.
12. **The Contractor XER variant.** Oracle says it exists and differs. xerlane has no way to tell the two apart from a file and models the Primavera PM tables only.
13. **Tables xerlane does not model** (about 60, among them CURRTYPE, ACTVCODE, UDFVALUE, TASKMEMO, RSRCRATE). They are read, counted and reported as UNMODELED_TABLE with their row count; their rows are not loaded.
14. **Cost and commitment exports and third-party API data.** Oracle's XER has resource assignment costs (TASKRSRC); no loaded page describes a commitment export. xerlane loads XER only.
15. **A real P6 export.** None was available. Every file xerlane has read was written by `tools/make_sample_xer.py` from the description above.
