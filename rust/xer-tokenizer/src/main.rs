//! Command line for the XER tokenizer.
//!
//!     xer-tokenizer FILE              print the tokens as JSON
//!     xer-tokenizer --summary FILE    print counts and the problem codes
//!     xer-tokenizer --bench N FILE    tokenize N times in this process and print the timings

use std::hint::black_box;
use std::process::ExitCode;
use std::time::Instant;

use xer_tokenizer::{to_json, tokenize};

fn usage() -> ExitCode {
    eprintln!("usage: xer-tokenizer [--summary | --bench N] FILE");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mode, path) = match args.as_slice() {
        [path] => ("json", path.clone()),
        [flag, path] if flag == "--summary" => ("summary", path.clone()),
        [flag, n, path] if flag == "--bench" => match n.parse::<usize>() {
            Ok(runs) if runs > 0 => return bench(path, runs),
            _ => return usage(),
        },
        _ => return usage(),
    };
    let data = match std::fs::read(&path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            return ExitCode::from(2);
        }
    };
    let tokens = tokenize(&data);
    if mode == "summary" {
        println!(
            "bytes {} lines {} tables {} rows {} problems {} ended {}",
            tokens.size,
            tokens.line_count,
            tokens.blocks.len(),
            tokens.row_count(),
            tokens.problems.len(),
            tokens.ended
        );
        for p in &tokens.problems {
            println!("  {} line {:?} {}", p.code, p.line, p.message);
        }
    } else {
        println!("{}", to_json(&tokens));
    }
    if tokens.fatal() {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    }
}

fn bench(path: &str, runs: usize) -> ExitCode {
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            return ExitCode::from(2);
        }
    };
    let mut ms: Vec<f64> = Vec::with_capacity(runs);
    let mut rows = 0;
    for _ in 0..runs {
        let start = Instant::now();
        let tokens = tokenize(black_box(&data));
        ms.push(start.elapsed().as_secs_f64() * 1000.0);
        rows = tokens.row_count();
        black_box(tokens);
    }
    ms.sort_by(|a, b| a.total_cmp(b));
    println!(
        "tokenize {} bytes, {} rows, {} runs: min {:.1} ms, median {:.1} ms, max {:.1} ms",
        data.len(),
        rows,
        runs,
        ms[0],
        ms[runs / 2],
        ms[runs - 1]
    );
    ExitCode::SUCCESS
}
