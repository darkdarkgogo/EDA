"""Deterministic subprocess fixture; never imported by production code."""

import argparse
import os
from pathlib import Path
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("success", "error", "sleep", "raw", "repair", "drc", "missing_output", "license", "mutation"), default="success")
    parser.add_argument("--input-to-mutate", type=Path)
    parser.add_argument("-f", type=Path, required=True)
    args = parser.parse_args()
    if not args.f.is_file():
        return 9
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
    if args.mode != "missing_output":
        (root / "deliverables" / "post_scan.v").write_text(f"// offline test fixture {root.name}\nmodule top(); endmodule\n", encoding="utf-8")
    (root / "reports" / "scan.rpt").write_text("Number of scan chains: 1\nMaximum chain length: 1\n", encoding="utf-8")
    Path("cwd.txt").write_text(str(Path.cwd()), encoding="utf-8")
    Path("environment.txt").write_text(os.environ.get("SCAN_TEST_INHERITED", "") + "/" + os.environ.get("SCAN_TEST_OVERRIDE", ""), encoding="utf-8")
    print("[INFO] insert_scan completed successfully", flush=True)
    if args.mode == "drc":
        print("DFTR10 x1\nTotal violations: 1", flush=True)
    else:
        print("Total violations: 0", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
