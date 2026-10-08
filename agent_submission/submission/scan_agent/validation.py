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
from .reports import ChainObservation, ReportEvidence, SignalObservation, collect_report_evidence


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
    requirement_checks: tuple["RequirementCheck", ...] = ()

    @property
    def passed(self) -> bool:
        """The sole acceptance decision for final-run selection."""
        return (not any((self.failures, self.missing_artifacts, self.missing_evidence, self.disallowed_drc))
                and self.input_integrity and all(item.status == "pass" for item in self.requirement_checks))


@dataclass(frozen=True)
class RequirementCheck:
    field: str
    requested_json: object
    observed_json: object
    status: str
    reason: str
    evidence: tuple[dict[str, str], ...]


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


def _normal_scalar(value: object) -> object:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            return int(lowered)
        except ValueError:
            return lowered
    return value


def _location_ref(location) -> dict[str, str]:
    return {"path": location.source, "locator": f"line {location.line_number}: {location.source_line.strip()}"}


def _requirement_checks(requirements: Mapping[str, object], report: ReportEvidence) -> tuple[RequirementCheck, ...]:
    checks: list[RequirementCheck] = []
    for field, signal_type, value_key in (
        ("clocks", "clock", "off_state"), ("resets", "reset", "off_state"),
        ("scan_enables", "scan_enable", "off_state"), ("constants", "constant", "constant_value"),
    ):
        wanted = requirements.get(field, [])
        if not isinstance(wanted, (list, tuple)):
            continue
        for index, item in enumerate(wanted):
            if not isinstance(item, Mapping):
                continue
            port = item.get("port")
            matches = [row for row in report.signals if row.signal_type == signal_type and row.port == port]
            requested = dict(item)
            name = f"{field}[{index}]:{port}"
            if not matches:
                checks.append(RequirementCheck(name, requested, None, "unverified", "no matching signal row", ()))
                continue
            observed_rows = [{"signal_type": row.signal_type, "port": row.port, "off_state": row.off_state,
                              "constant_value": row.constant_value, "usage": row.usage, "view": row.view,
                              "internal_clocks": row.associated_internal}
                             for row in matches]
            refs = tuple(_location_ref(row.location) for row in matches)
            if len(matches) != 1:
                checks.append(RequirementCheck(name, requested, observed_rows, "fail", "duplicate matching signal rows", refs))
                continue
            row = matches[0]
            observed = observed_rows[0]
            comparable = {value_key: row.off_state if value_key == "off_state" else row.constant_value}
            if field == "clocks" and "internal_clocks" in item:
                comparable["internal_clocks"] = row.associated_internal
            for optional in ("usage", "view"):
                if optional in item:
                    comparable[optional] = getattr(row, optional)
            expected = {key: _normal_scalar(value) for key, value in {key: item[key] for key in comparable if key in item}.items()}
            actual = {key: _normal_scalar(value) for key, value in comparable.items() if key in expected}
            passed = bool(expected) and expected == actual
            checks.append(RequirementCheck(name, requested, observed, "pass" if passed else "fail",
                "matched report row" if passed else f"observed fields do not match: expected={expected}, observed={actual}", refs))

    configs = {}
    for row in report.config:
        configs.setdefault(row.name, []).append(row)
    for field in ("chain_constraints", "lockup", "wrapper_settings"):
        wanted = requirements.get(field, {})
        if not isinstance(wanted, Mapping):
            continue
        for key, expected_value in wanted.items():
            source_configs = report.wrapper_config if field == "wrapper_settings" else report.config
            rows = [row for row in source_configs if row.name == str(key).lower()]
            if field == "wrapper_settings" and key == "chain_length" and not rows:
                rows = [row for row in source_configs if row.name == "max_length"]
            name = f"{field}.{key}"
            if not rows:
                checks.append(RequirementCheck(name, expected_value, None, "unverified", "no matching configuration row", ()))
            elif len(rows) != 1:
                checks.append(RequirementCheck(name, expected_value, [row.value for row in rows], "fail", "duplicate configuration rows",
                                               tuple(_location_ref(row.location) for row in rows)))
            else:
                observed = _normal_scalar(rows[0].value)
                expected = _normal_scalar(expected_value)
                passed = observed == expected
                checks.append(RequirementCheck(name, expected_value, observed, "pass" if passed else "fail",
                    "matched configuration row" if passed else f"expected {expected!r}, observed {observed!r}",
                    (_location_ref(rows[0].location),)))

            if field == "wrapper_settings" and key == "chain_count":
                wrapper_rows = [row for row in report.chains if row.chain_class == "W"]
                count_passed = len(wrapper_rows) == expected_value
                checks.append(RequirementCheck(name + ".structure", expected_value, len(wrapper_rows),
                    "pass" if count_passed else "fail", "wrapper chain rows match the requested count" if count_passed else "wrapper chain row count differs",
                    tuple(_location_ref(row.location) for row in wrapper_rows)))
            if field == "wrapper_settings" and key == "chain_length":
                wrapper_rows = [row for row in report.chains if row.chain_class == "W"]
                lengths = [row.length for row in wrapper_rows]
                length_passed = bool(lengths) and all(length <= expected_value for length in lengths)
                checks.append(RequirementCheck(name + ".structure", expected_value, lengths,
                    "pass" if length_passed else "fail", "wrapper chain lengths satisfy the request" if length_passed else "wrapper chain lengths are absent or exceed the request",
                    tuple(_location_ref(row.location) for row in wrapper_rows)))

    constraints = requirements.get("chain_constraints", {})
    if isinstance(constraints, Mapping):
        chains = report.chains
        chain_names = [row.name for row in chains]
        configured_counts = configs.get("chain_count", [])
        complete = bool(chains) and len(set(chain_names)) == len(chain_names)
        if configured_counts:
            complete = complete and len(configured_counts) == 1 and _normal_scalar(configured_counts[0].value) == len(chains)
        for key, expected_value in constraints.items():
            name = f"chain_constraints.{key}"
            if key not in {"chain_count", "min_chain_count", "max_chain_count", "max_length"}:
                checks.append(RequirementCheck(name, expected_value, None, "fail", "unsupported chain constraint", ()))
                continue
            if key in {"chain_count", "min_chain_count", "max_chain_count"}:
                observed_value = len(chains) if complete else None
                observed = observed_value
                passed = observed_value is not None and (
                    observed_value == expected_value if key == "chain_count" else
                    observed_value >= expected_value if key == "min_chain_count" else observed_value <= expected_value)
                reason = "complete chain rows matched" if passed else "complete post-insertion chain rows are missing or violate the count"
            elif key == "max_length":
                observed = [row.length for row in chains]
                passed = complete and all(length <= expected_value for length in observed)
                reason = "all chain lengths satisfy the maximum" if passed else "chain lengths are missing or exceed the maximum"
            else:
                continue
            refs = tuple(_location_ref(row.location) for row in chains)
            checks.append(RequirementCheck(name, expected_value, observed, "pass" if passed else "unverified" if not chains else "fail", reason, refs))

    # Signal declarations must also agree with the post-insertion chain wiring.
    # A signal report alone cannot establish which clocks and enables the tool
    # actually connected to each generated chain.
    chains = report.chains
    for field, attribute in (("clocks", "clocks"), ("scan_enables", "scan_enable")):
        wanted = requirements.get(field, [])
        if not isinstance(wanted, (list, tuple)) or not wanted:
            continue
        expected = {item.get("port") for item in wanted if isinstance(item, Mapping)}
        observed = [list(row.clocks) if attribute == "clocks" else row.scan_enable for row in chains]
        if not chains or not expected:
            status, reason = "unverified", "post-insertion chain evidence is missing or requirement identities are unsupported"
        else:
            matched = all(
                bool(set(row.clocks).intersection(expected)) if attribute == "clocks"
                else row.scan_enable in expected
                for row in chains
            )
            status = "pass" if matched else "fail"
            reason = "all post-insertion chains use requested signals" if matched else "chain clock or scan-enable differs from the request"
        checks.append(RequirementCheck(f"{field}.chain_wiring", [dict(item) for item in wanted], observed,
            status, reason, tuple(_location_ref(row.location) for row in chains)))

    # These requirement families are accepted by the extraction schema, but
    # this report contract has no canonical identity/attribute mapping for
    # them yet. Emit an explicit required unverified check instead of silently
    # allowing a nonempty request to disappear from the acceptance decision.
    for field in ("clock_domains", "scan_segments"):
        wanted = requirements.get(field, [])
        if isinstance(wanted, (list, tuple)) and wanted:
            checks.append(RequirementCheck(field, [dict(item) if isinstance(item, Mapping) else item for item in wanted],
                None, "unverified", "no deterministic report mapping is defined for this requirement family", ()))
    edge_policy = requirements.get("edge_policy")
    if edge_policy is not None:
        rows = [row for row in report.config if row.name in {"edge_policy", "scan_edge_policy"}]
        if len(rows) == 1 and _normal_scalar(rows[0].value) == _normal_scalar(edge_policy):
            checks.append(RequirementCheck("edge_policy", edge_policy, rows[0].value, "pass", "matched configuration row",
                                           (_location_ref(rows[0].location),)))
        else:
            checks.append(RequirementCheck("edge_policy", edge_policy, [row.value for row in rows],
                "unverified" if not rows else "fail", "edge policy is absent or ambiguous in scan configuration report",
                tuple(_location_ref(row.location) for row in rows)))

    for index, item in enumerate(requirements.get("partitions", []) if isinstance(requirements.get("partitions", []), (list, tuple)) else []):
        if not isinstance(item, Mapping):
            continue
        name = f"partitions[{index}]:{item.get('name')}"
        matches = [row for row in report.partitions if row.name == item.get("name")]
        if not matches:
            checks.append(RequirementCheck(name, dict(item), None, "unverified", "no matching partition rows", ()))
            continue
        if len(matches) > 1 and (not all(row.member for row in matches)
                                 or len({row.member for row in matches}) != len(matches)):
            checks.append(RequirementCheck(name, dict(item), [row.name for row in matches], "fail", "duplicate partition evidence",
                                           tuple(_location_ref(row.location) for row in matches)))
            continue
        row = matches[0]
        members = {match.member for match in matches if match.member}
        observed = {"name": row.name, "include": sorted(members) if members else list(row.include),
                    "exclude": list(row.exclude), "clocks": sorted({clock for match in matches for clock in match.clocks}),
                    "rising_edge_clocks": sorted({clock for match in matches for clock in match.rising_edge_clocks}),
                    "falling_edge_clocks": sorted({clock for match in matches for clock in match.falling_edge_clocks})}
        if members:
            observed["members"] = sorted(members)
        passed = item.get("name") == row.name
        for key in ("include", "exclude", "clocks", "rising_edge_clocks", "falling_edge_clocks"):
            if key not in item:
                continue
            requested_values = set(item[key])
            actual_values = set(observed.get(key, []))
            if key == "include" and members:
                passed = passed and requested_values.issubset(actual_values)
            elif key == "exclude" and members:
                passed = passed and not requested_values.intersection(actual_values)
            else:
                passed = passed and requested_values == actual_values
        checks.append(RequirementCheck(name, dict(item), observed, "pass" if passed else "fail",
            "matched partition row" if passed else "partition membership or clock fields differ",
            tuple(_location_ref(match.location) for match in matches)))
    return tuple(checks)


