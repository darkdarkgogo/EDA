# Scan Insertion Agent — phase one

Python 3.12 on the official `scan-agent-base:ubuntu24` image runs a LangGraph
workflow with `openai`, `langgraph`, and `pypdf`. It retrieves relevant passages
from the Scan User Manual with deterministic keyword ranking and supplies them
to the evaluation-provided DeepSeek V4 Pro model. Task one generates a dofile;
task two repairs `original.dofile`. Python validates real `dftexp_scan` results
before declaring success. Phase one returns `unsupported_netlist_repair` when a
diagnosis requires Pre-scan netlist changes rather than editing a netlist. It
does not run LEC.

## Build

Run from `agent_submission/`, the build-context root containing `Dockerfile` and
`submission/`. The official base must already be loaded locally:

```bash
docker image inspect scan-agent-base:ubuntu24
docker build -t scan-agent:phase1 .
docker run --rm --entrypoint python3 scan-agent:phase1 -B -c 'import openai, langgraph, pypdf'
```

The build installs the pinned runtime packages from the Tsinghua PyPI mirror at
`https://pypi.tuna.tsinghua.edu.cn/simple`. It does not download a local
embedding model. The LLM uses the evaluation-provided OpenAI-compatible API.

To transfer the complete image to a server without pulling from a registry:

```bash
docker save -o scan-agent-phase1.tar scan-agent:phase1
scp scan-agent-phase1.tar USER@SERVER:/path/to/upload/
ssh USER@SERVER 'docker load -i /path/to/upload/scan-agent-phase1.tar'
```

The server does not need Hugging Face access. It still needs network access to
the configured LLM endpoint and the DFTEXP license server.

The image entrypoint is `/submission/agent_system`; it forwards CLI arguments
and runs Python with bytecode writes disabled. Only `submission/` is copied into
the image. The build context allowlist contains `submission/`, `.env`,
`Dockerfile`, `README.md`, and `submission.zip`; tests, docs, caches, and public
cases are excluded. `.env` is sent to the Docker builder but is not copied into
the image. Never put credentials under `submission/`.

## Formal evaluation

Set `LLM_API_KEY` (your secret API key) before running. `LLM_BASE_URL` and
`LLM_MODEL` default to the evaluation endpoint and `deepseek-v4-pro`; set them
only when you need to override those defaults. The evaluation system supplies
`SCANINSERTION_LICENSE_SERVER` to the real scan process. No credentials are
embedded in the image. Python does not load `.env` automatically, so pass a
local `.env` with Docker's `--env-file` option or export variables in the host
shell.

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
`/opt/dftexp_scan/doc/Scan_User_Manual.pdf`. Retrieval removes repeated page
headers, splits the manual on Chinese and numbered section headings, and keeps
each heading with its passage across page boundaries. A local BM25F index
weights headings and body text separately, matches complete command identifiers,
and selects passages across setup, signals, configuration, DRC, insertion, and
output needs. Required CTL, Wrapper, Partition, Segment, Lockup, DRC-rule, and
additional output topics receive their own retrieval slot, up to fourteen passages
when the task needs them. When more topics are supplied, the six core phases and
required output commands take priority over extra DRC rule passages. The PDF is not
copied into the submission image. Decision logs
record the retrieval method as `keyword_only`.

## Local pre-populated case

Fill `LLM_API_KEY` in `.env` first. For a local run that invokes the licensed
EDA tool, also uncomment `SCANINSERTION_LICENSE_SERVER` there and set a valid
license server address. Run from `agent_submission/`; Docker then loads those
values into the container with `--env-file`. For an already complete input
directory, explicitly skip the readiness wait:

```bash
docker run --rm \
  --env-file .env \
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
mkdir -p .pytest-tmp
python -m pytest -v --basetemp .pytest-tmp/local-suite
python -m compileall submission
```

## Real-tool smoke test

`file_flag` remains the production default until a licensed `dftexp_scan -h` or
minimal-run confirms the correct launch interface. Set
`DFTEXP_SCAN_LAUNCH_MODE=file_flag` or `stdin_source` to select the audited
mode. Both modes are covered by the fake executable in offline tests.

With a licensed executable, model credentials, and a minimal published case,
run the opt-in smoke test (the case must contain `.case_ready`):

```bash
DFTEXP_REAL_SMOKE=1 \
DFTEXP_SCAN_EXECUTABLE=/opt/dftexp_scan/bin/dftexp_scan \
DFTEXP_SMOKE_INPUT=/absolute/minimal/case/input \
DFTEXP_SMOKE_OUTPUT=/absolute/minimal/case/output \
python -m pytest -q tests/test_real_dftexp_scan.py
```

The smoke test records the real `-h` output and retains the agent's reports,
logs, and decision audit. It is skipped as externally blocked when the
executable, License, case, or evaluation model settings are unavailable.

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
`preset_issues.json`; neither is used for model input or decisions. The integrity
pass also skips reading `preset_issues.json`. Reusing an output directory
with previous evidence quarantines it and returns `compliance_failure`.
