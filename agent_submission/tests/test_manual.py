from pathlib import Path
import threading
import time

import pytest

import scan_agent.manual as manual
from scan_agent.deadline import DeadlineExceeded
from scan_agent.manual import ManualChunk, ManualIndex, _page_chunks, load_manual


class _Page:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _Reader:
    def __init__(self, _path: Path) -> None:
        self.pages = [
            _Page("Command Reference\nset_scan_signal configures the scan signal for DFTR9."),
            _Page("Command Reference\nset_scan_cfg sets the chain length."),
        ]


def test_manual_search_ranks_matching_command_first(monkeypatch) -> None:
    monkeypatch.setattr(manual, "PdfReader", _Reader, raising=False)

    index = load_manual(Path("manual.pdf")).index
    assert index is not None
    results = index.search(["DFTR9", "set_scan_signal"], limit=1)

    assert results[0].page == 1
    assert "set_scan_signal" in results[0].text


def test_manual_load_builds_keyword_index_without_embedding_state(monkeypatch):
    class Reader:
        def __init__(self, _path):
            self.pages = [_Page("Configure scan enable with set_scan_signal.")]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))

    assert result.available is True
    assert result.index is not None
    assert result.index.search(["set_scan_signal"])[0].text == (
        "Configure scan enable with set_scan_signal."
    )


def test_missing_manual_is_recorded_not_raised(tmp_path: Path) -> None:
    result = load_manual(tmp_path / "missing.pdf")

    assert result.available is False
    assert result.index is None
    assert result.error == "manual PDF is unavailable"


def test_unreadable_manual_is_recorded_not_raised(monkeypatch) -> None:
    def unreadable(_path: Path):
        raise OSError("missing")

    monkeypatch.setattr(manual, "PdfReader", unreadable)
    result = load_manual(Path("missing.pdf"))

    assert result.available is False
    assert result.index is None
    assert result.error == "manual PDF is unavailable"


def test_large_single_line_is_split_without_losing_text(monkeypatch) -> None:
    content = "x" * 6100

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [_Page(content)]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))

    assert result.index is not None
    chunks = result.index.chunks
    assert all(0 < len(chunk.text) <= 2000 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == content
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.page == 1 for chunk in chunks)


def test_headings_start_new_chunks(monkeypatch) -> None:
    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [_Page("Introduction text\n## Scan configuration\nConfigure scan.\nset_scan_signal\nSignal details.")]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))

    assert result.index is not None
    assert [chunk.text for chunk in result.index.chunks] == [
        "Introduction text",
        "## Scan configuration\nConfigure scan.\nset_scan_signal\nSignal details.",
    ]
    assert result.index.chunks[-1].title == "## Scan configuration"


def test_search_counts_terms_case_insensitively_and_breaks_ties_stably() -> None:
    chunks = (
        ManualChunk(2, 0, "DFTR9"),
        ManualChunk(1, 1, "dftr9"),
        ManualChunk(1, 0, "Dftr9"),
        ManualChunk(3, 0, "DFTR9 dftr9"),
    )
    assert ManualIndex(chunks).search(["dFtR9"]) == [chunks[3], chunks[2], chunks[1], chunks[0]]


def test_unrelated_command_does_not_get_bonus() -> None:
    chunks = (
        ManualChunk(1, 0, "DFTR9 plain"),
        ManualChunk(2, 0, "set_scan_signal DFTR9"),
    )
    index = ManualIndex(chunks)
    assert index.search(["DFTR9"], limit=1) == [chunks[0]]
    assert index.search(["DFTR9"], limit=0) == []
    assert index.search(["DFTR9"], limit=-1) == []


