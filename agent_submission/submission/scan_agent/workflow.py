"""Thin LangGraph orchestration around deterministic scan boundaries."""

from dataclasses import asdict, dataclass, field
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from queue import Empty, Queue
from threading import Event, Thread
import time
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .artifacts import (FinalManifest, RunPaths, build_issue_resolutions, build_requirement_mapping,
                        build_tool_runs, create_run, promote_final, quarantine_existing_evidence,
                        owned_output_entries,
                        validate_references, verify_final_manifest,
                        write_decision_log, write_dofile_diff, write_json_atomic, write_run_dofile)
from .diagnostics import DiagnosticSummary, parse_tool_log
from .deadline import DeadlineExceeded, check_deadline, iter_paths_with_deadline
from .dofile import (DofileProposal, RepairRecord, _proposal, generate_initial_dofile,
                     repair_dofile, validate_dofile_candidate)
from .inputs import (InputMutationError, assert_inputs_unchanged, classify_task,
                     hash_protected_inputs, inventory_inputs, parse_limits,
                     reject_input_links, require_semantic_input, wait_for_case_ready)
from .llm import (LLMClient, LLMConfigurationError, LLMOutputError, LLMTransportError,
                  Requirements, _requirements, extract_requirements, requirements_data)
from .manual import ManualChunk, ManualIndex, ManualLoadResult, load_manual
from .runner import LaunchMode, ToolResult, run_scan_tool
from .reports import ReportEvidence, collect_report_evidence
from .state import AgentStatus, Budget, InputInventory
from .validation import ValidationReport, validate_run


_STARTUP_INPUT_SCAN_SECONDS = 10.0
_DEADLINE_FAILURE_REASON = "absolute deadline reached during failure finalization"


@dataclass
class WorkflowDependencies:
    """Production collaborators are replaceable only through explicit injection."""

    client: LLMClient
    requirement_extractor: Callable = extract_requirements
    initial_generator: Callable = generate_initial_dofile
    repairer: Callable = repair_dofile
    manual_loader: Callable = load_manual
    tool_runner: Callable = run_scan_tool
    clock: Callable[[], float] = time.monotonic
    ready_waiter: Callable = wait_for_case_ready
    executable: tuple[str, ...] = ("dftexp_scan",)
    launch_mode: LaunchMode = "file_flag"
    env: dict[str, str] = field(default_factory=dict)
    manual_path: Path = Path("/opt/dftexp_scan/doc/Scan_User_Manual.pdf")

    @classmethod
    def from_env(cls) -> "WorkflowDependencies":
        mode = os.environ.get("DFTEXP_SCAN_LAUNCH_MODE", "file_flag")
        if mode not in {"file_flag", "stdin_source"}:
            raise LLMConfigurationError("DFTEXP_SCAN_LAUNCH_MODE must be file_flag or stdin_source")
        return cls(client=LLMClient.from_env(), launch_mode=mode)


@dataclass(frozen=True)
class AgentResult:
    status: AgentStatus
    final_run: str | None
    failure_reason: str | None = None


class WorkflowState(TypedDict, total=False):
    input_dir: Path
    output_dir: Path
    started_at: float
    startup_deadline: float
    task_type: str
    inventory: InputInventory
    protected_hashes: dict[str, str]
    budget: Budget
    requirements: Requirements
    manual: ManualLoadResult
    manual_error: str | None
    current_dofile: str
    previous_dofile: str | None
    current_run: int
    paths: RunPaths
    tool_result: ToolResult
    diagnostics: DiagnosticSummary
    report_evidence: ReportEvidence
    validation: ValidationReport
    now: float
    requires_netlist_repair: bool
    repeated_dofile: bool
    history: list[RepairRecord]
    tool_runs: list[dict]
    file_changes: list[dict]
    validation_results: list[dict]
    proposals: list[dict]
    token_usage: list[dict]
    status: str
    failure_reason: str | None
    final_run: str | None
    final_manifest: FinalManifest
    decision_published: bool


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _failure(status: AgentStatus, reason: str) -> dict:
    return {"status": status, "failure_reason": reason, "final_run": None}


class _ModelBudgetExhausted(RuntimeError):
    """No model work may begin or continue beyond the finalization reserve."""


