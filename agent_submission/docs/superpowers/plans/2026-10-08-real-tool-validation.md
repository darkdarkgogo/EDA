# Real-tool Compatibility and Deterministic Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace placeholder scan commands with the documented DFTEXP_Scan flow and require real report evidence for every supported scan requirement before publication.

**Architecture:** Keep LangGraph as the bounded orchestrator and the LLM as a structured proposal generator. Add a documented Tcl allowlist, an explicit runner launch mode, report-specific parsers, and field-level deterministic checks whose evidence closes into `decision_log.json`.

**Tech Stack:** Python 3, LangGraph, OpenAI-compatible SDK, pypdf, SentenceTransformers, Qwen3 Embedding, in-memory cosine vectors, subprocess, pytest, Docker.

**Spec:** `agent_submission/docs/superpowers/specs/2026-10-08-real-tool-validation-design.md`

## Global Constraints

- Preserve `/input/.case_ready` waiting in formal mode and `SCAN_AGENT_SKIP_READY_WAIT=1` for local pre-populated cases.
- Never read `golden.dofile` or `preset_issues.json` semantically or send them to the model.
- Never modify a pre-scan netlist; return `unsupported_netlist_repair` when a repair requires that change.
- Keep every tool output beneath `runs/Rn/work` until deterministic validation and hash-checked promotion.
- Use only real subprocess, report, artifact, and input evidence for success.
- Missing, malformed, contradictory, duplicated, unsupported, or unverified required evidence fails closed.
- Keep the absolute case deadline, ten-second finalization reserve, and maximum tool-run limit.
- Do not copy either PDF into the submission image and do not modify `reference_submission`.
- Run focused tests per task and one full offline suite after all tasks.
- Bake the pinned Qwen3 model into Docker, precompute manual vectors at image build when the official base includes the PDF, and preserve keyword-only fallback.

---

### Task 1: Documented dofile command contract

**Files:**
- Modify: `agent_submission/submission/scan_agent/dofile.py`
- Modify: `agent_submission/tests/test_dofile.py`
- Modify: `agent_submission/tests/helpers.py`

**Interfaces:**
- Produces: `validate_dofile_candidate(text: str) -> DofileSafetyReport` for the documented flow.
- Produces: `resolve_output_destinations(text: str, work_dir: Path) -> tuple[Path, ...]` with safe Tcl report redirection.
- Requires core phases `load_lib`, `load_netlist`, `present_design`, `examine_scan_drc`, `examine_scan_chain`, `insert_dft_logic`, three core `rpt_scan_*` reports, and `dump_netlist`.

- [ ] **Step 1: Replace fixture scripts with documented commands and write failing phase tests**

```python
DOCUMENTED_DFILE = """\
load_lib /input/lib/stdcells.lib
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

def test_documented_flow_is_accepted():
    assert validate_dofile_candidate(DOCUMENTED_DFILE).safe

@pytest.mark.parametrize("obsolete", ["examine_scan", "insert_scan"])
def test_placeholder_commands_do_not_satisfy_real_phases(obsolete):
    report = validate_dofile_candidate(DOCUMENTED_DFILE.replace("examine_scan_drc", obsolete)
                                       if obsolete == "examine_scan" else
                                       DOCUMENTED_DFILE.replace("insert_dft_logic", obsolete))
    assert not report.safe
```

- [ ] **Step 2: Run the focused tests and confirm they fail on the placeholder phase map**

Run: `python -m pytest tests/test_dofile.py -q`

Expected: failures identify missing documented DRC, preview, insertion, and report phases.

- [ ] **Step 3: Implement the documented phase map and safe report redirection**

Use this ordered phase contract:

```python
_PHASES = {
    "load_library": {"load_lib"},
    "load_netlist": {"load_netlist"},
    "present": {"present_design"},
    "drc": {"examine_scan_drc"},
    "preview": {"examine_scan_chain"},
    "insertion": {"insert_dft_logic"},
    "signal_report": {"rpt_scan_signal"},
    "config_report": {"rpt_scan_cfg"},
    "chain_report": {"rpt_scan_chain"},
    "output": {"dump_netlist"},
}
```

