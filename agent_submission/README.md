# Scan Insertion Agent — phase one

Python 3.12 on the official `scan-agent-base:ubuntu24` image runs a LangGraph
workflow with `openai`, `langgraph`, and `pypdf`. Use the evaluation-provided
DeepSeek V4 Pro endpoint and model. Task one generates a dofile; task two repairs
`original.dofile`. Python validates real `dftexp_scan` results before declaring
success. Phase one returns `unsupported_netlist_repair` when a diagnosis requires
Pre-scan netlist changes rather than editing a netlist. It does not run LEC.

## Build

Run from `agent_submission/`, the build-context root containing `Dockerfile` and
`submission/`. The official base must already be loaded locally:

```bash
docker image inspect scan-agent-base:ubuntu24
docker build -t scan-agent:phase1 .
docker run --rm --entrypoint python3 scan-agent:phase1 -B -c 'import openai, langgraph, pypdf'
```

The image entrypoint is `/submission/agent_system`; it forwards CLI arguments
and runs Python with bytecode writes disabled. Only `submission/` is copied into
the image. Tests, docs, caches, `.env` files, public cases, and archives are
excluded from the build context. Never put credentials into the runtime source.

## Formal evaluation

Export `LLM_API_KEY` (secret API key), `LLM_BASE_URL` (OpenAI-compatible endpoint),
`LLM_MODEL` (evaluation-provided DeepSeek V4 Pro identifier), and
`SCANINSERTION_LICENSE_SERVER` (license server required by `dftexp_scan`) in the
host shell before running. The model settings are required; the license variable
is inherited by the real scan process. No credentials are embedded in the image.

```bash
docker run --rm \
  -e LLM_API_KEY \
  -e LLM_BASE_URL \
  -e LLM_MODEL \
  -e SCANINSERTION_LICENSE_SERVER \
  -v /absolute/case/input:/input:ro \
  -v /absolute/case/output:/output:rw \
  scan-agent:phase1 -input /input -output /output
```

Replace the absolute host paths with your case directories. Use a fresh output
directory for each invocation. Formal mode waits for `/input/.case_ready`;
the case budget starts after this sentinel appears. Leave
`SCAN_AGENT_SKIP_READY_WAIT` unset in formal mode. `limitations.md` supplies the
wall-time budget and any tool-run limit; the default maximum is three tool runs,
with ten seconds reserved for finalization. The tool manual is read from
`/opt/dftexp_scan/doc/Scan_User_Manual.pdf`; an unavailable manual is recorded.

## Local pre-populated case

For an already complete input directory, explicitly skip the readiness wait:

```bash
docker run --rm \
  -e LLM_API_KEY \
  -e LLM_BASE_URL \
  -e LLM_MODEL \
  -e SCANINSERTION_LICENSE_SERVER \
  -e SCAN_AGENT_SKIP_READY_WAIT=1 \
  -v /absolute/case/input:/input:ro \
  -v /absolute/case/output:/output:rw \
  scan-agent:phase1 -input /input -output /output
```

Real public-case validation requires the licensed tool and evaluation model
environment. Preserve its genuine logs and reports. Offline tests use explicit
test doubles and establish workflow behavior, not real EDA quality:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -v
python -m compileall submission
```

## Results and failure behavior

Exit code `0` means `success`; every other terminal Agent status returns `1`
(argument parsing errors use argparse's nonzero exit code). Inspect
`decision_log.json` for the status, `failure_reason`, and `final_run`.

- `success`: a real run passed deterministic checks and supplied required outputs.
- `tool_failure`: unavailable tool, license/process failure, invalid input, or failed validation.
- `budget_exhausted`: wall-time or tool-run budget reached.
- `invalid_model_output`: missing model configuration or rejected model response/transport failure.
- `no_progress`: a previously failed dofile was repeated.
- `unsupported_netlist_repair`: required netlist changes exceed phase one.
- `compliance_failure`: protected input changed or directories violate integrity requirements.

Each attempt retains `runs/Rn/Rn.log`, `run_metadata.json`, the executed dofile
under `deliverables/`, real reports, and validation evidence. Repairs produce
`diffs/dofile_R1_to_R2.diff` and subsequent round diffs. `decision_log.json`
uses output-relative references validated to remain within the output directory.
Candidate/model response/repair records retain the evidence behind each decision.

`final_results/` is promoted only from one validated successful run and contains
`final.log`, `deliverables/`, and `reports/` with verified source hashes.
On failure, `final_run` is null and no fake final netlist or reports are created.
Missing `dftexp_scan` yields failure evidence and a nonzero result. Input files
are protected by SHA-256 checks. Runtime inventory excludes `golden.dofile` and
`preset_issues.json`; their contents are never used for model input or decisions
(the integrity pass hashes all regular input files). Reusing an output directory
with previous evidence quarantines it and returns `compliance_failure`.
