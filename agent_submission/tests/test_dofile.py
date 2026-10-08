import json
from dataclasses import asdict

import pytest

from scan_agent.diagnostics import parse_tool_log
from scan_agent.dofile import (
    DofileProposal, RepairRecord, generate_initial_dofile, repair_dofile,
    resolve_output_destinations,
    validate_dofile_candidate,
)
from scan_agent.llm import LLMClient, LLMOutputError, Requirements
from scan_agent.manual import ManualChunk
from scan_agent.state import InputInventory
from test_llm import FakeTransport, requirement_data


SAFE = """load_lib /input/cells.lib
load_netlist /input/pre_scan.v
present_design top
set_scan_signal -type clock -port clk -off_state 0
set_scan_signal -type scan_enable -port scan_en -off_state 0
examine_scan_drc -verbose -file reports/drc.rpt
examine_scan_chain
insert_dft_logic
rpt_scan_signal > reports/scan_signal.rpt
rpt_scan_cfg > reports/scan_cfg.rpt
rpt_scan_chain -class all > reports/scan_chain.rpt
dump_netlist -file deliverables/post_scan.v
exit
"""


def proposal(text=SAFE, **overrides):
    return {"problem_type": "dofile", "root_cause": "initial generation",
            "evidence": [], "repair_summary": "configured scan", "dofile": text, **overrides}


def requirements():
    return Requirements(**requirement_data())


@pytest.mark.parametrize("line", [
    'set out_dir "/input/out"', 'exec rm -rf /work',
    'dump_netlist -file "/submission/post_scan.v"', 'source /opt/replace_tool.tcl',
    'system "touch /work/file"', 'rpt_scan -file {/opt/reports/chain.rpt}',
    'dump_netlist -file /work/../input/post_scan.v',
    'dump_netlist -file "C:/submission/post_scan.v"',
    'set out_dir {/opt/reports}',
    'set x [exec touch /work/file]', 'load_lib x.lib; exec touch /work/file',
    'if {1} {source /tmp/code.tcl}', '::exec touch /work/file',
    'dump_netlist -file=/input/post_scan.v',
    'dump_netlist -file {/work/a.v /input/b.v}',
])
def test_candidate_rejects_forbidden_writes_or_external_execution(line):
    report = validate_dofile_candidate(SAFE.replace("exit", line + "\nexit"))
    assert not report.safe
    assert any(item.line == line and item.reason for item in report.rejections)


def test_all_required_phases_and_read_only_protected_paths_are_accepted():
    report = validate_dofile_candidate(SAFE)
    assert report.safe
    assert report.rejections == ()
    assert report.missing_phases == ()


@pytest.mark.parametrize("destination", [
    "/etc/scan.v", "/output/decision_log.json", "/work/scan.v",
    "C:/outside/scan.v", r"C:\outside\scan.v", r"\\server\share\scan.v",
    "../scan.v", "nested/../scan.v", "nested//scan.v", "./scan.v", "",
])
def test_every_output_destination_must_be_normalized_relative(destination):
    line = f'dump_netlist -file "{destination}"'
    report = validate_dofile_candidate(SAFE.replace("dump_netlist -file deliverables/post_scan.v", line))
    assert not report.safe
    assert any(item.line == line and item.reason for item in report.rejections)


def test_execution_resolution_rejects_relative_symlink_escape(tmp_path):
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    try:
        (work / "nested").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(ValueError, match="escapes run work"):
        resolve_output_destinations(
            SAFE.replace("post_scan.v", "nested/post_scan.v"), work,
        )


def test_secondary_output_destination_cannot_hide_absolute_write():
    line = "dump_netlist -file deliverables/post_scan.v -output /etc/hidden.v"
    report = validate_dofile_candidate(SAFE.replace("dump_netlist -file deliverables/post_scan.v", line))
    assert not report.safe
    assert any(item.line == line for item in report.rejections)


def test_static_output_option_assignment_and_continuations_are_accepted():
    text = SAFE.replace("-file deliverables/post_scan.v", "-file=deliverables/post_scan.v")
    assert validate_dofile_candidate(text).safe


@pytest.mark.parametrize("line", [
    "rpt_scan_signal reports/scan_signal.rpt",
    "rpt_scan_signal >> reports/scan_signal.rpt",
    "rpt_scan_signal > /output/scan_signal.rpt",
    "rpt_scan_signal > ../scan_signal.rpt",
    "rpt_scan_signal > reports/a.rpt > reports/b.rpt",
])
def test_report_redirection_requires_one_safe_static_destination(line):
    report = validate_dofile_candidate(SAFE.replace(
        "rpt_scan_signal > reports/scan_signal.rpt", line,
    ))
    assert not report.safe
    assert any(item.line == line for item in report.rejections)


