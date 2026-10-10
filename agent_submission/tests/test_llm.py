import json
import sys
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from scan_agent.llm import (
    LLMClient, LLMConfigurationError, LLMOutputError, LLMTransportError,
    Requirements, extract_requirements,
)
from scan_agent.manual import ManualChunk
from scan_agent.state import InputInventory


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    @property
    def calls(self):
        return len(self.requests)

    def __call__(self, **request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def requirement_data():
    return {
        "task_type": "task1", "top_module": "top", "netlists": ["pre_scan.v"],
        "libraries": ["cells.lib"], "ctl_files": [], "clocks": [{"port": "clk", "off_state": 0}],
        "resets": [], "constants": [], "scan_enables": [{"port": "scan_en", "off_state": 0, "view": "spec", "usage": "all"}],
        "chain_constraints": {"chain_count": 2, "max_length": 100},
        "partitions": [], "clock_domains": [], "edge_policy": None,
        "lockup": {}, "scan_segments": [], "wrapper_settings": {},
        "allowed_drc": [], "required_outputs": ["post_scan.v"],
        "allow_netlist_modification": False, "wall_time_seconds": 120,
        "max_tool_runs": 3,
    }


def extract(transport):
    return extract_requirements(
        LLMClient(transport=transport, model="test-model"), "Scan top",
        "wall time: 120 seconds", InputInventory(["pre_scan.v", "cells.lib"]),
        [ManualChunk(2, 0, "load_lib loads a Liberty file")],
    )


def test_manual_data_includes_nonempty_title():
    from scan_agent.llm import manual_data

    assert manual_data([ManualChunk(18, 0, "set_scan_signal", "设置测试使能信号")]) == [
        {"page": 18, "chunk_index": 0, "text": "set_scan_signal", "title": "设置测试使能信号"},
    ]


def test_complete_json_retries_once_after_invalid_json():
    transport = FakeTransport(["not-json", '{"task_type":"task1"}'])
    result = LLMClient(transport=transport, model="deepseek-v4-pro", max_retries=2).complete_json("system", "user")
    assert result.data == {"task_type": "task1"}
    assert transport.calls == 2
    assert "not-json" in transport.requests[1]["messages"][-1]["content"]


@pytest.mark.parametrize("text", ['```json\n{"ok":true}\n```', '{"ok":true}'])
def test_single_json_fence_is_supported(text):
    assert LLMClient(transport=FakeTransport([text]), model="m").complete_json("s", "u").data == {"ok": True}


@pytest.mark.parametrize("text", [
    'prefix {"ok":true}', '```json\n```json\n{"ok":true}\n```\n```',
    '```python\n{"ok":true}\n```', '[]', '{"n":NaN}', '{"x":1,"x":2}', '{"n":1e999}',
])
def test_invalid_response_fails_after_exactly_one_correction(text):
    transport = FakeTransport([text, text])
    with pytest.raises(LLMOutputError):
        LLMClient(transport=transport, model="m").complete_json("s", "u")
    assert transport.calls == 2


def test_usage_is_recorded_including_rejected_response():
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        for text in ["invalid", '{"ok":true}']
    ]
    client = LLMClient(transport=FakeTransport(responses), model="m")
    result = client.complete_json("s", "u")
    assert result.model == "m"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 15
    assert len(client.usage_history) == 2


def test_mapping_api_response_and_missing_usage():
    transport = FakeTransport([{"choices": [{"message": {"content": '{"ok":true}'}}]}])
    assert LLMClient(transport=transport, model="m").complete_json("s", "u").usage is None


def test_transport_errors_have_bounded_retries_and_hide_error_secrets():
    transport = FakeTransport([RuntimeError("API-key-secret")] * 3)
    with pytest.raises(LLMTransportError) as error:
        LLMClient(transport=transport, model="m", max_retries=2).complete_json("s", "u")
    assert transport.calls == 3
    assert "API-key-secret" not in str(error.value)


@pytest.mark.parametrize("missing", ["LLM_API_KEY"])
def test_from_env_rejects_missing_configuration_before_constructing_sdk(monkeypatch, missing):
    for key, value in {"LLM_API_KEY": "key", "LLM_BASE_URL": "https://example.test/v1", "LLM_MODEL": "m"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(missing, " ")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: pytest.fail("SDK constructed before env validation")))
    with pytest.raises(LLMConfigurationError, match=missing):
        LLMClient.from_env()


def test_from_env_defaults_endpoint_and_model(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    created = {}

    def sdk(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_request: None)))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=sdk))
    client = LLMClient.from_env()

    assert created == {
        "api_key": "key",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "max_retries": 0,
    }
    assert client.model == "deepseek-v4-pro"


def test_from_env_wraps_chat_completions_and_disables_sdk_retries(monkeypatch):
    for key, value in {"LLM_API_KEY": "key", "LLM_BASE_URL": "https://example.test/v1", "LLM_MODEL": "m"}.items():
        monkeypatch.setenv(key, value)
    requests = []
    def sdk(**kwargs):
        assert kwargs == {"api_key": "key", "base_url": "https://example.test/v1", "max_retries": 0}
        def create(**request):
            requests.append(request)
            return '{"ok":true}'
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=sdk))
    assert LLMClient.from_env().complete_json("s", "u").data == {"ok": True}
    assert requests[0]["model"] == "m"
    assert requests[0]["response_format"] == {"type": "json_object"}


def test_dashscope_deepseek_disables_default_thinking(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    requests = []

    def sdk(**_kwargs):
        def create(**request):
            requests.append(request)
            return '{"ok":true}'
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=sdk))
    assert LLMClient.from_env().complete_json("s", "u").data == {"ok": True}
    assert requests[0]["extra_body"] == {"enable_thinking": False}


