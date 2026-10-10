"""Parse documented DFTEXP_Scan report tables into auditable observations."""

from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path
import re
import time
from collections.abc import Callable

from .artifacts import RunPaths
from .deadline import check_deadline


@dataclass(frozen=True)
class EvidenceLocation:
    source: str
    line_number: int
    source_line: str


@dataclass(frozen=True)
class SignalObservation:
    signal_type: str
    port: str
    off_state: str | None
    constant_value: str | None
    usage: str | None
    view: str | None
    associated_internal: str | None
    owner_partition: str | None
    location: EvidenceLocation


@dataclass(frozen=True)
class ConfigObservation:
    name: str
    value: str
    location: EvidenceLocation


@dataclass(frozen=True)
class ChainObservation:
    chain_class: str
    name: str
    length: int
    data_input: str
    data_output: str
    scan_enable: str
    clocks: tuple[str, ...]
    partition: str | None
    chain_property: str | None
    location: EvidenceLocation


@dataclass(frozen=True)
class PartitionObservation:
    name: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    clocks: tuple[str, ...]
    rising_edge_clocks: tuple[str, ...]
    falling_edge_clocks: tuple[str, ...]
    member: str | None
    by_clock: str | None
    by_clock_edge: str | None
    location: EvidenceLocation


@dataclass(frozen=True)
class SegmentObservation:
    name: str
    length: int
    location: EvidenceLocation


@dataclass(frozen=True)
class ReportIssue:
    source: str
    line_number: int
    source_line: str
    reason: str


@dataclass(frozen=True)
class ReportEvidence:
    signals: tuple[SignalObservation, ...] = ()
    config: tuple[ConfigObservation, ...] = ()
    chains: tuple[ChainObservation, ...] = ()
    partitions: tuple[PartitionObservation, ...] = ()
    issues: tuple[ReportIssue, ...] = ()
    wrapper_config: tuple[ConfigObservation, ...] = ()
    segments: tuple[SegmentObservation, ...] = ()


REPORT_FILES = {
    "signals": "scan_signal.rpt",
    "config": "scan_cfg.rpt",
    "chains": "scan_chain.rpt",
    "partitions": "scan_partition.rpt",
    "wrapper_config": "wrapper_cfg.rpt",
}
MAX_REPORT_LINE_CHARS = 1_048_576


def _bounded_lines(stream, deadline_monotonic: float | None, clock: Callable[[], float]):
    """Read one bounded line at a time so a malformed giant row cannot evade deadlines."""
    while True:
        check_deadline(deadline_monotonic, clock, "report evidence reading")
        line = stream.readline(MAX_REPORT_LINE_CHARS + 1)
        check_deadline(deadline_monotonic, clock, "report evidence reading")
        if not line:
            return
        if len(line) > MAX_REPORT_LINE_CHARS and not line.endswith(("\n", "\r")):
            raise ValueError(f"report line exceeds {MAX_REPORT_LINE_CHARS} characters")
        yield line


def _records(
    text: str | Iterable[str], source: str,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
):
    lines = text.splitlines() if isinstance(text, str) else text
    for number, raw_line in enumerate(lines, 1):
        check_deadline(deadline_monotonic, clock, "report evidence parsing")
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if stripped and not set(stripped) <= {"-", "=", "+", "|", " "}:
            yield number, line, stripped


def _fixed_columns(header_line: str, row_line: str) -> list[str]:
    """Keep empty cells in the tool's aligned table output."""
    starts = [match.start() for match in re.finditer(r"\S+", header_line)]
    return [row_line[start:(starts[index + 1] if index + 1 < len(starts) else None)].strip(" |")
            for index, start in enumerate(starts)]


