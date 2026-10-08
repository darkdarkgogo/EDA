from pathlib import Path
from dataclasses import replace
import json
import threading

import pytest

from helpers import (SAFE_DOFILE, diagnosis, failed_cmd_result, failed_drc_result,
                     fake_dependencies, make_task1_case, scripted_dependencies, successful_scan_result)
from scan_agent.llm import LLMConfigurationError, LLMOutputError
from scan_agent.deadline import DeadlineExceeded
from scan_agent.workflow import WorkflowDependencies, build_workflow, run_agent


def test_workflow_repairs_once_then_promotes_second_run(tmp_path: Path) -> None:
    case = make_task1_case(tmp_path)
    deps = scripted_dependencies([failed_cmd_result(), successful_scan_result()],
                                 [SAFE_DOFILE + "# first\n", SAFE_DOFILE + "# corrected\n"])
    result = run_agent(case, tmp_path / "output", dependencies=deps)
    assert result.status == "success"
    assert result.final_run == "R2"
    assert (tmp_path / "output/diffs/dofile_R1_to_R2.diff").is_file()


def audit(output):
    return json.loads((output / "decision_log.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode,status,runs", [("success", "success", 1),
    ("tool_unavailable", "tool_failure", 1), ("timeout", "budget_exhausted", 1)])
def test_single_run_modes(tmp_path, mode, status, runs):
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, fake_dependencies(mode))
    assert result.status == status
    assert len(audit(output)["tool_runs"]) == runs
    assert (output / "final_results").exists() == (status == "success")
    if status != "success":
        assert result.final_run is None


def test_three_failed_runs_never_attempts_fourth(tmp_path):
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_cmd_result()] * 4)
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert len(audit(output)["tool_runs"]) == 3
    assert not (output / "runs/R4").exists()
    assert not (output / "final_results").exists()
    for number in range(1, 4):
        assert (output / f"runs/R{number}/run_metadata.json").is_file()
        assert (output / f"runs/R{number}/validation.json").is_file()


def test_explicit_tool_cap_is_obeyed(tmp_path):
    case = make_task1_case(tmp_path)
    (case / "limitations.md").write_text("wall time: 120 seconds\ntool runs: 1", encoding="utf-8")
    output = tmp_path / "output"
    result = run_agent(case, output, scripted_dependencies([failed_cmd_result(), successful_scan_result()]))
    assert result.status == "budget_exhausted"
    assert len(audit(output)["tool_runs"]) == 1


def test_repeated_dofile_hash_stops_before_second_run(tmp_path):
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_cmd_result()], [SAFE_DOFILE] * 3)
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "no_progress"
    assert len(audit(output)["tool_runs"]) == 1


def test_netlist_repair_requirement_is_terminal(tmp_path):
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_drc_result()], diagnoses=[diagnosis("requires_netlist_repair")])
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "unsupported_netlist_repair"
    assert result.final_run is None
    assert len(audit(output)["tool_runs"]) == 1
    assert not (output / "final_results").exists()


@pytest.mark.parametrize("phase", ["extract", "generate", "repair"])
def test_invalid_model_output_fails_closed(tmp_path, phase):
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_cmd_result()])
    def invalid(*args):
        raise LLMOutputError("fixture rejected output twice")
    if phase == "extract":
        deps.requirement_extractor = invalid
    elif phase == "generate":
        deps.initial_generator = invalid
    else:
        deps.repairer = invalid
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "invalid_model_output"
    assert len(audit(output)["tool_runs"]) == (1 if phase == "repair" else 0)
    assert not (output / "final_results").exists()


def test_protected_input_mutation_overrides_successful_tool(tmp_path):
    case = make_task1_case(tmp_path)
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    runner = deps.tool_runner
    def mutate(*args):
        result = runner(*args)
        (case / "netlist/design.v").write_text("changed", encoding="utf-8")
        return result
    deps.tool_runner = mutate
    result = run_agent(case, output, deps)
    assert result.status == "compliance_failure"
    assert result.final_run is None
    assert not (output / "final_results").exists()


