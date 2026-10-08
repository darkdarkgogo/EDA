"""Extract explicit tool facts while retaining their original line evidence."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str | None
    message: str
    source_line: str
    line_number: int
    command: str | None = None


@dataclass(frozen=True)
class DrcViolation:
    rule: str
    count: int | None
    source_line: str
    line_number: int
    severity: str | None = None
    code: str | None = None
    objects: tuple[str, ...] = ()
    count_is_explicit: bool = False


@dataclass(frozen=True)
class ReportFact:
    name: str
    value: int
    source_line: str
    line_number: int
    chain_name: str | None = None


@dataclass(frozen=True)
class DiagnosticSummary:
    messages: tuple[Diagnostic, ...] = ()
    fatal_errors: tuple[Diagnostic, ...] = ()
    license_errors: tuple[Diagnostic, ...] = ()
    drc_violations: tuple[DrcViolation, ...] = ()
    drc_totals: tuple[ReportFact, ...] = ()
    commands: tuple[str, ...] = ()
    chain_facts: tuple[ReportFact, ...] = ()
    insertion_facts: tuple[ReportFact, ...] = ()

    @property
    def total_violations(self) -> int | None:
        """The last explicit total; absence is unknown, never zero."""
        return self.drc_totals[-1].value if self.drc_totals else None

    @property
    def drc_evidence(self) -> tuple[DrcViolation | ReportFact, ...]:
        return (*self.drc_violations, *self.drc_totals)


_SEVERITY = re.compile(r"\[(INFO|WARNING|WARN|ERROR|FATAL)\]", re.IGNORECASE)
_CODE = re.compile(r"\[([A-Z][A-Z0-9]*-\d+)\]", re.IGNORECASE)
_RULE = re.compile(r"\bDFTR(?:-TIE\d+|\d+)(?:-\d+)?\b", re.IGNORECASE)
_COMMAND = re.compile(r"\b(?:set|load|read|present|examine|insert|rpt|report|dump|write)_\w+\b")
_TOTAL = re.compile(r"\b(?:DRC\s+)?Total\s+(?:DRC\s+)?violations\s*[:=]\s*(\d[\d,]*)\b", re.IGNORECASE)
_COUNT = re.compile(r"^\s*(?:x\s*|(?:count|violations?)\s*[:=]\s*|[:=|]\s*)(\d[\d,]*)\b", re.IGNORECASE)
_COUNT_SUFFIX = re.compile(r"^\s*\(?\s*(\d[\d,]*)\s+violations?\b", re.IGNORECASE)
_COUNT_TABLE = re.compile(r"^\s+(\d[\d,]*)(?:\s|$)")
_LICENSE = re.compile(
    r"\blicen[sc](?:e|ing)\b.*\b(?:fail\w*|unavailable|denied|expired|error)\b"
    r"|\b(?:fail\w*|unable|cannot|could\s+not)\b.*\b(?:obtain|checkout|check\s+out|acquire|connect)\b.*\blicen[sc]e\b"
    r"|\bno\s+(?:valid\s+|available\s+)?licen[sc]e\b"
    r"|\blicen[sc]e\s+server\b.*\b(?:down|not\s+responding|unreachable)\b"
    r"|\b(?:all\s+)?licen[sc]es\s+(?:are\s+)?in\s+use\b"
    r"|\bnot\s+licen[sc]ed\b",
    re.IGNORECASE,
)
_CHAIN_PATTERNS = (
    ("chain_count", re.compile(r"\b(?:number\s+of\s+(?:scan\s+)?chains|(?:total\s+)?(?:scan\s+)?chain\s+count|total\s+(?:scan\s+)?chains)\s*[:=]\s*(?P<value>\d+)\b", re.IGNORECASE)),
    ("max_length", re.compile(r"\bmax(?:imum)?\s+(?:scan\s+)?chain\s+length\s*[:=]\s*(?P<value>\d+)\b", re.IGNORECASE)),
    ("chain_length", re.compile(r"^\s*(?:scan\s+)?chain\s+(?P<chain_name>\S+)\s+length\s*[:=]\s*(?P<value>\d+)\b", re.IGNORECASE)),
)
_INSERTION_COMPLETE = re.compile(
    r"^\s*(?:\[INFO\]\s*)?(?:insert_scan|scan\s+insertion)\s+"
    r"(?:completed|succeeded)(?:\s+successfully)?\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _metadata(line: str) -> tuple[str | None, str | None]:
    severity, code = _SEVERITY.search(line), _CODE.search(line)
    return (severity.group(1).upper() if severity else None, code.group(1).upper() if code else None)


def parse_drc_text(text: str) -> list[DrcViolation]:
    """Parse counted rules and individual DFTDRC records, not Tcl rule settings."""
    violations = []
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(r"\bset_scan_drc_rule_handling\b", line):
            continue
        severity, code = _metadata(line)
        rules = list(_RULE.finditer(line))
        for index, rule in enumerate(rules):
            end = rules[index + 1].start() if index + 1 < len(rules) else len(line)
            tail = line[rule.end():end]
            count_match = _COUNT.search(tail) or _COUNT_SUFFIX.search(tail) or _COUNT_TABLE.search(tail)
            is_record = bool(code and code.startswith("DFTDRC-"))
            prefix = line[:rule.start()]
            header_prefix = _SEVERITY.sub("", prefix) if severity in {"WARNING", "WARN", "ERROR", "FATAL"} else prefix
            is_header = not header_prefix.strip(" |\t")
            if count_match:
                count = int(count_match.group(1).replace(",", ""))
            elif is_record or is_header:
                # A diagnostic with a described object is one occurrence. A bare
                # rule summary without an explicit count remains unknown.
                count = 1 if is_record and re.search(r"'[^']+'|\"[^\"]+\"", prefix) else None
            else:
                continue
            objects = tuple(match.group(1) or match.group(2) for match in re.finditer(r"'([^']+)'|\"([^\"]+)\"", line))
            violations.append(DrcViolation(rule.group().upper(), count, line, number, severity, code, objects, bool(count_match)))
    return violations


def parse_tool_log(text: str) -> DiagnosticSummary:
    """Retain every error and explicit total; downstream validation owns policy."""
    messages, fatal, license_errors, totals, commands, chain_facts, insertion_facts = [], [], [], [], [], [], []
    for number, line in enumerate(text.splitlines(), 1):
        found_commands = _COMMAND.findall(line)
        commands.extend(command for command in found_commands if command not in commands)
        severity, code = _metadata(line)
        license_failure = bool(_LICENSE.search(line))
        if severity or code or license_failure:
            diagnostic = Diagnostic(severity or ("ERROR" if license_failure else "UNKNOWN"), code, line.strip(), line, number, found_commands[0] if found_commands else None)
            messages.append(diagnostic)
            if severity in {"ERROR", "FATAL"} or license_failure:
                fatal.append(diagnostic)
            if license_failure:
                license_errors.append(diagnostic)
        if match := _TOTAL.search(line):
            totals.append(ReportFact("total_violations", int(match.group(1).replace(",", "")), line, number))
        for name, pattern in _CHAIN_PATTERNS:
            if match := pattern.search(line):
                chain_name = match.group("chain_name") if name == "chain_length" else None
                chain_facts.append(ReportFact(name, int(match.group("value")), line, number, chain_name))
        if _INSERTION_COMPLETE.fullmatch(line):
            insertion_facts.append(ReportFact("insertion_complete", 1, line, number))
    return DiagnosticSummary(tuple(messages), tuple(fatal), tuple(license_errors), tuple(parse_drc_text(text)), tuple(totals), tuple(commands), tuple(chain_facts), tuple(insertion_facts))
