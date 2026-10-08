# Scan Insertion Agent Phase One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an unattended, auditable Scan Insertion Agent that handles task-one dofile generation and task-two dofile repair without modifying Pre-scan netlists.

**Architecture:** LangGraph is a thin state machine around deterministic Python modules for input protection, tool execution, diagnostics, validation, budgets, and artifacts. DeepSeek V4 Pro is called through the OpenAI-compatible SDK only for structured requirement extraction and dofile generation or repair; Python owns all success decisions and filesystem mutations.

**Tech Stack:** Python 3.12, LangGraph, OpenAI Python SDK, pypdf, pytest, Docker, `dftexp_scan` from `scan-agent-base:ubuntu24`.

**Spec:** `agent_submission/docs/superpowers/specs/2026-10-07-scan-agent-design.md`

## Global Constraints

- Preserve `reference_submission` unchanged.
- The runtime entrypoint is exactly `/submission/agent_system -input /input -output /output`.
- Formal runs wait for `${input_dir}/.case_ready`; only `SCAN_AGENT_SKIP_READY_WAIT=1` skips the wait for local tests.
- The runtime must never read `golden.dofile` or `preset_issues.json`.
- The runtime must never modify `/input`, `/submission`, or `/opt`.
- Do not fabricate logs, netlists, reports, CTL, SCANDEF, or successful status.
- Use DeepSeek V4 Pro through `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`; do not call another model.
- If `limitations.md` omits a tool-run limit, use exactly three as the default maximum.
- Reserve ten seconds of wall time for final artifact and decision-log writing.
- Phase one never edits a Pre-scan netlist and never runs LEC; route required netlist repair to `unsupported_netlist_repair`.
- All paths stored in `decision_log.json` are relative to the requested output directory.
- A successful `final_results` directory is copied only from a real, validated `runs/Rn` directory.
- Run all Python tests with `python -m pytest` from `agent_submission`.

---

## File Map

| File | Responsibility |
|---|---|
| `submission/agent_system` | Parse evaluator arguments and start Python without interactive prompts. |
| `submission/main.py` | CLI composition root and process exit code. |
| `submission/scan_agent/state.py` | Shared typed records and terminal status values. |
| `submission/scan_agent/inputs.py` | Ready sentinel, task classification, limits, inventory, hashes. |
| `submission/scan_agent/artifacts.py` | Run directories, manifests, diffs, final copy, decision log. |
| `submission/scan_agent/runner.py` | Real subprocess execution with timeout and metadata. |
| `submission/scan_agent/diagnostics.py` | Log and DRC evidence extraction. |
| `submission/scan_agent/validation.py` | Deterministic acceptance rules. |
| `submission/scan_agent/manual.py` | PDF extraction, chunks, lightweight keyword search. |
| `submission/scan_agent/llm.py` | OpenAI-compatible client, JSON responses, retry, usage. |
| `submission/scan_agent/dofile.py` | Candidate safety checks, generation prompts, diffs. |
| `submission/scan_agent/workflow.py` | LangGraph nodes and conditional routing. |
| `tests/fake_dftexp_scan.py` | Deterministic executable test double for integration tests only. |

---

### Task 1: Package skeleton and evaluator entrypoint

**Files:**
- Create: `agent_submission/.env.example`
- Create: `agent_submission/.dockerignore`
- Create: `agent_submission/submission/agent_system`
- Create: `agent_submission/submission/main.py`
- Create: `agent_submission/submission/requirements.txt`
- Create: `agent_submission/requirements-dev.txt`
- Create: `agent_submission/submission/scan_agent/__init__.py`
- Create: `agent_submission/tests/test_entrypoint.py`

**Interfaces:**
- Consumes: Evaluator arguments `-input DIR -output DIR` or their double-hyphen equivalents.
- Produces: `main(argv: Sequence[str] | None = None) -> int`; shell exit code mirrors this return value.

- [ ] **Step 1: Write the failing CLI parsing test**

```python
from pathlib import Path

from main import build_parser


def test_parser_accepts_evaluator_argument_spelling(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["-input", str(tmp_path), "-output", str(tmp_path / "out")])
    assert args.input == str(tmp_path)
    assert args.output == str(tmp_path / "out")
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python -m pytest tests/test_entrypoint.py -v`

