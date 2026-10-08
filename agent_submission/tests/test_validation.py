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
        log = "[INFO] insert_dft_logic completed successfully\n" + log
    paths.log.write_text(log, encoding="utf-8")
    (paths.reports / "drc.rpt").write_text("Total violations: 0\n", encoding="utf-8")
    (paths.deliverables / "post_scan.v").write_text("module top(); endmodule\n", encoding="utf-8")
    constraints = overrides.get("chain_constraints", {})
    count = constraints.get("chain_count", constraints.get("min_chain_count", 1)) if isinstance(constraints, dict) else 1
    length = constraints.get("max_length", 1) if isinstance(constraints, dict) else 1
    if structure:
        (paths.reports / "scan_signal.rpt").write_text("""Port PortProperty SignalType OffState HookupPin HookupSense AssociatedInternal Usage View ConstantValue OwnerPartition
clk user_defined clock 0 - - - - - - Default_Partition
scan_en user_defined scan_enable 0 - - - all spec - Default_Partition
""", encoding="utf-8")
        (paths.reports / "scan_cfg.rpt").write_text(
            f"ScanConfigurationParameter Value\nchain_count {count}\nmax_length {length}\nadd_lockup True\ninsert_terminal_lockup False\n",
            encoding="utf-8",
        )
        chain_lines = ["Chain Length Input Output ScanEnable Clocks Partition ChainProperty"]
        chain_lines.extend(f"I {index} 1 si{index} so{index} scan_en clk Default_Partition tool_created" for index in range(count))
        (paths.reports / "scan_chain.rpt").write_text("\n".join(chain_lines) + "\n", encoding="utf-8")
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