def test_chinese_headings_are_kept_and_command_examples_are_not_headings() -> None:
    chunks = _page_chunks(
        "Scan 引擎用户手册  运行 Scan 引擎\n2026-07-06  13\n"
        "2.1.2.2 定义 DFT Signal\n设置测试使能信号\n"
        "用户可以使用以下命令：\nset_scan_signal -type scan_enable\n"
        "insert_dft_logic\n后续说明。",
        18,
    )
    assert chunks[0].title == "2.1.2.2 定义 DFT Signal"
    assert chunks[1].title == "2.1.2.2 定义 DFT Signal > 设置测试使能信号"
    assert "insert_dft_logic\n后续说明。" in chunks[1].text
    assert all("Scan 引擎用户手册" not in chunk.text for chunk in chunks)
    assert all("2026-07-06" not in chunk.text for chunk in chunks)


def test_heading_carries_to_next_page(monkeypatch) -> None:
    class Reader:
        def __init__(self, _path):
            self.pages = [
                _Page("2.1.2.4 配置 Scan Chain\nset_scan_cfg -chain_count 2"),
                _Page("继续介绍链长。\nset_scan_cfg -max_length 100"),
            ]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    chunks = load_manual(Path("manual.pdf")).index.chunks
    assert chunks[-1].title == "2.1.2.4 配置 Scan Chain"


def test_exact_command_does_not_match_larger_identifier() -> None:
    chunks = (
        ManualChunk(1, 0, "reset_scan_signal is a different identifier"),
        ManualChunk(2, 0, "set_scan_signal configures the signal"),
    )
    assert ManualIndex(chunks).search(["set_scan_signal"]) == [chunks[1]]


def test_title_match_outweighs_a_single_body_mention() -> None:
    chunks = (
        ManualChunk(1, 0, "See the setup command elsewhere."),
        ManualChunk(2, 0, "The command configures scan enable.", "设置测试使能信号"),
    )
    assert ManualIndex(chunks).search(["测试使能信号"])[0] == chunks[1]


def test_shorter_equally_matching_chunk_ranks_first() -> None:
    chunks = (
        ManualChunk(1, 0, "DFTR9 " + "filler " * 200),
        ManualChunk(2, 0, "DFTR9 explains the issue."),
    )
    assert ManualIndex(chunks).search(["DFTR9"])[0] == chunks[1]


def test_empty_and_no_match_queries_return_no_chunks() -> None:
    index = ManualIndex((ManualChunk(1, 0, "set_scan_signal"),))
    assert index.search([]) == []
    assert index.search(["  "]) == []
    assert index.search(["no_such_command"]) == []


def test_diverse_search_covers_each_query_group() -> None:
    chunks = (
        ManualChunk(1, 0, "set_scan_signal"),
        ManualChunk(2, 0, "set_scan_signal set_scan_signal"),
        ManualChunk(3, 0, "insert_dft_logic"),
    )
    assert ManualIndex(chunks).search_diverse(
        [["set_scan_signal"], ["insert_dft_logic"]], limit=2,
    ) == [chunks[1], chunks[2]]


def test_title_phrase_prioritizes_reference_section_over_broad_example() -> None:
    chunks = (
        ManualChunk(1, 0, "load_lib load_netlist present_design set_scan_signal set_scan_cfg examine_scan_drc insert_dft_logic"),
        ManualChunk(2, 0, "examine_scan_drc starts the check.", "2.1.3 执行 DRC"),
    )
    assert ManualIndex(chunks).search(["执行 DRC", "examine_scan_drc"])[0] == chunks[1]


def test_group_result_contains_its_primary_command_when_available() -> None:
    chunks = (
        ManualChunk(1, 0, "配置 Scan Chain", "配置 Scan Chain"),
        ManualChunk(2, 0, "set_scan_cfg -chain_count 2", "定义 Scan Chain 的数量和长度"),
    )
    assert ManualIndex(chunks).search_diverse(
        [["配置 Scan Chain", "set_scan_cfg"]], limit=1,
    ) == [chunks[1]]


def test_group_prefers_reference_section_over_example_subsection() -> None:
    chunks = (
        ManualChunk(1, 0, "set_wrapper_cfg -style shared", "2.1.2.6 配置 Wrapper Chain > 定义 Wrapper 策略"),
        ManualChunk(2, 0, "set_wrapper_cfg set_wrapper_cfg set_wrapper_cfg", "2.1.2.6 配置 Wrapper Chain > Wrapper Chain 示例"),
    )
    assert ManualIndex(chunks).search_diverse(
        [["配置 Wrapper Chain", "set_wrapper_cfg"]], limit=1,
    ) == [chunks[0]]


