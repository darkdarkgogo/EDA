# Scan Insertion Agent 第一阶段设计

## 1. 目标

在保留官方 `reference_submission` 不变的前提下，新建 `agent_submission`，实现一个可无人值守运行的 Scan Insertion Agent。系统使用 LangGraph 编排显式状态流，直接通过 OpenAI 兼容接口调用评测指定的 DeepSeek V4 Pro，并由确定性 Python 代码负责工具执行、预算控制、结果验证、证据归档和最终输出。

第一阶段覆盖：

- 任务一：根据 `task_spec.md`、输入网表、Liberty 库和工具手册生成 dofile，并根据真实工具结果自动修复。
- 任务二：从 `original.dofile` 出发，定位并修复 dofile 执行问题、DRC 问题和不符合任务要求的 DFT 配置。
- 最多按 case 限制或默认三轮调用 `dftexp_scan`。
- 生成符合赛题附录要求的 `runs/`、`final_results/`、`diffs/` 和 `decision_log.json`。

第一阶段不自动修改 Pre-scan 网表。当系统判断问题必须修改网表才能解决时，应停止继续尝试 dofile 修复，将状态记为 `unsupported_netlist_repair`，保留完整证据并如实失败。网表修改和 Yosys EQY 将作为第二阶段能力加入。

## 2. 非目标

- 不训练或微调任何模型。
- 不调用评测规定之外的大语言模型。
- 不引入多智能体讨论、投票或子 Agent。
- 不修改 `/input` 中的任何文件。
- 不伪造网表、日志、DRC、扫描链、Wrapper 或 LEC 报告。
- 不把 public case 的 `golden.dofile` 或 `preset_issues.json` 当作 Hidden case 运行时依赖。
- 不在第一阶段实现 Pre-scan 网表自动编辑或 LEC。

## 3. 技术选择

### 3.1 编排

使用 LangGraph 作为薄状态机，只承担：

- 节点之间的显式状态传递；
- 条件路由；
- dofile 修复循环；
- 失败和完成终态管理。

不依赖 LangChain 的高层 Agent、文件工具或自主 Shell Agent。所有有副作用的操作由本项目的 Python 工具层实现。

### 3.2 模型调用

使用 `openai.OpenAI` 客户端，通过以下环境变量连接 DeepSeek V4 Pro：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

模型仅负责需求抽取、dofile 生成、未知问题诊断和 dofile 修复。程序性判断由 Python 完成。

### 3.3 依赖

第一阶段依赖控制为：

- `openai`
- `langgraph`
- `pypdf`
- `pytest` 仅用于测试镜像或开发环境，不要求进入最终运行镜像

不引入向量数据库、embedding 模型、AutoGen、Hermes 或 Claude Code 运行时。

## 4. 目录结构

```text
agent_submission/
├── .env.example
├── .dockerignore
├── Dockerfile
├── README.md
├── docs/
│   └── superpowers/
│       ├── specs/
│       └── plans/
├── submission/
│   ├── agent_system
│   ├── main.py
│   ├── requirements.txt
│   └── scan_agent/
│       ├── __init__.py
│       ├── artifacts.py
│       ├── diagnostics.py
│       ├── dofile.py
│       ├── inputs.py
│       ├── llm.py
│       ├── manual.py
│       ├── runner.py
│       ├── state.py
│       ├── validation.py
│       └── workflow.py
└── tests/
    ├── fixtures/
    ├── test_artifacts.py
    ├── test_diagnostics.py
    ├── test_dofile.py
    ├── test_inputs.py
    ├── test_runner.py
    ├── test_validation.py
    └── test_workflow.py
```

每个模块只有一个主要职责：

- `inputs.py`：等待 case、清点输入、判断任务类型、解析任务书和限制。
- `manual.py`：提取工具手册、分块并进行轻量关键词检索。
- `llm.py`：模型调用、JSON 解析、重试和用量记录。
- `dofile.py`：dofile 清理、安全检查、版本保存和 diff。
- `runner.py`：以受控工作目录运行真实 `dftexp_scan`，保存退出码、日志和耗时。
- `diagnostics.py`：解析错误、DRC 和工具阶段信息。
- `validation.py`：根据任务要求和真实报告执行硬性验收。
- `artifacts.py`：维护轮次目录、最终结果和 `decision_log.json`。
- `workflow.py`：声明 LangGraph 节点和条件边。

## 5. 状态模型

共享状态使用 `TypedDict`，保存原始事实而非拼接后的 Prompt：

```python
class AgentState(TypedDict):
    input_dir: str
    output_dir: str
    case_id: str
    task_type: Literal["task1", "task2"]
    input_inventory: dict[str, list[str]]
    protected_hashes: dict[str, str]
    requirements: dict[str, object]
    manual_chunks: list[dict[str, object]]
    current_run: int
    max_tool_runs: int
    deadline_monotonic: float
    current_dofile: str
    previous_dofile: str | None
    tool_result: dict[str, object] | None
    diagnostics: list[dict[str, object]]
    validation_results: list[dict[str, object]]
    repair_history: list[dict[str, object]]
    final_run: str | None
    status: str
    failure_reason: str | None
```

