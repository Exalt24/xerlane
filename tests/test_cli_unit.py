"""Command line formatting helpers and argument handling, without a database."""
from __future__ import annotations

import pytest

from xerlane import cli
from xerlane.validate import Finding


def test_clip_leaves_short_lines_alone_and_ends_long_ones_with_dots_at_the_width():
    assert cli._clip("short") == "short"
    exact = "x" * cli.WIDTH
    assert cli._clip(exact) == exact
    long = "y" * (cli.WIDTH + 1)
    out = cli._clip(long)
    assert len(out) == cli.WIDTH and out.endswith("...") and out.startswith("y" * (cli.WIDTH - 3))


def test_summary_counts_each_severity():
    fs = [Finding("A", "error", None, None, None, "m"), Finding("B", "error", None, None, None, "m"), Finding("C", "warning", None, None, None, "m"), Finding("D", "info", None, None, None, "m")]
    assert cli._summary(fs) == "2 errors, 1 warnings, 1 notes"
    assert cli._summary([]) == "0 errors, 0 warnings, 0 notes"


def test_print_findings_shows_errors_and_warnings_up_to_the_limit_and_every_note(capsys):
    fs = [Finding("E", "error", "TASK", str(i), i, "bad") for i in range(5)] + [Finding("W", "warning", None, None, None, "careful"), Finding("N", "info", None, None, None, "fyi")]
    cli._print_findings(fs, limit=3)
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == ["  [error] E TASK 0 (line 0): bad", "  [error] E TASK 1 (line 1): bad", "  [error] E TASK 2 (line 2): bad"]
    assert out[3] == "  ... and 3 more" and out[4] == "  note: fyi"


def test_print_findings_prints_nothing_for_nothing(capsys):
    cli._print_findings([])
    assert capsys.readouterr().out == ""


def test_labels_cover_every_project_table_but_the_project_itself():
    assert cli.LABELS == {"PROJWBS": "wbs", "TASK": "tasks", "TASKPRED": "links", "TASKRSRC": "assigns"}


def test_the_parser_requires_a_tenant_for_load_reconcile_and_diff_but_not_validate():
    ap = cli.build_parser()
    for cmd in ("load", "reconcile", "diff"):
        with pytest.raises(SystemExit):
            ap.parse_args([cmd, "f.xer"])
    assert ap.parse_args(["validate", "f.xer"]).tenant is None
    assert ap.parse_args(["reconcile", "f.xer", "--tenant", "t"]).sample == 300
    assert ap.parse_args(["reconcile", "f.xer", "--tenant", "t", "--sample", "5"]).sample == 5
    assert ap.parse_args(["load", "f.xer", "--tenant", "t", "--label", "x"]).label == "x"


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_validate_without_a_database_reads_the_file_only(tmp_path, capsys):
    import make_sample_xer as gen
    p = tmp_path / "v.xer"
    p.write_bytes(gen.make(2, 40)[0])
    assert cli.main(["validate", str(p)]) == 0
    out = capsys.readouterr().out
    assert "before load  0 errors" in out and "after load" not in out
    bad = tmp_path / "b.xer"
    bad.write_bytes(gen.make(2, 40, defects=["cycle"])[0])
    assert cli.main(["validate", str(bad)]) == 1
    assert "CYCLE" in capsys.readouterr().out


def test_a_missing_file_is_exit_2_with_one_line_on_stderr(tmp_path, capsys):
    assert cli.main(["validate", str(tmp_path / "nope.xer")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("xerlane: FileNotFoundError") and err.count("\n") == 1
