"""Accept scan runs only from process, artifact, report, and input evidence."""

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path, PureWindowsPath
import re
import time
from collections.abc import Callable
from typing import Mapping

from .artifacts import RunPaths
from .deadline import check_deadline, iter_paths_with_deadline
from .diagnostics import DiagnosticSummary, DrcViolation, ReportFact, parse_tool_log
from .inputs import InputMutationError, assert_inputs_unchanged
from .runner import ToolResult


@dataclass(frozen=True)
class ValidationEvidence:
    source: str
    line_number: int
    source_line: str


@dataclass(frozen=True)
class ValidationReport:
    failures: tuple[str, ...]
    missing_artifacts: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    disallowed_drc: tuple[DrcViolation, ...]
    input_integrity: bool
    evidence: tuple[ValidationEvidence, ...]

    @property
    def passed(self) -> bool:
        """The sole acceptance decision for final-run selection."""
        return not any((self.failures, self.missing_artifacts, self.missing_evidence, self.disallowed_drc)) and self.input_integrity


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError("requirements and chain_constraints must be mappings or dataclass records")


def _read_text(
    path: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
) -> str:
    chunks = []
    with path.open("rb") as source:
        while True:
            check_deadline(deadline_monotonic, clock, "validation evidence")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    check_deadline(deadline_monotonic, clock, "validation evidence")
    return b"".join(chunks).decode("utf-8", errors="replace")


def _relative_artifact(directory: Path, name: str) -> Path | None:
    windows = PureWindowsPath(name)
    parts = name.replace("\\", "/").split("/")
    if not name or name.startswith(("/", "\\")) or Path(name).is_absolute() or windows.drive or ".." in parts:
        return None
    path = directory.joinpath(*parts)
    return path if path.resolve().is_relative_to(directory.resolve()) else None


def _allowed(rule: str, allowed: set[str]) -> bool:
    # Parent DFTR9 permits DFTR9-1; DFTR-TIE0 remains its own exact family.
    return rule in allowed or bool(re.fullmatch(r"DFTR\d+-\d+", rule) and rule.rsplit("-", 1)[0] in allowed)


def _current_drc(summary: DiagnosticSummary) -> tuple[DrcViolation, ...]:
    """Use the latest total-delimited snapshot, retaining any trailing findings."""
    start = summary.drc_totals[-2].line_number if len(summary.drc_totals) > 1 else 0
    return tuple(item for item in summary.drc_violations if item.line_number > start)


def _check_drc(summary: DiagnosticSummary, source: str, allowed: set[str], failures: list[str], disallowed: list[DrcViolation]) -> None:
    violations = _current_drc(summary)
    disallowed.extend(item for item in violations if item.count != 0 and not _allowed(item.rule, allowed))
    if any(item.count != 0 and not _allowed(item.rule, allowed) for item in violations):
        failures.append(f"disallowed DRC in {source}")
    if any(item.count is None for item in violations):
        failures.append(f"unknown DRC count in {source}")
    if summary.total_violations is not None:
        total = summary.total_violations
        final_line = summary.drc_totals[-1].line_number
        counted = [item for item in violations if item.line_number < final_line]
        # A rule summary accounts for its individual object messages once.
        # Retain both as evidence and reject summaries smaller than the records.
        known = 0
        for rule in sorted({item.rule for item in counted}):
            records = [item for item in counted if item.rule == rule and item.count is not None]
            explicit = [item for item in records if item.count_is_explicit]
            occurrences = sum(item.count for item in records if not item.count_is_explicit)
            subtotal = sum(item.count for item in explicit) if explicit else occurrences
            if explicit and occurrences > subtotal:
                failures.append(f"DRC summary undercounts {rule} in {source}")
            known += subtotal
        if total != known and (total != 0 or counted):
            failures.append(f"unaccounted or inconsistent DRC total in {source}: total={total}, counted={known}")
        if any(item.line_number > final_line and item.count != 0 for item in violations):
            failures.append(f"DRC findings follow the final total in {source}")


def _current_chain_snapshot(facts: tuple[ReportFact, ...]) -> tuple[ReportFact, ...]:
    """A new count header begins a fresh snapshot within one report file."""
    start = max((fact.line_number for fact in facts if fact.name == "chain_count"), default=0)
    return tuple(fact for fact in facts if fact.line_number >= start)


