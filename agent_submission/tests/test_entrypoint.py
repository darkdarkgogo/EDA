from pathlib import Path

from main import build_parser


def test_parser_accepts_evaluator_argument_spelling(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["-input", str(tmp_path), "-output", str(tmp_path / "out")])
    assert args.input == str(tmp_path)
    assert args.output == str(tmp_path / "out")