Recognize exactly one static `>` destination for `rpt_*`, normalize it with `_normalized_output_relative`, and reject `>>`, missing destinations, absolute paths, parent traversal, variables that do not resolve statically, and more than one redirect. Keep `exec`, `system`, `source`, bracket substitution, and arbitrary Tcl control flow forbidden because this iteration generates no validated source fragments.

- [ ] **Step 4: Update the LLM system instruction to require the documented commands and report filenames**

The prompt must name `reports/drc.rpt`, `reports/scan_signal.rpt`, `reports/scan_cfg.rpt`, `reports/scan_chain.rpt`, optional `reports/scan_partition.rpt`, and `deliverables/post_scan.v`; all paths remain relative to the run work directory.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_dofile.py tests/test_llm.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_submission/submission/scan_agent/dofile.py agent_submission/tests/test_dofile.py agent_submission/tests/helpers.py
git commit -m "fix: use documented DFTEXP scan commands"
```

---

### Task 2: Explicit and auditable tool launch modes

**Files:**
- Modify: `agent_submission/submission/scan_agent/runner.py`
- Modify: `agent_submission/submission/scan_agent/workflow.py`
- Modify: `agent_submission/tests/fake_dftexp_scan.py`
- Modify: `agent_submission/tests/test_runner.py`

**Interfaces:**
- Produces: `LaunchMode = Literal["file_flag", "stdin_source"]`.
- Updates: `run_scan_tool(..., launch_mode: LaunchMode = "file_flag") -> ToolResult`.
- Updates: `WorkflowDependencies.launch_mode` from `DFTEXP_SCAN_LAUNCH_MODE`, defaulting to `file_flag` until licensed evidence selects otherwise.

- [ ] **Step 1: Write failing runner tests for both launch modes**

```python
@pytest.mark.parametrize("mode", ["file_flag", "stdin_source"])
def test_runner_records_and_executes_launch_mode(tmp_path, mode):
    result = run_scan_tool(paths, dofile, fake_executable(), 5.0, {}, launch_mode=mode)
    metadata = json.loads((paths.root / "run_metadata.json").read_text())
    assert result.exit_code == 0
    assert metadata["launch_mode"] == mode
```

Also assert that an unknown environment value fails before process creation and that `stdin_source` sends exactly one quoted, contained `Source` command followed by `exit`.

- [ ] **Step 2: Run the runner tests and verify the missing parameter failure**

Run: `python -m pytest tests/test_runner.py -q`

- [ ] **Step 3: Implement launch modes without a shell**

For `file_flag`, execute `[*executable, "-f", resolved_dofile]`. For `stdin_source`, execute `[*executable]` with `stdin=PIPE` and communicate `Source "<contained path>"\nexit\n`. Preserve merged stdout/stderr, timeout termination, produced-file inventory, and truthful metadata.

- [ ] **Step 4: Thread the selected launch mode through workflow dependencies**

Accept only `file_flag` or `stdin_source`; invalid configuration returns a non-success Agent result before any EDA invocation.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_runner.py tests/test_workflow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_submission/submission/scan_agent/runner.py agent_submission/submission/scan_agent/workflow.py agent_submission/tests/fake_dftexp_scan.py agent_submission/tests/test_runner.py
git commit -m "feat: add auditable DFTEXP launch modes"
```

---

### Task 3: Report-specific evidence parsers

**Files:**
- Create: `agent_submission/submission/scan_agent/reports.py`
- Create: `agent_submission/tests/test_reports.py`
- Modify: `agent_submission/submission/scan_agent/diagnostics.py`

**Interfaces:**
- Produces: `EvidenceLocation(source: str, line_number: int, source_line: str)`.
- Produces: `SignalObservation`, `ConfigObservation`, `ChainObservation`, and `PartitionObservation` immutable records.
- Produces: `ReportEvidence(signals, config, chains, partitions)`.
- Produces: `collect_report_evidence(run_paths, deadline_monotonic=None, clock=time.monotonic) -> ReportEvidence`.

- [ ] **Step 1: Write failing parser tests from the manual table layouts**

Use representative text containing:

```text
ScanConfigurationParameter    Value
chain_count                  4
max_length                   100
mix_clocks                   False
add_lockup                   True

Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I 0 10 si0 so0 se clk0 Default_Partition tool_created
W wrp_i_0 8 wrp_si0 wrp_so0 wrp_shift wrp_clk Default_Partition tool_created
```

