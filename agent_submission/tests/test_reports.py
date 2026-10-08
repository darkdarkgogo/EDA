from pathlib import Path
from itertools import count

import pytest

from scan_agent.artifacts import create_run
from scan_agent.reports import (
    collect_report_evidence, parse_chain_report, parse_config_report,
    parse_partition_report, parse_signal_report,
)
from scan_agent.deadline import DeadlineExceeded


def test_config_parser_normalizes_values_and_keeps_line_evidence():
    rows, issues = parse_config_report("""ScanConfigurationParameter    Value
chain_count                  4
max_length                   100
mix_clocks                   False
add_lockup                   True
""")
    assert [(row.name, row.value) for row in rows] == [
        ("chain_count", "4"), ("max_length", "100"),
        ("mix_clocks", "False"), ("add_lockup", "True"),
    ]
    assert rows[0].location.line_number == 2
    assert rows[0].location.source_line == "chain_count                  4"
    assert not issues


def test_chain_parser_retains_internal_wrapper_and_clock_rows():
    rows, issues = parse_chain_report("""Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I 0 10 si0 so0 se clk0 Default_Partition tool_created
W wrp_i_0 8 wrp_si0 wrp_so0 wrp_shift wrp_clk Default_Partition tool_created
""")
    assert len(rows) == 2
    assert (rows[0].chain_class, rows[0].name, rows[0].length) == ("I", "0", 10)
    assert rows[0].clocks == ("clk0",)
    assert rows[0].partition == "Default_Partition"
    assert rows[1].chain_class == "W"
    assert rows[1].location.line_number == 3
    assert not issues


def test_chain_parser_retains_duplicate_rows_and_marks_malformed_rows():
    rows, issues = parse_chain_report("""Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I 0 10 si0 so0 se clk0 Default_Partition tool_created
I 0 10 si0 so0 se clk0 Default_Partition tool_created
I bad nope
""")
    assert len(rows) == 2
    assert rows[0].name == rows[1].name
    assert rows[0].location.line_number == 2
    assert rows[1].location.line_number == 3
    assert issues and "chain row" in issues[0].reason


def test_chain_parser_marks_unknown_class_instead_of_ignoring_it():
    rows, issues = parse_chain_report("""Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I scan_0 10 si so se clk Default_Partition tool_created
X scan_1 10 si so se clk Default_Partition tool_created
""")
    assert len(rows) == 1
    assert len(issues) == 1
    assert "unsupported" in issues[0].reason


def test_signal_parser_records_signal_type_off_state_usage_view_and_location():
    rows, issues = parse_signal_report("""Port PortProperty SignalType OffState HookupPin HookupSense AssociatedInternal Usage View ConstantValue OwnerPartition
clk pre_existing clock 0 - - - - - - Default_Partition
se user_defined scan_enable 1 - - - all spec - Default_Partition
""")
    assert [(row.port, row.signal_type, row.off_state) for row in rows] == [
        ("clk", "clock", "0"), ("se", "scan_enable", "1"),
    ]
    assert rows[1].usage == "all" and rows[1].view == "spec"
    assert rows[1].location.line_number == 3
    assert not issues


def test_partition_parser_captures_configuration_and_membership():
    rows, issues = parse_partition_report("""Partition Include Exclude Clocks RisingEdgeClocks FallingEdgeClocks
core u0,u1 u2 clk0 clk0 -
""")
    assert rows[0].name == "core"
    assert rows[0].include == ("u0", "u1")
    assert rows[0].exclude == ("u2",)
    assert rows[0].clocks == ("clk0",)
    assert rows[0].rising_edge_clocks == ("clk0",)
    assert not issues


def test_report_collection_uses_only_fixed_files_and_marks_missing(tmp_path: Path):
    run = create_run(tmp_path, 1)
    (run.reports / "scan_cfg.rpt").write_text(
        "ScanConfigurationParameter Value\nchain_count 1\n", encoding="utf-8",
    )
    evidence = collect_report_evidence(run)
    assert len(evidence.config) == 1
    assert len(evidence.issues) == 4
    assert {issue.source for issue in evidence.issues} == {
        "runs/R1/reports/scan_signal.rpt", "runs/R1/reports/scan_chain.rpt",
        "runs/R1/reports/scan_partition.rpt", "runs/R1/reports/wrapper_cfg.rpt",
    }


def test_report_collection_rejects_unbounded_single_line(tmp_path: Path):
    run = create_run(tmp_path, 1)
    (run.reports / "scan_cfg.rpt").write_text("x" * 1_048_577, encoding="utf-8")
    evidence = collect_report_evidence(run)
    issue = next(issue for issue in evidence.issues if issue.source.endswith("scan_cfg.rpt"))
    assert "exceeds 1048576 characters" in issue.reason


def test_report_parser_checks_deadline_between_rows():
    ticks = count()
    with pytest.raises(DeadlineExceeded, match="report evidence parsing"):
        parse_config_report("ScanConfigurationParameter Value\na 1\nb 2\n", deadline_monotonic=2,
                            clock=lambda: next(ticks))