def test_time_reserve_prevents_repair_and_preserves_attempt(tmp_path):
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_cmd_result()])
    now = [0.0]
    deps.ready_waiter = lambda *args: 0.0
    deps.clock = lambda: now[0]
    runner = deps.tool_runner
    def consume(*args):
        assert args[3] == 110.0
        result = runner(*args)
        now[0] = 110.0
        return result
    deps.tool_runner = consume
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert len(audit(output)["tool_runs"]) == 1


def test_generation_consumes_budget_before_any_tool_run(tmp_path):
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    now = [0.0]
    deps.ready_waiter = lambda *args: 0.0
    deps.clock = lambda: now[0]
    generator = deps.initial_generator
    def consume(*args):
        candidate = generator(*args)
        now[0] = 111.0
        return candidate
    deps.initial_generator = consume
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert audit(output)["tool_runs"] == []


def test_task2_repairs_original_with_static_rejections(tmp_path):
    case = make_task1_case(tmp_path)
    original = "# original\n" + SAFE_DOFILE.replace("insert_scan", "exec forbidden")
    (case / "original.dofile").write_text(original, encoding="utf-8")
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    def no_generation(*args):
        pytest.fail("Task 2 must repair original.dofile")
    deps.initial_generator = no_generation
    result = run_agent(case, output, deps)
    assert result.status == "success"
    assert audit(output)["task_type"] == "task2"
    static = json.loads((output / "original_static_diagnostics.json").read_text(encoding="utf-8"))
    assert any("exec forbidden" in rejection["line"] for rejection in static["rejections"])
    assert (case / "original.dofile").read_text(encoding="utf-8") == original


def test_ready_wait_happens_before_inventory(tmp_path, monkeypatch):
    from scan_agent import workflow
    case = make_task1_case(tmp_path)
    deps = fake_dependencies("success")
    events = []
    original_inventory = workflow.inventory_inputs
    original_wait = deps.ready_waiter
    def wait(*args):
        events.append("ready")
        return original_wait(*args)
    def inventory(*args):
        assert events == ["ready"]
        events.append("inventory")
        return original_inventory(*args)
    deps.ready_waiter = wait
    monkeypatch.setattr(workflow, "inventory_inputs", inventory)
    assert run_agent(case, tmp_path / "output", deps).status == "success"


def test_production_llm_configuration_precedes_any_scan_run(tmp_path, monkeypatch):
    from scan_agent import workflow
    case = make_task1_case(tmp_path)
    events = []
    def configure():
        events.append("llm")
        raise LLMConfigurationError("missing model configuration")
    def tool(*args):
        pytest.fail("missing LLM configuration must not invoke scan")
    monkeypatch.setattr(workflow.LLMClient, "from_env", configure)
    monkeypatch.setattr(workflow, "run_scan_tool", tool)
    result = run_agent(case, tmp_path / "output")
    assert result.status == "invalid_model_output"
    assert events == ["llm"]
    assert not (tmp_path / "output/runs").exists()


@pytest.mark.parametrize("invalid", [None, {"success": True}, "success"])
def test_invalid_tool_outcome_preserves_evidence(tmp_path, invalid):
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    deps.tool_runner = lambda *args: invalid
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "tool_failure"
    assert audit(output)["tool_runs"][0]["failure_kind"] == "invalid_tool_outcome"
    assert (output / "runs/R1/R1.log").is_file()
    assert not (output / "final_results").exists()


def test_raised_tool_failure_retains_partial_stdout(tmp_path):
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    def interrupted(paths, *args):
        paths.log.write_bytes(b"actual partial stdout\n")
        raise RuntimeError("fixture runner error")
    deps.tool_runner = interrupted
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "tool_failure"
    assert (output / "runs/R1/R1.log").read_bytes() == b"actual partial stdout\n"


def test_invalid_manual_outcome_is_terminal(tmp_path):
    deps = fake_dependencies("success")
    deps.manual_loader = lambda *args: "claimed available"
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "tool_failure"
    assert audit(output)["tool_runs"] == []