@pytest.mark.parametrize("obsolete", ["examine_scan", "insert_scan"])
def test_placeholder_commands_do_not_satisfy_documented_phases(obsolete):
    documented = SAFE.replace("examine_scan_drc", obsolete) if obsolete == "examine_scan" else SAFE.replace("insert_dft_logic", obsolete)
    report = validate_dofile_candidate(documented)
    assert not report.safe
    assert report.missing_phases
    text = SAFE.replace("-file deliverables/post_scan.v", "-file " + "\\" + "\ndeliverables/post_scan.v")
    assert validate_dofile_candidate(text).safe


@pytest.mark.parametrize("command,phase", [
    ("load_lib", "load_library"), ("load_netlist", "load_netlist"),
    ("present_design", "present"), ("examine_scan_drc", "drc"),
    ("examine_scan_chain", "preview"), ("insert_dft_logic", "insertion"),
    ("rpt_scan_signal", "signal_report"), ("rpt_scan_cfg", "config_report"),
    ("rpt_scan_chain", "chain_report"), ("dump_netlist", "output"),
])
def test_each_missing_required_phase_is_reported(command, phase):
    report = validate_dofile_candidate("\n".join(line for line in SAFE.splitlines() if not line.startswith(command)))
    assert not report.safe
    assert phase in report.missing_phases


def test_comments_and_quoted_command_names_do_not_prove_phases():
    report = validate_dofile_candidate("# " + SAFE.replace("\n", "\n# ") + '\nset example "insert_dft_logic"\n')
    assert not report.safe
    assert "insertion" in report.missing_phases


@pytest.mark.parametrize("text", ["", "  \n# comment\n", SAFE.replace("examine_scan_drc", "if {0} {examine_scan_drc}"),
                                       SAFE.replace("insert_dft_logic", "exit\ninsert_dft_logic")])
def test_empty_conditional_or_unreachable_phases_cannot_pass(text):
    assert not validate_dofile_candidate(text).safe


def test_phase_order_and_output_destination_are_required():
    reversed_phases = "\n".join(reversed(SAFE.splitlines()[:-1])) + "\nexit\n"
    assert not validate_dofile_candidate(reversed_phases).safe
    assert not validate_dofile_candidate(SAFE.replace("-file deliverables/post_scan.v", "-file")).safe


@pytest.mark.parametrize("line", ["set_scan_unknown -port x", "rpt_scan_secret > reports/x.rpt"])
def test_unknown_tool_commands_with_documented_prefixes_are_rejected(line):
    report = validate_dofile_candidate(SAFE.replace("exit", line + "\nexit"))
    assert not report.safe
    assert any("unsupported Tcl command" in item.reason for item in report.rejections)


def test_configuration_commands_must_precede_drc():
    moved = SAFE.replace("set_scan_signal -type scan_enable -port scan_en -off_state 0\n", "")
    moved = moved.replace("examine_scan_drc -verbose -file reports/drc.rpt",
                          "examine_scan_drc -verbose -file reports/drc.rpt\nset_scan_signal -type scan_enable -port scan_en -off_state 0")
    report = validate_dofile_candidate(moved)
    assert not report.safe
    assert any("must precede examine_scan_drc" in item.reason for item in report.rejections)


def test_variable_resolution_prevents_protected_output_and_allows_input_variables():
    text = "set input_dir /input\nset output_dir reports\n" + SAFE.replace("/input/", "$input_dir/")
    assert validate_dofile_candidate(text).safe
    text = "set destination /input\n" + SAFE.replace("-file deliverables/post_scan.v", "-file $destination/post_scan.v")
    report = validate_dofile_candidate(text)
    assert not report.safe
    assert any(item.line == "dump_netlist -file $destination/post_scan.v" for item in report.rejections)


@pytest.mark.parametrize("command,argument,phase", [
    ("load_lib", "/input/cells.lib", "load_library"),
    ("load_netlist", "/input/pre_scan.v", "load_netlist"),
    ("present_design", "top", "present"),
])
@pytest.mark.parametrize("value,prefix", [("$missing", ""), ("$empty", "set empty {}\n"), ("{}", "")])
def test_required_input_and_presentation_phases_reject_unresolved_or_empty_arguments(command, argument, phase, value, prefix):
    line = f"{command} {value}"
    report = validate_dofile_candidate(prefix + SAFE.replace(f"{command} {argument}", line))
    assert not report.safe
    assert phase in report.missing_phases
    assert any(item.line == line and item.reason for item in report.rejections)


