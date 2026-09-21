"""评测 harness 自检：FakeModel + FakeEndpoint，无外部依赖，CI 常跑。

覆盖：工具选择与参数断言、写工具拦截（不真实执行）、多轮流程、
参数 JSON 解析失败容错、场景加载过滤与指标聚合。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

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