Expected: FAIL because `main.py` does not exist.

- [ ] **Step 3: Implement the minimal composition root and entry script**

```python
# submission/main.py
import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Insertion Agent")
    parser.add_argument("-input", "--input", dest="input", required=True)
    parser.add_argument("-output", "--output", dest="output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /submission
exec python3 main.py "$@"
```

Set `requirements.txt` to `langgraph`, `openai`, and `pypdf`; set `requirements-dev.txt` to `-r submission/requirements.txt` plus `pytest`.

- [ ] **Step 4: Run the entrypoint test**

Run: `python -m pytest tests/test_entrypoint.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the skeleton**

```bash
git add agent_submission/.env.example agent_submission/.dockerignore agent_submission/requirements-dev.txt agent_submission/submission agent_submission/tests/test_entrypoint.py
git commit -m "feat: add scan agent package skeleton"
```

---

### Task 2: State types, ready wait, input classification, limits, and protection hashes

**Files:**
- Create: `agent_submission/submission/scan_agent/state.py`
- Create: `agent_submission/submission/scan_agent/inputs.py`
- Create: `agent_submission/tests/test_inputs.py`

**Interfaces:**
- Produces: `wait_for_case_ready(input_dir: Path, skip: bool, poll_seconds: float = 0.05) -> float`.
- Produces: `classify_task(input_dir: Path) -> Literal["task1", "task2"]`.
- Produces: `parse_limits(text: str, started_at: float) -> Budget`.
- Produces: `inventory_inputs(input_dir: Path) -> InputInventory` that excludes `.case_ready`, `golden.dofile`, and `preset_issues.json` from runtime content.
- Produces: `hash_protected_inputs(input_dir: Path) -> dict[str, str]` and `assert_inputs_unchanged(input_dir: Path, expected: dict[str, str]) -> None`.

- [ ] **Step 1: Write failing input and budget tests**

```python
def test_task2_is_detected_only_from_original_dofile(tmp_path: Path) -> None:
    (tmp_path / "original.dofile").write_text("present_design top", encoding="utf-8")
    assert classify_task(tmp_path) == "task2"


def test_public_answer_files_are_not_in_runtime_inventory(tmp_path: Path) -> None:
    for name in ("task_spec.md", "golden.dofile", "preset_issues.json"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    inventory = inventory_inputs(tmp_path)
    assert inventory.runtime_files == ["task_spec.md"]


def test_default_tool_run_limit_is_three() -> None:
    budget = parse_limits("总时间不超过 150 秒", started_at=100.0)
    assert budget.max_tool_runs == 3
    assert budget.deadline_monotonic == 250.0
```

- [ ] **Step 2: Run tests and verify missing interfaces**

Run: `python -m pytest tests/test_inputs.py -v`

Expected: FAIL on imports from `scan_agent.inputs`.

- [ ] **Step 3: Implement typed records and deterministic parsing**

```python
@dataclass(frozen=True)
class Budget:
    started_at: float
    deadline_monotonic: float
    max_tool_runs: int
    reserve_seconds: float = 10.0

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline_monotonic - now)
```

Implement Chinese and English regexes for wall-time seconds and tool-call counts. Inventory paths must be normalized POSIX-style relative paths and sorted. Hash files by streaming 1 MiB chunks.

- [ ] **Step 4: Add mutation-detection and sentinel tests**

```python
def test_changed_input_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "task_spec.md"
    source.write_text("before", encoding="utf-8")
    expected = hash_protected_inputs(tmp_path)
    source.write_text("after", encoding="utf-8")
    with pytest.raises(InputMutationError):
        assert_inputs_unchanged(tmp_path, expected)
