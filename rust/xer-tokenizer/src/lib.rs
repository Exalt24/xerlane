//! A tokenizer for the Primavera XER file layout.
//!
//! It reads the same structure `xerlane/parse.py` reads: an `ERMHDR` first line, then tables
//! (`%T name`), each with one field list (`%F`), any number of rows (`%R`), and one `%E` end
//! marker, all tab delimited, cp1252 encoded, with CRLF or LF line ends.
//!
//! It does the structural half of the parser and nothing else. It does not know which tables
//! xerlane models, does not convert a field to a number or a date, and does not check a rule
//! between tables. The problems it reports are the ones the Python parser reports before it
//! looks at a column's type, with the same code, severity, line and table, in the same order.
//! `tools/xer_conformance.py` compares the two over every file it can build.

use std::collections::HashSet;

/// One thing the tokenizer did not understand. The message is for people; the conformance
/// check compares `code`, `severity`, `line` and `table`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Problem {
    pub code: &'static str,
    pub severity: &'static str,
    pub message: String,
    pub line: Option<usize>,
    pub table: Option<String>,
}

/// One `%R` line. `raw` holds the fields as written, marker removed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub line: usize,
    pub raw: Vec<String>,
    pub problems: Vec<Problem>,
}

/// One `%T` table with its field list and rows. A second table of the same name is flagged
/// `duplicate` and its rows each carry a `DUPLICATE_TABLE` problem.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub name: String,
    pub line: usize,
    pub fields: Option<Vec<String>>,
    pub fields_line: Option<usize>,
    pub rows: Vec<Row>,
    pub duplicate: bool,
}

/// Everything read from one file.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Tokens {
    pub size: usize,
    pub line_count: usize,
    pub header: Option<Vec<String>>,
    pub blocks: Vec<Block>,
    pub problems: Vec<Problem>,
    pub ended: bool,
}

impl Tokens {
    /// True when the file cannot be used at all: no lines, no `ERMHDR` first line, or no `%E`.
    pub fn fatal(&self) -> bool {
        self.problems
            .iter()
            .any(|p| matches!(p.code, "EMPTY_FILE" | "NOT_XER" | "MISSING_END"))
    }

    pub fn row_count(&self) -> usize {
        self.blocks.iter().map(|b| b.rows.len()).sum()
    }
}

/// cp1252 for the bytes 0x80 to 0x9F. `None` is a byte the code page leaves undefined, which
/// Python's codec refuses and this tokenizer reports as `BAD_ENCODING`.
const CP1252_HIGH: [Option<char>; 32] = [
    Some('\u{20AC}'),
    None,
    Some('\u{201A}'),
    Some('\u{0192}'),
    Some('\u{201E}'),
    Some('\u{2026}'),
    Some('\u{2020}'),
    Some('\u{2021}'),
    Some('\u{02C6}'),
    Some('\u{2030}'),
    Some('\u{0160}'),
    Some('\u{2039}'),
    Some('\u{0152}'),
    None,
    Some('\u{017D}'),
    None,
    None,
    Some('\u{2018}'),
    Some('\u{2019}'),
    Some('\u{201C}'),
    Some('\u{201D}'),
    Some('\u{2022}'),
    Some('\u{2013}'),
    Some('\u{2014}'),
    Some('\u{02DC}'),
    Some('\u{2122}'),
    Some('\u{0161}'),
    Some('\u{203A}'),
    Some('\u{0153}'),
    None,
    Some('\u{017E}'),
    Some('\u{0178}'),
];

/// Decode one line. Returns the text and, when a byte is undefined in cp1252, the offset of the
/// first one. Each undefined byte becomes U+FFFD, like `bytes.decode("cp1252", errors="replace")`.
fn decode_cp1252(raw: &[u8]) -> (String, Option<usize>) {
    if raw.is_ascii() {
        // Safe path: ASCII is valid UTF-8 and equal in cp1252.
        return (String::from_utf8_lossy(raw).into_owned(), None);
    }
    let mut out = String::with_capacity(raw.len() + 8);
    let mut first_bad = None;
    for (i, &b) in raw.iter().enumerate() {
        let c = match b {
            0x00..=0x7F => b as char,
            0x80..=0x9F => match CP1252_HIGH[(b - 0x80) as usize] {
                Some(c) => c,
                None => {
                    if first_bad.is_none() {
                        first_bad = Some(i);
                    }
                    '\u{FFFD}'
                }
            },
            _ => b as char,
        };
        out.push(c);
    }
    (out, first_bad)
}

