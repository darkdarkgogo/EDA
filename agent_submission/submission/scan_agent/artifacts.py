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
    if (previous_id != "original" and not re.fullmatch(r"R[1-9]\d*", previous_id)) or not re.fullmatch(r"R[1-9]\d*", current_id):
        raise ValueError("diff IDs must be original or one-based Rn identifiers")
    diff = "".join(difflib.unified_diff(previous.splitlines(keepends=True), current.splitlines(keepends=True), fromfile=f"{previous_id}.dofile", tofile=f"{current_id}.dofile"))
    name = f"dofile_{previous_id}_to_{current_id}.diff" if previous_id == "original" else f"{previous_id}_to_{current_id}.diff"
    path = _contained(output_dir, output_dir / "diffs" / name)
    return _atomic_text(path, diff)


def write_original_dofile(output_dir: Path, content: str) -> Path:
    """Keep the supplied task-two script so every initial fix has a real source."""
    return _atomic_text(_contained(output_dir, output_dir / "candidates" / "original.dofile"), content)


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
    final_dofile: Path | None = None,
) -> list[dict]:
    """Map typed requirements to their latest deterministic report checks."""
    checks = validations[-1].get("requirement_checks", []) if validations else []
    lines = final_dofile.read_text(encoding="utf-8").splitlines() if final_dofile and final_dofile.is_file() else []
    commands = {
        "top_module": ("present_design",), "netlists": ("load_netlist",),
        "libraries": ("load_lib",), "ctl_files": ("load_ctl",),
        "clocks": ("set_scan_signal -type clock", "set_scan_signal"),
        "resets": ("set_scan_signal -type reset", "set_scan_signal"),
        "constants": ("set_scan_signal -type constant", "set_scan_signal"),
        "scan_enables": ("set_scan_signal -type scan_enable", "set_scan_signal"),
        "chain_constraints": ("set_scan_cfg",), "partitions": ("add_scan_partition", "set_scan_partition"),
        "clock_domains": ("set_scan_cfg", "set_scan_partition"),
        "edge_policy": ("set_scan_cfg",), "lockup": ("set_scan_cfg",),
        "scan_segments": ("set_scan_segment",), "wrapper_settings": ("set_wrapper",),
        "allowed_drc": ("examine_scan_drc",),
        "required_outputs": ("dump_netlist", "rpt_scan"),
    }
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
        match = next(((number, line.strip()) for token in commands.get(name, ())
                      for number, line in enumerate(lines, 1) if value not in ([], {}, None)
                      and not line.lstrip().startswith("#") and token in " ".join(line.split())), None)
        config_ref = ({"source": "final_results/deliverables/final.dofile", "locator": f"L{match[0]}"}
                      if match else None)
        records.append({"field": name, "requirement_type": name,
                        "requested_json": json.dumps(value, ensure_ascii=False, allow_nan=False),
                        "observed_json": json.dumps(observed, ensure_ascii=False, allow_nan=False),
                        "status": status, "reason": reason,
                        "requirement": f"{name}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}",
                        "dft_config": match[1] if match else "无对应 Dofile 命令",
                        "config_ref": config_ref,
                        "requirement_ref": {"source": "requirements.json", "locator": name},
                        "configuration": configurations, "evidence": evidence})
    return records


