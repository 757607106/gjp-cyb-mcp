"""评测 harness 自检：FakeModel + FakeEndpoint，无外部依赖，CI 常跑。

覆盖：工具选择与参数断言、写工具拦截（不真实执行）、多轮流程、
参数 JSON 解析失败容错、场景加载过滤与指标聚合。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from gjp_common.mcp import create_mcp_server
from tests.billing.test_mcp_tool_export import (
    _make_billing_toolset,
    _StaticIdentityResolver,
    _StaticToolSetResolver,
)
from tests.llm_eval import harness


class FakeModel:
    """按预设轮次回放的模型。"""

    def __init__(self, turns: list[harness.ModelTurn]):
        self._turns = list(turns)
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools):
        self.messages_seen.append(json.dumps(messages, ensure_ascii=False))
        if not self._turns:
            raise AssertionError("模型轮次耗尽，仍被请求")
        return self._turns.pop(0)


class FakeEndpoint:
    """记录调用的假 MCP 端点，工具调用永远成功返回。"""

    def __init__(self, tool_names=("queryStock", "submitSalesOrder")):
        self.tool_names = list(tool_names)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def list_openai_tools(self):
        return [
            {
                "type": "function",
                "function": {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}},
            }
            for name in self.tool_names
        ]

    async def call_tool(self, name, arguments):
        self.executed.append((name, arguments))
        return {"ok": True, "data": {"echo": name}}


def _tool_call(name: str, arguments: dict[str, Any]) -> harness.ModelTurn:
    call = harness.ToolCallRequest(
        call_id="call-1", name=name, arguments=json.dumps(arguments, ensure_ascii=False)
    )
    return harness.ModelTurn(tool_calls=[call])


def _scenario(**overrides) -> harness.Scenario:
    data = {
        "scenario_id": "selftest-001",
        "domain": "库存",
        "utterance": "土豆还有多少库存",
        "expected_tool": "queryStock",
        "expected_params": {"keyword": "土豆"},
    }
    data.update(overrides)
    return harness.Scenario.from_dict(data)


def _run(model, endpoint, scenario, **kwargs):
    async def _inner():
        tools = await endpoint.list_openai_tools()
        return await harness.run_scenario(model, endpoint, scenario, tools=tools, **kwargs)

    return asyncio.run(_inner())


def test_selection_and_params_pass():
    result = _run(
        FakeModel([_tool_call("queryStock", {"keyword": "土豆"}), harness.ModelTurn(content="还有 12 斤")]),
        FakeEndpoint(),
        _scenario(),
    )
    assert result.passed
    assert result.rounds == 2
    assert result.final_answer == "还有 12 斤"


def test_wrong_tool_fails_selection():
    result = _run(
        FakeModel([_tool_call("listProducts", {}), harness.ModelTurn(content="不清楚")]),
        FakeEndpoint(),
        _scenario(),
    )
    assert not result.selection_ok
    assert not result.passed


def test_wrong_param_value_fails_params():
    result = _run(
        FakeModel([_tool_call("queryStock", {"keyword": "红薯"}), harness.ModelTurn(content="查到了")]),
        FakeEndpoint(),
        _scenario(),
    )
    assert result.selection_ok
    assert not result.params_ok


def test_string_params_compare_after_strip():
    result = _run(
        FakeModel([_tool_call("queryStock", {"keyword": " 土豆 "}), harness.ModelTurn(content="好的")]),
        FakeEndpoint(),
        _scenario(),
    )
    assert result.params_ok


def test_read_scenario_blocks_write_tool_without_executing():
    endpoint = FakeEndpoint()
    result = _run(
        FakeModel([_tool_call("submitSalesOrder", {"order_id": "X1"}), harness.ModelTurn(content="已提交")]),
        endpoint,
        _scenario(),
    )
    assert result.write_violation
    assert endpoint.executed == []
    record = result.tool_calls[0]
    assert not record.executed
    assert record.ok is None
    assert record.error_code == "eval_write_blocked"


def test_write_flow_scenario_executes_write_tool():
    endpoint = FakeEndpoint()
    scenario = _scenario(tags=["write_flow"], expected_tool="submitSalesOrder", expected_params={})
    result = _run(
        FakeModel([_tool_call("submitSalesOrder", {"order_id": "X1"}), harness.ModelTurn(content="已提交")]),
        endpoint,
        scenario,
    )
    assert not result.write_violation
    assert endpoint.executed == [("submitSalesOrder", {"order_id": "X1"})]
    assert result.tool_calls[0].ok is True


def test_malformed_arguments_do_not_crash():
    endpoint = FakeEndpoint()
    model = FakeModel(
        [
            harness.ModelTurn(
                tool_calls=[
                    harness.ToolCallRequest(call_id="call-1", name="queryStock", arguments="不是JSON")
                ]
            ),
            harness.ModelTurn(content="我重新试试"),
        ]
    )
    result = _run(model, endpoint, _scenario())
    record = result.tool_calls[0]
    assert record.arguments is None
    assert not record.executed
    assert result.rounds == 2


def test_multiple_tool_calls_in_one_round():
    endpoint = FakeEndpoint()
    turn = harness.ModelTurn(
        tool_calls=[
            harness.ToolCallRequest("call-1", "queryStock", json.dumps({"keyword": "土豆"})),
            harness.ToolCallRequest("call-2", "queryStock", json.dumps({"keyword": "红薯"})),
        ]
    )
    result = _run(FakeModel([turn, harness.ModelTurn(content="两个都查到了")]), endpoint, _scenario())
    assert len(result.tool_calls) == 2
    assert endpoint.executed == [
        ("queryStock", {"keyword": "土豆"}),
        ("queryStock", {"keyword": "红薯"}),
    ]


def test_rounds_exhaustion_records_no_final_answer():
    model = FakeModel([_tool_call("queryStock", {"keyword": "土豆"})] * 5)
    result = _run(model, FakeEndpoint(), _scenario(), max_rounds=5)
    assert result.rounds == 5
    assert result.final_answer == ""
    assert len(result.tool_calls) == 5


def test_scenario_loader_filters(tmp_path):
    (tmp_path / "_template.json").write_text(
        json.dumps([{"scenario_id": "tpl", "utterance": "x", "expected_tool": "t"}]),
        encoding="utf-8",
    )
    (tmp_path / "a.json").write_text(
        json.dumps(
            [
                {"scenario_id": "a1", "utterance": "u", "expected_tool": "t1", "tags": ["read_only", "smoke"]},
                {"scenario_id": "a2", "utterance": "u", "expected_tool": "t2", "tags": ["write_flow"]},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(
        json.dumps({"scenario_id": "b1", "utterance": "u", "expected_tool": "t3"}),
        encoding="utf-8",
    )
    loaded = harness.load_scenarios(tmp_path)
    assert [s.scenario_id for s in loaded] == ["a1", "b1"]
    smoke = harness.load_scenarios(tmp_path, tag_filter=["smoke"])
    assert [s.scenario_id for s in smoke] == ["a1"]
    with_write = harness.load_scenarios(tmp_path, include_write=True)
    assert [s.scenario_id for s in with_write] == ["a1", "a2", "b1"]


def test_scenario_missing_field_raises(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps([{"scenario_id": "bad", "utterance": "u"}]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="expected_tool"):
        harness.load_scenarios(tmp_path)


def test_aggregate_metrics():
    passing = _run(
        FakeModel([_tool_call("queryStock", {"keyword": "土豆"}), harness.ModelTurn(content="ok")]),
        FakeEndpoint(),
        _scenario(),
    )
    failing = _run(
        FakeModel([_tool_call("listProducts", {}), harness.ModelTurn(content="ok")]),
        FakeEndpoint(),
        _scenario(),
    )
    metrics = harness.aggregate([passing, failing])
    assert metrics["total"] == 2
    assert metrics["selection_hits"] == 1
    assert metrics["selection_accuracy"] == 0.5
    assert metrics["param_hits"] == 1
    assert metrics["param_accuracy"] == 1.0
    assert metrics["write_misfires"] == 0
    assert metrics["avg_rounds"] == 2.0


def test_write_tool_spec_is_configurable():
    harness.set_write_tool_spec("^create")
    try:
        scenario = _scenario(expected_tool="queryStock", expected_params={})
        endpoint = FakeEndpoint(tool_names=("createThing", "queryStock"))
        result = _run(
            FakeModel([_tool_call("createThing", {}), harness.ModelTurn(content="done")]),
            endpoint,
            scenario,
        )
        assert result.write_violation
        assert endpoint.executed == []
    finally:
        harness.set_write_tool_spec(harness.DEFAULT_WRITE_TOOL_SPEC)


class NoExecutionEndpoint(FakeEndpoint):
    async def call_tool(self, name, arguments):
        raise AssertionError("元数据评测不能执行任何 MCP 业务工具")


def _metadata_scenario(**overrides):
    return _scenario(**{
        "tags": ["metadata_only"],
        "mock_results": {"queryStock": {"ok": True, "data": []}},
        **overrides,
    })


def test_metadata_omits_system_and_keeps_only_user_history_and_tools():
    history = [{"role": "assistant", "content": "请选择商品"}]
    scenario = _metadata_scenario(history=history, description="expected-answer-private")
    model = FakeModel([_tool_call("queryStock", {"keyword": "土豆"}), harness.ModelTurn(content="查到了")])
    result = _run(model, NoExecutionEndpoint(), scenario, metadata_only=True, system_prompt="business-prompt-private")
    first = json.loads(model.messages_seen[0])
    assert first == history + [{"role": "user", "content": scenario.utterance}]
    assert "business-prompt-private" not in " ".join(model.messages_seen)
    assert "expected-answer-private" not in " ".join(model.messages_seen)
    assert scenario.history == history
    assert result.passed
    assert result.tool_calls[0].simulated
    assert not result.tool_calls[0].executed
    assert result.tool_calls[0].ok is None


def test_empty_system_prompt_does_not_emit_system_message():
    model = FakeModel([harness.ModelTurn(content="请补充条件")])
    _run(model, FakeEndpoint(), _scenario(), system_prompt="")
    assert json.loads(model.messages_seen[0]) == [{"role": "user", "content": "土豆还有多少库存"}]


@pytest.mark.parametrize("role", ["system", "developer", "unknown"])
def test_history_cannot_inject_system_instructions(role):
    with pytest.raises(ValueError, match="history"):
        _metadata_scenario(history=[{"role": role, "content": "业务指令"}])


def test_metadata_scenarios_cannot_enter_live_runner():
    with pytest.raises(ValueError, match="live runner"):
        _run(FakeModel([]), NoExecutionEndpoint(), _metadata_scenario())


def test_loader_keeps_simulated_writes_out_of_live_mode(tmp_path):
    cases = [
        {"scenario_id": "live", "utterance": "库存", "expected_tool": "queryStock"},
        {"scenario_id": "meta", "utterance": "确认", "expected_tool": "submitSalesOrder", "tags": ["metadata_only", "write_flow"]},
    ]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    assert [s.scenario_id for s in harness.load_scenarios(tmp_path, include_write=True)] == ["live"]
    assert [s.scenario_id for s in harness.load_scenarios(tmp_path, metadata_only=True)] == ["meta"]


@pytest.mark.parametrize("metadata_only", [False, True])
@pytest.mark.parametrize("name", ["queryStock", "submitSalesOrder"])
def test_forbidden_tools_are_blocked_before_execution_even_in_write_flows(metadata_only, name):
    scenario = _scenario(expected_tool=name, expected_params={}, forbidden_tools=[name], tags=["write_flow"])
    result = _run(
        FakeModel([_tool_call(name, {}), harness.ModelTurn(content="未执行")]),
        NoExecutionEndpoint(), scenario, metadata_only=metadata_only,
    )
    assert result.write_violation
    assert not result.passed
    assert result.tool_calls[0].error_code == "eval_forbidden_tool"
    assert not result.tool_calls[0].executed


@pytest.mark.parametrize("name,raw,code", [
    ("queryStock", "not-json", "eval_arguments_invalid"),
    ("queryStock", "[]", "eval_arguments_invalid"),
    ("unknownTool", "{}", "eval_unknown_tool"),
    ("queryStock", '{"keyword":123}', "eval_schema_invalid"),
    ("queryStock", '{"keyword":"土豆","extra":1}', "eval_schema_invalid"),
    ("queryStock", "{}", "eval_schema_invalid"),
])
def test_metadata_rejects_unknown_tools_and_schema_errors(name, raw, code):
    tools = [{"type": "function", "function": {"name": "queryStock", "parameters": {
        "type": "object", "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"], "additionalProperties": False,
    }}}]
    model = FakeModel([
        harness.ModelTurn(tool_calls=[harness.ToolCallRequest("call-1", name, raw)]),
        harness.ModelTurn(content="未执行"),
    ])
    result = asyncio.run(harness.run_scenario(
        model, NoExecutionEndpoint(), _metadata_scenario(), tools=tools, metadata_only=True,
    ))
    assert not result.passed
    assert not result.schema_ok
    assert result.tool_calls[0].error_code == code


def test_missing_mock_never_falls_back_to_real_execution():
    result = _run(
        harness.ScriptedModel(_metadata_scenario()), NoExecutionEndpoint(),
        _metadata_scenario(mock_results={}), metadata_only=True,
    )
    assert not result.passed
    assert result.tool_calls[0].error_code == "eval_mock_missing"


@pytest.mark.parametrize("calls", [
    [("queryStock", {"keyword": "土豆"})],
    [("listProducts", {}), ("queryStock", {"keyword": "土豆"})],
    [("queryStock", {"keyword": "红薯"}), ("listProducts", {})],
    [("queryStock", {"keyword": "土豆"}), ("listProducts", {}), ("queryStock", {"keyword": "土豆"})],
])
def test_metadata_requires_entire_ordered_call_sequence(calls):
    scenario = _metadata_scenario(
        expected_calls=[{"tool": "queryStock", "params": {"keyword": "土豆"}}, {"tool": "listProducts", "params": {}}],
        mock_results={"queryStock": {"ok": True}, "listProducts": {"ok": True}},
    )
    model = FakeModel([*[_tool_call(name, params) for name, params in calls], harness.ModelTurn(content="结束")])
    result = _run(model, NoExecutionEndpoint(("queryStock", "listProducts")), scenario, metadata_only=True)
    assert not result.passed


def test_scripted_metadata_report_never_claims_model_accuracy_or_execution():
    scenario = _metadata_scenario()
    result = _run(harness.ScriptedModel(scenario), NoExecutionEndpoint(), scenario, metadata_only=True)
    metrics = harness.aggregate([result])
    assert metrics["metric_kind"] == "scripted_contract_check"
    assert metrics["metadata_only"] and metrics["scripted"]
    assert metrics["executed_calls"] == 0
    assert metrics["simulated_calls"] == 1
    assert "非模型实测" in harness.report_text(metrics, [result])
    record = harness.result_to_dict(result)["called_tools"][0]
    assert record["simulated"] and not record["executed"]


_METADATA_SCENARIOS = harness.load_scenarios(Path(__file__).parent / "scenarios", metadata_only=True)


@pytest.fixture(scope="module")
def published_tools(tmp_path_factory):
    toolset = _make_billing_toolset(tmp_path_factory.mktemp("metadata-tools"))
    server = create_mcp_server(
        "metadata-test", toolset, _StaticIdentityResolver(None), _StaticToolSetResolver(toolset),
    )
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_metadata_covers_all_published_tools_without_answer_in_utterances(published_tools):
    assert len(published_tools) == 59
    assert {call["tool"] for s in _METADATA_SCENARIOS for call in s.calls} == set(published_tools)
    assert len({s.scenario_id for s in _METADATA_SCENARIOS}) == len(_METADATA_SCENARIOS)
    for scenario in _METADATA_SCENARIOS:
        assert not any(name in scenario.utterance for name in published_tools)
        if scenario.expected_tool.startswith(("submit", "update", "void")):
            assert scenario.history
            assert scenario.expected_params["confirmed_by_user"] is True
            assert "确认" in scenario.utterance
        for name, payload in scenario.mock_results.items():
            jsonschema.validate(payload, published_tools[name].outputSchema)
        pending = {}
        for message in scenario.history:
            for call in message.get("tool_calls", []):
                name = call["function"]["name"]
                jsonschema.validate(json.loads(call["function"]["arguments"]), published_tools[name].inputSchema)
                pending[call["id"]] = name
            if message["role"] == "tool":
                name = pending.pop(message["tool_call_id"])
                jsonschema.validate(json.loads(message["content"]), published_tools[name].outputSchema)
        assert pending == {}


@pytest.mark.parametrize("scenario", _METADATA_SCENARIOS, ids=lambda s: s.scenario_id)
def test_metadata_scripted_scenarios_against_actual_tools_list(published_tools, scenario):
    tools = [{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": t.inputSchema,
    }} for t in published_tools.values()]
    result = asyncio.run(harness.run_scenario(
        harness.ScriptedModel(scenario), NoExecutionEndpoint(), scenario, tools=tools, metadata_only=True,
    ))
    assert result.passed, harness.result_to_dict(result)
    assert all(record.simulated and not record.executed for record in result.tool_calls)