/// Python's `str.isspace` for one character. It differs from Rust's `char::is_whitespace` by
/// the four separators U+001C to U+001F, which Python counts as whitespace.
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1C}'..='\u{1F}')
}

fn is_blank(text: &str) -> bool {
    text.chars().all(py_isspace)
}

fn problem(
    code: &'static str,
    severity: &'static str,
    message: String,
    line: Option<usize>,
    table: Option<&str>,
) -> Problem {
    Problem {
        code,
        severity,
        message,
        line,
        table: table.map(str::to_owned),
    }
}

struct Tokenizer {
    out: Tokens,
    block: Option<usize>,
    names: HashSet<String>,
}

impl Tokenizer {
    fn new() -> Self {
        Tokenizer {
            out: Tokens::default(),
            block: None,
            names: HashSet::new(),
        }
    }

    fn block_name(&self) -> Option<String> {
        self.block.map(|i| self.out.blocks[i].name.clone())
    }

    fn file_problem(
        &mut self,
        code: &'static str,
        severity: &'static str,
        message: String,
        line: Option<usize>,
        table: Option<String>,
    ) {
        self.out.problems.push(Problem {
            code,
            severity,
            message,
            line,
            table,
        });
    }

    /// Handle one physical line. Returns false when the tokenizing must stop (a first line that
    /// is not `ERMHDR`).
    fn feed(&mut self, line_no: usize, raw: &[u8]) -> bool {
        self.out.line_count = line_no;
        let (text, bad_at) = decode_cp1252(raw);
        let mut enc_problems = Vec::new();
        if let Some(offset) = bad_at {
            enc_problems.push(problem(
                "BAD_ENCODING",
                "error",
                format!(
                    "byte 0x{:02X} at offset {} is not defined in cp1252",
                    raw[offset], offset
                ),
                Some(line_no),
                None,
            ));
        }
        let row_in_block = text.starts_with("%R\t") && self.block.is_some() && !self.out.ended;
        if !row_in_block {
            self.out.problems.extend(enc_problems.iter().cloned());
        }
        if line_no == 1 {
            if !(text == "ERMHDR" || text.starts_with("ERMHDR\t")) {
                let head: String = text.chars().take(30).collect();
                self.file_problem(
                    "NOT_XER",
                    "error",
                    format!("first line must start with ERMHDR, found {head:?}"),
                    Some(1),
                    None,
                );
                return false;
            }
            self.out.header = Some(text.split('\t').skip(1).map(str::to_owned).collect());
            return true;
        }
        if self.out.ended {
            if !is_blank(&text) {
                let head: String = text.chars().take(40).collect();
                self.file_problem(
                    "TRAILING_DATA",
                    "error",
                    format!("data after the %E end marker: {head:?}"),
                    Some(line_no),
                    None,
                );
            } else {
                self.file_problem(
                    "BLANK_LINE",
                    "info",
                    "blank line after the end marker".to_owned(),
                    Some(line_no),
                    None,
                );
            }
            return true;
        }
        let parts: Vec<&str> = text.split('\t').collect();
        let marker = parts[0];
        match marker {
            "%T" => self.table_line(line_no, &parts),
            "%F" => self.fields_line(line_no, &parts),
            "%R" => self.row_line(line_no, &parts, enc_problems),
            "%E" => {
                self.out.ended = true;
                if parts.len() > 1 && parts[1..].iter().any(|p| !p.is_empty()) {
                    self.file_problem(
                        "END_LINE_FORMAT",
                        "warning",
                        "text after the %E marker on the same line".to_owned(),
                        Some(line_no),
                        None,
                    );
                }
            }
            "ERMHDR" => self.file_problem(
                "DUPLICATE_HEADER",
                "error",
                "a second ERMHDR line".to_owned(),
                Some(line_no),
                None,
            ),
            _ if text.is_empty() => {
                let table = self.block_name();
                self.file_problem(
                    "BLANK_LINE",
                    "warning",
                    "blank line".to_owned(),
                    Some(line_no),
                    table,
                );
            }
            _ if marker.starts_with('%') => {
                let table = self.block_name();
                let head: String = marker.chars().take(10).collect();
                self.file_problem(
                    "UNKNOWN_MARKER",
                    "error",
                    format!("unknown record marker {head:?}"),
                    Some(line_no),
                    table,
                );
            }
            _ => {
                let table = self.block_name();
                let head: String = text.chars().take(40).collect();
                self.file_problem(
                    "STRAY_LINE",
                    "error",
                    format!("line does not start with a record marker: {head:?}"),
                    Some(line_no),
                    table,
                );
            }
        }
        true
    }

