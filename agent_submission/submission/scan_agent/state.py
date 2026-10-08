"""Typed input facts, budgets, and shared workflow state."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict


TaskType = Literal["task1", "task2"]


class AgentStatus(StrEnum):
    SUCCESS = "success"
    TOOL_FAILURE = "tool_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    NO_PROGRESS = "no_progress"
    UNSUPPORTED_NETLIST_REPAIR = "unsupported_netlist_repair"
    COMPLIANCE_FAILURE = "compliance_failure"


@dataclass(frozen=True)
class Budget:
    started_at: float
    deadline_monotonic: float
    max_tool_runs: int
    reserve_seconds: float = 10.0

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline_monotonic - now)


@dataclass(frozen=True)
class InputInventory:
    runtime_files: list[str]


class AgentState(TypedDict):
    input_dir: str
    output_dir: str
    case_id: str
    task_type: TaskType
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
