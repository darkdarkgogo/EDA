import argparse
from collections.abc import Sequence
from pathlib import Path

from scan_agent.state import AgentStatus
from scan_agent.workflow import run_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Insertion Agent")
    parser.add_argument("-input", "--input", dest="input", required=True)
    parser.add_argument("-output", "--output", dest="output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_agent(Path(args.input), Path(args.output))
    return 0 if result.status == AgentStatus.SUCCESS else 1


if __name__ == "__main__":
    raise SystemExit(main())
