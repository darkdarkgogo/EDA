"""Explicit test-only cases, model responses, and tool evidence."""

from dataclasses import replace
import json
from pathlib import Path

from scan_agent.artifacts import write_json_atomic
from scan_agent.llm import LLMClient
from scan_agent.runner import ToolResult
from scan_agent.workflow import WorkflowDependencies


SAFE_DOFILE = """load_lib /input/lib/stdcells.lib
load_netlist /input/netlist/design.v
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


def make_task1_case(root: Path) -> Path:
    case = root / "input"
    (case / "netlist").mkdir(parents=True)
    (case / "lib").mkdir()
    (case / "task_spec.md").write_text("Insert scan into top; output post_scan.v", encoding="utf-8")
    (case / "limitations.md").write_text("wall time: 120 seconds", encoding="utf-8")
    (case / "netlist/design.v").write_text("module top(); endmodule\n", encoding="utf-8")
    (case / "lib/stdcells.lib").write_text("library(test) {}\n", encoding="utf-8")
    (case / ".case_ready").touch()
    return case


def successful_scan_result() -> ToolResult:
    return ToolResult(0, False, 0.1, Path("unused"), ())


def failed_cmd_result(code: str = "CMD-0074") -> ToolResult:
    return ToolResult(7, False, 0.1, Path("unused"), (), "nonzero_exit", code)


def failed_drc_result(rule: str = "DFTR10") -> ToolResult:
    return ToolResult(0, False, 0.1, Path("unused"), (), None, rule)


def diagnosis(problem_type: str = "dofile") -> dict:
    return {"problem_type": problem_type, "root_cause": "fixture diagnosis",
            "repair_summary": "fixture correction",
            "evidence": [{"source": "runs/R1/R1.log", "locator": "line 1"}]}


def scripted_dependencies(tool_results: list[ToolResult], dofiles: list[str] | None = None,
                          diagnoses: list[dict[str, str]] | None = None) -> WorkflowDependencies:
    scripts = iter(dofiles or [SAFE_DOFILE + f"# candidate {i}\n" for i in range(5)])
    diagnoses_iter = iter(diagnoses or [])
    results = iter(tool_results)
    requests = []

    def transport(**request):
        requests.append(request)
        payload = json.loads(request["messages"][1]["content"])
        if "task_text" in payload:
            inventory = payload["inventory"]["runtime_files"]
            from scan_agent.inputs import parse_limits
            budget = parse_limits(payload["limitations_text"], 0)
            data = dict(task_type="task2" if "original.dofile" in inventory else "task1",
                        top_module="top", netlists=["netlist/design.v"], libraries=["lib/stdcells.lib"],
                        ctl_files=[], clocks=[], resets=[], constants=[], scan_enables=[], chain_constraints={},
                        partitions=[], clock_domains=[], edge_policy=None, lockup={}, scan_segments=[],
                        wrapper_settings={}, allowed_drc=[], required_outputs=["post_scan.v"],
                        allow_netlist_modification=False, wall_time_seconds=budget.deadline_monotonic,
                        max_tool_runs=budget.max_tool_runs)
        else:
            data = next(diagnoses_iter, diagnosis()) if "current_dofile" in payload else {**diagnosis(), "evidence": []}
            if "current_dofile" in payload and not payload["history"] and "original" in payload["current_dofile"]:
                data = {**data, "evidence": [{"source": "original_static_diagnostics.json", "locator": "rejections"}]}
            data = {**data, "dofile": "" if data["problem_type"] == "requires_netlist_repair" else next(scripts)}
        return json.dumps(data)

    def runner(paths, dofile_path, executable, timeout_seconds, env):
        template = next(results)
        if template.failure_kind == "nonzero_exit":
            log = f"[ERROR] [{template.failure_detail}] insert_dft_logic failed\n"
        elif template.failure_detail and template.failure_detail.startswith("DFTR"):
            log = f"{template.failure_detail} x1\nTotal violations: 1\n"
        elif template.success:
            log = "[INFO] insert_dft_logic completed successfully\nTotal violations: 0\n"
            (paths.deliverables / "post_scan.v").write_text("module top(); endmodule\n", encoding="utf-8")
            (paths.reports / "drc.rpt").write_text("Total violations: 0\n", encoding="utf-8")
            (paths.reports / "scan_signal.rpt").write_text("""Port PortProperty SignalType OffState HookupPin HookupSense AssociatedInternal Usage View ConstantValue OwnerPartition
clk user_defined clock 0 - - - - - - Default_Partition
scan_en user_defined scan_enable 0 - - - all spec - Default_Partition
""", encoding="utf-8")
            (paths.reports / "scan_cfg.rpt").write_text("""ScanConfigurationParameter Value
chain_count 1
max_length 1
add_lockup True
insert_terminal_lockup False
""", encoding="utf-8")
            (paths.reports / "scan_chain.rpt").write_text("""Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I 0 1 test_si0 test_so0 scan_en clk Default_Partition tool_created
""", encoding="utf-8")
        elif template.timed_out:
            log = "[INFO] insert_dft_logic started\n"
        else:
            log = ""
        paths.log.write_text(log, encoding="utf-8")
        result = replace(template, log_path=paths.log,
                         produced_files=tuple(p.relative_to(paths.root).as_posix() for p in paths.root.rglob("*") if p.is_file()))
        write_json_atomic(paths.root / "run_metadata.json", {"exit_code": result.exit_code, "failure_kind": result.failure_kind})
        return result

    client = LLMClient(transport=transport, model="test-only", max_retries=0)
    return WorkflowDependencies(client=client, tool_runner=runner)


def fake_dependencies(mode: str) -> WorkflowDependencies:
    results = {
        "success": successful_scan_result(),
        "tool_unavailable": ToolResult(None, False, 0, Path("unused"), (), "tool_unavailable"),
        "timeout": ToolResult(-1, True, 110, Path("unused"), (), "timeout"),
    }
    return scripted_dependencies([results[mode]])