def validate_run(
    requirements: object,
    tool_result: ToolResult,
    diagnostics: DiagnosticSummary,
    run_paths: RunPaths,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    report_evidence: ReportEvidence | None = None,
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

    if report_evidence is None:
        report_evidence = collect_report_evidence(run_paths, deadline_monotonic, clock)
    for issue in report_evidence.issues:
        if issue.reason == "report is missing" and issue.source.endswith("scan_partition.rpt") and not required.get("partitions"):
            continue
        if issue.reason == "report is missing" and issue.source.endswith("wrapper_cfg.rpt") and not required.get("wrapper_settings"):
            continue
        failures.append(f"malformed report evidence at {issue.source}:{issue.line_number}: {issue.reason}")
        missing_evidence.append(issue.source)
    for group, filename in (("signals", "scan_signal.rpt"), ("config", "scan_cfg.rpt"), ("chains", "scan_chain.rpt")):
        path = run_paths.reports / filename
        if not path.is_file() or path.stat().st_size == 0:
            missing_evidence.append(f"{group}_report")
            failures.append(f"missing required {group} report evidence")
        elif any(issue.source.endswith(filename) and issue.reason.startswith("missing ") for issue in report_evidence.issues):
            missing_evidence.append(f"{group}_report_table")
            failures.append(f"malformed required {group} report table")
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

    drc_report = run_paths.reports / "drc.rpt"
    if not drc_report.is_file() or drc_report.stat().st_size == 0:
        missing_evidence.append("drc_report")
        failures.append("missing explicit DRC report evidence")
    elif drc_report.resolve().is_relative_to(run_paths.root.resolve()):
        drc_text = _read_text(drc_report, deadline_monotonic, clock)
        drc_summary = parse_tool_log(drc_text)
        if not drc_summary.drc_evidence:
            missing_evidence.append("drc_report")
            failures.append("DRC report contains no explicit violation evidence")
        sources.append(((Path("runs") / run_paths.run_id / "reports/drc.rpt").as_posix(), drc_summary))

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
    positive_structure = [row for row in report_evidence.chains if row.length > 0]
    if not positive_structure:
        missing_evidence.append("positive_scan_structure")
        failures.append("missing positive post-insertion scan-structure evidence")
    try:
        constraints = _mapping(required.get("chain_constraints", {}))
        if not report_evidence.chains:
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
    for group in (report_evidence.signals, report_evidence.config, report_evidence.chains,
                  report_evidence.partitions, report_evidence.wrapper_config):
        for row in group:
            location = row.location
            item = ValidationEvidence(location.source, location.line_number, location.source_line)
            if item not in evidence:
                evidence.append(item)
    requirement_checks = _requirement_checks(required, report_evidence)
    chain_constraints = required.get("chain_constraints", {})
    if isinstance(chain_constraints, Mapping) and "max_length" in chain_constraints:
        names = [row.name for row in report_evidence.chains]
        configured = [row for row in report_evidence.config if row.name == "chain_count"]
        complete = bool(names) and len(set(names)) == len(names)
        if configured:
            complete = complete and len(configured) == 1 and _normal_scalar(configured[0].value) == len(names)
        if not complete:
            missing_evidence.append("complete_chain_lengths")
    for item in requirement_checks:
        if item.status != "pass":
            failures.append(f"requirement {item.field} {item.status}: {item.reason}")
            if item.status == "unverified":
                missing_evidence.append(item.field)
    return ValidationReport(tuple(failures), tuple(missing_artifacts), tuple(missing_evidence), tuple(disallowed),
                            input_integrity, tuple(evidence), requirement_checks)