def test_oversized_required_config_line_fails_even_without_config_requirements(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path)
    (paths.reports / "scan_cfg.rpt").write_text("x" * 1_048_577, encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    assert not report.passed
    assert any("exceeds 1048576 characters" in item for item in report.failures)


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
    case = _case(tmp_path, log, allowed_drc=["DFTR-TIE0"])
    if log == "[INFO] finished\n":
        case[3].reports.joinpath("drc.rpt").unlink()
    assert not validate_run(*case).passed


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
    ("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\n" + "\n".join(f"I {i} 90 si so se clk Default_Partition tool_created" for i in range(4)), True),
    ("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\n" + "\n".join(f"I {i} 90 si so se clk Default_Partition tool_created" for i in range(2)), False),
    ("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\n" + "\n".join(f"I {i} 101 si so se clk Default_Partition tool_created" for i in range(4)), False),
    ("unrecognized report\n", False),
])
def test_chain_constraints_require_report_facts(tmp_path, report_text, passed):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"chain_count": 4, "max_length": 100})
    (paths.reports / "scan_chain.rpt").write_text(report_text, encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed is passed


def test_chain_counts_in_log_cannot_replace_missing_report(tmp_path):
    case = _case(tmp_path, "Total violations: 0\nNumber of scan chains: 4\n", chain_constraints={"chain_count": 4})
    case[3].reports.joinpath("scan_chain.rpt").unlink()
    assert not validate_run(*case).passed


def test_max_chain_count_accepts_bounds_and_lengths_check_every_chain(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_chain_count": 5, "max_length": 100})
    (paths.reports / "scan_chain.rpt").write_text("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI a 90 si so se clk Default_Partition tool_created\nI b 101 si so se clk Default_Partition tool_created\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


@pytest.mark.parametrize("field,requirements,expected_status", [
    ("clocks", [{"port": "clk", "off_state": 0}], "pass"),
    ("clocks", [{"port": "clk", "off_state": 1}], "fail"),
    ("clocks", [{"port": "missing_clk", "off_state": 0}], "unverified"),
    ("scan_enables", [{"port": "scan_en", "off_state": 0, "view": "spec", "usage": "all"}], "pass"),
    ("scan_enables", [{"port": "scan_en", "off_state": 1, "view": "spec", "usage": "all"}], "fail"),
])
def test_signal_requirement_checks_are_evidence_backed(tmp_path, field, requirements, expected_status):
    case = _case(tmp_path, **{field: requirements})
    report = validate_run(*case)
    check = next(item for item in report.requirement_checks if item.field.startswith(field + "["))
    assert check.status == expected_status
    if expected_status == "pass":
        assert check.evidence and check.evidence[0]["path"].endswith("scan_signal.rpt")
    else:
        assert not report.passed


def test_duplicate_signal_rows_fail_closed(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, clocks=[{"port": "clk", "off_state": 0}])
    signal = paths.reports / "scan_signal.rpt"
    signal.write_text(signal.read_text(encoding="utf-8") + "clk user_defined clock 0 - - - - - - Default_Partition\n", encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    check = next(item for item in report.requirement_checks if item.field.startswith("clocks["))
    assert check.status == "fail"


@pytest.mark.parametrize("internal_clock,status", [("clk_int", "pass"), ("other_clk", "fail")])
def test_clock_internal_clock_mapping_is_checked(tmp_path, internal_clock, status):
    requirements, result, diagnostics, paths = _case(
        tmp_path, clocks=[{"port": "clk", "off_state": 0, "internal_clocks": internal_clock}],
    )
    signal = paths.reports / "scan_signal.rpt"
    signal.write_text(signal.read_text(encoding="utf-8").replace(
        "clk user_defined clock 0 - - - - - - Default_Partition",
        "clk user_defined clock 0 - - clk_int - - - Default_Partition",
    ), encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    check = next(item for item in report.requirement_checks if item.field.startswith("clocks["))
    assert check.status == status


@pytest.mark.parametrize("value,status", [(0, "pass"), (1, "fail")])
def test_constant_and_lockup_requirements_use_report_fields(tmp_path, value, status):
    requirements, result, diagnostics, paths = _case(
        tmp_path, constants=[{"port": "test_mode", "constant_value": value}],
        lockup={"add_lockup": True, "insert_terminal_lockup": False},
    )
    signals = paths.reports / "scan_signal.rpt"
    signals.write_text(signals.read_text(encoding="utf-8").replace(
        "scan_en user_defined scan_enable 0 - - - all spec - Default_Partition",
        "scan_en user_defined scan_enable 0 - - - all spec - Default_Partition\ntest_mode user_defined constant 0 - - - - - 0 Default_Partition",
    ), encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    constant = next(item for item in report.requirement_checks if item.field.startswith("constants["))
    lockup = [item for item in report.requirement_checks if item.field.startswith("lockup.")]
    assert constant.status == status
    assert all(item.status == "pass" for item in lockup)


def test_partition_membership_uses_report_rows(tmp_path):
    requirements, result, diagnostics, paths = _case(
        tmp_path, partitions=[{"name": "core", "include": ["u0", "u1"], "exclude": ["u2"]}],
    )
    (paths.reports / "scan_partition.rpt").write_text(
        "Partition Include Exclude Clocks RisingEdgeClocks FallingEdgeClocks\ncore u0,u1 u2 clk0 clk0 -\n",
        encoding="utf-8",
    )
    report = validate_run(requirements, result, diagnostics, paths)
    assert next(item for item in report.requirement_checks if item.field.startswith("partitions[" )).status == "pass"


def test_partition_member_rows_still_check_requested_clock_fields(tmp_path):
    case = _case(tmp_path, partitions=[{"name": "core", "include": ["u0"], "exclude": [],
                                       "clocks": ["clk0"]}])
    case[3].reports.joinpath("scan_partition.rpt").write_text(
        "Partition Cell Include Exclude Clocks RisingEdgeClocks FallingEdgeClocks\ncore u0 - - wrong_clk - -\ncore u1 - - wrong_clk - -\n",
        encoding="utf-8",
    )
    report = validate_run(*case)
    check = next(item for item in report.requirement_checks if item.field.startswith("partitions["))
    assert check.status == "fail"
    assert len(check.evidence) == 2


@pytest.mark.parametrize("field,value", [
    ("clock_domains", [{"name": "core", "clocks": ["clk"]}]),
    ("scan_segments", [{"name": "core", "include": ["u0"]}]),
    ("edge_policy", "falling"),
])
def test_unmapped_requirement_families_fail_closed(tmp_path, field, value):
    report = validate_run(*_case(tmp_path, **{field: value}))
    assert not report.passed
    check = next(item for item in report.requirement_checks if item.field == field)
    assert check.status == "unverified"


def test_conflicting_signal_types_for_same_port_fail_closed(tmp_path):
    case = _case(tmp_path, clocks=[{"port": "clk", "off_state": 0}])
    signal = case[3].reports / "scan_signal.rpt"
    signal.write_text(signal.read_text(encoding="utf-8") +
                      "clk user_defined reset 1 - - - - - - Default_Partition\n",
                      encoding="utf-8")
    report = validate_run(*case)
    check = next(item for item in report.requirement_checks if item.field.startswith("clocks["))
    assert check.status == "fail"
    assert "conflicting signal types" in check.reason


@pytest.mark.parametrize("field,requested,chain_row,status", [
    ("clocks", [{"port": "clk", "off_state": 0}], "clk", "pass"),
    ("clocks", [{"port": "other_clk", "off_state": 0}], "clk", "fail"),
    ("scan_enables", [{"port": "scan_en", "off_state": 0}], "scan_en", "pass"),
    ("scan_enables", [{"port": "other_en", "off_state": 0}], "scan_en", "fail"),
])
def test_chain_wiring_is_checked_against_requested_clock_and_enable(tmp_path, field, requested, chain_row, status):
    case = _case(tmp_path, **{field: requested})
    path = case[3].reports / "scan_chain.rpt"
    path.write_text(f"Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI a 1 si so scan_en {chain_row} Default_Partition tool_created\n",
                    encoding="utf-8")
    report = validate_run(*case)
    check = next(item for item in report.requirement_checks if item.field == field + ".chain_wiring")
    assert check.status == status


def test_wrapper_configuration_and_chain_rows_are_both_required(tmp_path):
    requirements, result, diagnostics, paths = _case(
        tmp_path, wrapper_settings={"chain_count": 1, "chain_length": 16, "style": "dedicated"},
    )
    (paths.reports / "wrapper_cfg.rpt").write_text(
        "WrapperConfigurationParameter Value\nchain_count 1\nmax_length 16\nstyle dedicated\n",
        encoding="utf-8",
    )
    (paths.reports / "scan_chain.rpt").write_text(
        "Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nW wrp0 8 wsi wso wrp_shift wrp_clk Default_Partition tool_created\n",
        encoding="utf-8",
    )
    report = validate_run(requirements, result, diagnostics, paths)
    assert report.passed
    assert all(item.status == "pass" for item in report.requirement_checks)


def test_zero_length_wrapper_chain_does_not_satisfy_length_limit(tmp_path):
    requirements, result, diagnostics, paths = _case(
        tmp_path, wrapper_settings={"chain_count": 1, "chain_length": 16, "style": "dedicated"},
    )
    (paths.reports / "wrapper_cfg.rpt").write_text(
        "WrapperConfigurationParameter Value\nchain_count 1\nmax_length 16\nstyle dedicated\n",
        encoding="utf-8",
    )
    (paths.reports / "scan_chain.rpt").write_text(
        "Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nW wrp0 0 wsi wso wrp_shift wrp_clk Default_Partition tool_created\n",
        encoding="utf-8",
    )

    report = validate_run(requirements, result, diagnostics, paths)
    structure = next(item for item in report.requirement_checks
                     if item.field == "wrapper_settings.chain_length.structure")
    assert structure.status == "fail"
    assert not report.passed


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
    (paths.reports / "scan_cfg.rpt").write_text("ScanConfigurationParameter Value\nchain_count 4\nmax_length 100\n", encoding="utf-8")
    (paths.reports / "scan_chain.rpt").write_text("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI scan_1 90 si1 so1 se clk Default_Partition tool_created\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_all_chain_lengths_with_count_can_prove_maximum(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "scan_cfg.rpt").write_text("ScanConfigurationParameter Value\nchain_count 2\nmax_length 100\n", encoding="utf-8")
    (paths.reports / "scan_chain.rpt").write_text("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI scan_1 90 si1 so1 se clk Default_Partition tool_created\nI scan_2 99 si2 so2 se clk Default_Partition tool_created\n", encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed


def test_unknown_count_rule_cannot_hide_behind_explicit_zero(tmp_path):
    assert not validate_run(*_case(tmp_path, "DFTR9: unknown count\nTotal violations: 0\n")).passed


def test_required_drc_report_requires_drc_evidence(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, required_outputs=["post_scan.v", "scan_drc.rpt"])
    (paths.reports / "scan_drc.rpt").write_text("unrecognized report\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_duplicate_chain_names_cannot_prove_complete_lengths(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "scan_cfg.rpt").write_text("ScanConfigurationParameter Value\nchain_count 2\nmax_length 100\n", encoding="utf-8")
    (paths.reports / "scan_chain.rpt").write_text("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI scan_1 90 si1 so1 se clk Default_Partition tool_created\nI scan_1 90 si2 so2 se clk Default_Partition tool_created\n", encoding="utf-8")
    report = validate_run(requirements, result, diagnostics, paths)
    assert not report.passed
    assert "complete_chain_lengths" in report.missing_evidence


@pytest.mark.parametrize("second_chain,passed", [("scan_1", False), ("scan_2", True)])
def test_full_chain_table_requires_unique_names(tmp_path, second_chain, passed):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "scan_cfg.rpt").write_text("ScanConfigurationParameter Value\nchain_count 2\nmax_length 100\n", encoding="utf-8")
    (paths.reports / "scan_chain.rpt").write_text(f"Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI scan_1 90 si1 so1 se clk Default_Partition tool_created\nI {second_chain} 90 si2 so2 se clk Default_Partition tool_created\n", encoding="utf-8")
    assert validate_run(requirements, result, diagnostics, paths).passed is passed


def test_partial_chain_rows_across_snapshots_cannot_prove_coverage(tmp_path):
    requirements, result, diagnostics, paths = _case(tmp_path, chain_constraints={"max_length": 100})
    (paths.reports / "scan_cfg.rpt").write_text("ScanConfigurationParameter Value\nchain_count 2\nmax_length 100\n", encoding="utf-8")
    (paths.reports / "scan_chain.rpt").write_text("Chain Length Input Output ScanEnable Clocks Partition ChainProperty\nI scan_1 90 si1 so1 se clk Default_Partition tool_created\n", encoding="utf-8")
    assert not validate_run(requirements, result, diagnostics, paths).passed


def test_unknown_severity_prefixed_rule_cannot_hide_behind_zero(tmp_path):
    report = validate_run(*_case(tmp_path, "[WARNING] DFTR9: unknown count\nTotal violations: 0\n", allowed_drc=["DFTR9"]))
    assert not report.passed
    assert any("unknown DRC count" in failure for failure in report.failures)
