"""Output-contained run evidence, atomic writes, and verified final copies."""

from dataclasses import dataclass
import difflib
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any, Iterable, Mapping

from .deadline import check_deadline, iter_paths_with_deadline


_WINDOWS = os.name == "nt"
OWNED_OUTPUT_NAMESPACES = frozenset({
    "runs", "final_results", "decision_log.json", "diffs", "candidates",
    "requirements.json", "original_static_diagnostics.json", "repairs",
    "model_responses",
})
OWNED_OUTPUT_PREFIXES = ("quarantine-", ".final-")


class MissingArtifactError(RuntimeError):
    """A required real run artifact is absent."""


class BrokenReferenceError(ValueError):
    """An audit reference is missing or escapes the output directory."""


class ArtifactIntegrityError(RuntimeError):
    """A copied artifact differs from its source."""


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    root: Path
    log: Path
    deliverables: Path
    reports: Path
    work: Path

    @property
    def dofile(self) -> Path:
        return self.deliverables / f"{self.run_id}.dofile"


@dataclass(frozen=True)
class FinalManifest:
    run_id: str
    root: Path
    hashes: dict[str, str]


def _contained(output_dir: Path, path: Path) -> Path:
    root, resolved = output_dir.resolve(), path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise BrokenReferenceError(f"path is not strictly beneath output directory: {path}")
    return path


