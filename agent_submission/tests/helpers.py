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
examine_scan
insert_scan
dump_netlist -file post_scan.v
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
            log = f"[ERROR] [{template.failure_detail}] insert_scan failed\n"
        elif template.failure_detail and template.failure_detail.startswith("DFTR"):
            log = f"{template.failure_detail} x1\nTotal violations: 1\n"
        elif template.success:
            log = "[INFO] insert_scan completed successfully\nTotal violations: 0\n"
            (paths.deliverables / "post_scan.v").write_text("module top(); endmodule\n", encoding="utf-8")
            (paths.reports / "scan.rpt").write_text(
                "Number of scan chains: 1\nMaximum chain length: 1\n",
                encoding="utf-8",
            )
        elif template.timed_out:
            log = "[INFO] insert_scan started\n"
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