def test_required_input_and_presentation_phases_accept_resolved_nonempty_variables():
    prefix = "set lib /input/cells.lib\nset netlist /input/pre_scan.v\nset top top\n"
    text = SAFE.replace("load_lib /input/cells.lib", "load_lib $lib").replace("load_netlist /input/pre_scan.v", "load_netlist $netlist").replace("present_design top", "present_design $top")
    assert validate_dofile_candidate(prefix + text).safe


@pytest.mark.parametrize("line,prefix", [
    ("dump_netlist -file {} -format verilog", ""),
    ("dump_netlist -file $path", "set path {}\n"),
    ("dump_netlist -file= -format verilog", ""),
    ("dump_netlist -file=$path -format verilog", "set path {}\n"),
    ("dump_netlist -format verilog", ""),
    ("dump_netlist -file -format verilog", ""),
])
def test_output_phase_requires_a_nonempty_resolved_filename(line, prefix):
    report = validate_dofile_candidate(prefix + SAFE.replace("dump_netlist -file deliverables/post_scan.v", line))
    assert not report.safe
    assert "output" in report.missing_phases
    assert any(item.line == line and "filename" in item.reason for item in report.rejections)


@pytest.mark.parametrize("line", ["dump_netlist -format verilog -file $path", "dump_netlist -file=$path -format verilog"])
def test_output_phase_accepts_explicit_resolved_filename_with_format_option(line):
    text = "set path post_scan.v\n" + SAFE.replace("dump_netlist -file post_scan.v", line)
    assert validate_dofile_candidate(text).safe


@pytest.mark.parametrize("line,prefix", [
    ("load_netlist $missing", ""),
    ("present_design $missing", ""),
    ("load_lib $missing", ""),
    ("dump_netlist -file {} -format verilog", ""),
    ("dump_netlist -file $path", "set path {}\n"),
])
def test_resolved_argument_rejection_is_sent_to_single_correction(line, prefix):
    command = line.split()[0]
    original = next(item for item in SAFE.splitlines() if item.startswith(command + " "))
    bad = prefix + SAFE.replace(original, line)
    transport = FakeTransport([json.dumps(proposal(bad)), json.dumps(proposal())])
    result = generate_initial_dofile(LLMClient(transport=transport, model="m"), requirements(), InputInventory([]), [])
    assert result.dofile == SAFE
    assert transport.calls == 2
    correction = json.loads(transport.requests[1]["messages"][-1]["content"])
    errors = json.loads(correction["validation_errors"])
    assert errors["rejections"] == [asdict(item) for item in validate_dofile_candidate(bad).rejections]
    assert any(item["line"] == line for item in errors["rejections"])


@pytest.mark.parametrize("line", [
    'dump_netlist -file $unknown/post_scan.v', 'dump_netlist -file [format /input/%s post_scan.v]',
    'open /input/post_scan.v w', 'file delete /input/pre_scan.v',
    'eval {exec touch x}', 'rename exec execute', 'interp eval {} {exec touch x}',
    'load /tmp/plugin.so', 'proc custom {} {exec touch x}',
])
def test_unresolved_or_dynamic_execution_is_rejected(line):
    assert not validate_dofile_candidate(SAFE + line).safe


def test_rejections_retain_exact_whitespace_and_line_number():
    line = '  dump_netlist -file "/submission/post_scan.v"  '
    report = validate_dofile_candidate(SAFE + line + "\n")
    rejection = next(item for item in report.rejections if item.line == line)
    assert rejection.line_number == 14
    assert "protected" in rejection.reason


def test_generation_uses_only_requirements_inventory_and_manual_data():
    transport = FakeTransport([json.dumps(proposal())])
    result = generate_initial_dofile(LLMClient(transport=transport, model="m"), requirements(),
                                     InputInventory(["pre_scan.v", "cells.lib"]), [ManualChunk(3, 0, "examine_scan")])
    assert isinstance(result, DofileProposal)
    assert result.dofile == SAFE
    payload = json.loads(transport.requests[0]["messages"][1]["content"])
    assert payload == {"requirements": asdict(requirements()), "inventory": {"runtime_files": ["pre_scan.v", "cells.lib"]},
                       "manual_chunks": [{"page": 3, "chunk_index": 0, "text": "examine_scan"}]}


