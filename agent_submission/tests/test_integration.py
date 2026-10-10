"""Offline end-to-end audit tests using the actual graph and subprocess runner."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

from scan_agent.artifacts import validate_references
from scan_agent.dofile import DofileProposal
from scan_agent.llm import LLMOutputError
from scan_agent.runner import run_scan_tool
from scan_agent.workflow import run_agent
from tests.helpers import SAFE_DOFILE, diagnosis, make_task1_case, scripted_dependencies


FAKE_TOOL = Path(__file__).with_name("fake_dftexp_scan.py")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependencies(mode="success", scripts=None, diagnoses=None):
    deps = scripted_dependencies([], dofiles=scripts, diagnoses=diagnoses)
    deps.tool_runner = run_scan_tool
    deps.executable = (sys.executable, str(FAKE_TOOL), "--mode", mode)
    deps.manual_path = FAKE_TOOL.with_name("missing-manual.pdf")
    transport = deps.client.transport

    def with_usage(**request):
        return {"choices": [{"message": {"content": transport(**request)}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}

    deps.client.transport = with_usage
    return deps


@pytest.mark.parametrize("task2", [False, True])
def test_offline_success_has_closed_decision_log(tmp_path, task2):
    case = make_task1_case(tmp_path)
    if task2:
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE, encoding="utf-8")
    output = tmp_path / "output"
    result = run_agent(case, output, dependencies=dependencies())
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    final_id = "R2" if task2 else "R1"
    assert result.final_run == payload["final_run"] == final_id
    assert payload["task_type"] == ("task2" if task2 else "task1")
    validate_references(output, payload)
    requirements = json.loads((output / "requirements.json").read_text(encoding="utf-8"))
    assert {entry["field"]: json.loads(entry["requested_json"]) for entry in payload["requirement_audit"]} == requirements
    for entry in payload["requirement_mapping"]:
        ref = entry["config_ref"]
        line = int(ref["locator"][1:])
        dofile_lines = (output / ref["source"]).read_text(encoding="utf-8").splitlines()
        assert dofile_lines[line - 1].strip() == entry["dft_config"]
    assert payload["manual"]["available"] is False
    assert payload["token_usage"] == [{"model": "test-only", "prompt_tokens": 11,
                                       "completion_tokens": 7, "total_tokens": 18}] * 2
    metadata = json.loads((output / "runs/R1/run_metadata.json").read_text(encoding="utf-8"))
    assert payload["tool_runs"][0]["duration_seconds"] == metadata["duration_seconds"]
    assert payload["tool_runs"][0]["produced_files"] == ["runs/R1/" + name for name in metadata["produced_files"]]
    assert len(payload["final_artifacts"]) >= 4
    for artifact in payload["final_artifacts"]:
        assert sha256(output / artifact["path"]) == sha256(output / artifact["source"]) == artifact["sha256"]
    assert sha256(output / "final_results/deliverables/post_scan.v") == sha256(
        output / f"runs/{final_id}/deliverables/post_scan.v")
    assert payload["file_changes"] == ([] if not task2 else [payload["file_changes"][0]])
    if task2:
        assert [(run["run_id"], run.get("role")) for run in payload["tool_runs"]] == [
            ("R1", "original"), ("R2", None)]
        assert (output / "runs/R1/R1.log").is_file()
        assert (output / "runs/R1/deliverables/R1.dofile").read_text(encoding="utf-8") == (
            case / "original.dofile").read_text(encoding="utf-8")
        assert payload["file_changes"][0]["change_id"] == "F1"
        assert payload["file_changes"][0]["diff_path"] == "diffs/dofile_R1_to_R2.diff"
        issue = payload["issue_resolutions"][0]
        assert issue["found"]["run_ref"] == "R1"
        assert issue["found"]["source"].startswith("runs/R1/")
        assert issue["verify"]["passed"] is True
    else:
        assert payload["issue_resolutions"] == []


def test_task2_never_reads_public_preset_issues(tmp_path, monkeypatch):
    case = make_task1_case(tmp_path)
    (case / "original.dofile").write_text(
        "# original\n" + SAFE_DOFILE.replace("insert_dft_logic", "exec forbidden"),
        encoding="utf-8",
    )
    preset = case / "preset_issues.json"
    preset.write_text('{"问题列表":[{"现象":"fabricated answer"}]}', encoding="utf-8")
    real_open = Path.open
    real_read_text = Path.read_text

    def checked_open(path, *args, **kwargs):
        assert path != preset
        return real_open(path, *args, **kwargs)

    def checked_read_text(path, *args, **kwargs):
        assert path != preset
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(Path, "read_text", checked_read_text)
    output = tmp_path / "output"
    result = run_agent(case, output, dependencies("repair"))
    assert result.status == "success"
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert payload["final_run"] == "R2"
    assert "fabricated answer" not in json.dumps(payload, ensure_ascii=False)
    assert [run["run_id"] for run in payload["tool_runs"]] == ["R1", "R2"]


def test_tool_unavailable_never_creates_fake_final_artifacts(tmp_path):
    case = make_task1_case(tmp_path)
    deps = dependencies()
    deps.executable = (str(tmp_path / "absent-tool"),)
    output = tmp_path / "output"
    result = run_agent(case, output, dependencies=deps)
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert result.status == "tool_failure"
    assert payload["final_run"] is None
    assert "tool_unavailable" in json.dumps(payload)
    assert not (output / "final_results").exists()
    validate_references(output, payload)


@pytest.mark.parametrize("task2", [False, True])
def test_two_run_repair_closes_found_diagnosis_fix_verify(tmp_path, task2):
    case = make_task1_case(tmp_path)
    if task2:
        (case / "original.dofile").write_text("# original\n" + SAFE_DOFILE.replace("insert_dft_logic", "exec forbidden"), encoding="utf-8")
    output = tmp_path / "output"
    result = run_agent(case, output, dependencies("repair"))
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert result.status == "success"
    assert result.final_run == payload["final_run"] == "R2"
    validate_references(output, payload)
    issue = payload["issue_resolutions"][-1]
    assert issue["found"]["source"] == ("runs/R1/R1.log" if task2 else "runs/R1/validation.json")
    assert issue["found"]["run_ref"] == "R1"
    assert issue["diagnosis"]["root_cause"] == "fixture diagnosis"
    assert issue["fix"]["summary"] == "fixture correction"
    assert issue["verify"]["run_id"] == "R2"
    assert issue["verify"]["passed"] is True
    if task2:
        assert issue["found"]["excerpt"] in (output / "runs/R1/R1.log").read_text(encoding="utf-8")
        assert issue["history"] == []
    else:
        assert issue["history"][0]["dofile_hash"] == sha256(output / "runs/R1/deliverables/R1.dofile")
    assert len(payload["tool_runs"]) == 2
    assert len(payload["validation_results"]) == (1 if task2 else 2)
    assert len(payload["issue_resolutions"]) >= (2 if task2 else 1)
    repair_change = payload["file_changes"][-1]
    assert repair_change["diff"] == "diffs/dofile_R1_to_R2.diff"
    diff = (output / repair_change["diff"]).read_text(encoding="utf-8")
    if task2:
        assert "-exec forbidden" in diff and "+insert_dft_logic" in diff
    else:
        assert "-# candidate 0" in diff and "+# candidate 1" in diff
    for artifact in payload["final_artifacts"]:
        assert artifact["source"].startswith("runs/R2/")
        assert sha256(output / artifact["path"]) == sha256(output / artifact["source"]) == artifact["sha256"]
    assert "offline test fixture R2" in (output / "final_results/deliverables/post_scan.v").read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario,status,runs", [
    ("drc", "budget_exhausted", 3),
    ("missing_output", "budget_exhausted", 3),
    ("license", "tool_failure", 1),
    ("timeout", "budget_exhausted", 1),
    ("no_progress", "no_progress", 1),
    ("injected_no_progress", "no_progress", 1),
    ("mutation", "compliance_failure", 1),
    ("budget", "budget_exhausted", 1),
    ("netlist", "unsupported_netlist_repair", 1),
    ("invalid_repair", "invalid_model_output", 1),
])
def test_failed_terminal_scenarios_preserve_every_attempt(tmp_path, scenario, status, runs):
    case = make_task1_case(tmp_path)
    output = tmp_path / "output"
    mode = scenario if scenario in {"drc", "missing_output", "license", "mutation"} else "error"
    scripts = [SAFE_DOFILE] * 3 if scenario == "no_progress" else None
    diagnoses = [diagnosis("requires_netlist_repair")] if scenario == "netlist" else None
    deps = dependencies(mode, scripts, diagnoses)
    if scenario == "injected_no_progress":
        deps.repairer = lambda *args: DofileProposal("dofile", "repeat", (), "unchanged", SAFE_DOFILE + "# candidate 0\n")
    if scenario == "invalid_repair":
        def invalid(*args):
            raise LLMOutputError("fixture invalid repair")
        deps.repairer = invalid
    if scenario == "mutation":
        deps.executable += ("--input-to-mutate", str(case / "netlist/design.v"))
    if scenario == "timeout":
        deps.executable = (sys.executable, str(FAKE_TOOL), "--mode", "sleep")
        deps.tool_runner = lambda paths, dofile, executable, timeout, env: run_scan_tool(paths, dofile, executable, 0.2, env)
    if scenario == "budget":
        now = [0.0]
        deps.ready_waiter = lambda *args: 0.0
        deps.clock = lambda: now[0]
        def consume(*args):
            result = run_scan_tool(*args)
            now[0] = 110.0
            return result
        deps.tool_runner = consume
    result = run_agent(case, output, deps)
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert result.status == payload["status"] == status
    assert result.final_run is payload["final_run"] is None
    assert not (output / "final_results").exists()
    assert "final_artifacts" not in payload
    assert len(payload["tool_runs"]) == runs
    assert len(payload["validation_results"]) == (0 if scenario == "budget" else runs)
    assert len(list((output / "runs").iterdir())) == runs
    validate_references(output, payload)
    for attempt in payload["tool_runs"]:
        metadata = json.loads((output / attempt["metadata_file"]).read_text(encoding="utf-8"))
        for field in ("exit_code", "timed_out", "duration_seconds", "failure_kind"):
            assert attempt[field] == metadata[field]
        if scenario == "budget":
            assert not (output / f"runs/{attempt['run_id']}/validation.json").exists()
            continue
        validation = json.loads((output / f"runs/{attempt['run_id']}/validation.json").read_text(encoding="utf-8"))
        assert not validation["passed"]
    if scenario in {"no_progress", "injected_no_progress", "netlist", "invalid_repair"}:
        issue = payload["issue_resolutions"][-1]
        assert issue["found"]["source"] == "runs/R1/validation.json"
        assert issue["verify"] == {"status": "not_run", "passed": None}
        assert issue["outcome"] == ("no_progress" if "no_progress" in scenario else "requires_netlist_repair" if scenario == "netlist" else "failed")
        if scenario == "no_progress":
            assert len(issue["model_response_files"]) == 2  # original and schema correction
    if scenario == "mutation":
        assert json.loads((output / "runs/R1/validation.json").read_text(encoding="utf-8"))["input_integrity"] is False


@pytest.mark.parametrize("mutate", ["final", "source", "both", "extra"])
def test_publication_rechecks_all_promoted_artifacts(tmp_path, monkeypatch, mutate):
    from scan_agent import workflow
    original = workflow.promote_final
    def tamper(output, paths, required, *args):
        manifest = original(output, paths, required, *args)
        if mutate in {"final", "both"}:
            (output / "final_results/reports/scan_signal.rpt").write_text("changed", encoding="utf-8")
        if mutate in {"source", "both"}:
            (paths.reports / "scan_signal.rpt").write_text("changed", encoding="utf-8")
        if mutate == "extra":
            (output / "final_results/deliverables/extra.v").write_text("changed", encoding="utf-8")
        return manifest
    monkeypatch.setattr(workflow, "promote_final", tamper)
    output = tmp_path / "output"
    result = run_agent(make_task1_case(tmp_path), output, dependencies())
    assert result.status == "tool_failure"
    assert result.final_run is None
    assert not (output / "final_results").exists()
    assert (output / "runs/R1/unpublished_final").exists()
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    validate_references(output, payload)


def test_windows_transient_publication_lock_keeps_atomic_success(tmp_path, monkeypatch):
    from scan_agent import artifacts
    original = artifacts.os.replace
    failures = []
    def locked_once(source, destination):
        if Path(destination).name == "final_results" and not failures:
            failures.append(str(source))
            error = PermissionError("fixture Windows sharing violation")
            error.winerror = 32
            raise error
        return original(source, destination)
    monkeypatch.setattr(artifacts.os, "replace", locked_once)
    monkeypatch.setattr(artifacts, "_WINDOWS", True, raising=False)
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, dependencies()).status == "success"
    assert len(failures) == 1


@pytest.mark.parametrize("metadata", ["not JSON", {"produced_files": ["../outside.v", "missing.v"]},
                                     '{"duration_seconds":1e999}', '{"produced_files":[1e999]}'])
def test_failed_runner_metadata_remains_auditable(tmp_path, metadata):
    deps = dependencies()
    def invalid(paths, *args):
        paths.log.write_text("actual partial stdout\n", encoding="utf-8")
        (paths.root / "run_metadata.json").write_text(metadata if isinstance(metadata, str) else json.dumps(metadata), encoding="utf-8")
        return None
    deps.tool_runner = invalid
    output = tmp_path / "output"
    assert run_agent(make_task1_case(tmp_path), output, deps).status == "tool_failure"
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    validate_references(output, payload)
    assert not (output / "final_results").exists()
    assert payload["tool_runs"][0]["metadata_error"]
    assert (output / "runs/R1/run_metadata.json").read_text(encoding="utf-8") == (metadata if isinstance(metadata, str) else json.dumps(metadata))


@pytest.mark.parametrize("seed", ["final_only", "success"])
def test_reused_output_quarantines_stale_success_and_publishes_current_failure(tmp_path, seed):
    case = make_task1_case(tmp_path)
    output = tmp_path / "output"
    if seed == "success":
        assert run_agent(case, output, dependencies()).status == "success"
        old_audit = (output / "decision_log.json").read_bytes()
    else:
        (output / "final_results/deliverables").mkdir(parents=True)
        (output / "final_results/deliverables/post_scan.v").write_bytes(b"stale netlist")
    stale = (output / "final_results/deliverables/post_scan.v").read_bytes()
    deps = dependencies()
    deps.ready_waiter = lambda *args: pytest.fail("reused outputs must fail before waiting or invoking collaborators")
    result = run_agent(case, output, deps)
    assert result.status == "compliance_failure"
    assert result.final_run is None
    assert not (output / "final_results").exists()
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert payload["status"] == "compliance_failure"
    assert payload["final_run"] is None
    assert payload["tool_runs"] == payload["token_usage"] == []
    validate_references(output, payload)
    archived = output / payload["prior_evidence"][0]["path"]
    assert (archived / "final_results/deliverables/post_scan.v").read_bytes() == stale
    if seed == "success":
        assert (archived / "decision_log.json").read_bytes() == old_audit
        assert (archived / "runs/R1/R1.log").exists()
        validate_references(archived, json.loads(old_audit))


@pytest.mark.parametrize("name", [
    "runs", "final_results", "decision_log.json", "diffs", "candidates",
    "requirements.json", "original_static_diagnostics.json", "repairs",
    "model_responses", "quarantine-stale", ".final-stale",
])
def test_every_owned_output_namespace_triggers_one_quarantine_policy(tmp_path, name):
    output = tmp_path / "output"
    output.mkdir()
    stale = output / name
    if name.endswith(".json"):
        stale.write_text("stale", encoding="utf-8")
    else:
        stale.mkdir()
    result = run_agent(make_task1_case(tmp_path), output, dependencies=dependencies("success"))
    assert result.status == "compliance_failure"
    archives = list(output.glob("quarantine-*"))
    assert archives
    assert (archives[0] / name).exists()


@pytest.mark.parametrize("failure_stage", ["promotion", "manifest", "decision_write"])
def test_failed_success_publication_retains_usage_and_prior_audit_evidence(tmp_path, monkeypatch, failure_stage):
    from scan_agent import workflow
    if failure_stage == "promotion":
        def fail(*args):
            raise OSError("fixture promotion failure")
        monkeypatch.setattr(workflow, "promote_final", fail)
    elif failure_stage == "manifest":
        def fail(*args):
            raise ValueError("fixture manifest failure")
        monkeypatch.setattr(workflow, "verify_final_manifest", fail)
    else:
        writer = workflow.write_decision_log
        def fail(root, payload, *args):
            if payload["status"] == "success":
                raise OSError("fixture decision write failure")
            return writer(root, payload, *args)
        monkeypatch.setattr(workflow, "write_decision_log", fail)
    output = tmp_path / "output"
    deps = dependencies("repair")
    result = run_agent(make_task1_case(tmp_path), output, deps)
    payload = json.loads((output / "decision_log.json").read_text(encoding="utf-8"))
    assert result.status == "tool_failure"
    assert payload["token_usage"] == [{"model": "test-only", "prompt_tokens": 11,
                                      "completion_tokens": 7, "total_tokens": 18}] * 3
    assert payload["token_usage_available"] is True
    assert {item["field"]: json.loads(item["requested_json"]) for item in payload["requirement_audit"]} == json.loads(
        (output / "requirements.json").read_text(encoding="utf-8"))
    assert payload["manual"]["available"] is False
    assert len(payload["tool_runs"]) == len(payload["validation_results"]) == 2
    assert payload["issue_resolutions"][-1]["verify"]["passed"] is True
    assert payload["file_changes"][0]["diff"] == "diffs/dofile_R1_to_R2.diff"
    assert payload["final_run"] is None
    assert not (output / "final_results").exists()
    validate_references(output, payload)
