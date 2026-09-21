"""LLM 工具识别评测（真实模型，默认整体跳过）。

同时设置以下环境变量后启用（服务为本地拉起的真实子进程，上游为真实 ERP）：

    ERP_BILLING_EVAL_API_KEY=<X-API-Key，真实 ERP Token>
    ERP_BILLING_EVAL_LLM_BASE_URL=<OpenAI 兼容基地址>
    ERP_BILLING_EVAL_LLM_API_KEY=<模型 Key>
    ERP_BILLING_EVAL_LLM_MODEL=<模型名，如 qwen-plus>

可选：

    ERP_BILLING_EVAL_MCP_URL — 已部署服务地址；不设则本地拉起子进程
    ERP_BILLING_EVAL_ERP_BASE_URL — 本地拉起时的 ERP 基地址
    ERP_BILLING_EVAL_TAGS — 逗号分隔标签过滤
    ERP_BILLING_EVAL_MAX_ROUNDS — 单场景最大轮次，默认 5

场景默认只读；只读场景里模型误调写工具会被拦截且不真实执行。
评测结束后汇总指标写入 tests/llm_eval/reports/ 并打印明细。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from tests.llm_eval import harness

_EVAL_DIR = Path(__file__).resolve().parent
# 评测凭据可固化在 config/local.env（已 gitignore），环境变量始终优先
harness.load_env_defaults(
    _EVAL_DIR.parents[2] / "config" / "local.env", prefix="ERP_BILLING_EVAL_"
)

_API_KEY = os.environ.get("ERP_BILLING_EVAL_API_KEY", "").strip()
_LLM_BASE_URL = os.environ.get("ERP_BILLING_EVAL_LLM_BASE_URL", "").strip()
_LLM_API_KEY = os.environ.get("ERP_BILLING_EVAL_LLM_API_KEY", "").strip()
_LLM_MODEL = os.environ.get("ERP_BILLING_EVAL_LLM_MODEL", "").strip()

pytestmark = pytest.mark.skipif(
    not (_API_KEY and _LLM_BASE_URL and _LLM_API_KEY and _LLM_MODEL),
    reason="需要 ERP_BILLING_EVAL_API_KEY 与 ERP_BILLING_EVAL_LLM_* 环境变量",
)

_RESULTS: list[harness.ScenarioResult] = []


def _scenarios():
    return harness.load_scenarios(
        _EVAL_DIR / "scenarios",
        tag_filter=(
            os.environ.get("ERP_BILLING_EVAL_TAGS", "").split(",")
            if os.environ.get("ERP_BILLING_EVAL_TAGS", "").strip()
            else None
        ),
    )


@pytest.fixture(scope="module")
def mcp_url():
    """优先连接已部署服务，否则本地拉起真实 uvicorn 子进程。"""
    deployed = os.environ.get("ERP_BILLING_EVAL_MCP_URL", "").strip()
    if deployed:
        yield deployed.rstrip("/")
        return
    service = harness.SpawnedService(
        project_root=_EVAL_DIR.parents[2],
        erp_base_url=os.environ.get(
            "ERP_BILLING_EVAL_ERP_BASE_URL", "https://test-ai.yuncyb.com/aicyberp-api"
        ).strip(),
    )
    url = service.start()
    yield url
    service.stop()


@pytest.fixture(scope="module", autouse=True)
def _report(mcp_url):
    """所有场景执行完写评测报告并打印汇总。"""
    yield
    if not _RESULTS:
        return
    metrics = harness.aggregate(_RESULTS)
    print()
    print(harness.report_text(metrics, _RESULTS))
    report_dir = _EVAL_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / ("eval-%s.json" % time.strftime("%Y%m%d%H%M%S"))
    report_path.write_text(
        json.dumps(
            {
                "metrics": metrics,
                "llm_model": _LLM_MODEL,
                "results": [harness.result_to_dict(r) for r in _RESULTS],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.scenario_id)
def test_llm_tool_recognition(scenario, mcp_url):
    """单场景：模型应选中期望工具、抽对关键参数、不误触发写工具。"""
    model = harness.OpenAICompatibleModel(_LLM_BASE_URL, _LLM_API_KEY, _LLM_MODEL)

    async def _run():
        async with harness.McpEndpoint(
            mcp_url, harness.conversation_headers(_API_KEY, scenario, "pytest")
        ) as endpoint:
            tools = await endpoint.list_openai_tools()
            return await harness.run_scenario(
                model,
                endpoint,
                scenario,
                tools=tools,
                max_rounds=int(os.environ.get("ERP_BILLING_EVAL_MAX_ROUNDS", "5")),
            )

    result = asyncio.run(_run())
    _RESULTS.append(result)
    assert result.selection_ok, (
        "工具选择错误：期望 %s，实际调用 %s"
        % (scenario.expected_tool, [record.tool for record in result.tool_calls])
    )
    assert result.params_ok, (
        "参数抽取错误：期望 %s，实际调用记录 %s"
        % (
            scenario.expected_params,
            [
                (record.tool, record.arguments)
                for record in result.tool_calls
                if record.tool == scenario.expected_tool
            ],
        )
    )
    assert not result.write_violation, "只读场景误触发写工具"