日志全文和大文件路径保存在文件系统，状态中只保存路径、摘要和结构化事实，避免重复占用 LLM 上下文。

## 6. 工作流

```text
wait_case_ready
  -> inventory_input
  -> classify_task
  -> extract_requirements
  -> load_manual
  -> create_initial_dofile
  -> prepare_run
  -> run_dftexp_scan
  -> collect_artifacts
  -> parse_evidence
  -> validate
      -> finalize_success
      -> diagnose_and_repair -> prepare_run
      -> finalize_failure
```

### 6.1 等待输入

`agent_system` 启动 Python 后，程序等待 `${input_dir}/.case_ready`。为了兼容本地 public case 测试，可通过显式环境变量 `SCAN_AGENT_SKIP_READY_WAIT=1` 跳过等待；正式镜像默认不跳过。

等待阶段不读取尚未完整挂载的 case 文件。哨兵出现后记录单调时钟，作为内部预算起点。

### 6.2 输入识别

任务类型按输入事实判断：

- 存在 `original.dofile`：任务二。
- 不存在 `original.dofile`：任务一。

程序不读取 public case 中的 `golden.dofile` 和 `preset_issues.json`。测试代码可以用它们计算离线质量指标，但正式工作流不得加载它们。

启动后计算 `/input` 全部普通文件的 SHA-256。终止前重新计算，任何变化都记为合规失败。

### 6.3 需求抽取

模型把 `task_spec.md` 和 `limitations.md` 转换为结构化要求，至少包含：

- 顶层模块；
- 输入网表、库和 CTL；
- 时钟、复位、常量和 scan enable；
- 扫描链数量、最大长度、分区、时钟域和边沿策略；
- lockup、scan segment 和 wrapper 要求；
- 允许忽略的 DRC；
- 是否允许修改网表；
- 必需输出文件；
- wall time 和工具调用上限。

Python 对 JSON 做类型、范围和输入文件存在性校验。无法解析时进行一次纠错调用；再次失败则终止，不使用含糊默认值继续运行。

### 6.4 首版 dofile

- 任务一：根据结构化要求、输入清单和手册片段生成完整 dofile。
- 任务二：把 `original.dofile` 作为首个待修对象，先进行静态检查，再由模型生成修复后的第一轮 dofile。

所有文件路径指向当前轮次工作目录可访问的输入或输出。禁止 dofile 写入 `/input`、`/submission` 和 `/opt`。

### 6.5 工具运行

每轮建立：

```text
runs/Rn/
├── Rn.log
├── run_metadata.json
├── deliverables/
│   └── Rn.dofile
├── reports/
└── work/
```

`dftexp_scan` 在 `runs/Rn/work` 中运行。`runner.py` 记录：

- 精确参数；
- 开始和结束单调时间；
- wall time；
- 退出码；
- 是否超时；
- 实际产生的文件清单及 SHA-256。

工具不可执行、License 失败、超时或非零退出都是真实失败，绝不转换为模拟成功。

### 6.6 证据解析与验证

解析内容包括：

- 致命 `ERROR` 和命令失败；
- DRC 规则、对象、数量和严重级别；
- 插链阶段是否完成；
- 扫描链数量、长度、分区、时钟和覆盖率；
- 任务要求的报告和交付物是否真实存在、非空；
- 日志及报告中是否出现任务明确允许的固有 DRC。

成功必须同时满足：

- `dftexp_scan` 退出码为 0；
- 无导致流程失败的错误；
- 所有不允许的 DRC 归零；
- 可机器验证的扫描结构要求通过；
- `task_spec.md` 要求的交付物存在且非空；
- `/input` 哈希未改变。

模型的“成功”描述不参与最终布尔判断。

### 6.7 修复循环

修复顺序：

1. Python 静态规则识别确定性问题并生成诊断建议。
2. 检索与问题相关的手册片段。
3. 把结构化需求、当前 dofile、精简证据、相关手册和历史尝试交给模型。
4. 模型返回 JSON 诊断、证据、修复说明和完整新 dofile。
5. Python 检查候选 dofile 的安全性和完整性。
6. 保存 diff，进入下一轮真实工具运行。

同一诊断和同一 dofile 哈希连续出现时视为无进展，终止循环，避免浪费预算。

## 7. 预算控制

- `limitations.md` 明确给出工具调用次数时严格采用该值。
- 未给出工具调用次数时默认最多三轮。
- 每轮开始前检查剩余 wall time。
- 为证据归档和 `decision_log.json` 保留至少十秒。
- 单轮工具超时为剩余预算减去十秒，且不得为负数。
- 单次 LLM 请求最多重试两次；API 重试不计为 EDA 工具轮次。
- 每次模型响应记录模型名和 API 返回的 token usage；不记录或输出密钥。

## 8. 手册检索

默认手册路径为 `/opt/dftexp_scan/doc/Scan_User_Manual.pdf`。使用 `pypdf` 提取文本，按页和命令标题切块，并建立小写关键词倒排索引。

查询词来自：