def test_cli_exit_zero_only_for_success(tmp_path, monkeypatch):
    import main
    from scan_agent.state import AgentStatus
    from scan_agent.workflow import AgentResult
    for status in AgentStatus:
        monkeypatch.setattr(main, "run_agent", lambda *args: AgentResult(status, "R1" if status == "success" else None))
        assert main.main(["-input", str(tmp_path), "-output", str(tmp_path / "output")]) == (0 if status == "success" else 1)


def test_graph_order_matches_plan():
    graph = build_workflow(fake_dependencies("success")).get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    order = ["__start__", "inventory_input", "extract_requirements", "load_manual", "create_candidate",
             "prepare_run", "run_tool", "parse_evidence", "validate"]
    assert set(zip(order, order[1:])) <= edges
    assert ("diagnose_and_repair", "prepare_run") in edges


def test_publication_failure_does_not_leave_final_results(tmp_path, monkeypatch):
    from scan_agent import workflow
    output = tmp_path / "output"
    writer = workflow.write_decision_log
    def fail_success_only(root, payload, *args):
        if payload["status"] == "success":
            raise OSError("fixture publication failure")
        return writer(root, payload, *args)
    monkeypatch.setattr(workflow, "write_decision_log", fail_success_only)
    result = run_agent(make_task1_case(tmp_path), output, fake_dependencies("success"))
    assert result.status == "tool_failure"
    assert result.final_run is None
    assert not (output / "final_results").exists()
    assert (output / "runs/R1/unpublished_final/deliverables/post_scan.v").is_file()


def test_promotion_deadline_returns_honest_budget_failure_audit(tmp_path, monkeypatch):
    from scan_agent import workflow

    def expire(*args):
        raise DeadlineExceeded("artifact promotion deadline reached")

    monkeypatch.setattr(workflow, "promote_final", expire)
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, fake_dependencies("success"))
    payload = audit(output)
    assert result.status == payload["status"] == "budget_exhausted"
    assert payload["failure_reason"] == "artifact promotion deadline reached"
    assert payload["tool_runs"] and payload["validation_results"]
    assert not (output / "final_results").exists()


def test_initial_link_scan_deadline_precedes_limitations_read(tmp_path, monkeypatch):
    from scan_agent import workflow

    case = make_task1_case(tmp_path)
    deps = fake_dependencies("success")
    deps.ready_waiter = lambda *args: 0.0
    deps.clock = lambda: 10.0
    observed = []

    def expire(root, deadline_monotonic=None, clock=None):
        observed.append(deadline_monotonic)
        raise DeadlineExceeded("input traversal deadline reached")

    def forbidden_read(*args, **kwargs):
        pytest.fail("limitations.md must not be read after an expired link scan")

    monkeypatch.setattr(workflow, "reject_input_links", expire)
    monkeypatch.setattr(workflow, "_read_text", forbidden_read)
    output = tmp_path / "output"
    result = run_agent(case, output, deps)

    assert observed == [workflow._STARTUP_INPUT_SCAN_SECONDS]
    assert result.status == "budget_exhausted"
    assert audit(output) == {
        "status": "budget_exhausted",
        "final_run": None,
        "failure_reason": "absolute deadline reached during failure finalization",
    }


def test_expired_failure_audit_uses_one_minimal_atomic_fallback(tmp_path, monkeypatch):
    from scan_agent import workflow

    calls = []

    def expire(*args, **kwargs):
        calls.append((args, kwargs))
        raise DeadlineExceeded("requirement audit deadline reached")

    monkeypatch.setattr(workflow, "_publish_decision", expire)
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, fake_dependencies("tool_unavailable"))

    assert len(calls) == 1
    assert result.status == "budget_exhausted"
    assert audit(output) == {
        "status": "budget_exhausted",
        "final_run": None,
        "failure_reason": "absolute deadline reached during failure finalization",
    }