```

Use a background thread to create `.case_ready` and assert that `wait_for_case_ready` returns only after it appears; separately assert `skip=True` returns immediately.

- [ ] **Step 5: Run input tests**

Run: `python -m pytest tests/test_inputs.py -v`

Expected: PASS.

- [ ] **Step 6: Commit input handling**

```bash
git add agent_submission/submission/scan_agent/state.py agent_submission/submission/scan_agent/inputs.py agent_submission/tests/test_inputs.py
git commit -m "feat: protect and classify case inputs"
```

---

### Task 3: Auditable run and final artifact management

**Files:**
- Create: `agent_submission/submission/scan_agent/artifacts.py`
- Create: `agent_submission/tests/test_artifacts.py`

**Interfaces:**
- Consumes: `output_dir: Path`, one-based run number, dofile text, tool-produced paths.
- Produces: `RunPaths`, `create_run(output_dir, run_number) -> RunPaths`.
- Produces: `write_run_dofile(paths, content) -> Path`.
- Produces: `write_dofile_diff(output_dir, previous, current, previous_id, current_id) -> Path`.
- Produces: `promote_final(output_dir, run_paths, required_names) -> FinalManifest`.
- Produces: `write_decision_log(output_dir, payload) -> Path` and `validate_references(output_dir, payload) -> None`.

- [ ] **Step 1: Write failing directory and promotion tests**

```python
def test_create_run_uses_required_names(tmp_path: Path) -> None:
    paths = create_run(tmp_path, 2)
    assert paths.root == tmp_path / "runs" / "R2"
    assert paths.log == paths.root / "R2.log"
    assert paths.dofile == paths.deliverables / "R2.dofile"


def test_promote_final_rejects_missing_required_artifact(tmp_path: Path) -> None:
    paths = create_run(tmp_path, 1)
    paths.log.write_text("real log", encoding="utf-8")
    write_run_dofile(paths, "exit")
    with pytest.raises(MissingArtifactError):
        promote_final(tmp_path, paths, ["post_scan.v"])
```

- [ ] **Step 2: Run tests and verify missing implementation**

Run: `python -m pytest tests/test_artifacts.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement paths, atomic JSON writes, diffs, and hash-preserving promotion**

Use `tempfile.NamedTemporaryFile` in the destination directory followed by `os.replace` for JSON files. Copy final artifacts with `shutil.copy2`, then compare source and destination SHA-256 before returning success.

```python
def create_run(output_dir: Path, run_number: int) -> RunPaths:
    run_id = f"R{run_number}"
    root = output_dir / "runs" / run_id
    deliverables = root / "deliverables"
    reports = root / "reports"
    work = root / "work"
    for directory in (deliverables, reports, work):
        directory.mkdir(parents=True, exist_ok=True)
    return RunPaths(run_id, root, root / f"{run_id}.log", deliverables, reports, work)
```

- [ ] **Step 4: Add closed-reference tests**

```python
def test_decision_log_reference_must_exist(tmp_path: Path) -> None:
    payload = {"tool_runs": [{"log_file": "runs/R1/R1.log"}]}
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, payload)
```

- [ ] **Step 5: Run artifact tests**

Run: `python -m pytest tests/test_artifacts.py -v`

Expected: PASS.

- [ ] **Step 6: Commit artifact management**

```bash
git add agent_submission/submission/scan_agent/artifacts.py agent_submission/tests/test_artifacts.py
git commit -m "feat: add auditable run artifact management"
```

---

### Task 4: Real subprocess runner and test-only fake executable

**Files:**
- Create: `agent_submission/submission/scan_agent/runner.py`
- Create: `agent_submission/tests/fake_dftexp_scan.py`
- Create: `agent_submission/tests/test_runner.py`

**Interfaces:**
- Consumes: `RunPaths`, dofile path, executable name, timeout seconds, inherited environment.
- Produces: `run_scan_tool(paths: RunPaths, dofile_path: Path, executable: Sequence[str], timeout_seconds: float, env: Mapping[str, str]) -> ToolResult` with exit code, timeout flag, duration, log path, and produced-file manifest.
- Raises no exception for normal process failure; unavailable executable is represented by `ToolResult.failure_kind == "tool_unavailable"`.

- [ ] **Step 1: Write failing success, failure, and timeout tests**

