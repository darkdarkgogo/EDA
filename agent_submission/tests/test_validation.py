from dataclasses import replace
from pathlib import Path

import pytest

from scan_agent.artifacts import create_run
from scan_agent.diagnostics import DiagnosticSummary, parse_tool_log
from scan_agent.inputs import hash_protected_inputs
from scan_agent.runner import ToolResult
from scan_agent.validation import validate_run


def _case(tmp_path, log="Total violations: 0\n", *, insertion=True, structure=True, **overrides):
    paths = create_run(tmp_path / "output", 1)
    if insertion:
        log = "[INFO] insert_scan completed successfully\n" + log
    paths.log.write_text(log, encoding="utf-8")
    (paths.deliverables / "post_scan.v").write_text("module top(); endmodule\n", encoding="utf-8")
    if structure and not overrides.get("chain_constraints"):
        (paths.reports / "scan.rpt").write_text("Number of scan chains: 1\nMaximum chain length: 1\n", encoding="utf-8")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "pre_scan.v").write_text("module top(); endmodule\n", encoding="utf-8")
    requirements = {"required_outputs": ["post_scan.v"], "allowed_drc": [], "input_dir": input_dir, "protected_hashes": hash_protected_inputs(input_dir), **overrides}
    result = ToolResult(0, False, 0.1, paths.log, ("deliverables/post_scan.v",))
    return requirements, result, parse_tool_log(log), paths


def test_real_complete_evidence_passes(tmp_path):
    report = validate_run(*_case(tmp_path))
    assert report.passed is True
    assert report.failures == ()
    assert report.input_integrity is True
    assert report.evidence[0].source.endswith("R1.log")
    assert any(item.source_line == "Total violations: 0" for item in report.evidence)


@pytest.mark.parametrize("insertion,structure,missing", [
    (False, True, "insertion_completion"),
    (True, False, "positive_scan_structure"),
    (False, False, "insertion_completion"),
])
def test_zero_drc_and_arbitrary_netlist_cannot_replace_positive_scan_evidence(
    tmp_path, insertion, structure, missing,
):
    report = validate_run(*_case(tmp_path, insertion=insertion, structure=structure))
    assert report.passed is False
    assert missing in report.missing_evidence


