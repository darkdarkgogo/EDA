"""Deterministic retrieval intents for generation and repair."""

from collections.abc import Sequence

from .llm import Requirements


def initial_query_groups(requirements: Requirements) -> list[list[str]]:
    """Cover the required dofile phases while adding supplied task details."""
    setup = ["读入文件", "load_lib", "load_netlist", "present_design"]
    if requirements.ctl_files:
        setup.append("load_ctl")

    signals = ["设置测试使能信号" if requirements.scan_enables else "定义测试时钟信号", "set_scan_signal"]
    if requirements.clocks:
        signals.append("clock")
    if requirements.resets:
        signals.append("reset")
    if requirements.constants:
        signals.append("constant")
    if requirements.scan_enables:
        signals.append("scan_enable")

    chain = ["配置 Scan Chain", "set_scan_cfg"]
    chain.extend(key for key in ("chain_count", "max_length") if key in requirements.chain_constraints)
    if requirements.clock_domains:
        chain.append("mix_clocks")
    if requirements.edge_policy:
        chain.append("mix_edges")
    drc = ["执行 DRC", "examine_scan_drc"]
    insertion = ["插入 Scan Chain", "insert_dft_logic", "examine_scan_chain"]
    outputs = ["输出网表", "dump_netlist", "rpt_scan_signal", "rpt_scan_cfg", "rpt_scan_chain"]
    base_groups = [setup, signals, chain, drc, insertion, outputs]

    special_groups = []
    if requirements.ctl_files:
        special_groups.append(["读入文件", "load_ctl"])
    if requirements.wrapper_settings:
        special_groups.append(["配置 Wrapper Chain", "set_wrapper_cfg"])
    if requirements.partitions:
        special_groups.append(["配置 DFT Partition", "add_scan_partition"])
    if requirements.scan_segments:
        special_groups.append(["识别已有插链信息", "set_scan_segment"])
    if requirements.lockup:
        special_groups.append(["定义 Lockup Latch", "set_scan_cfg"])
    suffixes = {name.lower().rsplit(".", 1)[-1] for name in requirements.required_outputs}
    if "ctl" in suffixes:
        special_groups.append(["输出 CTL 文件", "dump_ctl"])
    if "scandef" in suffixes or "def" in suffixes:
        special_groups.append(["输出 SCANDEF 文件", "dump_def"])
    special_groups.extend([[rule] for rule in dict.fromkeys(requirements.allowed_drc)])
    return [*base_groups, *special_groups]


def repair_query_groups(requirements: Requirements, terms: Sequence[str]) -> list[list[str]]:
    """Give each distinct diagnostic identifier its own retrieval slot."""
    distinct = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    return [[term] for term in distinct] if distinct else initial_query_groups(requirements)