```python
def test_runner_preserves_nonzero_exit_and_log(tmp_path: Path) -> None:
    paths = create_run(tmp_path, 1)
    dofile = write_run_dofile(paths, "exit")
    result = run_scan_tool(
        paths=paths,
        dofile_path=dofile,
        executable=[sys.executable, str(FAKE_TOOL), "--mode", "error"],
        timeout_seconds=5.0,
        env={},
    )
    assert result.exit_code == 7
    assert result.success is False
    assert "[ERROR]" in paths.log.read_text(encoding="utf-8")


def test_runner_marks_timeout_without_success(tmp_path: Path) -> None:
    # invoke fake tool sleep mode with a 0.05 second timeout
    assert result.timed_out is True
    assert result.success is False
```

- [ ] **Step 2: Run tests and verify missing runner**

Run: `python -m pytest tests/test_runner.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement subprocess execution**

Use `subprocess.Popen`, merge stderr into stdout, write output directly to `Rn.log`, call `communicate(timeout=timeout_seconds)`, and terminate then kill on timeout. Never synthesize log content.

```python
command = [*executable, "-f", str(dofile_path.resolve())]
started = time.monotonic()
with paths.log.open("w", encoding="utf-8", errors="replace") as log_handle:
    process = subprocess.Popen(command, cwd=paths.work, stdout=log_handle,
                               stderr=subprocess.STDOUT, text=True, env=merged_env)
```

After exit, inventory files created beneath the run directory and write `run_metadata.json` using the artifact module.

- [ ] **Step 4: Run runner tests**

Run: `python -m pytest tests/test_runner.py -v`

Expected: PASS for success, nonzero, timeout, and missing executable cases.

- [ ] **Step 5: Commit the runner**

```bash
git add agent_submission/submission/scan_agent/runner.py agent_submission/tests/fake_dftexp_scan.py agent_submission/tests/test_runner.py
git commit -m "feat: run scan tool with real failure semantics"
```

---

### Task 5: Log diagnostics and deterministic validation

**Files:**
- Create: `agent_submission/submission/scan_agent/diagnostics.py`
- Create: `agent_submission/submission/scan_agent/validation.py`
- Create: `agent_submission/tests/test_diagnostics.py`
- Create: `agent_submission/tests/test_validation.py`

**Interfaces:**
- Produces: `parse_tool_log(text: str) -> DiagnosticSummary`.
- Produces: `parse_drc_text(text: str) -> list[DrcViolation]`.
- Produces: `validate_run(requirements, tool_result, diagnostics, run_paths) -> ValidationReport`.
- `ValidationReport.passed` is the only value used to select a successful final run.

- [ ] **Step 1: Write failing diagnostic parser tests**

```python
def test_parser_extracts_rule_counts_and_fatal_errors() -> None:
    text = """
[ERROR] [CMD-0074] Unknown option '-active_state'
[DFTDRC-4001] DFTR1 x1610
Total violations: 1610
"""
    summary = parse_tool_log(text)
    assert summary.fatal_errors[0].code == "CMD-0074"
    assert summary.drc_violations[0].rule == "DFTR1"
    assert summary.drc_violations[0].count == 1610
```

- [ ] **Step 2: Run parser tests and verify failure**

Run: `python -m pytest tests/test_diagnostics.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement diagnostic records and parsers**

Parse bracketed severity/code lines, `DFTR` families including `DFTR-TIE0`, explicit totals, command names, and common License messages. Preserve matching source lines as evidence; do not infer a zero violation count when no DRC evidence exists.

- [ ] **Step 4: Write failing validation tests**

```python
def test_exit_zero_without_required_netlist_is_not_success(tmp_path: Path) -> None:
    report = validate_run(
        requirements={"required_outputs": ["post_scan.v"], "allowed_drc": []},
        tool_result=successful_tool_result(tmp_path),
        diagnostics=empty_diagnostics(),
        run_paths=create_run(tmp_path, 1),
    )
    assert report.passed is False
    assert "post_scan.v" in report.missing_artifacts


def test_allowed_tie_rule_does_not_hide_other_drc() -> None:
    # DFTR-TIE0 is allowed while DFTR9 remains a failure
    assert report.passed is False
```

- [ ] **Step 5: Implement deterministic validators**

Validation checks exit status, timeout, fatal errors, allowed and disallowed DRC, required nonempty artifacts, chain constraints when corresponding report facts exist, and input hash integrity. Missing evidence yields failure, not optimistic success.

