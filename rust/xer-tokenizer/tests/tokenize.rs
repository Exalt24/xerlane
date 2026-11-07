//! Cases for the tokenizer, each written from what xerlane/parse.py does with the same bytes.

use xer_tokenizer::{to_json, tokenize};

const GOOD: &str = "ERMHDR\t99.0\t2026-11-06\tProject\r\n%T\tTASK\r\n%F\ttask_id\ttask_code\r\n%R\t1\tA\r\n%R\t2\t\r\n%E\r\n";

fn codes(data: &[u8]) -> Vec<&'static str> {
    tokenize(data).problems.iter().map(|p| p.code).collect()
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

#[test]
fn a_clean_file_has_one_table_two_rows_and_no_problem() {
    let t = tokenize(GOOD.as_bytes());
    assert!(t.problems.is_empty());
    assert!(t.ended);
    assert_eq!(t.header, Some(strings(&["99.0", "2026-11-06", "Project"])));
    assert_eq!(t.blocks.len(), 1);
    assert_eq!(t.blocks[0].name, "TASK");
    assert_eq!(t.blocks[0].fields, Some(strings(&["task_id", "task_code"])));
    assert_eq!(t.blocks[0].rows[1].raw, strings(&["2", ""]));
    assert_eq!(t.blocks[0].rows[1].line, 5);
    assert_eq!(t.row_count(), 2);
}

#[test]
fn lf_and_crlf_give_the_same_tokens() {
    let lf = GOOD.replace("\r\n", "\n");
    let a = tokenize(GOOD.as_bytes());
    let b = tokenize(lf.as_bytes());
    assert_eq!(a.blocks, b.blocks);
    assert_eq!(a.problems, b.problems);
}

#[test]
fn only_one_carriage_return_is_removed_from_a_line_end() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\tx\r\r\n%E\n");
    assert_eq!(t.blocks[0].rows[0].raw, strings(&["x\r"]));
}

#[test]
fn a_last_line_without_a_line_end_is_still_a_line() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\tx\n%E");
    assert!(t.ended);
    assert_eq!(t.line_count, 5);
}

#[test]
fn an_empty_file_is_empty_file_and_fatal() {
    let t = tokenize(b"");
    assert_eq!(codes(b""), vec!["EMPTY_FILE"]);
    assert!(t.fatal());
    assert_eq!(t.problems[0].line, None);
}

#[test]
fn a_first_line_that_is_not_ermhdr_stops_everything() {
    let data = "\u{FEFF}ERMHDR\n%T\tT\n".as_bytes();
    let t = tokenize(data);
    assert_eq!(t.problems.len(), 1);
    assert_eq!(t.problems[0].code, "NOT_XER");
    assert_eq!(t.line_count, 1);
    assert!(t.blocks.is_empty());
    assert!(t.fatal());
}

#[test]
fn a_file_with_no_end_marker_reports_the_last_line() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\tx\n");
    let p = t.problems.last().unwrap();
    assert_eq!(p.code, "MISSING_END");
    assert_eq!(p.line, Some(4));
    assert!(t.fatal());
}

#[test]
fn data_after_the_end_marker_is_trailing_data_and_blank_lines_are_info() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%E\n\nmore\n  \n");
    let got: Vec<(&str, &str, usize)> = t
        .problems
        .iter()
        .map(|p| (p.code, p.severity, p.line.unwrap()))
        .collect();
    assert_eq!(
        got,
        vec![
            ("BLANK_LINE", "info", 5),
            ("TRAILING_DATA", "error", 6),
            ("BLANK_LINE", "info", 7)
        ]
    );
}

#[test]
fn text_after_the_end_marker_on_its_line_is_a_warning() {
    let t = tokenize(b"ERMHDR\n%E\tjunk\n");
    assert_eq!(t.problems.len(), 1);
    assert_eq!(t.problems[0].code, "END_LINE_FORMAT");
    assert_eq!(t.problems[0].severity, "warning");
}

#[test]
fn a_repeated_table_name_marks_the_block_and_each_of_its_rows() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\t1\n%T\tT\n%F\ta\n%R\t2\n%E\n");
    assert!(t.blocks[1].duplicate);
    assert_eq!(
        t.problems.iter().map(|p| p.code).collect::<Vec<_>>(),
        vec!["DUPLICATE_TABLE"]
    );
    assert_eq!(t.blocks[1].rows[0].problems[0].code, "DUPLICATE_TABLE");
    assert!(t.blocks[0].rows[0].problems.is_empty());
}