def test_safety_correction_receives_exact_rejected_line_and_reason_once():
    bad_line = '  dump_netlist -file "/input/post_scan.v"  '
    bad = SAFE.replace("dump_netlist -file deliverables/post_scan.v", bad_line)
    transport = FakeTransport([json.dumps(proposal(bad)), json.dumps(proposal())])
    result = generate_initial_dofile(LLMClient(transport=transport, model="m"), requirements(), InputInventory([]), [])
    assert result.dofile == SAFE
    assert transport.calls == 2
    correction = json.loads(transport.requests[1]["messages"][-1]["content"])
    errors = json.loads(correction["validation_errors"])
    assert errors["rejections"] == [asdict(item) for item in validate_dofile_candidate(bad).rejections]
    assert errors["missing_phases"] == list(validate_dofile_candidate(bad).missing_phases)


def test_second_unsafe_candidate_fails_without_additional_requests():
    bad = SAFE.replace("insert_dft_logic", "# insert_dft_logic")
    transport = FakeTransport([json.dumps(proposal(bad))] * 2)
    with pytest.raises(LLMOutputError):
        generate_initial_dofile(LLMClient(transport=transport, model="m"), requirements(), InputInventory([]), [])
    assert transport.calls == 2


def test_repair_serializes_structured_diagnostics_and_history():
    diagnostics = parse_tool_log("[ERROR] [CMD-1] insert_scan failed\nDFTR9 x1\nTotal violations: 1\n")
    history = [RepairRecord("abc123", "CMD-1", "changed insertion config")]
    transport = FakeTransport([json.dumps(proposal())])
    result = repair_dofile(LLMClient(transport=transport, model="m"), requirements(), SAFE + "# previous", diagnostics, history, [])
    assert result.dofile == SAFE
    payload = json.loads(transport.requests[0]["messages"][1]["content"])
    assert set(payload) == {"requirements", "current_dofile", "diagnostics", "history", "manual_chunks"}
    assert payload["diagnostics"]["fatal_errors"][0]["source_line"].startswith("[ERROR]")
    assert payload["history"] == [asdict(history[0])]


def test_repair_prompt_uses_only_declared_requirement_and_diagnostic_fields():
    from dataclasses import dataclass
    from scan_agent.diagnostics import Diagnostic, DiagnosticSummary
    @dataclass(frozen=True)
    class ExtraRequirements(Requirements):
        private_text: str = "must not serialize"
    @dataclass(frozen=True)
    class ExtraDiagnostic(Diagnostic):
        private_text: str = "must not serialize"
    diagnostics = DiagnosticSummary(messages=(ExtraDiagnostic("ERROR", "CMD-1", "failed", "failed", 1),))
    transport = FakeTransport([json.dumps(proposal())])
    repair_dofile(LLMClient(transport=transport, model="m"), ExtraRequirements(**requirement_data()),
                  SAFE + "# previous", diagnostics, [], [])
    assert "must not serialize" not in transport.requests[0]["messages"][1]["content"]


def test_repair_rejects_repeated_failed_dofile():
    import hashlib
    digest = hashlib.sha256(SAFE.encode("utf-8")).hexdigest()
    transport = FakeTransport([json.dumps(proposal())] * 2)
    with pytest.raises(LLMOutputError, match="repeated"):
        repair_dofile(LLMClient(transport=transport, model="m"), requirements(), SAFE,
                      parse_tool_log(""), [RepairRecord(digest, "DFTR9", "previous")], [])
    assert transport.calls == 2


@pytest.mark.parametrize("change", [
    {"problem_type": "unknown"}, {"evidence": [{"source": "", "locator": ""}]},
    {"repair_summary": ""}, {"dofile": ""}, {"root_cause": 42},
])
def test_proposal_schema_failure_has_one_correction(change):
    transport = FakeTransport([json.dumps(proposal(**change)), json.dumps(proposal())])
    assert generate_initial_dofile(LLMClient(transport=transport, model="m"), requirements(), InputInventory([]), []).dofile == SAFE
    assert transport.calls == 2


def test_netlist_repair_diagnosis_can_be_returned_without_executable_dofile():
    transport = FakeTransport([json.dumps(proposal("", problem_type="requires_netlist_repair", evidence=[{"source": "runs/R1/R1.log", "locator": "line 1"}]))])
    result = repair_dofile(LLMClient(transport=transport, model="m"), requirements(), SAFE, parse_tool_log(""), [], [])
    assert result.problem_type == "requires_netlist_repair"
    assert result.dofile == ""


def test_terminal_netlist_diagnosis_cannot_bypass_candidate_safety():
    bad = proposal("exec touch /input/x", problem_type="requires_netlist_repair",
                   evidence=[{"source": "runs/R1/R1.log", "locator": "line 1"}])
    transport = FakeTransport([json.dumps(bad)] * 2)
    with pytest.raises(LLMOutputError):
        repair_dofile(LLMClient(transport=transport, model="m"), requirements(), SAFE, parse_tool_log(""), [], [])
