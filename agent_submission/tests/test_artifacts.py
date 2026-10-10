import hashlib
import json
from pathlib import Path

import scan_agent.artifacts as artifacts

import pytest

from scan_agent.artifacts import (
    BrokenReferenceError,
    ArtifactIntegrityError,
    MissingArtifactError,
    create_run,
    owned_output_entries,
    promote_final,
    validate_references,
    write_run_dofile,
    write_decision_log,
    write_dofile_diff,
)
from scan_agent.deadline import DeadlineExceeded


def test_create_run_uses_required_names(tmp_path: Path) -> None:
    paths = create_run(tmp_path, 2)
    assert paths.root == tmp_path / "runs" / "R2"
    assert paths.log == paths.root / "R2.log"
    assert paths.dofile == paths.deliverables / "R2.dofile"
    assert paths.reports.is_dir()
    assert paths.work.is_dir()


def test_owned_namespace_detection_includes_dangling_symlink_entries(tmp_path, monkeypatch):
    dangling = tmp_path / "runs"
    original = Path.iterdir

    def entries(path):
        return iter((dangling,)) if path == tmp_path else original(path)

    monkeypatch.setattr(Path, "iterdir", entries)
    assert owned_output_entries(tmp_path) == (dangling,)


def test_promote_final_rejects_missing_required_artifact(tmp_path: Path) -> None:
    paths = create_run(tmp_path, 1)
    paths.log.write_text("real log", encoding="utf-8")
    write_run_dofile(paths, "exit")
    with pytest.raises(MissingArtifactError):
        promote_final(tmp_path, paths, ["post_scan.v"])
    assert not (tmp_path / "final_results").exists()


def test_decision_log_reference_must_exist(tmp_path: Path) -> None:
    payload = {"tool_runs": [{"log_file": "runs/R1/R1.log"}]}
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, payload)


@pytest.mark.parametrize("number", [0, -1, True, 1.5])
def test_run_number_must_be_one_based(tmp_path: Path, number: object) -> None:
    with pytest.raises(ValueError):
        create_run(tmp_path, number)


def _real_run(tmp_path: Path):
    paths = create_run(tmp_path, 2)
    paths.log.write_bytes(b"real scan log\r\n")
    write_run_dofile(paths, "present_design top\nexit\n")
    (paths.deliverables / "post_scan.v").write_bytes(b"module top(); endmodule\n")
    (paths.reports / "scan.rpt").write_bytes(b"tool-produced scan report\n")
    return paths


def test_promote_final_renames_and_preserves_hashes(tmp_path: Path) -> None:
    paths = _real_run(tmp_path)
    (paths.work / "temporary.txt").write_text("not a deliverable")
    manifest = promote_final(tmp_path, paths, ["post_scan.v", "scan.rpt"])
    assert manifest.run_id == "R2"
    assert manifest.root == tmp_path / "final_results"
    pairs = [(paths.log, "final.log"), (paths.dofile, "deliverables/final.dofile"),
             (paths.deliverables / "post_scan.v", "deliverables/post_scan.v"),
             (paths.reports / "scan.rpt", "reports/scan.rpt")]
    assert len(manifest.hashes) == len(pairs)
    for source, name in pairs:
        target = manifest.root / name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
        assert manifest.hashes[f"final_results/{name}"] == digest
    assert not (manifest.root / "deliverables/R2.dofile").exists()
    assert not (manifest.root / "work").exists()


@pytest.mark.parametrize("missing", ["log", "dofile"])
def test_promotion_requires_real_log_and_dofile(tmp_path: Path, missing: str) -> None:
    paths = _real_run(tmp_path)
    getattr(paths, missing).unlink()
    with pytest.raises(MissingArtifactError):
        promote_final(tmp_path, paths, ["post_scan.v"])
    assert not (tmp_path / "final_results").exists()


@pytest.mark.parametrize("relative", ["deliverables/post_scan.v", "reports/scan.rpt", "R2.log", "deliverables/R2.dofile"])
def test_promotion_rejects_empty_required_artifact(tmp_path: Path, relative: str) -> None:
    paths = _real_run(tmp_path)
    (paths.root / relative).write_bytes(b"")
    with pytest.raises(MissingArtifactError):
        promote_final(tmp_path, paths, ["post_scan.v", "scan.rpt"])
    assert not (tmp_path / "final_results").exists()


def test_corrupt_copy_never_publishes_final(tmp_path: Path, monkeypatch) -> None:
    paths = _real_run(tmp_path)
    def corrupt(source, destination, deadline, clock):
        Path(destination).write_bytes(b"corrupted")
    monkeypatch.setattr(artifacts, "_copy_file", corrupt)
    with pytest.raises(ArtifactIntegrityError):
        promote_final(tmp_path, paths, ["post_scan.v"])
    assert not (tmp_path / "final_results").exists()
    assert not list(tmp_path.glob(".final-*"))