Tests must assert normalized integers/booleans, chain class, signal name and polarity, clock list, partition name, exact source line, duplicate-row retention, malformed-row recording, and absolute-deadline checks.

- [ ] **Step 2: Run the new test module and verify import failure**

Run: `python -m pytest tests/test_reports.py -q`

- [ ] **Step 3: Implement bounded parsers and fixed report discovery**

Read only these contained files when present:

```python
REPORT_FILES = {
    "signals": "scan_signal.rpt",
    "config": "scan_cfg.rpt",
    "chains": "scan_chain.rpt",
    "partitions": "scan_partition.rpt",
}
```

Do not infer success from prose outside the documented report formats. Preserve unknown and malformed lines as parser issues so validation can fail closed.

- [ ] **Step 4: Keep legacy DRC/insertion parsing separate**

`diagnostics.py` continues to parse the tool log and DRC report. Update only its documented insertion markers from placeholder text to real `insert_dft_logic` completion messages; do not mix report-table parsing into it.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_reports.py tests/test_diagnostics.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_submission/submission/scan_agent/reports.py agent_submission/submission/scan_agent/diagnostics.py agent_submission/tests/test_reports.py
git commit -m "feat: parse documented scan evidence reports"
```

---

### Task 4: Field-level deterministic requirement validation

**Files:**
- Modify: `agent_submission/submission/scan_agent/llm.py`
- Modify: `agent_submission/submission/scan_agent/validation.py`
- Modify: `agent_submission/submission/scan_agent/artifacts.py`
- Modify: `agent_submission/tests/test_llm.py`
- Modify: `agent_submission/tests/test_validation.py`
- Modify: `agent_submission/tests/test_artifacts.py`

**Interfaces:**
- Produces: `RequirementCheck(field, requested_json, observed_json, status, reason, evidence)`.
- Extends: `ValidationReport.requirement_checks: tuple[RequirementCheck, ...]`.
- Updates: `build_requirement_mapping` to serialize actual checks instead of labeling most fields declaration-only.

- [ ] **Step 1: Write failing canonical requirement-schema tests**

Require canonical keys and types:

```python
clocks = [{"port": "clk", "off_state": 0}]
resets = [{"port": "rst_n", "off_state": 1}]
constants = [{"port": "test_mode", "constant_value": 0}]
scan_enables = [{"port": "scan_en", "off_state": 0, "view": "spec", "usage": "all"}]
partitions = [{"name": "core", "include": ["u_core"], "exclude": []}]
lockup = {"add_lockup": True, "insert_terminal_lockup": False}
wrapper_settings = {"chain_count": 2, "chain_length": 64, "style": "dedicated"}
```

Reject unknown keys, wrong scalar types, invalid polarity values, and incomplete required identity keys during structured-model correction.

- [ ] **Step 2: Write failing validation tests for every evidence family**

For each field, include one exact-match success, one mismatch, and one missing-evidence test. Add duplicate contradictory signal/config/chain rows, Wrapper rows, partition membership, lockup values, chain clocks and enables, and an explicit `unverified` functional-equivalence check that prevents success when required.

- [ ] **Step 3: Implement canonical schema validation in `llm.py`**

Keep the public `Requirements` fields stable. Normalize each item to the canonical dictionaries above and permit only documented optional keys. Empty lists/objects remain valid only when the task specifies no such requirement.

- [ ] **Step 4: Implement requirement checks over `ReportEvidence`**

Match exact ports/names and every specified attribute. Chain constraints retain count/range/length checks and additionally validate clocks, enables, wrapper class, and partition when requested. Any required field with no matching observation yields `status="unverified"`; contradictions yield `status="fail"`; only exact supported matches yield `status="pass"`.

- [ ] **Step 5: Close the decision-log mapping**

Each mapping entry must include requested JSON, observed JSON, status, reason, and contained evidence references. Overall success requires every required check to be `pass`, in addition to DRC, insertion, output, process, and input-integrity checks.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_llm.py tests/test_reports.py tests/test_validation.py tests/test_artifacts.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_submission/submission/scan_agent/llm.py agent_submission/submission/scan_agent/validation.py agent_submission/submission/scan_agent/artifacts.py agent_submission/tests/test_llm.py agent_submission/tests/test_validation.py agent_submission/tests/test_artifacts.py
git commit -m "feat: verify scan requirements from report evidence"
```