def _relative_path(output_dir: Path, name: str) -> Path:
    windows = PureWindowsPath(name)
    if not name or Path(name).is_absolute() or windows.is_absolute() or windows.drive:
        raise BrokenReferenceError(f"reference must be output-relative: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise BrokenReferenceError(f"reference contains parent traversal: {name!r}")
    return _contained(output_dir, output_dir.joinpath(*parts))


def _atomic_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        _replace_atomic(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _replace_atomic(source: Path, destination: Path) -> None:
    """Allow a bounded retry for transient Windows file-scanner locks."""
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if not _WINDOWS or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 4:
                raise
            time.sleep(0.05)


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> Path:
    """Write JSON evidence through the shared atomic file primitive."""
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return _atomic_text(path, content)


def owned_output_entries(output_dir: Path) -> tuple[Path, ...]:
    """Return all agent-owned top-level entries, including dangling symlinks."""
    if not output_dir.exists():
        return ()
    entries = []
    for path in output_dir.iterdir():
        if path.name in OWNED_OUTPUT_NAMESPACES or path.name.startswith(OWNED_OUTPUT_PREFIXES):
            entries.append(path)
    return tuple(sorted(entries, key=lambda path: path.name))


def quarantine_existing_evidence(output_dir: Path) -> Path:
    """Recoverably move an earlier invocation out of the current namespaces.

    Exact child names are moved as directory entries, including symlinks; this
    never follows a stale symlink to mutate its target. A fresh unique archive
    avoids reusing an existing archive directory or overwriting prior evidence.
    """
    sources = owned_output_entries(output_dir)
    archive = _contained(output_dir, Path(tempfile.mkdtemp(prefix="quarantine-", dir=output_dir)))
    for source in sources:
        _replace_atomic(source, _contained(output_dir, archive / source.name))
    return archive


def create_run(output_dir: Path, run_number: int) -> RunPaths:
    if isinstance(run_number, bool) or not isinstance(run_number, int) or run_number < 1:
        raise ValueError("run_number must be a one-based integer")
    run_id = f"R{run_number}"
    root = _contained(output_dir, output_dir / "runs" / run_id)
    paths = RunPaths(run_id, root, root / f"{run_id}.log", root / "deliverables", root / "reports", root / "work")
    for directory in (paths.deliverables, paths.reports, paths.work):
        _contained(output_dir, directory).mkdir(parents=True, exist_ok=True)
    return paths


def write_run_dofile(paths: RunPaths, content: str) -> Path:
    return _atomic_text(_contained(paths.root, paths.dofile), content)


def write_dofile_diff(output_dir: Path, previous: str, current: str, previous_id: str, current_id: str) -> Path:
    for run_id in (previous_id, current_id):
        if not re.fullmatch(r"R[1-9]\d*", run_id):
            raise ValueError("diff run IDs must be one-based Rn identifiers")
    diff = "".join(difflib.unified_diff(previous.splitlines(keepends=True), current.splitlines(keepends=True), fromfile=f"{previous_id}.dofile", tofile=f"{current_id}.dofile"))
    path = _contained(output_dir, output_dir / "diffs" / f"{previous_id}_to_{current_id}.diff")
    return _atomic_text(path, diff)


def _sha256(
    path: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            check_deadline(deadline_monotonic, clock, "artifact hashing")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(
    path: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
) -> str:
    chunks = []
    with path.open("rb") as source:
        while True:
            check_deadline(deadline_monotonic, clock, "audit file reading")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    check_deadline(deadline_monotonic, clock, "audit file reading")
    return b"".join(chunks).decode("utf-8", errors="strict")


def _copy_file(
    source: Path,
    destination: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
) -> None:
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            check_deadline(deadline_monotonic, clock, "artifact promotion")
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
    shutil.copystat(source, destination)


def promote_final(
    output_dir: Path,
    run_paths: RunPaths,
    required_names: Iterable[str],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FinalManifest:
    """Publish one validated run; callers own the validation success decision."""
    expected = output_dir / "runs" / run_paths.run_id
    if not re.fullmatch(r"R[1-9]\d*", run_paths.run_id) or run_paths.root.resolve() != expected.resolve():
        raise BrokenReferenceError("final source must be a run beneath output/runs")
    _contained(output_dir, run_paths.root)
    sources = [(run_paths.log, Path("final.log")), (run_paths.dofile, Path("deliverables/final.dofile"))]
    for source, _ in sources:
        check_deadline(deadline_monotonic, clock, "artifact promotion")
        _contained(run_paths.root, source)
        if not source.is_file() or source.stat().st_size == 0:
            raise MissingArtifactError(f"missing or empty run artifact: {source}")
    for name in required_names:
        check_deadline(deadline_monotonic, clock, "artifact promotion")
        candidates = [_relative_path(run_paths.deliverables, name), _relative_path(run_paths.reports, name)]
        if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
            raise MissingArtifactError(f"missing or empty required artifact: {name}")
    for directory, label in ((run_paths.deliverables, "deliverables"), (run_paths.reports, "reports")):
        _contained(run_paths.root, directory)
        for source in iter_paths_with_deadline(
            directory, deadline_monotonic, clock, "artifact promotion",
        ):
            _contained(run_paths.root, source)
            if source.is_file() and source != run_paths.dofile:
                relative = Path(label) / source.relative_to(directory)
                if relative == Path("deliverables/final.dofile"):
                    raise ArtifactIntegrityError("run contains a conflicting final.dofile")
                sources.append((source, relative))
    final = _contained(output_dir, output_dir / "final_results")
    if final.exists():
        raise FileExistsError(f"final results already exist: {final}")
    hashes = {}
    with tempfile.TemporaryDirectory(prefix=".final-", dir=output_dir) as temporary:
        stage = Path(temporary) / "final_results"
        (stage / "deliverables").mkdir(parents=True)
        (stage / "reports").mkdir()
        for source, relative in sources:
            check_deadline(deadline_monotonic, clock, "artifact promotion")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = _sha256(source, deadline_monotonic, clock)
            _copy_file(source, destination, deadline_monotonic, clock)
            if (_sha256(source, deadline_monotonic, clock) != before
                    or _sha256(destination, deadline_monotonic, clock) != before):
                raise ArtifactIntegrityError(f"copy hash mismatch: {source}")
            hashes[(Path("final_results") / relative).as_posix()] = before
        _replace_atomic(stage, final)
    return FinalManifest(run_paths.run_id, final, hashes)


def validate_references(
    output_dir: Path,
    payload: Mapping[str, Any],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Check nested file/path references and evidence locators before publication."""
    verified_paths: set[str] = set()

    def check_path(value: Any) -> None:
        check_deadline(deadline_monotonic, clock, "audit reference validation")
        if not isinstance(value, str):
            raise BrokenReferenceError("path reference must be a string")
        if value in verified_paths:
            return
        path = _relative_path(output_dir, value)
        if not path.exists():
            raise BrokenReferenceError(f"reference does not exist: {value}")
        verified_paths.add(value)

    def visit(value: Any, key: str = "") -> None:
        check_deadline(deadline_monotonic, clock, "audit reference validation")
        if key in {"requested_json", "observed_json"}:
            # Requirement values may contain domain fields named source/file;
            # they are data, not audit artifact references.
            return
        if key == "locator" and (not isinstance(value, str) or not value.strip()):
            raise BrokenReferenceError("evidence locator must be nonempty")
        if key == "final_run" and value is not None:
            check_path(f"runs/{value}" if isinstance(value, str) and re.fullmatch(r"R[1-9]\d*", value) else value)
        elif key in {"path", "file", "source", "reference", "diff", "log", "dofile"} or key.endswith(("_path", "_file")):
            if value is not None:
                check_path(value)
        elif key in {"paths", "files", "references", "artifacts"} or key.endswith(("_paths", "_files")):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        check_path(item)
                    else:
                        visit(item)
            else:
                visit(value)
        elif isinstance(value, Mapping):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)


def write_decision_log(
    output_dir: Path,
    payload: Mapping[str, Any],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Path:
    validate_references(output_dir, payload, deadline_monotonic, clock)
    check_deadline(deadline_monotonic, clock, "decision-log serialization")
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    check_deadline(deadline_monotonic, clock, "decision-log serialization")
    path = _contained(output_dir, output_dir / "decision_log.json")
    return _atomic_text(path, content)


def build_requirement_mapping(
    requirements: Mapping[str, object],
    configurations: list[dict],
    validations: list[dict],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Map typed requirements to their latest deterministic report checks."""
    checks = validations[-1].get("requirement_checks", []) if validations else []
    records = []
    for name, value in requirements.items():
        check_deadline(deadline_monotonic, clock, "requirement audit")
        matched = [item for item in checks if isinstance(item, Mapping) and
                   (item.get("field") == name or str(item.get("field", "")).startswith(name + "[")
                    or str(item.get("field", "")).startswith(name + "."))]
        if matched:
            statuses = {item.get("status") for item in matched}
            status = "fail" if "fail" in statuses else "unverified" if "unverified" in statuses else "pass"
            observed = [item.get("observed_json") for item in matched]
            reason = "; ".join(str(item.get("reason", "")) for item in matched if item.get("reason"))
            evidence = [ref for item in matched for ref in item.get("evidence", []) if isinstance(ref, Mapping)]
        elif value in ([], {}):
            status, observed, reason, evidence = "pass", value, "no requirement declared", []
        else:
            status, observed, reason, evidence = "unverified", None, "no deterministic checker is available for this requirement", []
        records.append({"field": name, "requirement_type": name,
                        "requested_json": json.dumps(value, ensure_ascii=False, allow_nan=False),
                        "observed_json": json.dumps(observed, ensure_ascii=False, allow_nan=False),
                        "status": status, "reason": reason,
                        "requirement": {"source": "requirements.json", "locator": name},
                        "configuration": configurations, "evidence": evidence})
    return records


def build_tool_runs(
    output_dir: Path,
    attempts: list[dict],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Read actual runner metadata, resolving its run-relative file inventory."""
    runs = []
    for attempt in attempts:
        check_deadline(deadline_monotonic, clock, "tool-run audit")
        validate_references(output_dir, attempt, deadline_monotonic, clock)
        entry = dict(attempt)
        try:
            def reject_constant(value):
                raise ValueError("non-finite run metadata")
            def finite_float(text):
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError("non-finite run metadata")
                return value
            metadata = json.loads(_read_text(
                                  output_dir / attempt["metadata_file"], deadline_monotonic, clock),
                                  parse_constant=reject_constant, parse_float=finite_float)
            if not isinstance(metadata, dict):
                raise ValueError("run metadata must be an object")
        except (OSError, ValueError) as error:
            # Keep the original metadata file as evidence; an unreadable
            # document must not prevent publishing a closed failure audit.
            entry["metadata_error"] = type(error).__name__
            metadata = {}
        for name in ("exit_code", "timed_out", "duration_seconds", "success", "failure_kind", "failure_detail",
                     "command", "launch_mode", "dofile_file"):
            if name in metadata:
                value = metadata[name]
                types = {"exit_code": (int,), "timed_out": (bool,), "duration_seconds": (int, float),
                         "success": (bool,), "failure_kind": (str,), "failure_detail": (str,),
                         "command": (list,), "launch_mode": (str,), "dofile_file": (str,)}
                if value is None or type(value) in types[name]:
                    entry[name] = value
                else:
                    entry["metadata_error"] = "invalid metadata field types"
        # Workflow boundary failures do not erase a subprocess's actual result.
        if attempt.get("failure_kind") == "invalid_tool_outcome":
            entry["runner_failure_kind"] = metadata.get("failure_kind")
            entry["failure_kind"] = attempt["failure_kind"]
        produced = metadata.get("produced_files", [])
        entry["produced_files"] = []
        if not isinstance(produced, list):
            entry["metadata_error"] = "invalid produced-file inventory"
        else:
            unresolved = []
            for name in produced:
                check_deadline(deadline_monotonic, clock, "tool-run audit")
                try:
                    if not isinstance(name, str):
                        raise BrokenReferenceError("produced filename must be text")
                    path = _relative_path(output_dir / "runs" / attempt["run_id"], name)
                    if not path.is_file():
                        raise BrokenReferenceError("produced file is absent")
                except BrokenReferenceError:
                    unresolved.append(name)
                else:
                    entry["produced_files"].append(path.relative_to(output_dir).as_posix())
            if unresolved:
                entry["metadata_error"] = "unresolved produced-file inventory"
                entry["unresolved_inventory_json"] = json.dumps(unresolved, ensure_ascii=False)
        validate_references(output_dir, entry, deadline_monotonic, clock)
        runs.append(entry)
    return runs


def build_issue_resolutions(
    output_dir: Path,
    tool_runs: list[dict],
    validations: list[dict],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Close saved repair attempts only against a subsequently executed run."""
    issues = []
    repair_dir = output_dir / "repairs"
    repair_paths = () if not repair_dir.is_dir() else (
        path for path in iter_paths_with_deadline(
            repair_dir, deadline_monotonic, clock, "repair audit",
        ) if path.parent == repair_dir and path.match("repair_*.json")
    )
    for path in repair_paths:
        check_deadline(deadline_monotonic, clock, "repair audit")
        reference = path.relative_to(output_dir).as_posix()
        validate_references(output_dir, {"path": reference}, deadline_monotonic, clock)
        record = json.loads(_read_text(path, deadline_monotonic, clock))
        verification = {"status": "not_run", "passed": None}
        fix = record.get("fix")
        if fix:
            # A repeated candidate can have the same hash as an earlier failed
            # run; it must never borrow that earlier run's verification.
            run_id = record["candidate_run"]
            attempt = next((run for run in tool_runs if run["run_id"] == run_id), None)
            if (attempt and _sha256(
                    output_dir / attempt["dofile_file"], deadline_monotonic, clock,
            ) == fix["dofile_hash"]):
                validation = next((item for item in validations if item["run_id"] == run_id), None)
                verification = {"status": "validated" if validation else "not_validated", "passed": None,
                                "log_file": attempt["log_file"], "metadata_file": attempt["metadata_file"]}
                if validation:
                    verification.update(validation)
        issues.append({**record, "record_file": reference, "verify": verification})
    return issues


def verify_final_manifest(
    output_dir: Path,
    manifest: FinalManifest,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Recheck every promoted byte immediately before successful audit writing."""
    final = _contained(output_dir, output_dir / "final_results")
    actual = {
        path.relative_to(output_dir).as_posix()
        for path in iter_paths_with_deadline(
            final, deadline_monotonic, clock, "final-manifest audit",
        )
        if path.is_file()
    }
    if actual != set(manifest.hashes):
        raise ArtifactIntegrityError("final artifact inventory differs from promotion manifest")
    records = []
    for name, digest in manifest.hashes.items():
        check_deadline(deadline_monotonic, clock, "final-manifest audit")
        relative = Path(name).relative_to("final_results")
        if relative == Path("final.log"):
            source = Path("runs") / manifest.run_id / f"{manifest.run_id}.log"
        elif relative == Path("deliverables/final.dofile"):
            source = Path("runs") / manifest.run_id / "deliverables" / f"{manifest.run_id}.dofile"
        else:
            source = Path("runs") / manifest.run_id / relative
        validate_references(output_dir, {"path": name, "source": source.as_posix()}, deadline_monotonic, clock)
        if (_sha256(output_dir / name, deadline_monotonic, clock) != digest
                or _sha256(output_dir / source, deadline_monotonic, clock) != digest):
            raise ArtifactIntegrityError(f"final artifact differs from selected run: {name}")
        records.append({"path": name, "source": source.as_posix(), "sha256": digest})
    return records