def test_large_copy_checks_deadline_between_chunks(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    ticks = iter((0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="artifact promotion deadline"):
        artifacts._copy_file(source, destination, 1.0, lambda: next(ticks))
    assert destination.stat().st_size == 1024 * 1024


def test_audit_reference_walk_checks_absolute_deadline(tmp_path: Path) -> None:
    (tmp_path / "one.log").write_text("one", encoding="utf-8")
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="audit reference validation deadline"):
        validate_references(
            tmp_path, {"files": ["one.log", "one.log"]},
            deadline_monotonic=1.0, clock=lambda: next(ticks),
        )


def test_dofile_and_diff_are_atomic_and_contained(tmp_path: Path, monkeypatch) -> None:
    calls = []
    replace = artifacts.os.replace
    def tracked_replace(source, destination):
        assert Path(source).parent == Path(destination).parent
        calls.append(Path(destination))
        replace(source, destination)
    monkeypatch.setattr(artifacts.os, "replace", tracked_replace)
    paths = create_run(tmp_path, 1)
    assert write_run_dofile(paths, "before\n").read_text() == "before\n"
    diff = write_dofile_diff(tmp_path, "before\n", "after\n", "R1", "R2")
    assert diff == tmp_path / "diffs/R1_to_R2.diff"
    assert "--- R1.dofile\n+++ R2.dofile\n" in diff.read_text()
    assert "-before\n+after\n" in diff.read_text()
    assert calls == [paths.dofile, diff]


@pytest.mark.parametrize("reference", ["../external.log", "/external.log", "C:\\external.log", "runs/R1/missing.log"])
def test_invalid_references_are_rejected(tmp_path: Path, reference: str) -> None:
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, {"requirement_mapping": [{"evidence": {"path": reference, "locator": "line 1"}}]})


def test_repeated_evidence_path_is_resolved_once(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "scan.rpt").write_text("report\n", encoding="utf-8")
    original = artifacts._relative_path
    calls = []

    def tracked(root, value):
        calls.append(value)
        return original(root, value)

    monkeypatch.setattr(artifacts, "_relative_path", tracked)
    validate_references(tmp_path, {"evidence": [
        {"source": "scan.rpt", "locator": f"line {line}"} for line in range(100)
    ]})
    assert calls == ["scan.rpt"]


def test_decision_log_closes_nested_references_and_is_atomic(tmp_path: Path, monkeypatch) -> None:
    paths = _real_run(tmp_path)
    calls = []
    replace = artifacts.os.replace
    def tracked_replace(source, destination):
        assert Path(source).parent == tmp_path
        calls.append(Path(destination))
        replace(source, destination)
    monkeypatch.setattr(artifacts.os, "replace", tracked_replace)
    payload = {"final_run": "R2", "tool_runs": [{"log_file": "runs/R2/R2.log"}],
               "requirement_mapping": [{"evidence": {"path": "runs/R2/reports/scan.rpt", "locator": "line 1"}}],
               "files": ["runs/R2/deliverables/post_scan.v"], "status": "success"}
    target = write_decision_log(tmp_path, payload)
    assert target == tmp_path / "decision_log.json"
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert calls == [target]
    assert paths.log.exists()


def test_failed_decision_write_preserves_existing_log(tmp_path: Path, monkeypatch) -> None:
    target = write_decision_log(tmp_path, {"status": "tool_failure", "final_run": None})
    original = target.read_bytes()
    def fail_replace(source, destination):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(artifacts.os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_decision_log(tmp_path, {"status": "budget_exhausted", "final_run": None})
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_empty_locator_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, {"evidence": {"locator": " "}})


@pytest.mark.parametrize("reference", [".", "./", "././", "runs/.."])
def test_output_root_itself_is_not_an_evidence_reference(tmp_path, reference):
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, {"source": reference, "locator": "line 1"})


@pytest.mark.parametrize("target", ["root", "outside"])
def test_symlink_root_alias_and_escape_are_not_evidence(tmp_path, monkeypatch, target):
    destination = tmp_path if target == "root" else tmp_path.parent
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(destination, target_is_directory=True)
    except OSError:
        # Windows may deny symlink creation without Developer Mode. Exercise
        # exactly the resolved-path boundary with an injected root alias then.
        alias.mkdir()
        original = Path.resolve
        monkeypatch.setattr(Path, "resolve", lambda self, *args, **kwargs:
                            destination if self == alias else original(self, *args, **kwargs))
    with pytest.raises(BrokenReferenceError):
        validate_references(tmp_path, {"source": "alias", "locator": "line 1"})
