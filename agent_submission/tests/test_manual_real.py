"""Local quality checks for the supplied manual; CI may omit the PDF."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scan_agent.manual import load_manual
from scan_agent.manual_queries import initial_query_groups


@pytest.fixture(scope="module")
def actual_index():
    manual = Path(__file__).resolve().parents[2] / "Scan_User_Manual.pdf"
    if not manual.is_file():
        pytest.skip("supplied Scan User Manual is not present")
    result = load_manual(manual)
    assert result.available and result.index is not None
    return result.index


def test_actual_manual_sections_and_headers(actual_index) -> None:
    assert len(actual_index.chunks) > 100
    assert all("Scan 引擎用户手册" not in chunk.text for chunk in actual_index.chunks)
    assert any("设置测试使能信号" in chunk.title for chunk in actual_index.chunks)


def test_actual_manual_generation_retrieval_covers_six_topics(actual_index) -> None:
    requirements = SimpleNamespace(
        ctl_files=[], clocks=[{"port": "clk"}], resets=[], constants=[],
        scan_enables=[{"port": "scan_en"}],
        chain_constraints={"chain_count": 2, "max_length": 100},
        partitions=[], clock_domains=[], edge_policy=None, lockup={},
        scan_segments=[], wrapper_settings={}, allowed_drc=[],
        required_outputs=["post_scan.v"],
    )
    groups = initial_query_groups(requirements)
    selected = actual_index.search_diverse(groups)
    assert len(selected) == 6
    assert [chunk.page for chunk in selected] == [13, 18, 28, 53, 62, 68]
    assert all(group[1] in chunk.text for group, chunk in zip(groups, selected))


def test_actual_manual_drc_rule_reaches_definition(actual_index) -> None:
    results = actual_index.search(["DFTR9"], limit=1)
    assert results[0].page == 53
    assert "DFTR9 检查" in results[0].text
    assert actual_index.search(["DFTR-L1"], limit=1)[0].page == 54
    assert actual_index.search(["DFTR-L2"], limit=1)[0].page == 54


def test_actual_manual_special_requirements_get_their_own_passages(actual_index) -> None:
    requirements = SimpleNamespace(
        ctl_files=["core.ctl"], clocks=[{"port": "clk"}], resets=[], constants=[],
        scan_enables=[{"port": "scan_en"}], chain_constraints={"chain_count": 2},
        partitions=[{"name": "part"}], clock_domains=[], edge_policy=None,
        lockup={}, scan_segments=[], wrapper_settings={"style": "shared"},
        allowed_drc=[], required_outputs=["post_scan.v", "scan.ctl"],
    )
    groups = initial_query_groups(requirements)
    selected = actual_index.search_diverse(groups, limit=min(14, len(groups)))
    titles = [chunk.title for chunk in selected]
    assert any(chunk.page == 14 and "load_ctl" in chunk.text and "读入文件" in chunk.title
               for chunk in selected)
    assert any("配置 Wrapper Chain" in title for title in titles)
    assert any("配置 DFT Partition" in title for title in titles)
    assert any("输出 CTL 文件" in title for title in titles)


def test_actual_manual_maximal_requirements_keep_core_and_special_topics(actual_index) -> None:
    requirements = SimpleNamespace(
        ctl_files=["core.ctl"], clocks=[{"port": "clk"}], resets=[], constants=[],
        scan_enables=[{"port": "scan_en"}], chain_constraints={"chain_count": 2},
        partitions=[{"name": "part"}], clock_domains=[], edge_policy=None,
        lockup={"enabled": True}, scan_segments=[{"name": "seg"}],
        wrapper_settings={"style": "shared"}, allowed_drc=["DFTR-L1"],
        required_outputs=["post_scan.v", "scan.ctl", "scan.def"],
    )
    groups = initial_query_groups(requirements)
    assert len(groups) == 14
    selected = actual_index.search_diverse(groups, limit=14)
    assert len(selected) == 14
    assert all(group[1] in chunk.text for group, chunk in zip(groups[:6], selected[:6]))
    assert any("load_ctl" in chunk.text and chunk.page == 14 for chunk in selected)
    assert any("配置 Wrapper Chain" in chunk.title for chunk in selected)
    assert any("配置 DFT Partition" in chunk.title for chunk in selected)
    assert any("set_scan_segment" in chunk.text for chunk in selected)
    assert any("Lockup Latch" in chunk.title for chunk in selected)
    assert any("DFTR-L1\n检查" in chunk.text for chunk in selected)
    assert any("dump_ctl" in chunk.text and "输出 CTL 文件" in chunk.title for chunk in selected)
    assert any("dump_def" in chunk.text and "输出 SCANDEF 文件" in chunk.title for chunk in selected)

    requirements.allowed_drc = ["DFTR9", "DFTR-L1", "DFTR-L2"]
    many_rule_groups = initial_query_groups(requirements)
    assert len(many_rule_groups) == 16
    bounded = actual_index.search_diverse(many_rule_groups, limit=14)
    assert any("dump_ctl" in chunk.text and "输出 CTL 文件" in chunk.title for chunk in bounded)
    assert any("dump_def" in chunk.text and "输出 SCANDEF 文件" in chunk.title for chunk in bounded)