def test_exit_zero_without_required_netlist_is_not_success(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    (paths.deliverables / "post_scan.v").unlink()
    report = validate_run(requirements, result, diagnostics, paths)
    assert report.passed is False
    assert "post_scan.v" in report.missing_artifacts


def test_empty_required_artifact_is_missing(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    (paths.deliverables / "post_scan.v").write_bytes(b"")
    assert validate_run(requirements, result, diagnostics, paths).missing_artifacts == ("post_scan.v",)


def test_required_report_can_be_in_reports_directory(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, required_outputs=["post_scan.v", "scan.rpt"])
    (paths.reports / "scan.rpt").write_text("Number of scan chains: 4\n", encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed


def test_allowed_tie_rule_does_not_hide_other_drc(tmp_path):
    case = _case(tmp_path, "DFTR-TIE0 x12\nDFTR9 x1\nTotal violations: 13\n", allowed_drc=["DFTR-TIE0"])
    report = validate_run(*case)
    assert report.passed is False
    assert [item.rule for item in report.disallowed_drc] == ["DFTR9"]


def test_all_explicit_allowed_counts_pass(tmp_path):
    assert validate_run(*_case(tmp_path, "DFTR-TIE0 x12\nTotal violations: 12\n", allowed_drc=["DFTR-TIE0"])).passed


def test_parent_rule_permission_applies_to_subrule_but_not_other_family(tmp_path):
    assert validate_run(*_case(tmp_path, "DFTR9-1 x2\nTotal violations: 2\n", allowed_drc=["DFTR9"])).passed


@pytest.mark.parametrize("log", [
    "[INFO] finished\n",
    "Total violations: 2\n",
    "DFTR-TIE0 x1\nTotal violations: 2\n",
    "[DFTDRC-4001] DFTR-TIE0\nTotal violations: 1\n",
    "DFTR-TIE0 x1\nTotal violations: 0\n",
])
def test_missing_unknown_or_inconsistent_drc_fails(tmp_path, log):
    assert not validate_run(*_case(tmp_path, log, allowed_drc=["DFTR-TIE0"])).passed


def test_counted_rules_without_total_are_explicit_evidence(tmp_path):
    assert validate_run(*_case(tmp_path, "DFTR-TIE0 x12\n", allowed_drc=["DFTR-TIE0"])).passed


def test_latest_complete_drc_snapshot_can_show_resolved_previous_violations(tmp_path):
    report = validate_run(*_case(tmp_path, "DFTR9 x3\nTotal violations: 3\nTotal violations: 0\n"))
    assert report.passed
    assert sum(item.source.endswith("R1.log") for item in report.evidence) == 4


def test_violations_after_last_total_are_not_hidden(tmp_path):
    assert not validate_run(*_case(tmp_path, "Total violations: 0\nDFTR9 x1\n")).passed


@pytest.mark.parametrize("changes", [
    {"exit_code": 7}, {"timed_out": True}, {"exit_code": None},
    {"failure_kind": "tool_unavailable"},
])
def test_process_failures_cannot_pass(tmp_path, changes):
    requirements, result, diagnostics, paths = _case(tmp_path)
    assert not validate_run(requirements, replace(result, **changes), diagnostics, paths).passed


@pytest.mark.parametrize("error", ["[ERROR] [CMD-0074] Unknown option", "License checkout failed"])
def test_fatal_error_overrides_exit_zero(tmp_path, error):
    assert not validate_run(*_case(tmp_path, error + "\nTotal violations: 0\n")).passed


def test_report_error_and_report_drc_are_checked(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    (paths.reports / "drc.rpt").write_text("DFTR9 x1\nTotal violations: 1\n", encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    assert not report.passed
    assert report.disallowed_drc[0].rule == "DFTR9"
    assert any(item.source.endswith("drc.rpt") for item in report.evidence)


@pytest.mark.parametrize("report_text,passed", [
    ("Number of scan chains: 4\nMaximum chain length: 90\n", True),
    ("Number of scan chains: 2\nMaximum chain length: 90\n", False),
    ("Number of scan chains: 4\nMaximum chain length: 101\n", False),
    ("unrecognized report\n", False),
])
def test_chain_constraints_require_report_facts(tmp_path, report_text, passed):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"chain_count": 4, "max_length": 100})
    (paths.reports / "chain.rpt").write_text(report_text, encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed is passed


def test_chain_counts_in_log_cannot_replace_missing_report(tmp_path):
    case = _case(tmp_path, "Total violations: 0\nNumber of scan chains: 4\n", chain_constraints={"chain_count": 4})
    assert not validate_run(*case).passed


def test_max_chain_count_accepts_bounds_and_lengths_check_every_chain(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_chain_count": 5, "max_length": 100})
    (paths.reports / "chain.rpt").write_text("Number of scan chains: 4\nChain scan_1 length: 90\nChain scan_2 length: 101\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_input_hash_mutation_fails(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    (requirements["input_dir"] / "pre_scan.v").write_text("changed", encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    assert report.input_integrity is False
    assert not report.passed


def test_missing_input_hash_evidence_fails(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    del requirements["protected_hashes"]
    report = validate_run(requirements, result, diagnostics, paths)
    assert not report.passed
    assert "input_hashes" in report.missing_evidence


@pytest.mark.parametrize("name", ["../post_scan.v", "C:/post_scan.v", "/post_scan.v", ""])
def test_invalid_artifact_names_cannot_escape_run(tmp_path, name):
    assert not validate_run(*_case(tmp_path, required_outputs=[name])).passed


def test_missing_output_requirements_fail_closed(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    del requirements["required_outputs"]
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_supplied_diagnostics_must_match_real_log(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, "DFTR9 x1\nTotal violations: 1\n")
    assert not validate_run(requirements, result, parse_tool_log("Total violations: 0\n"), paths).passed


def test_missing_or_empty_log_is_not_success(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    paths.log.write_bytes(b"")
    report = validate_run(requirements, result, DiagnosticSummary(), paths)
    assert not report.passed
    assert "tool_log" in report.missing_evidence


def test_unknown_chain_requirement_cannot_be_silently_ignored(tmp_path):
    assert not validate_run(*_case(tmp_path, chain_constraints={"partitions": {"wb": 34}})).passed


def test_individual_drc_records_and_summary_are_not_double_counted(tmp_path):
    log = "[WARNING] [DFTDRC-4001] Clock of 'reg0' inactive (DFTR9-1)\n[WARNING] [DFTDRC-4001] Clock of 'reg1' inactive (DFTR9-1)\nDFTR9-1 x2\nTotal violations: 2\n"
    assert validate_run(*_case(tmp_path, log, allowed_drc=["DFTR9"])).passed


def test_partial_chain_lengths_do_not_prove_maximum(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "chain.rpt").write_text("Number of scan chains: 4\nChain scan_1 length: 90\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_all_chain_lengths_with_count_can_prove_maximum(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "chain.rpt").write_text("Number of scan chains: 2\nChain scan_1 length: 90\nChain scan_2 length: 99\n", encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed


def test_unknown_count_rule_cannot_hide_behind_explicit_zero(tmp_path):
    assert not validate_run(*_case(tmp_path, "DFTR9: unknown count\nTotal violations: 0\n")).passed


def test_required_drc_report_requires_drc_evidence(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, required_outputs=["post_scan.v", "scan_drc.rpt"])
    (paths.reports / "scan_drc.rpt").write_text("unrecognized report\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_duplicate_chain_names_cannot_prove_complete_lengths(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "chain.rpt").write_text("Number of scan chains: 2\nChain scan_1 length: 90\nChain scan_1 length: 90\n", encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    assert not report.passed
    assert "complete_chain_lengths" in report.missing_evidence


@pytest.mark.parametrize("second_chain", ["scan_1", "scan_2"])
def test_cross_file_partial_chain_rows_cannot_prove_coverage(tmp_path, second_chain):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "chain_a.rpt").write_text("Number of scan chains: 2\nChain scan_1 length: 90\n", encoding="utf-8")
    (paths.reports / "chain_b.rpt").write_text(f"Number of scan chains: 2\nChain {second_chain} length: 90\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_partial_chain_rows_across_snapshots_cannot_prove_coverage(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "chain.rpt").write_text("Number of scan chains: 2\nChain scan_1 length: 90\nNumber of scan chains: 2\nChain scan_2 length: 90\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_unknown_severity_prefixed_rule_cannot_hide_behind_zero(tmp_path):
    report = validate_run(*_case(tmp_path, "[WARNING] DFTR9: unknown count\nTotal violations: 0\n", allowed_drc=["DFTR9"]))
    assert not report.passed
    assert any("unknown DRC count" in failure for failure in report.failures)