---

### Task 5: Workflow and offline integration closure

**Files:**
- Modify: `agent_submission/submission/scan_agent/workflow.py`
- Modify: `agent_submission/tests/fake_dftexp_scan.py`
- Modify: `agent_submission/tests/helpers.py`
- Modify: `agent_submission/tests/test_workflow.py`
- Modify: `agent_submission/tests/test_integration.py`

**Interfaces:**
- Consumes: documented dofile contract, launch mode, `collect_report_evidence`, and extended `ValidationReport`.
- Produces: successful offline runs containing real-format DRC, signal, config, chain, and optional partition reports under `runs/Rn/reports`.

- [ ] **Step 1: Update the fake tool to emit documented command and report evidence**

The fake must reject `examine_scan` and `insert_scan`, recognize `examine_scan_drc`, `examine_scan_chain`, and `insert_dft_logic`, and create only reports explicitly requested by the dofile. Its output tables must match the fixtures in Task 3.

- [ ] **Step 2: Write failing workflow tests for evidence routing**

Cover successful Clock/Reset/Scan Enable/config/chain validation and terminal failures for missing signal report, missing config report, missing chain report, Wrapper mismatch, partition mismatch, malformed table, and contradictory duplicate row. Assert no failed scenario creates `final_results`.

- [ ] **Step 3: Integrate report collection before validation**

Collect after the real tool exits and work artifacts are archived. Pass `ReportEvidence` into `validate_run`, retain parser issues in the per-run validation JSON, and include requirement checks in the decision log.

- [ ] **Step 4: Preserve bounded repair behavior**

A report mismatch is repair-eligible only when a dofile change can address it. Missing or malformed evidence may request one bounded repair; repeated candidates, netlist-repair diagnoses, tool limits, deadlines, and input mutation keep their existing terminal precedence.

- [ ] **Step 5: Run focused integration tests**

Run: `python -m pytest tests/test_workflow.py tests/test_integration.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_submission/submission/scan_agent/workflow.py agent_submission/tests/fake_dftexp_scan.py agent_submission/tests/helpers.py agent_submission/tests/test_workflow.py agent_submission/tests/test_integration.py
git commit -m "feat: close report-driven scan workflow"
```

---

### Task 6: Reproducible package and opt-in licensed smoke test

**Files:**
- Modify: `agent_submission/submission/requirements.txt`
- Modify: `agent_submission/requirements-dev.txt`
- Modify: `agent_submission/README.md`
- Create: `agent_submission/tests/test_real_dftexp_scan.py`
- Modify: `agent_submission/tests/test_package.py`

**Interfaces:**
- Pins: `langgraph==1.2.14`, `openai==3.26.0`, `pypdf==6.11.0`, `torch==2.14.1`, `transformers==5.19.0`, `sentence-transformers==6.1.0`, and `pytest==9.1.1`.
- Produces: opt-in smoke test controlled by `DFTEXP_REAL_SMOKE=1`, `DFTEXP_SCAN_EXECUTABLE`, `DFTEXP_SMOKE_INPUT`, and the existing License environment.

- [ ] **Step 1: Write failing package pin tests**

```python
def test_runtime_dependencies_are_exactly_pinned():
    assert Path("submission/requirements.txt").read_text().splitlines() == [
        "langgraph==1.2.14", "openai==3.26.0", "pypdf==6.11.0",
        "torch==2.14.1", "transformers==5.19.0", "sentence-transformers==6.1.0",
    ]
```

- [ ] **Step 2: Pin dependencies and verify an isolated resolver/import**

Create a temporary virtual environment outside the submission context, install both requirements files once, and run imports for `langgraph`, `openai`, `pypdf`, `torch`, `transformers`, and `sentence_transformers`. Remove the temporary environment after verification.

- [ ] **Step 3: Add the opt-in real-tool smoke test**

The ordinary suite must skip unless `DFTEXP_REAL_SMOKE=1`. When enabled, require the executable, input case, model settings, and License configuration; run one case through `run_agent`, preserve genuine logs/reports, and fail rather than substitute the fake executable.

