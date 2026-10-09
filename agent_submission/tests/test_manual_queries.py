from types import SimpleNamespace

from scan_agent.manual_queries import initial_query_groups, repair_query_groups


def _requirements(**changes):
    values = {
        "ctl_files": [], "clocks": [{"port": "clk"}], "resets": [],
        "constants": [], "scan_enables": [{"port": "se"}],
        "chain_constraints": {"chain_count": 2, "max_length": 100},
        "partitions": [], "clock_domains": [], "edge_policy": None,
        "lockup": {}, "scan_segments": [], "wrapper_settings": {},
        "allowed_drc": [], "required_outputs": ["post_scan.v"],
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_initial_queries_cover_six_dofile_intents() -> None:
    groups = initial_query_groups(_requirements())
    assert len(groups) == 6
    assert "load_netlist" in groups[0]
    assert "读入文件" in groups[0]
    assert "set_scan_signal" in groups[1]
    assert "设置测试使能信号" in groups[1]
    assert "set_scan_cfg" in groups[2]
    assert "配置 Scan Chain" in groups[2]
    assert "examine_scan_drc" in groups[3]
    assert "执行 DRC" in groups[3]
    assert "insert_dft_logic" in groups[4]
    assert "插入 Scan Chain" in groups[4]
    assert "dump_netlist" in groups[5]
    assert "输出网表" in groups[5]


def test_initial_queries_include_only_relevant_special_topics() -> None:
    groups = initial_query_groups(_requirements(
        ctl_files=["core.ctl"], wrapper_settings={"style": "shared"},
        partitions=[{"name": "p"}], lockup={"add_lockup": True},
        allowed_drc=["DFTR9"], required_outputs=["scan.ctl", "post_scan.v"],
    ))
    assert ["读入文件", "load_ctl"] in groups
    assert ["配置 Wrapper Chain", "set_wrapper_cfg"] in groups
    assert ["配置 DFT Partition", "add_scan_partition"] in groups
    assert ["定义 Lockup Latch", "set_scan_cfg"] in groups
    assert ["DFTR9"] in groups
    assert ["输出 CTL 文件", "dump_ctl"] in groups


def test_required_outputs_precede_extra_drc_rules_at_context_limit() -> None:
    groups = initial_query_groups(_requirements(
        ctl_files=["core.ctl"], wrapper_settings={"style": "shared"},
        partitions=[{"name": "p"}], scan_segments=[{"name": "s"}],
        lockup={"enabled": True}, allowed_drc=["DFTR9", "DFTR-L1", "DFTR-L2"],
        required_outputs=["post_scan.v", "scan.ctl", "scan.def"],
    ))
    assert len(groups) == 16
    assert ["输出 CTL 文件", "dump_ctl"] in groups[:14]
    assert ["输出 SCANDEF 文件", "dump_def"] in groups[:14]
    assert [group[1] for group in groups[:6]] == [
        "load_lib", "set_scan_signal", "set_scan_cfg", "examine_scan_drc",
        "insert_dft_logic", "dump_netlist",
    ]


def test_repair_queries_keep_distinct_diagnostic_intents() -> None:
    groups = repair_query_groups(_requirements(), ["DFTR9", "set_scan_signal", "DFTR9"])
    assert groups == [["DFTR9"], ["set_scan_signal"]]
    assert repair_query_groups(_requirements(), []) == initial_query_groups(_requirements())