def test_task2_static_diagnostics_reach_repairer(tmp_path):
    case = make_task1_case(tmp_path)
    original = "# original\n" + SAFE_DOFILE.replace("insert_scan", "exec forbidden")
    (case / "original.dofile").write_text(original, encoding="utf-8")
    deps = fake_dependencies("success")
    repairer = deps.repairer
    observed = []
    def repair(client, requirements, current, diagnostics, history, chunks):
        assert current == original
        assert not history
        assert any("exec forbidden" in item.source_line for item in diagnostics.fatal_errors)
        observed.append(current)
        return repairer(client, requirements, current, diagnostics, history, chunks)
    deps.repairer = repair
    assert run_agent(case, tmp_path / "output", deps).status == "success"
    assert observed == [original]


def test_missing_required_output_cannot_be_promoted(tmp_path):
    output = tmp_path / "output"
    deps = scripted_dependencies([successful_scan_result()] * 3)
    runner = deps.tool_runner
    def missing(*args):
        result = runner(*args)
        (args[0].deliverables / "post_scan.v").unlink()
        return replace(result, produced_files=tuple(name for name in result.produced_files if not name.endswith("post_scan.v")))
    deps.tool_runner = missing
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert not (output / "final_results").exists()
    assert len(audit(output)["tool_runs"]) == 3


def test_input_mutation_during_invalid_repair_still_is_compliance_failure(tmp_path):
    case = make_task1_case(tmp_path)
    output = tmp_path / "output"
    deps = scripted_dependencies([failed_cmd_result()])
    def invalid_repair(*args):
        (case / "lib/stdcells.lib").write_text("changed", encoding="utf-8")
        raise LLMOutputError("fixture invalid repair")
    deps.repairer = invalid_repair
    assert run_agent(case, output, deps).status == "compliance_failure"
    assert not (output / "final_results").exists()


def test_injected_repair_repeating_failed_hash_is_stopped(tmp_path):
    from scan_agent.dofile import DofileProposal
    deps = scripted_dependencies([failed_cmd_result()], [SAFE_DOFILE])
    deps.repairer = lambda *args: DofileProposal("dofile", "repeat", (), "same", SAFE_DOFILE)
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "no_progress"
    assert len(audit(output)["tool_runs"]) == 1


def test_work_outputs_are_collected_for_validation(tmp_path):
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    runner = deps.tool_runner
    def work_output(*args):
        result = runner(*args)
        source = args[0].deliverables / "post_scan.v"
        source.replace(args[0].work / "post_scan.v")
        return replace(result, produced_files=("work/post_scan.v",))
    deps.tool_runner = work_output
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "success"
    assert (output / "final_results/deliverables/post_scan.v").is_file()


def test_route_precedence_for_terminal_conditions():
    from scan_agent.state import Budget
    from scan_agent.validation import ValidationReport
    from scan_agent.workflow import _terminal_failure, route_after_validation
    failed = ValidationReport(("failed",), (), (), (), True, ())
    state = {"validation": failed, "budget": Budget(0, 120, 3), "now": 120,
             "current_run": 3, "requires_netlist_repair": True, "repeated_dofile": True,
             "status": "", "tool_result": failed_cmd_result()}
    assert route_after_validation(state) == "failure"
    assert _terminal_failure(state)["status"] == "unsupported_netlist_repair"
    state["validation"] = replace(failed, input_integrity=False)
    assert _terminal_failure(state)["status"] == "compliance_failure"
    state["validation"] = failed
    state["requires_netlist_repair"] = False
    assert _terminal_failure(state)["failure_reason"] == "wall-time reserve reached"
    state["now"] = 0
    assert _terminal_failure(state)["failure_reason"] == "tool-run limit reached"
    state["current_run"] = 1
    assert _terminal_failure(state)["status"] == "no_progress"
    state["validation"] = replace(failed, failures=())
    assert route_after_validation(state) == "success"