- [ ] **Step 4: Document launch-mode evidence and external blockers**

README must state that `file_flag` remains the default pending a licensed `dftexp_scan -h`/minimal-run confirmation, describe `DFTEXP_SCAN_LAUNCH_MODE`, and provide the exact opt-in smoke command. Keep `.case_ready` formal behavior explicit.

- [ ] **Step 5: Run final verification once**

Run from `agent_submission`:

```bash
mkdir -p .pytest-tmp
python -m pytest -q --basetemp .pytest-tmp/plan-suite
python -m compileall submission
git diff --check
```

Expected: offline suite PASS; real-tool test SKIP unless explicitly enabled; compilation and whitespace checks PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_submission/submission/requirements.txt agent_submission/requirements-dev.txt agent_submission/README.md agent_submission/tests/test_real_dftexp_scan.py agent_submission/tests/test_package.py
git commit -m "build: pin scan agent runtime and add real smoke entry"
```

---

### Task 7: Local Qwen hybrid manual retrieval for Docker

**Files:**
- Create: `agent_submission/submission/scan_agent/embeddings.py`
- Create: `agent_submission/submission/scan_agent/manual_cache.py`
- Modify: `agent_submission/submission/scan_agent/manual.py`
- Modify: `agent_submission/submission/scan_agent/workflow.py`
- Modify: `agent_submission/Dockerfile`
- Modify: `agent_submission/submission/requirements.txt`
- Modify: `agent_submission/README.md`
- Test: `agent_submission/tests/test_embeddings.py`
- Test: `agent_submission/tests/test_manual.py`
- Test: `agent_submission/tests/test_package.py`

**Interfaces:**
- Produces: local `QwenEmbedder` document and instruction-aware query embedding methods.
- Extends: `ManualIndex` with in-memory vectors and deterministic reciprocal-rank fusion with existing keyword scores.
- Produces: a build-time manual cache keyed to PDF SHA-256 and chunk page/index identities.

- [ ] **Step 1: Cover Qwen adapter options and Chinese-query to English-manual retrieval using fake embeddings**
- [ ] **Step 2: Implement local-only loading and deadline-bounded batches of normalized Qwen embeddings**
- [ ] **Step 3: Fuse semantic and keyword ranks, preserving keyword-only retrieval if model/cache is missing**
- [ ] **Step 4: Validate cached vectors against the exact manual hash and stable chunk identities**
- [ ] **Step 5: Pin CPU dependencies and Qwen revision in Docker, and precompute vectors when the base image already contains the manual PDF**
- [ ] **Step 6: Document that embeddings are local/offline, LLM remains API-backed, and the image is several gigabytes**
- [ ] **Step 7: Run focused and full offline verification; build Docker only when the official base image is available locally**

---

## Final Acceptance

### Execution record — 2026-10-08

- Source implementation, offline contract tests, and Qwen hybrid retrieval wiring are complete.
- Offline verification after review fixes: `503 passed, 6 skipped`; `compileall` and `git diff --check` pass.
- Review findings about contradictory signal types and zero-length wrapper rows now fail closed.
- Docker image build has not run because the local Docker Desktop engine is unavailable (`dockerDesktopLinuxEngine` named pipe is missing).
- The licensed real-tool smoke test remains opt-in and was skipped; it needs the licensed executable, License server, evaluation model settings, and a published minimal case.

- [ ] All generated dofiles use documented DFTEXP_Scan commands.
- [ ] Both launch modes are covered offline and the selected mode is recorded.
- [ ] Success requires DRC, insertion, signal, configuration, chain, and every task-specific report check.
- [ ] Wrapper and partition requirements fail closed when evidence is absent or mismatched.
- [ ] Decision-log requirement entries contain real report references and observed values.
- [ ] Failed validation never publishes `final_results`.
- [ ] Formal `.case_ready`, budgets, input integrity, and netlist-modification prohibition remain intact.
- [ ] Dependencies are exactly pinned and import together.
- [ ] Docker contains the revision-pinned Qwen model and requires no embedding API or vector database.
- [ ] Manual retrieval uses hash-matched vectors in memory with deterministic keyword-only fallback.
- [ ] Real EDA validation is either genuinely executed with retained evidence or explicitly reported as externally blocked.