def parse_signal_report(text: str | Iterable[str], source: str = "reports/scan_signal.rpt", *,
                        deadline_monotonic: float | None = None, clock: Callable[[], float] = time.monotonic):
    rows, issues = [], []
    header = None
    fixed_header = None
    for number, line, stripped in _records(text, source, deadline_monotonic, clock):
        columns = stripped.strip("|").split()
        lowered = [item.lower() for item in columns]
        if "port" in lowered and "signaltype" in lowered:
            header = {name: lowered.index(name) for name in ("port", "signaltype")}
            for optional in ("offstate", "usage", "view", "constantvalue", "associatedinternal", "ownerpartition"):
                if optional in lowered:
                    header[optional] = lowered.index(optional)
            fixed_header = line if re.search(r"\s{2,}", line) else None
            continue
        if header is None or stripped.startswith("Design:") or stripped.isdigit():
            continue
        values = _fixed_columns(fixed_header, line) if fixed_header else stripped.strip("|").split()
        try:
            if max(header.values()) >= len(values):
                raise ValueError("row has fewer values than the report header")
            signal_type = values[header["signaltype"]].lower()
            embedded_view = None
            if match := re.fullmatch(r"([a-z_]+)\((spec|existing)\)", signal_type):
                signal_type, embedded_view = match.groups()
            port = values[header["port"]]
            if signal_type not in {"clock", "reset", "scan_enable", "constant", "scan_data_in", "scan_data_out",
                                   "wrapper_clock", "wrp_data_in", "wrp_data_out", "wrp_in_shift_en",
                                   "wrp_out_shift_en", "wrp_in_capture_en", "wrp_out_capture_en"}:
                raise ValueError(f"unsupported signal type: {signal_type}")
            def get(key):
                index = header.get(key)
                return values[index] if index is not None and index < len(values) and values[index] not in {"", "-", "--"} else None
            rows.append(SignalObservation(signal_type, port, get("offstate"), get("constantvalue"),
                                          get("usage"), get("view") or embedded_view, get("associatedinternal"), get("ownerpartition"),
                                          EvidenceLocation(source, number, line)))
        except (IndexError, ValueError) as error:
            issues.append(ReportIssue(source, number, line, str(error)))
    if header is None:
        issues.append(ReportIssue(source, 0, "", "missing Port/SignalType table header"))
    return tuple(rows), tuple(issues)


def parse_config_report(text: str | Iterable[str], source: str = "reports/scan_cfg.rpt", *,
                        deadline_monotonic: float | None = None, clock: Callable[[], float] = time.monotonic):
    rows, issues = [], []
    in_table = False
    for number, line, stripped in _records(text, source, deadline_monotonic, clock):
        if re.search(r"(?:Scan|Wrapper)ConfigurationParameter\s+Value", stripped, re.IGNORECASE):
            in_table = True
            continue
        if not in_table or stripped.startswith(("Design:", "Scan partition:")) or stripped.isdigit():
            continue
        fields = stripped.strip("|").split(None, 1)
        if len(fields) != 2:
            issues.append(ReportIssue(source, number, line, "malformed configuration row"))
            continue
        name, value = fields[0].lower(), fields[1].strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not value:
            issues.append(ReportIssue(source, number, line, "malformed configuration name/value"))
            continue
        rows.append(ConfigObservation(name, value, EvidenceLocation(source, number, line)))
    if not in_table:
        issues.append(ReportIssue(source, 0, "", "missing configuration parameter/value table header"))
    return tuple(rows), tuple(issues)


def parse_chain_report(text: str | Iterable[str], source: str = "reports/scan_chain.rpt", *,
                       deadline_monotonic: float | None = None, clock: Callable[[], float] = time.monotonic):
    rows, issues = [], []
    in_table = False
    for number, line, stripped in _records(text, source, deadline_monotonic, clock):
        if re.match(r"Chain\s+Length\s+Input\s+Output", stripped, re.IGNORECASE):
            in_table = True
            continue
        if not in_table or stripped.startswith("Design:") or stripped.isdigit():
            continue
        parts = stripped.strip("|").split()
        try:
            if not parts or parts[0] not in {"I", "W"}:
                raise ValueError("unsupported or malformed chain row class")
            if len(parts) < 7:
                raise ValueError("chain row has fewer than seven columns")
            length = int(parts[2])
            if length < 0:
                raise ValueError("chain length cannot be negative")
            partition = parts[-2] if len(parts) >= 9 else None
            prop = parts[-1] if len(parts) >= 9 else None
            clock_end = len(parts) - 2 if len(parts) >= 9 else len(parts)
            clocks = tuple(item.strip(",") for item in parts[6:clock_end] if item.strip(","))
            if not clocks:
                raise ValueError("chain row has no clock")
            rows.append(ChainObservation(parts[0], parts[1], length, parts[3], parts[4], parts[5],
                                         clocks, partition, prop, EvidenceLocation(source, number, line)))
        except (IndexError, ValueError) as error:
            issues.append(ReportIssue(source, number, line, str(error)))
    if not in_table:
        issues.append(ReportIssue(source, 0, "", "missing Chain/Length/Input/Output table header"))
    return tuple(rows), tuple(issues)