def build_formal_requirement_mapping(requirements: Mapping[str, object], final_dofile: Path) -> list[dict]:
    """Emit only requirements backed by an exact line of the executed Dofile."""
    if not final_dofile.is_file():
        return []
    lines = final_dofile.read_text(encoding="utf-8").splitlines()
    commands = {
        "top_module": "present_design", "netlists": "load_netlist", "libraries": "load_lib",
        "ctl_files": "load_ctl", "clocks": "set_scan_signal", "resets": "set_scan_signal",
        "constants": "set_scan_signal", "scan_enables": "set_scan_signal",
        "chain_constraints": "set_scan_cfg", "partitions": "add_scan_partition",
        "clock_domains": "set_scan_cfg", "edge_policy": "set_scan_cfg",
        "lockup": "set_scan_cfg", "scan_segments": "set_scan_segment",
        "wrapper_settings": "set_wrapper", "allowed_drc": "set_scan_drc_rule_handling",
        "required_outputs": "dump_netlist",
    }
    signal_types = {"clocks": "clock", "resets": "reset", "constants": "constant",
                    "scan_enables": "scan_enable"}
    mapping = []
    def add_entry(requirement: str, number: int, line: str, locator: str | None = None) -> None:
        entry = {"requirement": requirement, "dft_config": line,
                 "config_ref": {"source": "final_results/deliverables/final.dofile", "locator": f"L{number}"}}
        if locator is not None:
            entry["requirement_ref"] = {"source": "requirements.json", "locator": locator}
        mapping.append(entry)

    for field, value in requirements.items():
        command = commands.get(field)
        if command is None or value in ([], {}, None, False):
            continue
        if field == "chain_constraints" and isinstance(value, dict):
            for key, expected in value.items():
                option = key if key in {"chain_count", "max_length", "max_chain_count"} else "chain_count"
                candidates = [(number, line.strip()) for number, line in enumerate(lines, 1)
                              if line.strip().startswith("set_scan_cfg ") and f"-{option} {expected}" in " ".join(line.split())]
                if key not in {"chain_count", "max_length", "max_chain_count"}:
                    candidates = [(number, line) for number, line in candidates
                                  if any(previous.strip() == f"set_current_scan_partition {key}"
                                         for previous in lines[max(0, number - 4):number - 1])]
                if candidates:
                    number, line = candidates[0]
                    add_entry(f"{field}.{key}: {expected}", number, line, f"{field}.{key}")
            continue
        if field == "lockup" and isinstance(value, dict):
            for key, expected in value.items():
                match = next(((number, line.strip()) for number, line in enumerate(lines, 1)
                              if line.strip().startswith("set_scan_cfg ")
                              and f"-{key} {str(expected).lower()}" in " ".join(line.split())), None)
                if match:
                    add_entry(f"{field}.{key}: {expected}", match[0], match[1], f"{field}.{key}")
            continue
        if field == "edge_policy":
            expected = "-mix_edges true" if value == "mixed" else "-mix_edges false"
            match = next(((number, line.strip()) for number, line in enumerate(lines, 1)
                          if line.strip().startswith("set_scan_cfg ") and expected in " ".join(line.split())), None)
            if match:
                add_entry(f"{field}: {value}", match[0], match[1], field)
            continue
        items = value if isinstance(value, list) else [value]
        for index, item in enumerate(items):
            detail = item.get("port") or item.get("name") if isinstance(item, dict) else item
            matches = []
            for number, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or command not in stripped:
                    continue
                if field in signal_types and f"-type {signal_types[field]}" not in " ".join(stripped.split()):
                    continue
                if field in {"top_module", "clocks", "resets", "constants", "scan_enables", "partitions", "netlists", "libraries", "ctl_files", "allowed_drc"}:
                    if detail and str(detail) not in stripped:
                        continue
                if field == "clock_domains" and isinstance(item, dict):
                    if str(item.get("clock", "")) not in stripped:
                        continue
                if field == "required_outputs" and str(item) not in stripped:
                    continue
                matches.append((number, stripped))
            if not matches:
                continue
            number, line = matches[0]
            locator = f"{field}[{index}]" if isinstance(value, list) else field
            add_entry(f"{field}: {json.dumps(item, ensure_ascii=False, allow_nan=False)}", number, line, locator)
    if requirements.get("task_type") == "task1":
        preprocessing = {
            "insert_dft_logic -connect_icg_only": "按任务说明重连 ICG 测试控制信号",
            "insert_dft_logic -replace_only": "按任务说明将普通触发器替换为扫描触发器",
            "insert_dft_logic -replace_unscan": "按任务说明回替不应接入扫描链的触发器",
        }
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("set_scan_cell_mapping "):
                requirement = "按任务说明和工艺库配置触发器扫描映射"
            else:
                requirement = next((description for command, description in preprocessing.items()
                                    if stripped.startswith(command)), None)
            if requirement:
                mapping.append({"requirement": requirement, "dft_config": stripped,
                                "config_ref": {"source": "final_results/deliverables/final.dofile", "locator": f"L{number}"}})
    return mapping


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
        entry["tool_call_id"] = attempt["run_id"]
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
        entry["exit_status"] = ("aborted" if entry.get("timed_out") or entry.get("failure_kind") in {"timeout", "tool_timeout", "tool_killed"}
                                else "completed" if entry.get("exit_code") == 0 and not entry.get("failure_kind")
                                else "error")
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
    file_changes: list[dict] | None = None,
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
        found = dict(record.get("found") or {})
        source = found.get("source", "")
        source_run = re.match(r"runs/(R[1-9]\d*)/", source)
        found.setdefault("run_ref", source_run.group(1) if source_run else None)
        found.setdefault("excerpt", "; ".join(found.get("failures", [])) or str(found.get("locator", "static diagnosis")))
        diagnosis = dict(record.get("diagnosis") or {})
        diagnosis.setdefault("summary", "分析先前验证结果并修正候选 Dofile")
        diagnosis.setdefault("located_object", diagnosis.get("problem_type", "Dofile"))
        diagnosis.setdefault("root_cause", "未能完成前一次工具验证")
        diagnosis.setdefault("violated_requirement", "")
        candidate_run = record.get("candidate_run")
        change = next((item for item in (file_changes or []) if item.get("path") ==
                       f"runs/{candidate_run}/deliverables/{candidate_run}.dofile"), None)
        evidence = {"run_ref": candidate_run if verification.get("status") != "not_run" else None,
                    "source": verification.get("log_file"),
                    "resolved": False}
        if verification.get("passed") and evidence["source"]:
            positive = None
            with (output_dir / evidence["source"]).open("r", encoding="utf-8", errors="replace") as log:
                for number, line in enumerate(log, 1):
                    if number % 10000 == 0:
                        check_deadline(deadline_monotonic, clock, "repair verification audit")
                    if ("Write Netlist successfully" in line or "Thank you" in line
                            or "insert_dft_logic completed successfully" in line):
                        positive = (number, line.rstrip("\r\n"))
                        break
            if positive:
                evidence.update(resolved=True, locator=f"L{positive[0]}", excerpt=positive[1])
        attempts = []
        if fix:
            attempts.append({"fix": {"action": str(fix.get("summary", "修正候选 Dofile")),
                                     "artifact_ref": [change["change_id"]] if change else []},
                             "verify": evidence})
        issues.append({**record, "record_file": reference, "verify": verification,
                       "found": found, "diagnosis": diagnosis,
                       "issue_id": f"I{len(issues) + 1}",
                       "phenomenon": found["excerpt"], "attempts": attempts})
    return issues