def test_task2_netlist_diagnosis_stops_before_any_tool_run(tmp_path):
    case = make_task1_case(tmp_path)
    (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    deps = scripted_dependencies([], diagnoses=[diagnosis("requires_netlist_repair")])
    output = tmp_path / "output"
    result = run_agent(case, output, deps)
    assert result.status == "unsupported_netlist_repair"
    assert audit(output)["tool_runs"] == []


def test_fractional_wall_time_remains_valid_after_monotonic_start(tmp_path):
    case = make_task1_case(tmp_path)
    (case / "limitations.md").write_text("wall time: 120.1 seconds", encoding="utf-8")
    deps = fake_dependencies("success")
    deps.ready_waiter = lambda *args: 987654.321
    deps.clock = lambda: 987654.321
    result = run_agent(case, tmp_path / "output", deps)
    assert result.status == "success"


def test_unsafe_injected_candidate_is_invalid_model_output(tmp_path):
    from scan_agent.dofile import DofileProposal
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    deps.initial_generator = lambda *args: DofileProposal("dofile", "unsafe", (), "unsafe", "exec forbidden")
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "invalid_model_output"
    assert audit(output)["tool_runs"] == []


def test_license_failure_is_not_sent_for_dofile_repair(tmp_path):
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    runner = deps.tool_runner
    def licensed(*args):
        result = runner(*args)
        args[0].log.write_text("[ERROR] License checkout failed\n", encoding="utf-8")
        return result
    deps.tool_runner = licensed
    deps.repairer = lambda *args: pytest.fail("license failure cannot be fixed by a dofile")
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "tool_failure"
    assert len(audit(output)["tool_runs"]) == 1


def test_invalid_runner_outcome_preserves_existing_metadata(tmp_path):
    from scan_agent.artifacts import write_json_atomic
    output = tmp_path / "output"
    deps = fake_dependencies("success")
    metadata = {"command": ["real fixture tool"], "exit_code": 77}
    def invalid(paths, *args):
        write_json_atomic(paths.root / "run_metadata.json", metadata)
        paths.log.write_text("actual stdout", encoding="utf-8")
        return None
    deps.tool_runner = invalid
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "tool_failure"
    assert json.loads((output / "runs/R1/run_metadata.json").read_text(encoding="utf-8")) == metadata


def timed_dependencies(deps):
    now = [0.0]
    deps.ready_waiter = lambda *args: 0.0
    deps.clock = lambda: now[0]
    return now


def test_late_extraction_does_not_start_generation_or_load_manual(tmp_path):
    deps = fake_dependencies("success")
    now = timed_dependencies(deps)
    extract = deps.requirement_extractor
    calls = []
    def late(*args):
        calls.append("extract")
        requirements = extract(*args)
        now[0] = 115.0
        return requirements
    deps.requirement_extractor = late
    deps.initial_generator = lambda *args: calls.append("generate")
    deps.manual_loader = lambda *args: calls.append("manual")
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert calls == ["extract"]
    assert audit(output)["tool_runs"] == []
    assert not (output / "final_results").exists()


def test_late_extraction_error_retains_budget_for_terminal_precedence(tmp_path):
    deps = fake_dependencies("success")
    now = timed_dependencies(deps)
    def invalid(*args):
        now[0] = 125.0
        raise LLMOutputError("late invalid output")
    deps.requirement_extractor = invalid
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert audit(output) == {
        "status": "budget_exhausted",
        "final_run": None,
        "failure_reason": "absolute deadline reached during failure finalization",
    }


def test_extraction_never_starts_when_only_reserve_remains(tmp_path):
    case = make_task1_case(tmp_path)
    (case / "limitations.md").write_text("wall time: 10 seconds", encoding="utf-8")
    deps = fake_dependencies("success")
    timed_dependencies(deps)
    calls = []
    deps.requirement_extractor = lambda *args: calls.append("extract")
    result = run_agent(case, tmp_path / "output", deps)
    assert result.status == "budget_exhausted"
    assert calls == []


@pytest.mark.parametrize("task2", [False, True])
def test_manual_loading_reaches_reserve_before_candidate_model(tmp_path, task2):
    case = make_task1_case(tmp_path)
    if task2:
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    deps = fake_dependencies("success")
    now = timed_dependencies(deps)
    loader = deps.manual_loader
    calls = []
    def late_manual(*args):
        result = loader(*args)
        now[0] = 110.0
        return result
    deps.manual_loader = late_manual
    deps.initial_generator = lambda *args: calls.append("generate")
    deps.repairer = lambda *args: calls.append("repair")
    output = tmp_path / "output"
    assert run_agent(case, output, deps).status == "budget_exhausted"
    assert calls == []
    assert audit(output)["tool_runs"] == []


@pytest.mark.parametrize("stage", ["generate", "task2_repair", "repair"])
def test_late_candidate_model_never_prepares_another_run(tmp_path, stage):
    case = make_task1_case(tmp_path)
    if stage == "task2_repair":
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    deps = scripted_dependencies([failed_cmd_result()] if stage == "repair" else [])
    now = timed_dependencies(deps)
    attr = "initial_generator" if stage == "generate" else "repairer"
    model = getattr(deps, attr)
    def late(*args):
        proposal = model(*args)
        now[0] = 115.0
        return proposal
    setattr(deps, attr, late)
    output = tmp_path / "output"
    result = run_agent(case, output, deps)
    assert result.status == "budget_exhausted"
    assert result.final_run is None
    runs = 1 if stage == "repair" else 0
    assert len(audit(output)["tool_runs"]) == runs
    assert len(audit(output)["proposals"]) == runs
    assert not (output / f"runs/R{runs + 1}").exists()
    assert not (output / "final_results").exists()
    if runs:
        assert (output / "runs/R1/run_metadata.json").is_file()
        assert (output / "runs/R1/R1.log").is_file()


def test_transport_requests_and_correction_receive_remaining_timeout(tmp_path):
    deps = fake_dependencies("success")
    now = timed_dependencies(deps)
    transport = deps.client.transport
    timeouts = []
    def timed(**request):
        timeouts.append(request.get("timeout"))
        response = transport(**request)
        if len(timeouts) == 1:
            now[0] = 20.0
            return "invalid JSON requiring correction"
        return response
    deps.client.transport = timed
    assert run_agent(make_task1_case(tmp_path), tmp_path / "output", deps).status == "success"
    assert timeouts == [110.0, 90.0, 90.0]


def test_transport_retry_rechecks_remaining_timeout(tmp_path):
    deps = fake_dependencies("success")
    deps.client.max_retries = 2
    now = timed_dependencies(deps)
    transport = deps.client.transport
    timeouts = []
    def timed(**request):
        timeouts.append(request.get("timeout"))
        if len(timeouts) == 1:
            now[0] = 109.0
            raise RuntimeError("fixture connection failure")
        return transport(**request)
    deps.client.transport = timed
    assert run_agent(make_task1_case(tmp_path), tmp_path / "output", deps).status == "success"
    assert timeouts == [110.0, 1.0, 1.0]


@pytest.mark.parametrize("failure", ["invalid_json", "transport_error"])
def test_reserve_prevents_transport_correction_or_retry(tmp_path, failure):
    deps = fake_dependencies("success")
    deps.client.max_retries = 2
    now = timed_dependencies(deps)
    calls = []
    def late_transport(**request):
        calls.append(request)
        now[0] = 110.0
        if failure == "transport_error":
            raise RuntimeError("fixture connection failure")
        return {"choices": [{"message": {"content": "invalid JSON"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    deps.client.transport = late_transport
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, deps)
    assert result.status == "budget_exhausted"
    assert len(calls) == 1
    assert audit(output)["tool_runs"] == []
    assert len(audit(output)["token_usage"]) == (1 if failure == "invalid_json" else 0)


def test_repair_never_starts_if_retrieval_reaches_reserve(tmp_path, monkeypatch):
    from scan_agent import workflow
    deps = scripted_dependencies([failed_cmd_result()])
    now = timed_dependencies(deps)
    chunks = workflow._chunks
    calls = []
    def late_retrieval(state, terms, *args, **kwargs):
        if state["current_run"] == 1:
            now[0] = 110.0
        return chunks(state, terms, *args, **kwargs)
    monkeypatch.setattr(workflow, "_chunks", late_retrieval)
    deps.repairer = lambda *args: calls.append("repair")
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "budget_exhausted"
    assert calls == []
    assert len(audit(output)["tool_runs"]) == 1
    assert not (output / "runs/R2").exists()


def test_retry_limit_is_preserved_when_time_remains(tmp_path):
    deps = fake_dependencies("success")
    deps.client.max_retries = 2
    timed_dependencies(deps)
    calls = []
    def unavailable(**request):
        calls.append(request["timeout"])
        raise RuntimeError("fixture connection failure")
    deps.client.transport = unavailable
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "invalid_model_output"
    assert calls == [110.0, 110.0, 110.0]


@pytest.mark.parametrize("task2", [False, True])
def test_repair_requests_receive_budget_left_after_failed_tool(tmp_path, task2):
    case = make_task1_case(tmp_path)
    if task2:
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    deps = scripted_dependencies([failed_cmd_result(), successful_scan_result()])
    now = timed_dependencies(deps)
    transport, runner = deps.client.transport, deps.tool_runner
    timeouts = []
    def timed(**request):
        timeouts.append(request["timeout"])
        return transport(**request)
    def consumes(*args):
        result = runner(*args)
        now[0] = 50.0
        return result
    deps.client.transport, deps.tool_runner = timed, consumes
    assert run_agent(case, tmp_path / "output", deps).status == "success"
    assert timeouts == [110.0, 110.0, 60.0]


@pytest.mark.parametrize("stage", ["extract", "generate", "task2_repair", "repair"])
def test_absolute_request_deadline_returns_before_blocked_transport_finishes(tmp_path, stage):
    case = make_task1_case(tmp_path)
    (case / "limitations.md").write_text("wall time: 10.05 seconds", encoding="utf-8")
    if stage == "task2_repair":
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    deps = scripted_dependencies([failed_cmd_result()] if stage == "repair" else [])
    deps.client.max_retries = 2
    timed_dependencies(deps)
    transport = deps.client.transport
    release = threading.Event()
    completed = threading.Event()
    workers = []
    calls = []
    target = 1 if stage == "extract" else (3 if stage == "repair" else 2)

    def blocked(**request):
        calls.append(request)
        response = transport(**request)
        if len(calls) == target:
            workers.append(threading.current_thread())
            release.wait(1.0)
            completed.set()
            return {"choices": [{"message": {"content": response}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return response

    deps.client.transport = blocked
    output = tmp_path / "output"
    try:
        result = run_agent(case, output, deps)
        assert result.status == "budget_exhausted"
        assert not completed.is_set()
        assert workers[0].daemon
        assert workers[0].is_alive()
        assert len(calls) == target
        before = (output / "decision_log.json").read_bytes()
        assert deps.client.usage_history == []
        assert len(audit(output)["tool_runs"]) == (1 if stage == "repair" else 0)
        assert not (output / "final_results").exists()
        release.set()
        workers[0].join(timeout=1.0)
        assert not workers[0].is_alive()
        assert (output / "decision_log.json").read_bytes() == before
        assert deps.client.usage_history == []
        assert len(calls) == target
    finally:
        release.set()


def test_chunk_progress_does_not_extend_total_request_deadline(tmp_path):
    case = make_task1_case(tmp_path)
    (case / "limitations.md").write_text("wall time: 10.05 seconds", encoding="utf-8")
    deps = fake_dependencies("success")
    timed_dependencies(deps)
    transport = deps.client.transport
    release = threading.Event()
    progress = []
    workers = []
    calls = []

    def chunks(**request):
        calls.append(request)
        workers.append(threading.current_thread())
        # Each simulated read progresses before its .05s operation timeout,
        # while the full response takes more than the total request budget.
        for chunk in range(20):
            if release.wait(0.01):
                break
            progress.append(chunk)
        return transport(**request)

    deps.client.transport = chunks
    output = tmp_path / "output"
    try:
        result = run_agent(case, output, deps)
        assert result.status == "budget_exhausted"
        assert len(calls) == 1
        assert workers[0].is_alive()
        assert len(progress) < 20
        assert calls[0]["timeout"] == pytest.approx(0.05)
        assert audit(output)["tool_runs"] == []
        assert not (output / "final_results").exists()
    finally:
        release.set()
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=1.0)