def parse_partition_report(text: str | Iterable[str], source: str = "reports/scan_partition.rpt", *,
                           deadline_monotonic: float | None = None, clock: Callable[[], float] = time.monotonic):
    rows, issues = [], []
    header = None
    fixed_header = None
    current_partition = None
    for number, line, stripped in _records(text, source, deadline_monotonic, clock):
        columns = stripped.strip("|").split()
        lowered = [item.lower() for item in columns]
        if "partition" in lowered and ("include" in lowered or "cell" in lowered or "instancename" in lowered):
            header = lowered
            fixed_header = line if re.search(r"\s{2,}", line) else None
            continue
        if header is None or stripped.startswith("Design:") or stripped.isdigit():
            continue
        values = _fixed_columns(fixed_header, line) if fixed_header else stripped.strip("|").split()
        try:
            if len(values) < 2:
                raise ValueError("partition row has fewer than two columns")
            index = {name: i for i, name in enumerate(header)}
            get = lambda name: values[index[name]] if name in index and index[name] < len(values) else ""
            partition = get("partition") or current_partition
            if not partition:
                raise ValueError("partition name is missing")
            current_partition = partition
            def words(name):
                value = get(name)
                return tuple(part.strip("{},") for item in value.split() if item not in {"-", "{}"}
                             for part in item.split(",") if part.strip("{},"))
            cell = get("cell") or get("instancename") or None
            rows.append(PartitionObservation(partition, words("include"), words("exclude"), words("clocks"),
                words("risingedgeclocks"), words("fallingedgeclocks"), cell,
                get("byclock") or None, get("byclockedge") or None,
                EvidenceLocation(source, number, line)))
        except (IndexError, ValueError) as error:
            issues.append(ReportIssue(source, number, line, str(error)))
    if header is None:
        issues.append(ReportIssue(source, 0, "", "missing Partition table header"))
    return tuple(rows), tuple(issues)


def collect_report_evidence(
    run_paths: RunPaths,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReportEvidence:
    """Read only the fixed, run-contained scan reports and preserve every row."""
    groups = {"signals": (), "config": (), "chains": (), "partitions": (), "wrapper_config": ()}
    issues = []
    for group, filename in REPORT_FILES.items():
        check_deadline(deadline_monotonic, clock, "report evidence collection")
        path = run_paths.reports / filename
        if not path.exists():
            issues.append(ReportIssue(f"runs/{run_paths.run_id}/reports/{filename}", 0, "", "report is missing"))
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(run_paths.root.resolve()) or not path.is_file():
            issues.append(ReportIssue(f"reports/{filename}", 0, "", "report path is not a contained regular file"))
            continue
        parser = {"signals": parse_signal_report, "config": parse_config_report, "wrapper_config": parse_config_report,
                  "chains": parse_chain_report, "partitions": parse_partition_report}[group]
        source = f"runs/{run_paths.run_id}/reports/{filename}"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                rows, parse_issues = parser(_bounded_lines(stream, deadline_monotonic, clock), source,
                                            deadline_monotonic=deadline_monotonic, clock=clock)
        except ValueError as error:
            rows, parse_issues = (), (ReportIssue(source, 0, "", str(error)),)
        groups[group] = rows
        issues.extend(parse_issues)
    segments = []
    segment_path = run_paths.reports / "scan_segment.rpt"
    if segment_path.is_file() and segment_path.resolve().is_relative_to(run_paths.root.resolve()):
        with segment_path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line, _ in _records(_bounded_lines(stream, deadline_monotonic, clock),
                                            "reports/scan_segment.rpt", deadline_monotonic, clock):
                if match := re.match(r"\s*(seg\d+)\s+user_defined\s+(\d+)\s+", line):
                    segments.append(SegmentObservation(match.group(1), int(match.group(2)),
                        EvidenceLocation(f"runs/{run_paths.run_id}/reports/scan_segment.rpt", number, line)))
    return ReportEvidence(groups["signals"], groups["config"], groups["chains"], groups["partitions"],
                          tuple(issues), groups["wrapper_config"], tuple(segments))