def test_drc_rule_definition_outweighs_a_log_example() -> None:
    chunks = (
        ManualChunk(1, 0, "DFTR9 DFTR9 DFTR9 is shown in a log."),
        ManualChunk(2, 0, "DFTR9 检查时钟打开时时序元件是否正常开启。" + "其他说明" * 80),
    )
    assert ManualIndex(chunks).search(["DFTR9"])[0] == chunks[1]


def test_sentence_fragment_and_table_row_are_not_headings() -> None:
    chunks = _page_chunks(
        "2.2.1 替换寄存器\n1 Total Flip-Flop (FF) Count 400\n移位寄存器中的 DFF\n"
        "将删除之前指定设计的所有相关配置。如果用户指定的设计名称和网表文件中的\n"
        "后续说明。",
        70,
    )
    assert len(chunks) == 1
    assert chunks[0].title == "2.2.1 替换寄存器"


def test_hyphenated_drc_rule_is_a_single_identifier() -> None:
    chunks = (
        ManualChunk(1, 0, "DFTR-L1 Warning Error", "2.1.3.3 调整 DRC 违例等级"),
        ManualChunk(2, 0, "DFTR-L1 检查锁存器规则。", "2.1.3.1 DRC 规则"),
    )
    assert ManualIndex(chunks).search(["DFTR-L1"])[0] == chunks[1]


def test_extraction_failure_is_recorded_not_raised(monkeypatch) -> None:
    class BrokenPage:
        def extract_text(self) -> str:
            raise ValueError("invalid page")

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [BrokenPage()]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))
    assert result == manual.ManualLoadResult(False, None, "manual PDF is unavailable")


def test_page_without_extractable_text_is_available(monkeypatch) -> None:
    class EmptyPage:
        def extract_text(self):
            return None

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [EmptyPage()]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))
    assert result.available is True
    assert result.error is None
    assert result.index == ManualIndex(())


def test_blocked_pdf_construction_returns_at_absolute_deadline(monkeypatch, tmp_path):
    release = threading.Event()

    def blocked_reader(path):
        release.wait(2.0)
        return _Reader()

    monkeypatch.setattr("scan_agent.manual.PdfReader", blocked_reader)
    started = time.monotonic()
    try:
        result = load_manual(tmp_path / "manual.pdf", deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert result.available is False
        assert result.error == "manual PDF loading deadline reached"
    finally:
        release.set()


def test_blocked_page_extraction_returns_at_absolute_deadline(monkeypatch):
    release = threading.Event()

    class BlockingPage:
        def extract_text(self):
            release.wait(2.0)
            return "late text"

    class Reader:
        def __init__(self, path):
            self.pages = [BlockingPage()]

    monkeypatch.setattr("scan_agent.manual.PdfReader", Reader)
    started = time.monotonic()
    try:
        result = load_manual(Path("manual.pdf"), deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert result == manual.ManualLoadResult(False, None, "manual PDF loading deadline reached")
    finally:
        release.set()


def test_page_chunking_checks_deadline_between_lines() -> None:
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="manual page chunking deadline"):
        _page_chunks("one\ntwo\nthree", 1, deadline_monotonic=1.0,
                     clock=lambda: next(ticks))


def test_manual_search_checks_deadline_between_chunks() -> None:
    index = ManualIndex(tuple(ManualChunk(1, number, "scan") for number in range(3)))
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="manual search deadline"):
        index.search(["scan"], deadline_monotonic=1.0, clock=lambda: next(ticks))


def test_manual_index_build_checks_deadline_between_chunks() -> None:
    ticks = iter((0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="manual PDF loading deadline"):
        ManualIndex(
            (ManualChunk(1, 0, "scan"), ManualChunk(1, 1, "scan")),
            _build_deadline=1.0,
            _build_clock=lambda: next(ticks),
        )
