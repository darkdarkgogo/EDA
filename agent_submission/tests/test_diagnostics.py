import pytest

from scan_agent.diagnostics import parse_drc_text, parse_tool_log


def test_parser_extracts_rule_counts_and_fatal_errors():
    text = "[ERROR] [CMD-0074] Unknown option '-active_state'\n[DFTDRC-4001] DFTR1 x1610\nTotal violations: 1610\n"
    summary = parse_tool_log(text)
    assert summary.fatal_errors[0].code == "CMD-0074"
    assert summary.fatal_errors[0].source_line == text.splitlines()[0]
    assert summary.fatal_errors[0].line_number == 1
    assert summary.drc_violations[0].rule == "DFTR1"
    assert summary.drc_violations[0].count == 1610
    assert summary.total_violations == 1610


def test_hyphenated_rules_subrules_objects_and_explicit_counts():
    violations = parse_drc_text("[WARNING] [DFTDRC-4005] Clock of 'reg[0]' inactive (DFTR9-1)\nDFTR-TIE0: 12 violations\nDFTR-TIE1 count = 0\nDFTR10 | 21\n")
    assert [(item.rule, item.count) for item in violations] == [("DFTR9-1", 1), ("DFTR-TIE0", 12), ("DFTR-TIE1", 0), ("DFTR10", 21)]
    assert violations[0].objects == ("reg[0]",)
    assert violations[0].severity == "WARNING"
    assert violations[0].code == "DFTDRC-4005"


def test_config_and_command_mentions_are_not_drc_evidence():
    summary = parse_tool_log("set_scan_drc_rule_handling {DFTR-TIE0 DFTR9} Ignore\n[INFO] examine_scan_drc\n[INFO] Rules include DFTR1\n")
    assert summary.total_violations is None
    assert summary.drc_violations == ()
    assert summary.drc_evidence == ()
    assert summary.commands == ("set_scan_drc_rule_handling", "examine_scan_drc")


def test_totals_preserve_each_source_and_do_not_assume_zero():
    summary = parse_tool_log("[INFO] Tool finished\n")
    assert summary.total_violations is None
    summary = parse_tool_log("Total violations: 4\nDRC Total violations: 0\n")
    assert summary.total_violations == 0
    assert [fact.value for fact in summary.drc_totals] == [4, 0]
    assert summary.drc_totals[-1].source_line == "DRC Total violations: 0"


@pytest.mark.parametrize("line", [
    "License checkout failed for dftexp_scan",
    "Failed to obtain a license",
    "No valid license found",
    "Cannot connect to license server",
    "FLEXnet Licensing error:-15,570",
    "License has expired",
])
def test_common_license_failures_are_fatal(line):
    summary = parse_tool_log(line)
    assert len(summary.license_errors) == 1
    assert summary.license_errors[0] in summary.fatal_errors
    assert summary.license_errors[0].source_line == line


def test_license_success_and_warnings_are_not_fatal():
    summary = parse_tool_log("License checkout successful\n[WARNING] [SCAN-4902] max length\n[INFO] [SCAN-1000] completed\n")
    assert summary.fatal_errors == ()
    assert len(summary.messages) == 2


def test_command_name_on_error_is_preserved():
    summary = parse_tool_log("[ERROR] [CMD-0074] command 'set_scan_signal' failed\n[FATAL] tool aborted\n")
    assert summary.fatal_errors[0].command == "set_scan_signal"
    assert summary.fatal_errors[1].severity == "FATAL"


def test_multiple_rules_on_one_line_keep_individual_counts():
    summary = parse_tool_log("[DFTDRC-4001] DFTR1 x4, DFTR9 x2\nTotal violations: 6\n")
    assert [(item.rule, item.count) for item in summary.drc_violations] == [("DFTR1", 4), ("DFTR9", 2)]


def test_chain_report_facts_preserve_evidence_and_lengths():
    summary = parse_tool_log("Number of scan chains: 4\nMaximum chain length: 90\nChain scan_1 length: 89\n")
    assert [(fact.name, fact.value) for fact in summary.chain_facts] == [("chain_count", 4), ("max_length", 90), ("chain_length", 89)]
    assert summary.chain_facts[0].line_number == 1
    assert summary.chain_facts[2].chain_name == "scan_1"


@pytest.mark.parametrize("line", [
    "[INFO] insert_dft_logic completed successfully",
    "Scan insertion completed successfully",
])
def test_insertion_completion_requires_explicit_success_evidence(line):
    assert parse_tool_log(line).insertion_facts[0].name == "insertion_complete"


@pytest.mark.parametrize("line", [
    "[INFO] insert_dft_logic started",
    "set step insert_dft_logic completed",
    "[ERROR] insert_dft_logic completed unsuccessfully",
    "[INFO] insert_scan completed successfully",
])
def test_insertion_mentions_do_not_prove_completion(line):
    assert parse_tool_log(line).insertion_facts == ()


def test_bare_rule_without_count_is_unknown_not_one_or_zero():
    violation = parse_drc_text("[WARNING] [DFTDRC-4001] DFTR1\n")[0]
    assert violation.count is None


def test_bare_report_rule_with_unparseable_count_is_unknown():
    assert parse_drc_text("DFTR9: -1 violations\n")[0].count is None


@pytest.mark.parametrize("line", ["All licenses are in use", "Feature scan is not licensed", "License server is not responding"])
def test_license_exhaustion_and_missing_features_fail(line):
    assert parse_tool_log(line).license_errors


def test_plain_numeric_report_table_count_is_explicit():
    violation = parse_drc_text("DFTR10 21 Warning\n")[0]
    assert violation.count == 21
    assert violation.count_is_explicit is True


def test_severity_prefixed_rule_without_code_or_count_remains_unknown():
    summary = parse_tool_log("[WARNING] DFTR9: unknown count\nTotal violations: 0\n")
    violation = summary.drc_violations[0]
    assert violation.rule == "DFTR9"
    assert violation.count is None
    assert violation.severity == "WARNING"
    assert violation.source_line == "[WARNING] DFTR9: unknown count"


def test_severity_prefixed_config_and_info_mentions_are_still_excluded():
    summary = parse_tool_log("[WARNING] set_scan_drc_rule_handling {DFTR9} Ignore\n[INFO] DFTR9: available rule\n")
    assert summary.drc_violations == ()