    fn table_line(&mut self, line_no: usize, parts: &[&str]) {
        let name = if parts.len() > 1 { parts[1] } else { "" };
        if parts.len() != 2 || name.is_empty() {
            self.file_problem(
                "TABLE_LINE_FORMAT",
                "error",
                "a %T line needs exactly one table name".to_owned(),
                Some(line_no),
                None,
            );
        }
        let mut block = Block {
            name: name.to_owned(),
            line: line_no,
            fields: None,
            fields_line: None,
            rows: Vec::new(),
            duplicate: false,
        };
        if self.names.contains(name) {
            block.duplicate = true;
            self.file_problem(
                "DUPLICATE_TABLE",
                "error",
                format!("table {name} appears again; its rows are quarantined"),
                Some(line_no),
                Some(name.to_owned()),
            );
        }
        self.names.insert(name.to_owned());
        self.out.blocks.push(block);
        self.block = Some(self.out.blocks.len() - 1);
    }

    fn fields_line(&mut self, line_no: usize, parts: &[&str]) {
        let Some(idx) = self.block else {
            self.file_problem(
                "FIELDS_OUTSIDE_TABLE",
                "error",
                "%F line before any %T line".to_owned(),
                Some(line_no),
                None,
            );
            return;
        };
        let table = self.out.blocks[idx].name.clone();
        if self.out.blocks[idx].fields.is_some() {
            self.file_problem(
                "DUPLICATE_FIELD_LIST",
                "error",
                format!("second %F line for table {table}"),
                Some(line_no),
                Some(table),
            );
            return;
        }
        let names: Vec<String> = parts[1..].iter().map(|s| (*s).to_owned()).collect();
        let distinct: HashSet<&String> = names.iter().collect();
        if distinct.len() != names.len() {
            let mut dup: Vec<&String> = distinct
                .iter()
                .copied()
                .filter(|n| names.iter().filter(|m| m == n).count() > 1)
                .collect();
            dup.sort();
            let list: Vec<&str> = dup.iter().map(|s| s.as_str()).collect();
            self.file_problem(
                "DUPLICATE_FIELD",
                "error",
                format!("field names repeated: {}", list.join(", ")),
                Some(line_no),
                Some(table),
            );
        }
        let b = &mut self.out.blocks[idx];
        b.fields = Some(names);
        b.fields_line = Some(line_no);
    }

    fn row_line(&mut self, line_no: usize, parts: &[&str], enc_problems: Vec<Problem>) {
        let Some(idx) = self.block else {
            self.file_problem(
                "ROW_OUTSIDE_TABLE",
                "error",
                "%R line before any %T line".to_owned(),
                Some(line_no),
                None,
            );
            return;
        };
        let raw: Vec<String> = parts[1..].iter().map(|s| (*s).to_owned()).collect();
        let b = &mut self.out.blocks[idx];
        let mut problems = enc_problems;
        if b.duplicate {
            problems.push(problem(
                "DUPLICATE_TABLE",
                "error",
                format!("row of a repeated {} block", b.name),
                Some(line_no),
                Some(&b.name),
            ));
        }
        match &b.fields {
            None => problems.push(problem(
                "NO_FIELD_LIST",
                "error",
                format!("row before any %F line in table {}", b.name),
                Some(line_no),
                Some(&b.name),
            )),
            Some(fields) => {
                if raw.len() != fields.len() {
                    problems.push(problem(
                        "WRONG_FIELD_COUNT",
                        "error",
                        format!("{} fields, the %F line has {}", raw.len(), fields.len()),
                        Some(line_no),
                        Some(&b.name),
                    ));
                }
            }
        }
        b.rows.push(Row {
            line: line_no,
            raw,
            problems,
        });
    }