def _request_with_deadline(transport: Callable, request: dict, seconds: float,
                           deadline: float, cancelled: Event) -> object:
    """Wait only to the total deadline; overdue network work owns no graph state.

    The worker is deliberately a daemon, with no executor shutdown or join.
    It receives a detached request and may only deliver its result/exception
    to this private queue. JSON parsing and usage recording stay on the caller.
    """
    snapshot = {**deepcopy(request), "timeout": seconds}
    outcomes: Queue[tuple[bool, object, float]] = Queue(maxsize=1)

    def worker():
        if cancelled.is_set() or time.monotonic() >= deadline:
            return
        try:
            result = transport(**snapshot)
        except Exception as error:
            outcome = (False, error, time.monotonic())
        else:
            outcome = (True, result, time.monotonic())
        if not cancelled.is_set():
            outcomes.put_nowait(outcome)

    Thread(target=worker, name="scan-agent-model-request", daemon=True).start()
    try:
        succeeded, result, completed = outcomes.get(timeout=max(0.0, deadline - time.monotonic()))
    except Empty:
        cancelled.set()
        raise _ModelBudgetExhausted("total model-request deadline reached") from None
    if completed >= deadline:
        cancelled.set()
        raise _ModelBudgetExhausted("total model-request deadline reached")
    if not succeeded:
        raise result
    return result