- 当前 DRC 编号；
- 失败命令；
- 任务要求中的 Scan、Wrapper、Partition、Segment 等关键配置；
- 诊断器提取的端口或选项名。

每次模型调用只加入最相关的有限片段。手册缺失或无法解析时，系统记录该事实并使用内置最小规则继续；不得声称检索到了不存在的内容。

## 9. LLM 接口约束

关键响应均要求 JSON Schema 形态的数据，不接受仅有自由文本的决定。修复响应至少包含：

```json
{
  "problem_type": "dofile|drc|configuration|requires_netlist_repair",
  "root_cause": "具体根因",
  "evidence": [
    {"source": "runs/R1/R1.log", "locator": "可复查定位"}
  ],
  "repair_summary": "本轮修改及原因",
  "dofile": "完整的新dofile"
}
```

Python 拒绝以下候选：

- 不是有效 JSON；
- dofile 为空；
- 写入 `/input`、`/submission` 或 `/opt`；
- 调用与任务无关的 Shell 或外部程序；
- 删除核心输入加载、DRC、插链或输出阶段且没有等价替代；
- 重复已失败的 dofile 内容。

## 10. 输出与审计

输出严格遵循赛题指南附录：

```text
output/
├── decision_log.json
├── runs/
│   ├── R1/
│   └── R2/
├── final_results/
│   ├── final.log
│   ├── deliverables/
│   └── reports/
└── diffs/
```

`final_results` 只从一个真实通过验证的 `runs/Rn` 复制。没有成功轮次时不生成假的 `post_scan.v` 或报告；仍生成失败版 `decision_log.json`，并保留所有真实轮次证据。

`decision_log.json` 中：

- `final_run` 必须指向真实存在且通过验证的轮次；失败时为 `null`。
- `requirement_mapping` 逐条记录要求、配置和可复查引用。
- `issue_resolutions` 记录 found、diagnosis、fix、verify 闭环。
- `tool_runs` 与 `runs/Rn` 一一对应。
- `file_changes` 对应真实 diff。
- 所有路径相对于 `output`。

完成前执行引用闭合检查：所有引用路径必须存在，locator 必须非空，`final_results` 文件哈希必须与 `final_run` 来源一致。

## 11. 错误处理

终态分为：

- `success`：存在完全通过验证的 final run。
- `tool_failure`：工具、License 或输入导致无法完成。
- `budget_exhausted`：时间或工具轮次耗尽。
- `invalid_model_output`：模型连续返回无法校验的结果。
- `no_progress`：修复未产生新候选或重复同一失败。
- `unsupported_netlist_repair`：证据表明必须修改网表，超出第一阶段范围。
- `compliance_failure`：输入被修改或出现禁止操作。

所有失败都必须产生非零进程退出码、真实日志和失败版 `decision_log.json`。

## 12. 测试策略

### 12.1 单元测试

覆盖：

- `.case_ready` 等待与本地跳过开关；
- task1/task2 判断；
- `limitations.md` 时间和调用次数解析；
- 手册切块和关键词检索；
- dofile 安全规则；
- 日志、ERROR 和 DRC 提取；
- 成功/修复/失败路由；
- diff、final copy 和引用闭合；
- `/input` 哈希保护。

### 12.2 模拟集成测试

测试提供一个假的 `dftexp_scan` 可执行程序，但只用于测试代码路径，并明确生成测试夹具，不伪装成真实 EDA 成功。场景包括：

- 第一轮成功；
- 第一轮命令错误、第二轮成功；
- DRC 未清零；
- 工具超时；
- License 失败；
- 必需输出缺失；
- 修复无进展；
- 达到工具调用上限；
- 发现必须修改网表。

### 12.3 真实验收

有 License 的 Docker 环境中按以下顺序运行：

1. 任务一最小 public case。
2. 任务二最小 public case。
3. 其余 public case。
4. openC906 和 VeeR EH1 大型 case。
5. 对全部输出执行结构、哈希、引用和必需文件审计。

## 13. 第一阶段完成标准

- Docker 镜像能通过 `/submission/agent_system -input /input -output /output` 无人值守启动。
- 正式模式正确等待 `.case_ready`。
- 工作流不读取 `golden.dofile` 和 `preset_issues.json`。
- 每次 `dftexp_scan` 调用都有独立、完整、真实的轮次记录。
- 工具失败不会被标记为成功，也不会产生模拟报告或空壳网表。
- dofile 修复循环受时间和轮次预算约束。
- 成功输出符合正式附录；失败输出可审计且进程返回非零。
- `/input` 在运行前后哈希一致。
- 无 License 的自动化测试全部通过。
- 有 License 时，以全部 public case 的真实结果作为最终验收依据。

## 14. 第二阶段接口预留

第一阶段状态保留 `requires_netlist_repair` 诊断类型。第二阶段将新增：

- 受限网表复制和编辑；
- 每轮 `pre_scan_Rn.v`；
- 原始网表到修改网表的 diff；
- EQY 配置、调用和报告解析；
- `pre_scan_final.v` 和 `lec_report.rpt`；
- 只有 LEC 通过的网表轮次才允许成为 final run。

这些能力不影响第一阶段的 dofile-only 工作流接口。