    fn finish(&mut self) {
        if self.out.line_count == 0 {
            self.file_problem(
                "EMPTY_FILE",
                "error",
                "the file has no lines".to_owned(),
                None,
                None,
            );
            return;
        }
        if !self.out.ended {
            let line = self.out.line_count;
            self.file_problem(
                "MISSING_END",
                "error",
                "no %E end marker: the file is truncated or incomplete".to_owned(),
                Some(line),
                None,
            );
        }
        let mut found = Vec::new();
        for b in &self.out.blocks {
            if b.fields.is_none() && b.rows.is_empty() {
                found.push(problem(
                    "EMPTY_TABLE_BLOCK",
                    "warning",
                    format!("table {} has no %F line", b.name),
                    Some(b.line),
                    Some(&b.name),
                ));
            }
        }
        self.out.problems.extend(found);
    }
}

/// Split into physical lines the way Python iterates a binary file: on `\n`, the last line may
/// have no `\n`, and a trailing `\n` does not start another line. One `\r` before the `\n` is
/// removed; a lone `\r` elsewhere is data.
fn lines(data: &[u8]) -> impl Iterator<Item = &[u8]> {
    let body = data.strip_suffix(b"\n").unwrap_or(data);
    let empty = data.is_empty();
    let mut it = body.split(|&b| b == b'\n');
    std::iter::from_fn(move || {
        if empty {
            return None;
        }
        it.next()
            .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
    })
}

/// Tokenize the bytes of an XER file.
pub fn tokenize(data: &[u8]) -> Tokens {
    let mut t = Tokenizer::new();
    t.out.size = data.len();
    let mut stopped = false;
    for (i, line) in lines(data).enumerate() {
        if !t.feed(i + 1, line) {
            stopped = true;
            break;
        }
    }
    if !stopped {
        t.finish();
    }
    t.out
}

fn json_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn json_opt_usize(out: &mut String, v: Option<usize>) {
    match v {
        Some(n) => out.push_str(&n.to_string()),
        None => out.push_str("null"),
    }
}

fn json_strings(out: &mut String, items: &[String]) {
    out.push('[');
    for (i, s) in items.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        json_string(out, s);
    }
    out.push(']');
}

fn json_problems(out: &mut String, items: &[Problem]) {
    out.push('[');
    for (i, p) in items.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str("{\"code\":");
        json_string(out, p.code);
        out.push_str(",\"severity\":");
        json_string(out, p.severity);
        out.push_str(",\"line\":");
        json_opt_usize(out, p.line);
        out.push_str(",\"table\":");
        match &p.table {
            Some(t) => json_string(out, t),
            None => out.push_str("null"),
        }
        out.push('}');
    }
    out.push(']');
}

/// The tokens as one JSON object. The shape is what `tools/xer_conformance.py` builds from the
/// Python parser's result.
pub fn to_json(t: &Tokens) -> String {
    let mut out = String::with_capacity(t.size * 2);
    out.push_str("{\"size\":");
    out.push_str(&t.size.to_string());
    out.push_str(",\"line_count\":");
    out.push_str(&t.line_count.to_string());
    out.push_str(",\"ended\":");
    out.push_str(if t.ended { "true" } else { "false" });
    out.push_str(",\"header\":");
    match &t.header {
        Some(h) => json_strings(&mut out, h),
        None => out.push_str("null"),
    }
    out.push_str(",\"problems\":");
    json_problems(&mut out, &t.problems);
    out.push_str(",\"blocks\":[");
    for (i, b) in t.blocks.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str("{\"name\":");
        json_string(&mut out, &b.name);
        out.push_str(",\"line\":");
        out.push_str(&b.line.to_string());
        out.push_str(",\"duplicate\":");
        out.push_str(if b.duplicate { "true" } else { "false" });
        out.push_str(",\"fields_line\":");
        json_opt_usize(&mut out, b.fields_line);
        out.push_str(",\"fields\":");
        match &b.fields {
            Some(f) => json_strings(&mut out, f),
            None => out.push_str("null"),
        }
        out.push_str(",\"rows\":[");
        for (j, r) in b.rows.iter().enumerate() {
            if j > 0 {
                out.push(',');
            }
            out.push_str("{\"line\":");
            out.push_str(&r.line.to_string());
            out.push_str(",\"raw\":");
            json_strings(&mut out, &r.raw);
            out.push_str(",\"problems\":");
            json_problems(&mut out, &r.problems);
            out.push('}');
        }
        out.push_str("]}");
    }
    out.push_str("]}");
    out
}