- [ ] **Step 6: Run diagnostics and validation tests**

Run: `python -m pytest tests/test_diagnostics.py tests/test_validation.py -v`

Expected: PASS.

- [ ] **Step 7: Commit evidence parsing and validation**

```bash
git add agent_submission/submission/scan_agent/diagnostics.py agent_submission/submission/scan_agent/validation.py agent_submission/tests/test_diagnostics.py agent_submission/tests/test_validation.py
git commit -m "feat: validate scan runs from real evidence"
```

---

### Task 6: Manual extraction and lightweight retrieval

**Files:**
- Create: `agent_submission/submission/scan_agent/manual.py`
- Create: `agent_submission/tests/test_manual.py`

**Interfaces:**
- Produces: `load_manual(path: Path) -> ManualIndex`.
- Produces: `ManualIndex.search(terms: Sequence[str], limit: int = 6) -> list[ManualChunk]`.
- Missing or unreadable manuals return `ManualLoadResult(available=False, index=None, error="manual PDF is unavailable")`; they do not throw out of the workflow.

- [ ] **Step 1: Write a failing retrieval test with an injected PDF reader**

Monkeypatch `scan_agent.manual.PdfReader` with a fake reader whose two pages return deterministic strings: page one describes `set_scan_signal` and DFTR9; page two describes `set_scan_cfg` and chain length.

```python
def test_manual_search_ranks_matching_command_first() -> None:
    index = load_manual(Path("manual.pdf")).index
    results = index.search(["DFTR9", "set_scan_signal"], limit=1)
    assert results[0].page == 1
    assert "set_scan_signal" in results[0].text
```

- [ ] **Step 2: Run the retrieval test and verify failure**

Run: `python -m pytest tests/test_manual.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement extraction, chunking, and scoring**

Extract per-page text with `pypdf.PdfReader`. Split large pages at headings or 2,000 characters. Score a chunk as the sum of case-insensitive exact-term counts plus a bonus when a command token occurs in its first 200 characters. Stable-sort by score descending, page ascending, chunk index ascending.

- [ ] **Step 4: Add missing-manual behavior test and run suite**

```python
def test_missing_manual_is_recorded_not_raised(tmp_path: Path) -> None:
    result = load_manual(tmp_path / "missing.pdf")
    assert result.available is False
    assert result.index is None
```

Run: `python -m pytest tests/test_manual.py -v`

Expected: PASS.

- [ ] **Step 5: Commit manual retrieval**

```bash
git add agent_submission/submission/scan_agent/manual.py agent_submission/tests/test_manual.py
git commit -m "feat: add lightweight scan manual retrieval"
```

---

### Task 7: Structured LLM client, requirement schema, and dofile safety

**Files:**
- Create: `agent_submission/submission/scan_agent/llm.py`
- Create: `agent_submission/submission/scan_agent/dofile.py`
- Create: `agent_submission/tests/test_llm.py`
- Create: `agent_submission/tests/test_dofile.py`

**Interfaces:**
- Produces: `LLMClient.from_env() -> LLMClient` and `complete_json(system, user) -> LLMResponse`.
- Produces: `extract_requirements(client, task_text, limitations_text, inventory, manual_chunks) -> Requirements`.
- Produces: `generate_initial_dofile(client: LLMClient, requirements: Requirements, inventory: InputInventory, manual_chunks: Sequence[ManualChunk]) -> DofileProposal`.
- Produces: `repair_dofile(client: LLMClient, requirements: Requirements, current_dofile: str, diagnostics: DiagnosticSummary, history: Sequence[RepairRecord], manual_chunks: Sequence[ManualChunk]) -> DofileProposal`.
- Produces: `validate_dofile_candidate(text: str) -> DofileSafetyReport`.

- [ ] **Step 1: Write failing LLM JSON retry tests with an injected fake transport**

```python
def test_complete_json_retries_once_after_invalid_json() -> None:
    transport = FakeTransport(["not-json", '{"task_type":"task1"}'])
    client = LLMClient(transport=transport, model="deepseek-v4-pro", max_retries=2)
    result = client.complete_json("system", "user")
    assert result.data == {"task_type": "task1"}
    assert transport.calls == 2
