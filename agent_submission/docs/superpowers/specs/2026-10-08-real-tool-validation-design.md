# Real-tool compatibility and deterministic validation design

Date: 2026-10-08

## Goal

Make the phase-one Scan Insertion Agent compatible with the documented
DFTEXP_Scan command flow and require deterministic report evidence for every
supported task requirement. A zero process exit status alone must never prove
success.

This change preserves the existing LangGraph orchestration, bounded repair
loop, input-integrity protection, `.case_ready` timing handshake, artifact
audit trail, and phase-one prohibition on editing a pre-scan netlist.

## Scope

This iteration covers:

- documented DFTEXP_Scan command names and phase ordering;
- a configurable, auditable dofile launch mode;
- report generation and deterministic validation for scan signals, scan
  configuration, chains, wrappers, partitions, DRC, and insertion completion;
- exact runtime dependency versions;
- offline contract tests plus an opt-in real-tool test entry point.

Deterministic netlist/Liberty structural summarization and improved manual
retrieval are separate follow-up work. The manual PDFs are development inputs
and are not copied into the submission image.

## Documented command flow

The accepted core phases are:

1. `load_lib`
2. `load_netlist`
3. `present_design`
4. zero or more documented configuration commands, including
   `set_scan_signal`, `set_scan_cfg`, and `set_wrapper_cfg`
5. `examine_scan_drc`
6. `examine_scan_chain`
7. `insert_dft_logic`
8. report commands required by the extracted requirements
9. `dump_netlist`

The report layer may use:

- `rpt_scan_signal` for clock, reset, scan-enable, constant, data, and wrapper
  signal evidence;
- `rpt_scan_cfg` for chain and insertion configuration;
- `rpt_scan_chain` (including `-brief` or `-class all` where appropriate) for
  post-insertion chain and wrapper structure;
- `rpt_scan_partition` for partition membership and configuration;
- `rpt_scan_element` for scannable/non-scannable element evidence;
- `rpt_scan_drc_violation` or the documented DRC output for rule evidence.

The safety validator recognizes these documented commands instead of the
placeholder `examine_scan` and `insert_scan` names. It continues to reject
`exec`, `system`, absolute or escaping output paths, and unrestricted dynamic
Tcl. A `source` operation is permitted only if a future generated fragment is
resolved strictly beneath the current run directory and receives the same
validation before execution. No arbitrary external source file is allowed.

## Tool invocation

`runner.py` exposes an explicit launch-mode setting rather than silently
assuming one interface. Supported modes are:

- `file_flag`: execute the configured binary with `-f <dofile>`;
- `stdin_source`: start the binary and submit one validated `Source` command
  through standard input.

The production default must be selected from real `dftexp_scan` help output or
a licensed minimal smoke run. Until that evidence exists, the current mode is
retained but recorded in run metadata, and the alternative is covered by
offline subprocess tests. Command, mode, dofile path, exit status, timeout,
and produced files remain auditable.

## Requirement-to-evidence validation

Validation builds a normalized observation set from real report files. Each
supported requirement has an independent checker and an evidence reference.

| Requirement | Required evidence |
| --- | --- |
| Clock definition and polarity | matching `rpt_scan_signal` clock row |
| Reset definition and polarity | matching `rpt_scan_signal` reset row |
| Scan enable and off-state | matching `rpt_scan_signal` scan-enable row |
| Constant constraints | matching `rpt_scan_signal` constant row |
| Chain count and maximum length | `rpt_scan_cfg` plus complete `rpt_scan_chain` rows |
| Chain clock/domain and enable | matching post-insertion chain rows |
| Lockup and mixing settings | matching `rpt_scan_cfg` fields and chain facts where applicable |
| Wrapper settings and structure | wrapper configuration plus wrapper-class chain rows |
| Partition requirements | matching `rpt_scan_partition` rows |
| DRC allowance | explicit final DRC evidence with no unaccounted violations |
| Insertion completion | documented completion marker and positive post-insertion chain structure |
| Required output netlist | nonempty contained artifact promoted from the selected run |

Unknown, unsupported, absent, duplicated, or contradictory evidence fails
closed. The validator never treats model output as proof. Functional
equivalence remains explicitly unverified unless a documented real-tool report
or an approved deterministic checker is available; the decision log must not
claim it passed.

## Decision log

Every normalized requirement receives a mapping entry containing:

- requirement type and requested value;
- observed value;
- pass/fail/unverified status;
- report path and source line(s);
- concise mismatch reason when not passed.

Only `pass` entries may contribute to overall success. A required entry marked
`unverified` makes the run fail. References must continue to resolve strictly
beneath the output root.

## Timing and failure behavior

All new report collection and parsing uses the existing absolute case deadline
and finalization reserve. Missing reports, malformed rows, unsupported launch
mode, unavailable executable, timeout, nonzero exit, or input mutation produces
a non-success result and no `final_results` publication.

The `.case_ready` wait remains enabled in formal mode because the official
timing-mechanism update requires it. Local pre-populated cases may continue to
use `SCAN_AGENT_SKIP_READY_WAIT=1`.

## Dependency reproducibility

`submission/requirements.txt` pins exact tested versions of `langgraph`,
`openai`, and `pypdf`. The selected versions must install together in the
official Python environment and pass the complete offline suite. Development
dependencies are pinned separately.

## Verification

Offline tests must cover:

- documented command acceptance and placeholder command rejection;
- phase ordering and output containment;
- both runner launch modes using a fake executable;
- each report parser and each requirement checker;
- missing, contradictory, duplicate, and malformed evidence;
- Clock, Reset, Scan Enable, constant, chain, lockup, wrapper, and partition
  success and failure cases;
- truthful decision-log evidence mapping;
- unchanged bounded-run, deadline, input-integrity, and failure-publication
  behavior;
- exact dependency declarations and package layout.

An opt-in real-tool smoke test accepts a licensed executable and a minimal
public case, records the real help/version output and all generated evidence,
and never runs in the ordinary offline suite. It is reported as externally
blocked when the executable or License is unavailable; it must never be
represented by the fake tool.

## Follow-up work

After this compatibility and validation layer is proven, a separate iteration
will add deterministic netlist and Liberty summaries and a command-aware manual
index. Those summaries will be bounded, input-protected, and passed to the LLM
instead of raw large design files.
