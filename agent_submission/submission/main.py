import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from scan_agent.state import AgentStatus
from scan_agent.workflow import run_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Insertion Agent")
    parser.add_argument("-input", "--input", dest="input", required=True)
    parser.add_argument("-output", "--output", dest="output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir, output_dir = Path(args.input), Path(args.output)
    if not input_dir.is_dir():
        print(f"input directory does not exist: {input_dir}", file=sys.stderr)
        return 3
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"cannot create output directory: {error}", file=sys.stderr)
        return 3
    result = run_agent(input_dir, output_dir)
    return 0 if result.status == AgentStatus.SUCCESS else 1


if __name__ == "__main__":
    raise SystemExit(main())