def _complete_chain_lengths(facts: tuple[ReportFact, ...]) -> bool:
    counts = [fact.value for fact in facts if fact.name == "chain_count"]
    rows = [fact for fact in facts if fact.name == "chain_length"]
    identities = {fact.chain_name for fact in rows}
    return len(counts) == 1 and counts[0] > 0 and None not in identities and len(identities) == counts[0]


def _check_chains(constraints: Mapping[str, object], snapshots: list[tuple[ReportFact, ...]], failures: list[str], missing: list[str]) -> None:
    facts = [fact for snapshot in snapshots for fact in snapshot]
    supported = {"chain_count", "min_chain_count", "max_chain_count", "max_length"}
    for name, expected in constraints.items():
        if name not in supported:
            failures.append(f"unsupported chain constraint: {name}")
            continue
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            failures.append(f"invalid chain constraint: {name}")
            continue
        fact_names = {"max_length", "chain_length"} if name == "max_length" else {"chain_count"}
        observed = [fact.value for fact in facts if fact.name in fact_names]
        if name == "max_length" and observed and not any(fact.name == "max_length" for fact in facts):
            if not any(_complete_chain_lengths(snapshot) for snapshot in snapshots):
                missing.append("complete_chain_lengths")
                failures.append("missing complete chain-length report evidence")
        if not observed:
            missing.append(name)
            failures.append(f"missing report evidence for {name}")
        elif name == "chain_count" and any(value != expected for value in observed):
            failures.append(f"chain_count must equal {expected}; report values={observed}")
        elif name == "min_chain_count" and any(value < expected for value in observed):
            failures.append(f"chain_count below {expected}; report values={observed}")
        elif name in {"max_chain_count", "max_length"} and any(value > expected for value in observed):
            failures.append(f"{name} exceeds {expected}; report values={observed}")


