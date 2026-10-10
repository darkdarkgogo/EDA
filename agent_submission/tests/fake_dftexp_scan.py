"""Deterministic subprocess fixture; never imported by production code."""

import argparse
import os
from pathlib import Path
import re
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("success", "error", "sleep", "raw", "repair", "drc", "missing_output", "license", "mutation"), default="success")
    parser.add_argument("--input-to-mutate", type=Path)
    parser.add_argument("-f", type=Path)
    args = parser.parse_args()
    if args.f is None:
        source = sys.stdin.read()
        match = re.fullmatch(r'Source "([^"\r\n]+)"\nexit\n', source)
        if not match:
            print("invalid stdin Source protocol", file=sys.stderr)
            return 10
        args.f = Path(match.group(1))
        (Path.cwd() / "stdin.txt").write_text(source, encoding="utf-8")
    if not args.f.is_file():
        return 9
    dofile_text = args.f.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*(?:examine_scan|insert_scan)\b", dofile_text):
        print("obsolete placeholder command", file=sys.stderr)
        return 11
    if args.mode == "raw":
        sys.stdout.buffer.write(b"stdout\r\n\xff\n")
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(b"stderr\r\n")
        return 0
    print("[INFO] fake tool started", flush=True)
    root = args.f.parent.parent
    if args.mode == "error" or (args.mode == "repair" and root.name == "R1"):
        print("[ERROR] requested failure", file=sys.stderr, flush=True)
        return 7
    if args.mode == "sleep":
        time.sleep(30)
        return 0
    if args.mode == "license":
        print("[ERROR] License checkout failed", flush=True)
        return 8
    if args.mode == "mutation":
        args.input_to_mutate.write_text("fixture mutation\n", encoding="utf-8")
    work = Path.cwd()
    dofile = args.f.read_text(encoding="utf-8")
    requested = {}
    for line in dofile.splitlines():
        match = re.match(r"\s*(rpt_scan_(?:signal|cfg|chain|partition|drc_violation)|rpt_wrapper_cfg)\b.*?>\s*([^\s]+)\s*$", line)
        if match:
            requested["drc" if match.group(1) == "rpt_scan_drc_violation" else match.group(1)] = match.group(2)
        match = re.match(r"\s*dump_netlist\b.*?-file\s+(\S+)", line)
        if match:
            requested["netlist"] = match.group(1)
    if args.mode != "missing_output" and "netlist" in requested:
        output = work / requested["netlist"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"// offline test fixture {root.name}\nmodule top(); endmodule\n", encoding="utf-8")
    reports = {
        "drc": ("DFTR10 x1\nTotal violations: 1\n" if args.mode == "drc" else "Total violations: 0\n"),
        "rpt_scan_signal": """Port PortProperty SignalType OffState HookupPin HookupSense AssociatedInternal Usage View ConstantValue OwnerPartition
clk user_defined clock 0 - - - - - - Default_Partition
scan_en user_defined scan_enable 0 - - - all spec - Default_Partition
""",
        "rpt_scan_cfg": """ScanConfigurationParameter Value
chain_count 1
max_length 1
mix_edges False
mix_clocks False
mix_internal_clocks False
add_lockup True
insert_terminal_lockup False
""",
        "rpt_scan_chain": """Chain Length Input Output ScanEnable Clocks Partition ChainProperty
I 0 1 test_si0 test_so0 scan_en clk Default_Partition tool_created
""",
        "rpt_scan_partition": """Partition Include Exclude Clocks RisingEdgeClocks FallingEdgeClocks
Default_Partition - - clk - -
""",
        "rpt_wrapper_cfg": """WrapperConfigurationParameter Value
chain_count 0
max_length 1
style none
""",
    }
    for command, content in reports.items():
        name = requested.get(command) if command.startswith("rpt_") else requested.get("drc")
        if name:
            report = work / name
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(content, encoding="utf-8")
    Path("cwd.txt").write_text(str(Path.cwd()), encoding="utf-8")
    Path("environment.txt").write_text(os.environ.get("SCAN_TEST_INHERITED", "") + "/" + os.environ.get("SCAN_TEST_OVERRIDE", ""), encoding="utf-8")
    print("[INFO] insert_dft_logic completed successfully", flush=True)
    print(reports["drc"], end="", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