def test_extract_returns_every_typed_field_and_serializes_only_supplied_facts():
    transport = FakeTransport([json.dumps(requirement_data())])
    result = extract(transport)
    assert isinstance(result, Requirements)
    assert asdict(result) == requirement_data()
    payload = json.loads(transport.requests[0]["messages"][1]["content"])
    assert set(payload) == {"task_text", "limitations_text", "inventory", "manual_chunks"}
    assert payload["inventory"] == {"runtime_files": ["pre_scan.v", "cells.lib"]}
    assert payload["manual_chunks"] == [{"page": 2, "chunk_index": 0, "text": "load_lib loads a Liberty file"}]
    assert "Each partitions item has exactly name" in transport.requests[0]["messages"][0]["content"]


def test_extract_canonicalizes_only_inventoried_input_path_spellings():
    data = {**requirement_data(), "netlists": ["input/pre_scan.v"], "libraries": ["/input/cells.lib"]}
    transport = FakeTransport([json.dumps(data)])
    result = extract(transport)
    assert result.netlists == ["pre_scan.v"]
    assert result.libraries == ["cells.lib"]
    assert transport.calls == 1


@pytest.mark.parametrize("change", [
    {"netlists": ["missing.v"]}, {"allow_netlist_modification": "false"},
    {"chain_constraints": {"chain_count": True}}, {"chain_constraints": {"max_length": 0}},
    {"required_outputs": ["../post_scan.v"]}, {"wall_time_seconds": 999},
    {"max_tool_runs": 99}, {"clocks": ["clk"]},
    {"clocks": [{"port": "clk", "off_state": 2}]},
    {"clocks": [{"port": "clk", "off_state": 0, "surprise": True}]},
    {"scan_enables": [{"port": "scan_en"}]},
    {"constants": [{"port": "test_mode"}]},
    {"lockup": {"add_lockup": True}},
    {"lockup": {"add_lockup": "true", "insert_terminal_lockup": False}},
    {"wrapper_settings": {"chain_count": 1, "chain_length": 10}},
    {"wrapper_settings": {"chain_count": 1, "chain_length": 10, "style": "dedicated", "extra": 1}},
])
def test_requirement_schema_or_input_failure_gets_one_correction(change):
    transport = FakeTransport([json.dumps({**requirement_data(), **change}), json.dumps(requirement_data())])
    assert extract(transport).top_module == "top"
    assert transport.calls == 2
    assert "validation_errors" in transport.requests[1]["messages"][-1]["content"]


def test_task_type_is_taken_from_runtime_inventory():
    transport = FakeTransport([json.dumps({**requirement_data(), "task_type": "task2"})])
    assert extract(transport).task_type == "task1"
    assert transport.calls == 1


def test_one_correction_reports_all_record_key_errors() -> None:
    bad = deepcopy(requirement_data())
    bad["clocks"] = [{"name": "clk"}]
    bad["resets"] = [{"name": "rst_n", "active_low": True}]
    bad["scan_enables"] = [{"name": "scan_en", "off_state": 0}]
    transport = FakeTransport([json.dumps(bad), json.dumps(requirement_data())])
    assert extract(transport).top_module == "top"
    errors = json.loads(transport.requests[1]["messages"][-1]["content"])["validation_errors"]
    assert all(field in errors for field in ("clocks[0]", "resets[0]", "scan_enables[0]"))


@pytest.mark.parametrize("field", list(requirement_data()))
def test_missing_requirement_field_fails_closed(field):
    data = requirement_data()
    del data[field]
    transport = FakeTransport([json.dumps(data)] * 2)
    with pytest.raises(LLMOutputError, match=field):
        extract(transport)
    assert transport.calls == 2


def test_invalid_limits_fail_before_model_call():
    transport = FakeTransport([])
    with pytest.raises(ValueError):
        extract_requirements(LLMClient(transport=transport, model="m"), "task", "", InputInventory([]), [])
    assert transport.calls == 0


@pytest.mark.parametrize("field", ["wall_time_seconds", "max_tool_runs"])
def test_oversized_numeric_requirements_use_the_single_schema_correction(field):
    bad = {**requirement_data(), field: 10**400}
    transport = FakeTransport([json.dumps(bad), json.dumps(requirement_data())])
    assert extract(transport).wall_time_seconds == 120
    assert transport.calls == 2
    errors = json.loads(transport.requests[1]["messages"][-1]["content"])
    assert field in errors["validation_errors"]


@pytest.mark.parametrize("field", ["wall_time_seconds", "max_tool_runs"])
def test_repeated_oversized_numeric_requirements_fail_as_llm_output_error(field):
    bad = {**requirement_data(), field: 10**400}
    transport = FakeTransport([json.dumps(bad)] * 2)
    with pytest.raises(LLMOutputError, match=field):
        extract(transport)
    assert transport.calls == 2


def test_prompt_builders_ignore_extra_fields_on_fact_subclasses():
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class ExtraInventory(InputInventory):
        private_text: str = "must not serialize"
    @dataclass(frozen=True)
    class ExtraChunk(ManualChunk):
        private_text: str = "must not serialize"
    transport = FakeTransport([json.dumps(requirement_data())])
    extract_requirements(LLMClient(transport=transport, model="m"), "task", "wall time: 120 seconds",
                         ExtraInventory(["pre_scan.v", "cells.lib"]), [ExtraChunk(1, 0, "load_lib")])
    assert "must not serialize" not in transport.requests[0]["messages"][1]["content"]
