# Sources

Every page below was loaded on 2026-11-06 (Oracle PDFs downloaded and text-extracted; web pages read in Chromium with `webread`). A page that failed to load is not listed as a source: the fetches of
`fileformats.archiveteam.org`, `ippm.dk` and the PyPI page of PyP6Xer failed or were walled, so nothing here relies on them.

Oracle's documents are Oracle's copyright. This folder keeps short quotations and the field-name lists of the tables xerlane models, not the PDFs. The SHA-256 of each PDF as read is recorded so a reader can
check they fetched the same file.

## What Oracle published (loaded)

| # | Document | URL | Facts taken |
|---|---|---|---|
| 1 | P6 EPPM XER Import/Export Data Map Guide (Project), Version 23, April 2023 | https://docs.oracle.com/cd/F74771_01/English/Mapping_and_Schema/xer_import_export_data_map_project/xer_import_export_data_map_project.pdf | The table names that exist in an XER (CALENDAR, PROJECT, PROJWBS, RSRC, TASK, TASKPRED, TASKRSRC and about 60 more) and, for each, the column names. TASKPRED links `task_id` (successor) to `pred_task_id` (predecessor) with `pred_type`, `lag_hr_cnt`, `proj_id`, `pred_proj_id`, key `task_pred_id`. TASK carries `task_id`, `proj_id`, `wbs_id`, `clndr_id`, `task_code`, `task_name`, `task_type`, the target, early, late and actual start and end dates, `target_drtn_hr_cnt`, `remain_drtn_hr_cnt`, `total_float_hr_cnt`, `free_float_hr_cnt`. PROJWBS carries `wbs_id`, `proj_id`, `parent_wbs_id`, `wbs_short_name`, `wbs_name`, `proj_node_flag`, `seq_num`. TASKRSRC carries `taskrsrc_id`, `task_id`, `rsrc_id`, `proj_id`, `target_cost`, `target_qty`, `remain_cost`, `remain_qty`, `act_reg_cost`, `act_reg_qty`, `act_ot_cost`, `act_ot_qty`. The guide lists names only: no types, no keys, no file syntax. Excerpt: `oracle-xer-data-map-v23-excerpt.txt`. SHA-256 `88d0bb845721567b2701b5bd7382250c3be1cd33324762eba9859cfb5fffacc2`. |
| 2 | P6 EPPM XER Import/Export Data Map Guide (Project), Version 21 | https://docs.oracle.com/cd/F37128_01/English/Mapping_and_Schema/xer_import_export_data_map_project/xer_import_export_data_map_project.pdf | Downloaded and compared by script with 1 for CALENDAR, PROJECT, PROJWBS, RSRC and TASK: no column name in version 21 is missing from version 23. TASKPRED and TASKRSRC were not compared by script. SHA-256 `7bfa0abcf0da1fe6e147df260259ede55bfb0be237c142d7fb7e5308eae608a8`. |
| 3 | P6 Professional Importing and Exporting Guide, Version 21, April 2021 | https://docs.oracle.com/cd/F37128_01/English/admin/p6_pro_importing_exporting/p6_pro_importing_exporting.pdf | XER is "the Oracle proprietary format"; one XER file can hold several projects and keeps the relationships between activities of those projects; XER does not export baselines; the Contractor XER format differs from the Primavera PM XER format. SHA-256 `eaea17f0e1b9c6ec469e0d14eedbedfe1b50eb1da8102bbc9b4aadf82ef10208`. Quotes: `oracle-quotes.txt` section 1. |
| 4 | P6 EPPM Data Dictionary, Version 26, January 2026 | https://docs.oracle.com/cd/G48897_01/English/User_Guides/p6_eppm_data_dictionary/p6_eppm_data_dictionary.pdf | The meaning of Relationship Type (finish to start, finish to finish, start to start, start to finish), Lag (positive delay or negative lead), Total Float (negative means behind schedule), Free Float, Activity ID (a unique identifier). It is a glossary of the user interface fields, not a database schema. SHA-256 `2a3dcae77d958f8dce6b9c87623541d995527d071db61791642a4e816b485521`. Quotes: `oracle-quotes.txt` section 2. |

Also loaded: https://docs.oracle.com/cd/E90748_01/English/admin/p6_pro_importing_exporting/importing_and_exporting_projects.htm (a frame shell with a table of contents only; no facts taken).

## What Oracle did not publish in any page I could load

The physical syntax of the file: the header line, the table, field, row and end markers, the delimiter, the text encoding, the date and number spelling, how a line break inside a text field is written. Oracle's pages
describe tables and columns. The syntax below therefore comes from three third-party sources that I loaded and that agree with each other where they overlap. This is the one place the parser depends on something
Oracle did not write down. `../NOT_CONFIRMED.md` lists what even these sources do not settle.

| # | Source | URL | Facts taken |
|---|---|---|---|
| 5 | PRONOM, UK National Archives, fmt/1455 | https://pronom.nationalarchives.gov.uk/fmt/1455 | An ASCII text format developed by Oracle; the file starts with the bytes `ERMHDR` and ends with the bytes `0D0A` (carriage return, line feed). |
| 6 | xer-reader 0.4.1 (GPL-3.0), `reader.py`, `table.py`, `table_data.py` | https://raw.githubusercontent.com/jjCode01/xer-reader/main/xer_reader/src/reader.py | Encoding cp1252; tab separated; a table is `%T` then the table name, then a `%F` line of field names, then `%R` lines of values, and the file ends with `%E`; dates `YYYY-MM-DD HH:MM`; an empty value is null; the header line fields (index 0 version, 1 date, 4 user, 7 currency); types are chosen by column-name suffix; key columns per table. Same host, files `table.py` and `table_data.py`. |
| 7 | MPXJ (LGPL-2.1), `XerFile.java`, `PrimaveraXERFileReader.java`, `RelationTypeHelper.java`, `NotesHelper.java` | https://raw.githubusercontent.com/joniles/mpxj/master/src/main/java/org/mpxj/primavera/XerFile.java | Tab delimiter; record markers `ERMHDR`, `%T`, `%F`, `%R`, `%E`; cp1252; three date shapes; relationship codes `PR_FS`, `PR_FF`, `PR_SS`, `PR_SF`; types looked up by field name; a doubled quote in a string stands for one quote; in notes two 0x7F bytes stand for a line break. Quotations: `third-party-layout-facts.txt`. |

No code was copied from 6 or 7. They were read for the facts above.

## Pages that came up in search but are not relied on

Aspose, docs.fileformat.com (loaded, but it only says an XER file is a text file), schedulereader.com and other explainers.
