# Rust tokenizer

`rust/xer-tokenizer/` is a small Rust crate that reads the structure of an XER file: the `ERMHDR` line, `%T` tables, `%F` field lists, `%R` rows and the `%E` end. It exists as a second, independent reader of the same layout, so the Python parser can be checked against something that was not written from the same code.

It is not a loader. It does not know which tables xerlane models, does not turn a field into a number or a date, and does not apply any rule between tables. The Python loader in `xerlane/` stays the product. Nothing in this crate has been used on a client's file or in production.

## What it reads

The same things `xerlane/parse.py` reads before it looks at a column's type:

- the first line must be `ERMHDR` (alone or followed by a tab), or the file is `NOT_XER` and reading stops
- lines end in LF or CRLF; one `\r` before the `\n` is removed, any other `\r` is data
- text is cp1252; a byte that cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) is a `BAD_ENCODING` problem and becomes U+FFFD
- a table, its one field list, its rows, and the problems around them: a repeated table, a row before its field list, a row with the wrong field count, a stray line, an unknown marker, text after `%E`, a missing `%E`

Each problem has the same code, severity, line number and table as the Python parser gives it. The four problem codes that need the schema (`BAD_VALUE`, `MISSING_VALUE`, `MISSING_COLUMN`, `UNMODELED_TABLE`) are not produced here.

## Run it

```
cd rust/xer-tokenizer
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --check
cargo build --release
./target/release/xer-tokenizer ../../out/plan.xer            # the tokens as JSON
./target/release/xer-tokenizer --summary ../../out/plan.xer   # counts and problem codes
```

The toolchain is pinned in `rust-toolchain.toml` (1.99.1). The crate has no dependencies.

## The conformance check

`python tools/xer_conformance.py` builds the release binary, then reads every file of a corpus with both the Rust tokenizer and `xerlane/parse.py` and compares what they read: the size, the line count, the header fields, whether `%E` was seen, each table (name, line, field list, whether it repeats an earlier table), each row (line number and the fields exactly as written), and every structural problem in order. The Rust exit code must also agree with the Python parser's `fatal`. Any difference prints where it is and the exit code is 1.

The corpus is built each time and is the same each time:

| Group | What |
|---|---|
| generated | 35 schedules, seven seeds, one to 400 tasks, one to three projects |
| planted defect | each of the 15 defects of `tools/make_sample_xer.py` on its own with three seeds, and sets of them together |
| benchmark | the 6 MB, 65,686 row file (seed 11, 20,000 tasks) |
| edge case | 74 hand written byte strings (empty file, byte order mark, undefined cp1252 bytes in each kind of line, repeated tables, a 100,000 character field, and so on) |
| file in the repository | any `.xer` file found in the repository outside `.venv` and `node_modules` (none are committed; the two on the author's disk are included when present) |
| damaged | 300 files made by applying one to four random damages (a deleted, repeated or swapped line, a changed delimiter, a stray byte, a cut file, a byte order mark) to generated files, with a fixed seed |

The run prints how many files reach each structural problem code, and a warning if one is reached by none. All 19 are reached.

The measured run is in [rust-tokenizer-run.txt](rust-tokenizer-run.txt): 461 files, 113,727 rows, no difference. It also holds two deliberate breakages. Changing the tab delimiter in the Rust line splitter to a space made 389 of the 461 files differ and made `cargo test` fail. Removing one character class (U+001C to U+001F counted as whitespace after `%E`, as Python does) made exactly one file differ, the hand written one for it. Both were reverted and the check was green again.

The same check in CI on a Linux runner (GitHub Actions run 42334098465, job `rust`) read 459 files and 112,142 rows, the two fewer files being the two local ones, and found no difference.

What the check does not show: the two readers agree on every file in this corpus, not on every file there could be, and both were written by the same person from the same notes about the layout, so a misreading of the layout that both share is not caught. `tests/test_xer_conformance.py` tests the comparison and the corpus without the Rust binary; CI runs the full check in the `rust` job.

## Timing

Measured 2026-11-07 on the author's Windows desktop with `python tools/xer_bench.py`, on the benchmark file (6,098,032 bytes, 65,686 rows). The three rows measure different amounts of work and are not a race.

| What | Result |
|---|---|
| Python `parse_file`, 7 runs | min 629 ms, median 755 ms, max 938 ms |
| Rust tokenize in one process, 21 runs on bytes already in memory | min 37.1 ms, median 60.3 ms, max 74.6 ms |
| Rust binary started 7 times: start up, read the file, tokenize, write 9.9 MB of JSON to a file | min 78 ms, median 90 ms, max 290 ms |

The Python parser also hashes the file with SHA-256 and converts the numbers and dates of seven tables, which the Rust crate does not do, so this says how long the structural reading takes in each language and nothing about which loader is better. The runs vary on a machine that has other work on it (the Rust medians are well above its minimum). No Linux timing was taken.
