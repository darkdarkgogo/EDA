from pathlib import Path

from main import build_parser, main


def test_parser_accepts_evaluator_argument_spelling(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["-input", str(tmp_path), "-output", str(tmp_path / "out")])
    assert args.input == str(tmp_path)
    assert args.output == str(tmp_path / "out")


def test_missing_input_mount_fails_before_readiness_wait(tmp_path: Path, capsys) -> None:
    output = tmp_path / "output"
    assert main(["-input", str(tmp_path / "missing"), "-output", str(output)]) == 3
    assert "input directory does not exist" in capsys.readouterr().err
    assert not output.exists()


def test_uncreatable_output_mount_fails_before_agent_start(tmp_path: Path, capsys) -> None:
    output = tmp_path / "output"
    output.write_text("occupied", encoding="utf-8")
    assert main(["-input", str(tmp_path), "-output", str(output)]) == 3
    assert "cannot create output directory" in capsys.readouterr().err