def build_initial_task2_issues(
    output_dir: Path,
    input_dir: Path,
    tool_runs: list[dict],
    validations: list[dict],
    file_changes: list[dict],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Describe task-two fixes from the executed original and real evidence."""
    original_path = output_dir / "candidates" / "original.dofile"
    if not original_path.is_file() or not file_changes:
        return []
    original = _read_text(original_path, deadline_monotonic, clock).splitlines()
    original_attempt = next((entry for entry in tool_runs if entry.get("role") == "original"), None)
    if original_attempt is None:
        return []
    run = next((entry for entry in reversed(tool_runs) if entry.get("role") != "original"), None)
    actual_lines = _read_text(output_dir / run["dofile_file"], deadline_monotonic, clock).splitlines() if run else []
    original_errors: list[tuple[int, str]] = []
    with (output_dir / original_attempt["log_file"]).open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if number % 10000 == 0:
                check_deadline(deadline_monotonic, clock, "original-run log audit")
            if len(original_errors) < 1024 and ("[ERROR]" in line or "[WARNING]" in line
                                                or "DFTR" in line or "DFTDRC" in line):
                original_errors.append((number, line.rstrip("\r\n")))
    def command_key(line: str) -> str | None:
        normalized = " ".join(line.split())
        if not normalized or normalized.startswith("#"):
            return None
        command = normalized.split(" ", 1)[0]
        if command == "set_scan_signal":
            signal = re.search(r"-type\s+(\w+)", normalized)
            return f"{command}:{signal.group(1)}" if signal else command
        if command in {"load_lib", "load_netlist", "present_design", "set_scan_cell_mapping",
                       "set_scan_cfg", "set_scan_drc_rule_handling", "set_scan_element",
                       "set_scan_segment", "set_wrapper_cfg", "add_scan_partition",
                       "set_scan_partition", "set_current_scan_partition", "insert_dft_logic",
                       "examine_scan_drc", "examine_scan_chain"}:
            return command
        return None

    original_commands = [(number, line, command_key(line)) for number, line in enumerate(original, 1)]
    corrected_commands = [(number, line, command_key(line)) for number, line in enumerate(actual_lines, 1)]
    changed_lines: list[tuple[int, str]] = []
    seen_keys: dict[str, int] = {}
    for number, line, key in original_commands:
        if key is None:
            continue
        occurrence = seen_keys.get(key, 0)
        seen_keys[key] = occurrence + 1
        candidates = [item for item in corrected_commands if item[2] == key]
        if occurrence >= len(candidates):
            changed_lines.append((number, line))
            continue
        corrected = candidates[occurrence][1]
        if key in {"examine_scan_drc", "examine_scan_chain"}:
            # Report destination syntax is not itself a scan insertion issue.
            differs = False
        elif key in {"load_lib", "load_netlist"}:
            before = re.findall(r"[\w.-]+\.(?:lib|v|ctl)\b", line)
            after = re.findall(r"[\w.-]+\.(?:lib|v|ctl)\b", corrected)
            differs = before != after
        else:
            differs = " ".join(line.split()) != " ".join(corrected.split())
        if differs:
            changed_lines.append((number, line))
    for key in sorted({item[2] for item in corrected_commands if item[2]}):
        before = [item for item in original_commands if item[2] == key]
        after = [item for item in corrected_commands if item[2] == key]
        if len(after) > len(before):
            anchor = next(((number, line) for number, line, command in original_commands
                           if command in {"present_design", "insert_dft_logic"}), None)
            if anchor is not None:
                changed_lines.append(anchor)
    original_order = [item[2] for item in original_commands if item[2] in {"examine_scan_drc", "insert_dft_logic"}]
    corrected_order = [item[2] for item in corrected_commands if item[2] in {"examine_scan_drc", "insert_dft_logic"}]
    if original_order != corrected_order:
        changed_lines.extend((number, line) for number, line, key in original_commands if key == "insert_dft_logic")
    changed_lines = sorted(dict.fromkeys(changed_lines), key=lambda item: item[0])[:16]
    first_error = next(((number, line) for number, line in original_errors if "[ERROR]" in line), None)
    if first_error is not None:
        original_errors = [first_error]
    declared: list[dict] = []
    used_lines: set[int] = set()
    seen_errors: set[str] = set()
    for log_line, message in original_errors:
        if len(declared) >= 12:
            break
        code = re.search(r"(?:COM|CMD|SCAN|DFTDRC)-\d+|DFTR\d+", message)
        key = code.group(0) if code else message
        if key in seen_errors:
            continue
        seen_errors.add(key)
        command = next((name for name in (
            "load_netlist", "present_design", "set_scan_cell_mapping", "set_scan_signal",
            "set_scan_cfg", "examine_scan_drc", "examine_scan_chain", "insert_dft_logic",
            "dump_netlist", "load_lib", "set_scan_element", "set_scan_drc_rule_handling",
        ) if name in message), None)
        changed = next(((number, line) for number, line in changed_lines
                        if number not in used_lines and command and command in line), None)
        if changed is None:
            changed = next(((number, line) for number, line in changed_lines
                            if number not in used_lines), None)
        if changed is None:
            changed = next(((number, line) for number, line in enumerate(original, 1)
                            if command and command in line and not line.lstrip().startswith("#")), None)
        if changed is None:
            changed = (1, original[0] if original else "")
        used_lines.add(changed[0])
        missing_netlist = ("present_design" in changed[1]
                           and not any(key == "load_netlist" for _, _, key in original_commands)
                           and any(key == "load_netlist" for _, _, key in corrected_commands))
        root_cause = ("原始 Dofile 在 present_design 前没有 load_netlist，工具中没有可用设计"
                      if missing_netlist else f"R1 中的配置导致工具报错：{message}")
        declared.append({"对象": changed[1], "现象": message, "根因": root_cause,
                         "line_number": changed[0], "log_line": log_line})
    for number, line in changed_lines:
        if len(declared) >= 16:
            break
        if number in used_lines:
            continue
        key = command_key(line)
        corrected = next((candidate for _, candidate, candidate_key in corrected_commands
                          if candidate_key == key), None)
        if key == "insert_dft_logic" and original_order != corrected_order:
            root_cause = "原始 Dofile 在 examine_scan_drc 之前执行 insert_dft_logic，工具阶段顺序错误"
        elif key == "present_design" and not any(item[2] == "load_netlist" for item in original_commands):
            root_cause = "原始 Dofile 缺少 load_netlist，present_design 无法取得设计"
        else:
            root_cause = f"原始配置 {line.strip()} 与修正后配置 {corrected.strip() if corrected else '缺失'} 不同"
        declared.append({"对象": line, "现象": f"R1 执行的原始配置需要修正：{line.strip()}",
                         "根因": root_cause, "line_number": number})
    if not declared:
        return []
    requirements_path = output_dir / "requirements.json"
    saved_requirements = json.loads(_read_text(requirements_path, deadline_monotonic, clock)) if requirements_path.is_file() else {}
    allowed_drc = set(saved_requirements.get("allowed_drc", [])) if isinstance(saved_requirements, dict) else set()
    validation = next((item for item in validations if run and item.get("run_id") == run["run_id"]), None)
    change = file_changes[0]
    command_markers = []
    netlist_marker = None
    if run:
        with (output_dir / run["log_file"]).open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if number % 10000 == 0:
                    check_deadline(deadline_monotonic, clock, "task-two log audit")
                if "CMD-0034" in line:
                    command_markers.append((number, line.rstrip("\r\n")))
                if "Write Netlist successfully" in line:
                    netlist_marker = (number, line.rstrip("\r\n"))
    report_cache: dict[str, list[str]] = {}
    issues = []
    for index, item in enumerate(declared, 1):
        check_deadline(deadline_monotonic, clock, "task-two issue audit")
        if not isinstance(item, dict):
            continue
        obj = str(item.get("对象", ""))
        hint = obj.lower()
        criterion = str(item.get("修复成功判据", ""))
        port_hints = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*", obj)
                      if "_" in word and not word.startswith(("set_", "add_", "original"))
                      and word not in {"scan_enable", "scan_clock", "chain_count", "off_state", "active_state"}
                      and any(word in line for line in original)]
        port_hint = port_hints[0] if port_hints else None
        if "load_netlist" in hint:
            token = "present_design" if not any("load_netlist" in line for line in original) else "load_netlist"
        elif "scan_enable" in hint:
            token = "-type scan_enable"
        elif "clock" in hint and "set_scan_signal" in hint:
            token = "-type clock"
        elif "reset" in hint and "set_scan_signal" in hint:
            token = "-type reset"
        elif "JTAG" in obj.upper() and "扫描" in obj:
            token = "set_scan_element"
        elif "ICG" in obj.upper() and "TIE" in obj.upper():
            token = "set_scan_drc_rule_handling"
        elif "扫描使能" in obj:
            token = "-type scan_enable"
        elif "扫描链" in obj or "SI/SO" in criterion:
            token = "set_scan_cfg"
        elif "报告" in obj and "post_scan" in criterion:
            token = "dump_netlist"
        elif "时钟" in obj and "复位" in obj:
            token = "set_scan_signal"
        else:
            token = next((name for name in ("set_scan_drc_rule_handling", "set_scan_drc_cfg",
                     "set_scan_cell_mapping", "set_scan_element", "set_wrapper_cfg",
                     "set_scan_cfg", "set_scan_signal",
                     "add_scan_partition", "insert_dft_logic",
                     "examine_scan_drc", "examine_scan_chain", "dump_netlist", "load_lib", "present_design")
                     if name in hint), "")
        line_number = item.get("line_number")
        if not isinstance(line_number, int) or not 1 <= line_number <= len(original):
            line_number = next((number for number, line in enumerate(original, 1)
                                if token and token in line and (not port_hint or port_hint in line)
                                and not line.lstrip().startswith("#")), 1)
        excerpt = original[line_number - 1] if original else "原始 Dofile 为空"
        found = {"run_ref": original_attempt["run_id"],
                 "source": original_attempt["dofile_file"],
                 "locator": f"L{line_number}", "excerpt": excerpt,
                 "detection": "executed_original_dofile"}
        if isinstance(item.get("log_line"), int):
            found.update(source=original_attempt["log_file"], locator=f"L{item['log_line']}",
                         excerpt=str(item["现象"]), detection="tool_log")
        issue_type = str(item.get("类型", ""))
        report = ("scan_partition.rpt" if "add_scan_partition" in hint
                  else "scan_cfg.rpt" if "chain_count" in hint or "扫描链条数" in obj or "max_length" in criterion
                  else "drc.rpt" if "DRC" in issue_type.upper() else None)
        evidence_source = run["log_file"] if run else None
        evidence_line = None
        target_rules = set(re.findall(r"DFTR\d+", str(item.get("现象", "")))) - allowed_drc
        target_rules_absent = None
        remaining_rules_allowed = None
        direct_evidence = False
        removed_configuration = False
        if run:
            report_path = output_dir / "runs" / run["run_id"] / "reports" / report if report else None
            if report_path and report_path.is_file():
                evidence_source = report_path.relative_to(output_dir).as_posix()
                if report not in report_cache:
                    report_cache[report] = _read_text(report_path, deadline_monotonic, clock).splitlines()
                report_lines = report_cache[report]
                if report == "drc.rpt" and target_rules:
                    target_rules_absent = all(not re.search(rf"\b{re.escape(rule)}\b", line)
                                              for rule in target_rules for line in report_lines)
                if report == "drc.rpt":
                    remaining_rules = set(re.findall(r"DRC rule '([^']+)' fails", "\n".join(report_lines)))
                    remaining_rules_allowed = remaining_rules <= allowed_drc
                evidence_line = next(((n, line) for n, line in enumerate(report_lines, 1)
                                      if (report == "drc.rpt" and "Total violations:" in line)
                                      or (report == "scan_cfg.rpt" and line.lstrip().startswith("max_length" if "max_length" in criterion else "chain_count"))
                                      or (report == "scan_partition.rpt" and port_hint and port_hint in line)), None)
                direct_evidence = evidence_line is not None
            if evidence_line is None:
                evidence_source = run["log_file"]
                lookup = "load_netlist" if "load_netlist" in hint else token
                evidence_line = next(((n, line) for n, line in command_markers
                                      if lookup and lookup in line and (not port_hint or port_hint in line)), None)
                direct_evidence = evidence_line is not None
                absence_required = any(word in criterion for word in ("不包含", "不再包含", "删除", "不存在"))
                removed_configuration = bool(absence_required and token and any(token in line for line in original)
                                             and not any(token in line for line in actual_lines))
                if evidence_line is None and removed_configuration:
                    evidence_line = netlist_marker
        resolved = bool(run and validation and validation.get("passed") and
                        run.get("exit_status") == "completed" and evidence_line
                        and (direct_evidence or removed_configuration))
        if target_rules_absent is not None:
            resolved = resolved and target_rules_absent
        if remaining_rules_allowed is not None:
            resolved = resolved and remaining_rules_allowed
        actual_token = "load_netlist" if "load_netlist" in hint else token
        configured = next((line.strip() for line in actual_lines
                           if actual_token and actual_token in line and (not port_hint or port_hint in line)), None)
        action = (f"修正原始 Dofile，最终执行配置：{configured}" if configured
                  else "按任务要求生成并执行修正后的 Dofile；具体变更见 F1 diff")
        verify = {"run_ref": run["run_id"] if run else None,
                  "source": evidence_source, "resolved": resolved}
        if removed_configuration:
            verify["configuration_check"] = {"source": "final_results/deliverables/final.dofile",
                                             "locator": "entire file", "absent_command": token, "passed": True}
        if target_rules_absent is not None:
            verify["target_rules_absent"] = target_rules_absent
            verify["target_rules"] = sorted(target_rules)
        if remaining_rules_allowed is not None:
            verify["remaining_rules_allowed"] = remaining_rules_allowed
        if evidence_line:
            verify.update(locator=f"L{evidence_line[0]}", excerpt=evidence_line[1])
        issues.append({
            "issue_id": f"I{index}",
            "phenomenon": str(item.get("现象", "R1 原始 Dofile 需要修正")),
            "found": found,
            "diagnosis": {"summary": "检查 R1 真实运行日志、原始 Dofile 和任务要求",
                          "located_object": obj or excerpt,
                          "root_cause": str(item.get("根因", item.get("现象", "原始配置不符合要求"))),
                          "violated_requirement": str(item.get("类型", "")) if "配置" in str(item.get("类型", "")) else ""},
            "attempts": [{"fix": {"action": action,
                                   "artifact_ref": [entry["change_id"] for entry in file_changes if "change_id" in entry]},
                          "verify": verify}],
        })
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