def _integrity(
    state: WorkflowState,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    if state.get("protected_hashes"):
        assert_inputs_unchanged(
            state["input_dir"], state["protected_hashes"], deadline_monotonic, clock,
        )


def _read_text(
    path: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
    *,
    maximum_bytes: int | None = None,
) -> str:
    chunks = []
    total = 0
    with path.open("rb") as source:
        while True:
            check_deadline(deadline_monotonic, clock, "input read")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum_bytes is not None and total > maximum_bytes:
                raise ValueError(f"input document exceeds {maximum_bytes} bytes: {path.name}")
            chunks.append(chunk)
    check_deadline(deadline_monotonic, clock, "input read")
    return b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _same_files(
    first: Path,
    second: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
) -> bool:
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            check_deadline(deadline_monotonic, clock, "work artifact comparison")
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def route_after_validation(state: WorkflowState) -> str:
    """Order is deliberate: evidence, compliance, unsupported work, then budgets."""
    report = state.get("validation")
    if report is not None and report.passed and not state.get("status"):
        return "success"
    if report is not None and not report.input_integrity:
        return "failure"
    if state.get("requires_netlist_repair"):
        return "failure"
    budget = state.get("budget")
    if budget is not None and budget.remaining(state["now"]) <= budget.reserve_seconds:
        return "failure"
    if budget is not None and state["current_run"] >= budget.max_tool_runs:
        return "failure"
    if state.get("repeated_dofile"):
        return "failure"
    result = state.get("tool_result")
    if state.get("status") or result is None or result.timed_out or result.failure_kind not in {None, "nonzero_exit"}:
        return "failure"
    if state["diagnostics"].license_errors:
        return "failure"
    return "repair"


def _terminal_failure(state: WorkflowState) -> dict:
    # Mirror the route priority to preserve the exact terminal explanation.
    report = state.get("validation")
    if report is not None and not report.input_integrity:
        return _failure(AgentStatus.COMPLIANCE_FAILURE, "protected input integrity failed")
    if state.get("requires_netlist_repair"):
        return _failure(AgentStatus.UNSUPPORTED_NETLIST_REPAIR, "diagnosis requires pre-scan netlist repair")
    budget = state.get("budget")
    if budget and budget.remaining(state["now"]) <= budget.reserve_seconds:
        return _failure(AgentStatus.BUDGET_EXHAUSTED, "wall-time reserve reached")
    if budget and state["current_run"] >= budget.max_tool_runs:
        return _failure(AgentStatus.BUDGET_EXHAUSTED, "tool-run limit reached")
    if state.get("repeated_dofile"):
        return _failure(AgentStatus.NO_PROGRESS, "repeated previously failed dofile")
    if state.get("status"):
        return _failure(AgentStatus(state["status"]), state.get("failure_reason") or "workflow failed")
    result = state.get("tool_result")
    if result and result.timed_out:
        return _failure(AgentStatus.BUDGET_EXHAUSTED, "tool timeout exhausted its run budget")
    return _failure(AgentStatus.TOOL_FAILURE, result.failure_kind if result and result.failure_kind else "scan validation failed")


def _chunks(
    state: WorkflowState,
    terms: list[str],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list:
    manual = state.get("manual")
    return manual.index.search(
        terms,
        deadline_monotonic=deadline_monotonic,
        clock=clock,
        semantic_query=(
            f"Task type: {state.get('task_type', '')}. Retrieval focus: {'; '.join(terms)}. "
            f"Scan requirements: {json.dumps(requirements_data(state['requirements']), ensure_ascii=False, sort_keys=True)}"
            if "requirements" in state else "; ".join(terms)
        ),
    ) if manual and manual.index else []


def _accept_proposal(proposal: object, state: WorkflowState) -> dict:
    if not isinstance(proposal, DofileProposal):
        raise LLMOutputError("proposal must be a DofileProposal")
    # Reuse the model boundary schema even for injected collaborators.
    data = asdict(proposal)
    data["evidence"] = list(data["evidence"])
    try:
        proposal = _proposal(data, set())
    except (ValueError, TypeError) as error:
        raise LLMOutputError(f"invalid proposal: {error}") from error
    if proposal.evidence:
        try:
            validate_references(state["output_dir"], {"evidence": [asdict(item) for item in proposal.evidence]})
        except ValueError as error:
            raise LLMOutputError(f"invalid proposal evidence: {error}") from error
    path = state["output_dir"] / "candidates" / f"candidate_R{state['current_run'] + 1}.json"
    write_json_atomic(path, data)
    proposals = [*state["proposals"], {"path": path.relative_to(state["output_dir"]).as_posix()}]
    if proposal.problem_type == "requires_netlist_repair":
        return {"requires_netlist_repair": True, "proposals": proposals}
    repeated = _hash(proposal.dofile) in {record.dofile_hash for record in state["history"]}
    return {"previous_dofile": state.get("current_dofile"), "current_dofile": proposal.dofile,
            "repeated_dofile": repeated, "proposals": proposals}


def _collect_work(
    paths: RunPaths,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Archive actual work products so validation and promotion share one layout."""
    for source in iter_paths_with_deadline(
        paths.work, deadline_monotonic, clock, "work artifact collection",
    ):
        if source.is_symlink() or not source.resolve().is_relative_to(paths.work.resolve()):
            raise ValueError("tool work artifact escapes its run directory")
        if source.is_file():
            relative = source.relative_to(paths.work)
            if relative.parts and relative.parts[0] in {"deliverables", "reports"}:
                directory = paths.deliverables if relative.parts[0] == "deliverables" else paths.reports
                relative = Path(*relative.parts[1:])
            else:
                directory = paths.reports if source.suffix.lower() in {".rpt", ".txt", ".log"} else paths.deliverables
            target = directory / relative
            if target.exists():
                if not _same_files(source, target, deadline_monotonic, clock):
                    raise ValueError("conflicting tool artifacts")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as reader, target.open("wb") as writer:
                    while True:
                        check_deadline(deadline_monotonic, clock, "work artifact collection")
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
                shutil.copystat(source, target)


def _check_tool_result(result: object, paths: RunPaths) -> ToolResult:
    if (not isinstance(result, ToolResult) or type(result.timed_out) is not bool
            or (result.exit_code is not None and type(result.exit_code) is not int)
            or type(result.duration_seconds) not in (int, float)
            or not math.isfinite(result.duration_seconds) or result.duration_seconds < 0
            or not isinstance(result.log_path, Path) or result.log_path.resolve() != paths.log.resolve()
            or not isinstance(result.produced_files, tuple)
            or any(not isinstance(name, str) for name in result.produced_files)
            or (result.failure_kind is not None and not isinstance(result.failure_kind, str))):
        raise ValueError("invalid tool result")
    for name in result.produced_files:
        validate_references(paths.root, {"path": name})
    return result


def _publish_decision(
    state: WorkflowState,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    output = state["output_dir"]
    runs = build_tool_runs(output, state["tool_runs"], deadline_monotonic, clock)
    configurations = [{"source": run["dofile_file"], "locator": "complete executed dofile"} for run in runs]
    requirements = requirements_data(state["requirements"]) if state.get("requirements") else {}
    manual = state.get("manual")
    response_dir = output / "model_responses"
    responses = () if not response_dir.is_dir() else (
        path for path in iter_paths_with_deadline(
            response_dir, deadline_monotonic, clock, "model-response audit",
        ) if path.parent == response_dir and path.match("response_*.json")
    )
    payload = {
        "status": state["status"], "final_run": state.get("final_run"),
        "failure_reason": state.get("failure_reason"), "task_type": state.get("task_type"),
        "manual_error": state.get("manual_error"),
        "manual": {
            "available": manual.available if manual else None,
            "error": state.get("manual_error"),
            "semantic_available": manual.semantic_available if manual else None,
            "semantic_error": manual.semantic_error if manual else None,
            "retrieval": "hybrid_qwen3_keyword" if manual and manual.semantic_available else "keyword_only",
        },
        "requirement_mapping": build_requirement_mapping(
            requirements, configurations, state["validation_results"], deadline_monotonic, clock,
        ),
        "issue_resolutions": build_issue_resolutions(
            output, runs, state["validation_results"], deadline_monotonic, clock,
        ),
        "tool_runs": runs,
        "file_changes": state["file_changes"], "validation_results": state["validation_results"],
        "proposals": state["proposals"], "token_usage": state.get("token_usage", []),
        "token_usage_available": bool(state.get("token_usage")),
        "model_responses": [{"path": path.relative_to(output).as_posix()} for path in responses],
    }
    if state["status"] == AgentStatus.SUCCESS:
        if any(run.get("metadata_error") for run in runs):
            raise ValueError("selected evidence contains invalid run metadata")
        payload["final_artifacts"] = verify_final_manifest(
            output, state["final_manifest"], deadline_monotonic, clock,
        )
    write_decision_log(output, payload, deadline_monotonic, clock)


def _publish_minimal_deadline_failure(output_dir: Path) -> dict:
    """Atomically publish fixed-size truthful state after an expired deadline."""
    update = _failure(AgentStatus.BUDGET_EXHAUSTED, _DEADLINE_FAILURE_REASON)
    write_json_atomic(output_dir / "decision_log.json", update)
    return update


def build_workflow(dependencies: WorkflowDependencies) -> CompiledStateGraph:
    def final_deadline(state):
        budget = state.get("budget")
        return budget.deadline_monotonic if budget is not None else state.get("startup_deadline")

    def work_deadline(state):
        budget = state["budget"]
        return budget.deadline_monotonic - budget.reserve_seconds

    def model_seconds(state):
        budget = state["budget"]
        remaining = budget.remaining(dependencies.clock()) - budget.reserve_seconds
        if remaining <= 0:
            raise _ModelBudgetExhausted("wall-time reserve reached")
        return remaining

    def call_model(state, collaborator, *args):
        _integrity(state, work_deadline(state), dependencies.clock)
        # Sample the real clock before the injected budget clock so this
        # absolute guard never extends the case's remaining wall time.
        wall_started = time.monotonic()
        wall_deadline = wall_started + model_seconds(state)
        transport = dependencies.client.transport
        cancelled = Event()

        def bounded_transport(**request):
            if cancelled.is_set() or time.monotonic() >= wall_deadline:
                cancelled.set()
                raise _ModelBudgetExhausted("total model-request deadline reached")
            # OpenAI's per-request timeout is recomputed for every correction
            # and retry. The daemon guard also bounds the full response,
            # independently of HTTP connect/read/write/pool progress.
            raw = _request_with_deadline(transport, request, model_seconds(state), wall_deadline, cancelled)
            # Keep received responses (including schema-rejected candidates) on
            # the workflow thread. Late daemon outcomes never reach this writer.
            def member(value, name, default=None):
                return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)
            content = raw if isinstance(raw, str) else None
            choices = member(raw, "choices", [])
            if content is None and isinstance(choices, (list, tuple)) and choices:
                content = member(member(choices[0], "message"), "content")
            directory = state["output_dir"] / "model_responses"
            number = len(list(directory.glob("response_*.json"))) + 1
            write_json_atomic(directory / f"response_{number:04d}.json", {
                "model": client.model, "content": content if isinstance(content, str) else None})
            return raw

        client = LLMClient(transport=bounded_transport, model=dependencies.client.model,
                           max_retries=dependencies.client.max_retries)
        # Retain usage from every response, including rejected/late responses.
        client.usage_history = dependencies.client.usage_history
        try:
            return collaborator(client, *args)
        finally:
            if cancelled.is_set() or time.monotonic() >= wall_deadline:
                raise _ModelBudgetExhausted("total model-request deadline reached")
            # A slow or failing injected collaborator cannot authorize later
            # stages after it returns; a reached deadline wins over its error.
            model_seconds(state)

    def inventory_input(state):
        # The first metadata-only traversal rejects every symlink/reparse point
        # before any case file is opened semantically.
        startup_deadline = state["started_at"] + _STARTUP_INPUT_SCAN_SECONDS
        reject_input_links(state["input_dir"], startup_deadline, dependencies.clock)
        limitations = _read_text(
            require_semantic_input(state["input_dir"], "limitations.md"), startup_deadline, dependencies.clock,
            maximum_bytes=1024 * 1024,
        )
        budget = parse_limits(limitations, state["started_at"])
        deadline = budget.deadline_monotonic - budget.reserve_seconds
        check_deadline(deadline, dependencies.clock, "input preparation")
        inventory = inventory_inputs(state["input_dir"], deadline, dependencies.clock)
        hashes = hash_protected_inputs(state["input_dir"], deadline, dependencies.clock)
        return {"inventory": inventory, "task_type": classify_task(state["input_dir"]),
                "protected_hashes": hashes, "budget": budget,
                "startup_deadline": startup_deadline}

    def requirements_node(state):
        root = state["input_dir"]
        deadline = work_deadline(state)
        limitations = _read_text(require_semantic_input(root, "limitations.md"), deadline, dependencies.clock)
        # Inventory returned the deterministic budget through a graph update,
        # so extraction exceptions cannot erase it from later routing.
        budget = state["budget"]
        requirements = call_model(state, dependencies.requirement_extractor,
            _read_text(require_semantic_input(root, "task_spec.md"), deadline, dependencies.clock),
            limitations, state["inventory"], [])
        if not isinstance(requirements, Requirements):
            raise LLMOutputError("extractor must return Requirements")
        try:
            _requirements(requirements_data(requirements), state["inventory"],
                          parse_limits(limitations, 0).deadline_monotonic, budget.max_tool_runs)
        except (ValueError, TypeError) as error:
            raise LLMOutputError(f"invalid requirements: {error}") from error
        write_json_atomic(state["output_dir"] / "requirements.json", requirements_data(requirements))
        return {"requirements": requirements, "budget": budget}

    def manual_node(state):
        manual = dependencies.manual_loader(
            dependencies.manual_path, work_deadline(state), dependencies.clock,
        )
        if (not isinstance(manual, ManualLoadResult) or type(manual.available) is not bool
                or (manual.available and not isinstance(manual.index, ManualIndex))
                or (not manual.available and manual.index is not None)
                or (manual.index is not None and any(not isinstance(chunk, ManualChunk) for chunk in manual.index.chunks))):
            raise ValueError("invalid manual result")
        return {"manual": manual, "manual_error": manual.error}

    def repair_attempt(state, found, current, diagnostics, history, terms):
        # Persist the attempt before any model/manual work that can fail or run
        # out of budget. Graph updates are not committed when a node raises.
        directory = state["output_dir"] / "repairs"
        number = len(list(directory.glob("repair_*.json"))) + 1
        path = directory / f"repair_{number:04d}.json"
        record = {"found": found, "candidate_run": f"R{state['current_run'] + 1}",
                  "history": [asdict(item) for item in history], "diagnosis": None, "fix": None,
                  "outcome": "started"}
        write_json_atomic(path, record)
        responses_before = set((state["output_dir"] / "model_responses").glob("response_*.json"))
        try:
            proposal = call_model(state, dependencies.repairer, state["requirements"], current,
                                  diagnostics, history, _chunks(
                                      state, terms, work_deadline(state), dependencies.clock,
                                  ))
            update = _accept_proposal(proposal, state)
            record.update(diagnosis={"problem_type": proposal.problem_type, "root_cause": proposal.root_cause,
                                     "evidence": [asdict(item) for item in proposal.evidence]},
                          fix={"summary": proposal.repair_summary, "dofile_hash": _hash(proposal.dofile),
                               "candidate_file": update["proposals"][-1]["path"]},
                          outcome="requires_netlist_repair" if update.get("requires_netlist_repair") else
                                  "no_progress" if update.get("repeated_dofile") else "candidate_accepted")
            return update
        except Exception as error:
            repeated = isinstance(error, LLMOutputError) and "repeated previously failed dofile" in str(error)
            record.update(outcome="no_progress" if repeated else "failed", error_type=type(error).__name__)
            raise
        finally:
            record["model_response_files"] = [item.relative_to(state["output_dir"]).as_posix()
                for item in sorted(set((state["output_dir"] / "model_responses").glob("response_*.json")) - responses_before)]
            write_json_atomic(path, record)

    def create_candidate(state):
        if state["task_type"] == "task2":
            original = _read_text(
                require_semantic_input(state["input_dir"], "original.dofile"),
                work_deadline(state), dependencies.clock,
            )
            safety = validate_dofile_candidate(original)
            static = asdict(safety)
            static["original_text"] = original
            write_json_atomic(state["output_dir"] / "original_static_diagnostics.json", static)
            # Preserve the supplied script and its exact rejected lines as evidence.
            details = [f"[ERROR] line {item.line_number}: {item.reason}: {item.line}" for item in safety.rejections]
            details.extend(f"[ERROR] missing phase: {phase}" for phase in safety.missing_phases)
            diagnostics = parse_tool_log("\n".join(details))
            return repair_attempt(state, {"source": "original_static_diagnostics.json", "locator": "rejections and missing_phases",
                                          "dofile_hash": _hash(original)},
                                  original, diagnostics, [], ["examine_scan_drc", "examine_scan_chain", "insert_dft_logic"])
        proposal = call_model(state, dependencies.initial_generator, state["requirements"],
                                                  state["inventory"], _chunks(
                                                  state, ["scan", "insert_dft_logic"],
                                                      work_deadline(state), dependencies.clock,
                                                  ))
        return _accept_proposal(proposal, state)

    def prepare_run(state):
        _integrity(state, work_deadline(state), dependencies.clock)
        if state.get("requires_netlist_repair") or state.get("repeated_dofile"):
            return {}
        budget = state["budget"]
        if budget.remaining(dependencies.clock()) <= budget.reserve_seconds or state["current_run"] >= budget.max_tool_runs:
            return _failure(AgentStatus.BUDGET_EXHAUSTED, "no tool budget remains")
        safety = validate_dofile_candidate(state["current_dofile"])
        if not safety.safe:
            raise LLMOutputError("unsafe or incomplete dofile")
        number = state["current_run"] + 1
        paths = create_run(state["output_dir"], number)
        write_run_dofile(paths, state["current_dofile"])
        changes = list(state["file_changes"])
        if number > 1:
            diff = write_dofile_diff(state["output_dir"], state["previous_dofile"], state["current_dofile"], f"R{number - 1}", paths.run_id)
            diff = diff.replace(diff.with_name("dofile_" + diff.name))
            changes.append({"diff": diff.relative_to(state["output_dir"]).as_posix(),
                            "from_run": f"R{number - 1}", "to_run": paths.run_id,
                            "before_file": f"runs/R{number - 1}/deliverables/R{number - 1}.dofile",
                            "after_file": paths.dofile.relative_to(state["output_dir"]).as_posix()})
        return {"paths": paths, "current_run": number, "file_changes": changes}

    def run_tool(state):
        if state.get("requires_netlist_repair") or state.get("repeated_dofile"):
            return {}
        paths = state["paths"]
        timeout = state["budget"].remaining(dependencies.clock()) - state["budget"].reserve_seconds
        if timeout <= 0:
            return _failure(AgentStatus.BUDGET_EXHAUSTED, "wall-time reserve reached before execution")
        try:
            if dependencies.launch_mode == "file_flag":
                result = dependencies.tool_runner(paths, paths.dofile, dependencies.executable, timeout, dependencies.env)
            else:
                result = dependencies.tool_runner(paths, paths.dofile, dependencies.executable, timeout,
                                                  dependencies.env, launch_mode=dependencies.launch_mode)
            result = _check_tool_result(result, paths)
        except Exception as error:
            # An attempted run remains auditable even if a collaborator raises.
            paths.log.touch(exist_ok=True)
            result = ToolResult(None, False, 0, paths.log, (), "invalid_tool_outcome", type(error).__name__)
            write_json_atomic(paths.root / "workflow_failure.json", {"failure_kind": result.failure_kind, "error_type": type(error).__name__})
        entry = {"run_id": paths.run_id, "log_file": paths.log.relative_to(state["output_dir"]).as_posix(),
                 "dofile_file": paths.dofile.relative_to(state["output_dir"]).as_posix(),
                 "metadata_file": (paths.root / "run_metadata.json").relative_to(state["output_dir"]).as_posix(),
                 "exit_code": result.exit_code, "timed_out": result.timed_out, "failure_kind": result.failure_kind}
        if not (paths.root / "run_metadata.json").is_file():
            invalid = result.failure_kind == "invalid_tool_outcome"
            write_json_atomic(paths.root / "run_metadata.json", {
                "exit_code": result.exit_code, "failure_kind": result.failure_kind,
                "timed_out": None if invalid else result.timed_out,
                "duration_seconds": None if invalid else result.duration_seconds,
                "failure_detail": result.failure_detail, "produced_files": list(result.produced_files)})
        if (paths.root / "workflow_failure.json").is_file():
            entry["workflow_failure_file"] = (paths.root / "workflow_failure.json").relative_to(state["output_dir"]).as_posix()
        return {"tool_result": result, "tool_runs": [*state["tool_runs"], entry]}

    def parse_evidence(state):
        if state.get("requires_netlist_repair") or state.get("repeated_dofile"):
            return {}
        deadline = work_deadline(state)
        _collect_work(state["paths"], deadline, dependencies.clock)
        return {"diagnostics": parse_tool_log(_read_text(state["paths"].log, deadline, dependencies.clock)),
                "report_evidence": collect_report_evidence(state["paths"], deadline, dependencies.clock)}

    def validate(state):
        if state.get("status") or state.get("requires_netlist_repair") or state.get("repeated_dofile"):
            return {"now": dependencies.clock()}
        requirements = {**requirements_data(state["requirements"]), "input_dir": state["input_dir"],
                        "protected_hashes": state["protected_hashes"]}
        try:
            report = validate_run(
                requirements, state["tool_result"], state["diagnostics"], state["paths"],
                work_deadline(state), dependencies.clock, state.get("report_evidence"),
            )
        except DeadlineExceeded as error:
            report = ValidationReport(
                (str(error),), (), ("validation_deadline",), (), True, (),
            )
        record = {"run_id": state["paths"].run_id, **asdict(report), "passed": report.passed}
        # EvidenceReference closure uses source + locator, while validation retains line numbers.
        write_json_atomic(state["paths"].root / "validation.json", record)
        return {"validation": report, "validation_results": [*state["validation_results"],
                {"run_id": state["paths"].run_id, "passed": report.passed,
                 "requirement_checks": [asdict(item) for item in report.requirement_checks],
                 "path": (state["paths"].root / "validation.json").relative_to(state["output_dir"]).as_posix()}],
                "now": dependencies.clock()}

    def diagnose_and_repair(state):
        _integrity(state, work_deadline(state), dependencies.clock)
        history = [*state["history"], RepairRecord(_hash(state["current_dofile"]),
                   "; ".join(state["validation"].failures), "failed real scan validation")]
        state["history"] = history
        terms = [item.rule for item in state["diagnostics"].drc_violations] + list(state["diagnostics"].commands)
        found = {"source": state["validation_results"][-1]["path"], "locator": "failures",
                 "log_file": state["tool_runs"][-1]["log_file"],
                 "failures": list(state["validation"].failures), "dofile_hash": _hash(state["current_dofile"])}
        return {**repair_attempt(state, found, state["current_dofile"], state["diagnostics"], history, terms),
                "history": history}

    def finalize_success(state):
        deadline = final_deadline(state)
        _integrity(state, deadline, dependencies.clock)
        manifest = promote_final(
            state["output_dir"], state["paths"], state["requirements"].required_outputs,
            deadline, dependencies.clock,
        )
        _integrity(state, deadline, dependencies.clock)
        update = {"status": AgentStatus.SUCCESS, "final_run": state["paths"].run_id, "failure_reason": None,
                  "final_manifest": manifest,
                  "token_usage": [asdict(usage) for usage in dependencies.client.usage_history]}
        _publish_decision({**state, **update}, deadline, dependencies.clock)
        return update

    def finalize_failure(state):
        update = _terminal_failure(state)
        try:
            _integrity(state, final_deadline(state), dependencies.clock)
        except InputMutationError:
            update = _failure(AgentStatus.COMPLIANCE_FAILURE, "protected inputs changed before termination")
        except DeadlineExceeded:
            pass
        update["token_usage"] = [asdict(usage) for usage in dependencies.client.usage_history]
        try:
            _publish_decision(
                {**state, **update}, final_deadline(state), dependencies.clock,
            )
        except DeadlineExceeded:
            update = _publish_minimal_deadline_failure(state["output_dir"])
        return {**update, "decision_published": True}

    def guarded(function):
        def node(state):
            if state.get("status") and function not in (validate, finalize_failure):
                return {}
            try:
                return function(state)
            except _ModelBudgetExhausted as error:
                return _failure(AgentStatus.BUDGET_EXHAUSTED, str(error))
            except DeadlineExceeded as error:
                return _failure(AgentStatus.BUDGET_EXHAUSTED, str(error))
            except InputMutationError:
                return _failure(AgentStatus.COMPLIANCE_FAILURE, "protected inputs changed")
            except (LLMOutputError, LLMConfigurationError, LLMTransportError) as error:
                if isinstance(error, LLMOutputError) and "repeated previously failed dofile" in str(error):
                    return {"repeated_dofile": True}
                return _failure(AgentStatus.INVALID_MODEL_OUTPUT, str(error))
            except Exception as error:
                return _failure(AgentStatus.TOOL_FAILURE, f"{function.__name__} failed: {type(error).__name__}")
        return node

    builder = StateGraph(WorkflowState)
    nodes = {"inventory_input": inventory_input, "extract_requirements": requirements_node,
             "load_manual": manual_node, "create_candidate": create_candidate, "prepare_run": prepare_run,
             "run_tool": run_tool, "parse_evidence": parse_evidence, "validate": validate,
             "diagnose_and_repair": diagnose_and_repair, "finalize_success": finalize_success,
             "finalize_failure": finalize_failure}
    for name, function in nodes.items():
        builder.add_node(name, guarded(function))
    order = [START, "inventory_input", "extract_requirements", "load_manual", "create_candidate",
             "prepare_run", "run_tool", "parse_evidence", "validate"]
    for source, target in zip(order, order[1:]):
        builder.add_edge(source, target)
    builder.add_conditional_edges("validate", route_after_validation,
        {"success": "finalize_success", "repair": "diagnose_and_repair", "failure": "finalize_failure"})
    builder.add_edge("diagnose_and_repair", "prepare_run")
    builder.add_edge("finalize_success", END)
    builder.add_edge("finalize_failure", END)
    return builder.compile()


def run_agent(input_dir: Path, output_dir: Path, dependencies: WorkflowDependencies | None = None) -> AgentResult:
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if output_dir.is_relative_to(input_dir) or input_dir.is_relative_to(output_dir):
        return AgentResult(AgentStatus.COMPLIANCE_FAILURE, None, "input and output directories overlap")
    if owned_output_entries(output_dir):
        archive = quarantine_existing_evidence(output_dir)
        result = AgentResult(AgentStatus.COMPLIANCE_FAILURE, None, "output already contains agent evidence")
        write_decision_log(output_dir, {**asdict(result), "task_type": None,
            "requirement_mapping": [], "issue_resolutions": [], "tool_runs": [], "file_changes": [],
            "validation_results": [], "proposals": [], "model_responses": [],
            "manual": {"available": None, "error": None}, "manual_error": None,
            "token_usage": [], "token_usage_available": False,
            "prior_evidence": [{"path": archive.relative_to(output_dir).as_posix()}]})
        return result
    output_dir.mkdir(parents=True, exist_ok=True)
    skip = os.environ.get("SCAN_AGENT_SKIP_READY_WAIT") == "1"
    started = (dependencies.ready_waiter if dependencies else wait_for_case_ready)(input_dir, skip)
    if dependencies is None:
        try:
            dependencies = WorkflowDependencies.from_env()
        except Exception as error:
            reason = str(error) if isinstance(error, LLMConfigurationError) else f"model initialization failed: {type(error).__name__}"
            result = AgentResult(AgentStatus.INVALID_MODEL_OUTPUT, None, reason)
            write_decision_log(output_dir, asdict(result))
            return result
    initial: WorkflowState = {"input_dir": input_dir, "output_dir": output_dir, "started_at": started,
        "startup_deadline": started + _STARTUP_INPUT_SCAN_SECONDS,
        "current_run": 0, "history": [], "tool_runs": [], "file_changes": [], "validation_results": [], "proposals": [],
        "status": "", "final_run": None, "failure_reason": None, "now": dependencies.clock()}
    state = build_workflow(dependencies).invoke(initial, {"recursion_limit": 1000})
    # Promotion or publication failures still require a truthful failed audit.
    if state["status"] != AgentStatus.SUCCESS:
        # A guarded finalization exception discards that node's local graph
        # update. The client still owns every usage record received so far.
        state["token_usage"] = [asdict(usage) for usage in dependencies.client.usage_history]
        final = output_dir / "final_results"
        if final.exists():
            # Preserve failed publication evidence under the originating run.
            final.replace(state["paths"].root / "unpublished_final")
        if not state.get("decision_published"):
            deadline = (state["budget"].deadline_monotonic if state.get("budget")
                        else state.get("startup_deadline"))
            try:
                _publish_decision(state, deadline, dependencies.clock)
            except DeadlineExceeded:
                update = _publish_minimal_deadline_failure(output_dir)
                state.update(update)
    return AgentResult(AgentStatus(state["status"]), state.get("final_run"), state.get("failure_reason"))