def validate_run(
    requirements: object,
    tool_result: ToolResult,
    diagnostics: DiagnosticSummary,
    run_paths: RunPaths,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ValidationReport:
    """Validate facts without mutating inputs or publishing final artifacts.

    Requirements need ``required_outputs``, ``allowed_drc``, ``input_dir`` and
    the pre-run ``protected_hashes``. Optional ``chain_constraints`` supports
    chain_count, min_chain_count, max_chain_count and max_length. Chain evidence
    must come from a real report beneath RunPaths.reports or a required report
    artifact. Unsupported or unrecognized constraints fail closed. The log
    summary must match the log. Evidence sources are output-relative paths.
    """
    failures, missing_artifacts, missing_evidence, disallowed, evidence = [], [], [], [], []
    check_deadline(deadline_monotonic, clock, "run validation")
    try:
        required = _mapping(requirements)
    except TypeError as error:
        return ValidationReport((str(error),), (), ("requirements",), (), False, ())
    if tool_result.exit_code != 0:
        failures.append(f"tool exit code is {tool_result.exit_code}")
    if tool_result.timed_out:
        failures.append("tool timed out")
    if tool_result.failure_kind is not None:
        failures.append(f"tool failure: {tool_result.failure_kind}")

    allowed_value = required.get("allowed_drc")
    if not isinstance(allowed_value, (list, tuple)) or any(not isinstance(rule, str) or not re.fullmatch(r"DFTR(?:-TIE\d+|\d+)(?:-\d+)?", rule, re.IGNORECASE) for rule in allowed_value):
        failures.append("allowed_drc must explicitly list valid DFTR rules")
        allowed = set()
    else:
        allowed = {rule.upper() for rule in allowed_value}

    outputs = required.get("required_outputs")
    required_reports = set()
    if not isinstance(outputs, (list, tuple)) or not outputs or any(not isinstance(name, str) for name in outputs):
        failures.append("required_outputs must list required artifact names")
        missing_evidence.append("required_outputs")
    else:
        for name in outputs:
            check_deadline(deadline_monotonic, clock, "run validation")
            candidates = [_relative_artifact(directory, name) for directory in (run_paths.deliverables, run_paths.reports)]
            try:
                existing = [path for path in candidates if path is not None and path.resolve().is_relative_to(run_paths.root.resolve()) and path.is_file() and path.stat().st_size > 0]
                exists = bool(existing)
                required_reports.update(path for path in existing if path.suffix.lower() in {".rpt", ".txt", ".log"})
            except OSError:
                exists = False
            if not exists:
                missing_artifacts.append(name)
                failures.append(f"missing or empty required artifact: {name!r}")

    sources = []
    try:
        if tool_result.log_path.resolve() != run_paths.log.resolve() or not run_paths.log.resolve().is_relative_to(run_paths.root.resolve()) or not run_paths.log.is_file() or run_paths.log.stat().st_size == 0:
            raise OSError("missing, empty, or mismatched tool log")
        actual = parse_tool_log(_read_text(run_paths.log, deadline_monotonic, clock))
        if actual != diagnostics:
            failures.append("supplied diagnostics differ from real tool log")
        source = (Path("runs") / run_paths.run_id / run_paths.log.relative_to(run_paths.root)).as_posix()
        sources.append((source, actual))
    except OSError as error:
        failures.append(str(error))
        missing_evidence.append("tool_log")

    report_snapshots = []
    try:
        report_paths = {
            path for path in iter_paths_with_deadline(
                run_paths.reports, deadline_monotonic, clock, "validation report traversal",
            ) if path.is_file()
        } | required_reports
        for path in sorted(report_paths):
            check_deadline(deadline_monotonic, clock, "run validation")
            if not path.is_file():
                continue
            source = (Path("runs") / run_paths.run_id / path.relative_to(run_paths.root)).as_posix()
            if not path.resolve().is_relative_to(run_paths.root.resolve()):
                failures.append(f"report path escapes run: {source}")
                continue
            summary = parse_tool_log(_read_text(path, deadline_monotonic, clock))
            if path in required_reports and "drc" in path.stem.lower() and not summary.drc_evidence:
                missing_evidence.append(source)
                failures.append(f"missing DRC evidence in required report: {source}")
            sources.append((source, summary))
            report_snapshots.append(_current_chain_snapshot(summary.chain_facts))
    except OSError as error:
        failures.append(f"cannot read report evidence: {error}")

    has_drc = False
    for source, summary in sources:
        for item in (*summary.messages, *summary.drc_evidence, *summary.chain_facts, *summary.insertion_facts):
            item_evidence = ValidationEvidence(source, item.line_number, item.source_line)
            if item_evidence not in evidence:
                evidence.append(item_evidence)
        if summary.fatal_errors:
            failures.append(f"fatal tool/report errors in {source}")
        if summary.drc_evidence:
            has_drc = True
            _check_drc(summary, source, allowed, failures, disallowed)
    if not has_drc:
        missing_evidence.append("drc")
        failures.append("missing explicit DRC evidence")
    if not any(summary.insertion_facts for _, summary in sources):
        missing_evidence.append("insertion_completion")
        failures.append("missing explicit scan-insertion completion evidence")
    positive_structure = [
        fact for snapshot in report_snapshots for fact in snapshot
        if fact.name == "chain_count" and fact.value > 0
    ]
    if not positive_structure:
        missing_evidence.append("positive_scan_structure")
        failures.append("missing positive post-insertion scan-structure evidence")
    try:
        constraints = _mapping(required.get("chain_constraints", {}))
        _check_chains(constraints, report_snapshots, failures, missing_evidence)
    except TypeError as error:
        failures.append(str(error))

    input_integrity = False
    input_dir, expected = required.get("input_dir"), required.get("protected_hashes")
    if not isinstance(input_dir, (str, Path)) or not isinstance(expected, Mapping) or not expected or any(not isinstance(name, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in expected.items()):
        missing_evidence.append("input_hashes")
        failures.append("missing or invalid protected input hash evidence")
    else:
        try:
            assert_inputs_unchanged(Path(input_dir), dict(expected), deadline_monotonic, clock)
            input_integrity = True
        except InputMutationError as error:
            failures.append(str(error))
    return ValidationReport(tuple(failures), tuple(missing_artifacts), tuple(missing_evidence), tuple(disallowed), input_integrity, tuple(evidence))