#[test]
fn a_row_with_the_wrong_field_count_or_no_field_list_is_flagged_on_the_row() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%R\t1\n%F\ta\tb\n%R\t1\n%E\n");
    assert_eq!(t.blocks[0].rows[0].problems[0].code, "NO_FIELD_LIST");
    assert_eq!(t.blocks[0].rows[1].problems[0].code, "WRONG_FIELD_COUNT");
}

#[test]
fn structure_outside_a_table_is_named() {
    assert_eq!(
        codes(b"ERMHDR\n%F\ta\n%R\t1\n%E\n"),
        vec!["FIELDS_OUTSIDE_TABLE", "ROW_OUTSIDE_TABLE"]
    );
}

#[test]
fn a_bad_table_line_a_second_field_list_and_repeated_field_names_are_named() {
    let data = b"ERMHDR\n%T\n%F\ta\ta\n%F\tb\n%T\tU\tV\n%E\n";
    let t = tokenize(data);
    assert_eq!(
        codes(data),
        vec![
            "TABLE_LINE_FORMAT",
            "DUPLICATE_FIELD",
            "DUPLICATE_FIELD_LIST",
            "TABLE_LINE_FORMAT",
            "EMPTY_TABLE_BLOCK"
        ]
    );
    assert_eq!(t.problems[0].table, None);
    assert_eq!(t.problems[1].table.as_deref(), Some(""));
}

#[test]
fn unknown_marker_stray_line_and_blank_line_carry_the_current_table() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%Q\tx\nplain\n\n%E\n");
    let got: Vec<(&str, &str, Option<&str>)> = t
        .problems
        .iter()
        .map(|p| (p.code, p.severity, p.table.as_deref()))
        .collect();
    assert_eq!(
        got,
        vec![
            ("UNKNOWN_MARKER", "error", Some("T")),
            ("STRAY_LINE", "error", Some("T")),
            ("BLANK_LINE", "warning", Some("T")),
        ]
    );
}

#[test]
fn a_second_header_line_is_flagged() {
    assert_eq!(codes(b"ERMHDR\nERMHDR\t1\n%E\n"), vec!["DUPLICATE_HEADER"]);
}

#[test]
fn cp1252_text_is_decoded_and_an_undefined_byte_is_reported_once_per_line() {
    // 0x80 is the euro sign, 0xE9 is e with an acute accent, 0x81 and 0x9D are undefined.
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\t\x80\xe9\n%R\t\x81x\x9d\n%E\n");
    assert_eq!(t.blocks[0].rows[0].raw, strings(&["\u{20AC}\u{E9}"]));
    assert!(t.blocks[0].rows[0].problems.is_empty());
    assert!(t.problems.is_empty());
    let bad = &t.blocks[0].rows[1];
    assert_eq!(bad.problems.len(), 1);
    assert_eq!(bad.problems[0].code, "BAD_ENCODING");
    assert_eq!(bad.raw, strings(&["\u{FFFD}x\u{FFFD}"]));
}

#[test]
fn an_undefined_byte_outside_a_row_goes_on_the_file() {
    let t = tokenize(b"ERMHDR\tx\x81\n%E\n");
    assert_eq!(
        t.problems.iter().map(|p| p.code).collect::<Vec<_>>(),
        vec!["BAD_ENCODING"]
    );
    assert_eq!(t.problems[0].line, Some(1));
}

#[test]
fn whitespace_after_the_end_marker_follows_pythons_isspace() {
    // U+001C is whitespace to Python's str.strip and not to Rust's char::is_whitespace.
    let t = tokenize(b"ERMHDR\n%E\n\x1c\n\xa0\n");
    assert_eq!(
        t.problems.iter().map(|p| p.code).collect::<Vec<_>>(),
        vec!["BLANK_LINE", "BLANK_LINE"]
    );
}

#[test]
fn json_escapes_quotes_backslashes_and_control_characters() {
    let t = tokenize(b"ERMHDR\n%T\tT\n%F\ta\n%R\t\"q\"\\\x01\n%E\n");
    let json = to_json(&t);
    assert!(json.contains(r#""raw":["\"q\"\\\u0001"]"#), "{json}");
}