```

- [ ] **Step 2: Run LLM tests and verify missing implementation**

Run: `python -m pytest tests/test_llm.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement environment construction, response parsing, and usage capture**

The production transport wraps `client.chat.completions.create`; tests inject a callable transport. Missing API key, base URL, or model raises `LLMConfigurationError` before any tool run. Strip a single Markdown JSON fence only, parse JSON, and record prompt/completion/total token counts when the API returns them.

- [ ] **Step 4: Write failing dofile safety tests**

```python
@pytest.mark.parametrize("line", [
    'set out_dir "/input/out"',
    'exec rm -rf /work',
    'dump_netlist -file "/submission/post_scan.v"',
    'source /opt/replace_tool.tcl',
])
def test_candidate_rejects_forbidden_writes_or_external_execution(line: str) -> None:
    report = validate_dofile_candidate(f"load_lib x.lib\n{line}\nexit\n")
    assert report.safe is False
```

- [ ] **Step 5: Implement candidate validation and prompt builders**

Reject absolute output paths under protected roots, Tcl `exec`, `system`, arbitrary `source`, and absent load/present/DRC/insertion/output phases unless the task explicitly proves an equivalent command. Return exact rejected lines and reasons to the one allowed correction call.

Requirement extraction returns a typed `Requirements` record with clocks, resets, constants, scan enables, chain constraints, partitions, wrapper settings, allowed DRC, required outputs, and `allow_netlist_modification`.

- [ ] **Step 6: Run LLM and dofile tests**

Run: `python -m pytest tests/test_llm.py tests/test_dofile.py -v`

Expected: PASS.

- [ ] **Step 7: Commit model and dofile boundaries**

```bash
git add agent_submission/submission/scan_agent/llm.py agent_submission/submission/scan_agent/dofile.py agent_submission/tests/test_llm.py agent_submission/tests/test_dofile.py
git commit -m "feat: add structured llm and dofile safety layer"
```

---

### Task 8: LangGraph workflow and bounded repair loop

**Files:**
- Create: `agent_submission/submission/scan_agent/workflow.py`
- Modify: `agent_submission/submission/main.py`
- Create: `agent_submission/tests/helpers.py`
- Create: `agent_submission/tests/test_workflow.py`

**Interfaces:**
- Produces: `build_workflow(dependencies: WorkflowDependencies) -> CompiledStateGraph`.
- Produces: `run_agent(input_dir: Path, output_dir: Path, dependencies=None) -> AgentResult`.
- Updates `main(argv)` to return `0` only for `AgentStatus.SUCCESS`, otherwise `1`.
- Test helper `make_task1_case(root: Path) -> Path` creates a minimal input containing `task_spec.md`, `limitations.md`, `netlist/design.v`, and `lib/stdcells.lib`.
- Test helper `scripted_dependencies(tool_results: list[ToolResult], dofiles: list[str] | None = None, diagnoses: list[dict[str, str]] | None = None) -> WorkflowDependencies` returns deterministic injected collaborators.
- Test helper `fake_dependencies(mode: str) -> WorkflowDependencies` maps `success`, `tool_unavailable`, and `timeout` to complete scripted dependency sets.

- [ ] **Step 1: Write a failing two-run workflow test**

```python
def test_workflow_repairs_once_then_promotes_second_run(tmp_path: Path) -> None:
    input_case = make_task1_case(tmp_path)
    deps = scripted_dependencies(
        tool_results=[failed_cmd_result("CMD-0074"), successful_scan_result()],
        dofiles=["bad dofile", "corrected dofile"],
    )
    result = run_agent(input_case, tmp_path / "output", dependencies=deps)
    assert result.status == "success"
    assert result.final_run == "R2"
    assert (tmp_path / "output/diffs/dofile_R1_to_R2.diff").is_file()
```

- [ ] **Step 2: Run workflow test and verify failure**

Run: `python -m pytest tests/test_workflow.py::test_workflow_repairs_once_then_promotes_second_run -v`

Expected: FAIL on missing workflow.

- [ ] **Step 3: Implement graph nodes and conditional routing**

Add nodes in this order:

```python
builder.add_edge(START, "inventory_input")
builder.add_edge("inventory_input", "extract_requirements")
builder.add_edge("extract_requirements", "load_manual")
builder.add_edge("load_manual", "create_candidate")
builder.add_edge("create_candidate", "prepare_run")
builder.add_edge("prepare_run", "run_tool")
builder.add_edge("run_tool", "parse_evidence")
builder.add_edge("parse_evidence", "validate")
builder.add_conditional_edges("validate", route_after_validation, {
    "success": "finalize_success",
    "repair": "diagnose_and_repair",
    "failure": "finalize_failure",
})
builder.add_edge("diagnose_and_repair", "prepare_run")
builder.add_edge("finalize_success", END)
builder.add_edge("finalize_failure", END)
```

The route function checks, in order: passed validation, protected-input mutation, required netlist repair, deadline reserve, tool-run limit, repeated dofile hash, then repair eligibility.

- [ ] **Step 4: Add bounded-failure workflow tests**

Test exact terminal statuses for timeout budget exhaustion, three repeated failures, repeated dofile hash, invalid model output, input mutation, and required netlist repair.

```python
def test_netlist_repair_requirement_is_terminal(tmp_path: Path) -> None:
    input_case = make_task1_case(tmp_path)
    output_dir = tmp_path / "output"
    deps = scripted_dependencies(
        tool_results=[failed_drc_result("DFTR10")],
        diagnoses=[diagnosis("requires_netlist_repair")],
    )
    result = run_agent(input_case, output_dir, dependencies=deps)
    assert result.status == "unsupported_netlist_repair"
    assert result.final_run is None
```

- [ ] **Step 5: Run workflow tests**

Run: `python -m pytest tests/test_workflow.py -v`

Expected: PASS.

- [ ] **Step 6: Commit workflow orchestration**

```bash
git add agent_submission/submission/scan_agent/workflow.py agent_submission/submission/main.py agent_submission/tests/helpers.py agent_submission/tests/test_workflow.py
git commit -m "feat: orchestrate bounded scan repair workflow"
```

---

### Task 9: Decision log closure and full offline integration tests

**Files:**
- Modify: `agent_submission/submission/scan_agent/artifacts.py`
- Modify: `agent_submission/submission/scan_agent/workflow.py`
- Modify: `agent_submission/tests/fake_dftexp_scan.py`
- Create: `agent_submission/tests/test_integration.py`

**Interfaces:**
- Produces a task-one or task-two `decision_log.json` whose references all resolve beneath the output root.
- Produces no `final_results` deliverables on failed runs.
- Ensures successful final artifacts hash-match the selected run.

- [ ] **Step 1: Write failing end-to-end success test**

```python
def test_offline_success_has_closed_decision_log(tmp_path: Path) -> None:
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, dependencies=fake_dependencies("success"))
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert result.final_run == "R1"
    assert payload["final_run"] == "R1"
    validate_references(output, payload)
    assert sha256(output / "final_results/deliverables/post_scan.v") == sha256(
        output / "runs/R1/deliverables/post_scan.v"
    )
```

- [ ] **Step 2: Add failure-integrity test**

```python
def test_tool_unavailable_never_creates_fake_final_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output,
                       dependencies=fake_dependencies("tool_unavailable"))
    assert result.status == "tool_failure"
    assert not (output / "final_results/deliverables/post_scan.v").exists()
    assert "tool_unavailable" in (output / "decision_log.json").read_text(encoding="utf-8")
```

- [ ] **Step 3: Implement final decision-log builders**

Build `requirement_mapping` from validated requirements, `issue_resolutions` from repair history, `tool_runs` from metadata, and `file_changes` from real diff paths. Before atomic write, call `validate_references`; for failed runs, omit `final_run` references and set `final_run` to JSON null.

- [ ] **Step 4: Run complete offline suite**

Run: `python -m pytest -v`

Expected: PASS with scenarios for one-run success, two-run repair, DRC failure, timeout, unavailable tool, missing output, no progress, input mutation, budget exhaustion, and unsupported netlist repair.

- [ ] **Step 5: Commit decision-log integration**

```bash
git add agent_submission/submission/scan_agent/artifacts.py agent_submission/submission/scan_agent/workflow.py agent_submission/tests/fake_dftexp_scan.py agent_submission/tests/test_integration.py
git commit -m "feat: close scan agent audit trail"
```

---

### Task 10: Docker packaging, documentation, and final verification

**Files:**
- Create: `agent_submission/Dockerfile`
- Create: `agent_submission/README.md`
- Modify: `agent_submission/.dockerignore`
- Modify: `agent_submission/submission/agent_system`
- Create: `agent_submission/tests/test_package.py`

**Interfaces:**
- Produces a build context whose root contains `Dockerfile` and `submission/`.
- Produces a container entrypoint at `/submission/agent_system`.
- Documents formal mode, local mode, environment variables, output semantics, and known phase-one netlist limitation.

- [ ] **Step 1: Write failing package-structure tests**

```python
def test_dockerfile_uses_official_base_and_entrypoint() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM scan-agent-base:ubuntu24" in text
    assert 'ENTRYPOINT ["/submission/agent_system"]' in text


def test_submission_does_not_contain_cache_or_secret_env() -> None:
    assert not list(Path("submission").rglob("__pycache__"))
    assert not Path(".env").exists()
```

- [ ] **Step 2: Run package tests and verify failure**

Run: `python -m pytest tests/test_package.py -v`

Expected: FAIL because the Dockerfile and README do not exist.

- [ ] **Step 3: Implement Dockerfile and packaging rules**

```dockerfile
FROM scan-agent-base:ubuntu24
COPY submission/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN rm -rf /submission/*
COPY submission/ /submission/
RUN chmod +x /submission/agent_system
WORKDIR /work
ENTRYPOINT ["/submission/agent_system"]
```

`.dockerignore` excludes `.git`, `.env`, `__pycache__`, `.pytest_cache`, tests, docs, ZIP files, public cases, and Docker image archives.

- [ ] **Step 4: Document build and run commands**

README must include these exact formal and local patterns:

```bash
docker build -t scan-agent:phase1 .
docker run --rm \
  -e LLM_API_KEY \
  -e LLM_BASE_URL \
  -e LLM_MODEL \
  -e SCANINSERTION_LICENSE_SERVER \
  -v /absolute/case/input:/input:ro \
  -v /absolute/case/output:/output:rw \
  scan-agent:phase1 -input /input -output /output
```

For local pre-populated cases, document `-e SCAN_AGENT_SKIP_READY_WAIT=1`. Explicitly state that phase one returns `unsupported_netlist_repair` rather than editing a netlist.

- [ ] **Step 5: Run all tests and repository checks**

Run: `python -m pytest -v`

Expected: PASS.

Run: `git diff --check`

Expected: no whitespace errors.

Run: `python -m compileall submission`

Expected: every Python file compiles.

- [ ] **Step 6: Build the Docker image when the official base image is available**

Run: `docker image inspect scan-agent-base:ubuntu24`

If present, run: `docker build -t scan-agent:phase1 .`

Expected: successful build and successful import checks for `openai`, `langgraph`, and `pypdf`. If the base image is unavailable, record Docker build as externally blocked while preserving passing package and Python tests.

- [ ] **Step 7: Commit packaging and documentation**

```bash
git add agent_submission/Dockerfile agent_submission/README.md agent_submission/.dockerignore agent_submission/submission/agent_system agent_submission/tests/test_package.py
git commit -m "docs: package phase one scan agent"
```

---

## Final Acceptance Checklist

- [ ] `python -m pytest -v` passes from `agent_submission`.
- [ ] `python -m compileall submission` passes.
- [ ] `git diff --check` reports no errors.
- [ ] `reference_submission` has no modifications.
- [ ] Runtime code contains no read of `golden.dofile` or `preset_issues.json`.
- [ ] Missing `dftexp_scan` produces a nonzero Agent result and no fake final artifact.
- [ ] Input mutation produces `compliance_failure`.
- [ ] A successful two-run test creates `dofile_R1_to_R2.diff` and promotes only R2.
- [ ] Every `decision_log.json` reference closes under the output directory.
- [ ] Docker build succeeds when `scan-agent-base:ubuntu24` is loaded.
- [ ] Real public-case validation is run only in the licensed environment and preserves its genuine logs and reports.
