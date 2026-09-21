"""LLM 工具识别评测 CLI：独立 runner 入口，可整体迁移为验证平台。

脚本化模型全链路验证（无需任何外部凭据；默认指向不可达 ERP 地址，
工具返回结构化连接错误属预期，验证的是评测链路本身，且不会用无效
凭据反复触发 ERP 令牌校验锁定 82005）：

    uv run python tests/llm_eval/run_eval.py --scripted

真实 LLM 评测（X-API-Key 可固化在 config/local.env 的
ERP_BILLING_EVAL_API_KEY，模型凭据可用 --llm-* 或同名环境变量提供）：

    uv run python tests/llm_eval/run_eval.py \\
        --llm-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \\
        --llm-api-key sk-xxx \\
        --llm-model qwen-plus

已部署服务可直接 --mcp-url 指定，跳过本地拉起；报告写入
tests/llm_eval/reports/（已 gitignore），同时在终端打印汇总。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import harness

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
REPORT_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_ERP_BASE_URL = "https://test-ai.yuncyb.com/aicyberp-api"
# 脚本化模式只验证链路：不可达地址让工具快速返回结构化连接错误，
# 避免合成 Key 打真实 ERP 触发令牌校验失败锁定（82005）
SCRIPTED_ERP_BASE_URL = "https://127.0.0.1:1"

harness.load_env_defaults(PROJECT_ROOT / "config" / "local.env", prefix="ERP_BILLING_EVAL_")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM 工具识别评测 runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scripted", action="store_true", help="脚本化模型验证链路，无需凭据")
    mode.add_argument("--llm-base-url", help="OpenAI 兼容基地址，如 https://dashscope.aliyuncs.com/compatible-mode/v1")
    parser.add_argument("--llm-api-key", default="", help="模型 API Key")
    parser.add_argument("--llm-model", default="", help="模型名，如 qwen-plus")
    parser.add_argument("--api-key", default="", help="MCP 服务 X-API-Key；缺省读 ERP_BILLING_EVAL_API_KEY（含 config/local.env），脚本化模式最后回退合成 Key")
    parser.add_argument("--mcp-url", default="", help="已部署 MCP 服务地址；不设则本地拉起服务子进程")
    parser.add_argument("--erp-base-url", default="", help="本地拉起服务时的 ERP 业务 API 基地址；缺省时脚本化模式用不可达地址，真实评测用测试环境")
    parser.add_argument("--tags", default="", help="逗号分隔标签过滤，如 smoke")
    parser.add_argument("--include-write", action="store_true", help="包含 write_flow 场景（会真实执行写工具，慎用）")
    parser.add_argument("--max-rounds", type=int, default=harness.DEFAULT_MAX_ROUNDS)
    parser.add_argument("--report", default="", help="报告 JSON 输出路径，默认 reports/eval-<时间戳>.json")
    return parser.parse_args()


def _main() -> int:
    args = _parse_args()
    if not args.scripted and not (args.llm_base_url and args.llm_api_key and args.llm_model):
        print("真实评测需要同时提供 --llm-base-url / --llm-api-key / --llm-model", file=sys.stderr)
        return 2

    api_key = args.api_key or os.environ.get("ERP_BILLING_EVAL_API_KEY", "")
    if not api_key:
        if args.scripted:
            api_key = "eval-scripted-key"
        else:
            print("真实评测需要 X-API-Key（--api-key 或 config/local.env 的 ERP_BILLING_EVAL_API_KEY）", file=sys.stderr)
            return 2
    erp_base_url = args.erp_base_url or (
        SCRIPTED_ERP_BASE_URL if args.scripted else DEFAULT_ERP_BASE_URL
    )

    scenarios = harness.load_scenarios(
        SCENARIO_DIR,
        tag_filter=args.tags.split(",") if args.tags else None,
        include_write=args.include_write,
    )
    if not scenarios:
        print("没有匹配的场景", file=sys.stderr)
        return 2

    if args.llm_base_url and not args.scripted:
        model = harness.OpenAICompatibleModel(args.llm_base_url, args.llm_api_key, args.llm_model)

        def model_factory(scenario):
            return model
    else:
        def model_factory(scenario):
            return harness.ScriptedModel(scenario)

    service = None
    mcp_url = args.mcp_url
    if not mcp_url:
        service = harness.SpawnedService(
            project_root=PROJECT_ROOT,
            erp_base_url=erp_base_url,
        )
        mcp_url = service.start()
        print("本地 MCP 服务已就绪：%s（ERP 基地址 %s）" % (mcp_url, erp_base_url))

    print(
        "开始评测：%d 个场景（%s 模式%s）"
        % (
            len(scenarios),
            "脚本化" if args.scripted else args.llm_model,
            "，标签过滤=%s" % args.tags if args.tags else "",
        )
    )
    try:
        results = asyncio.run(
            harness.run_eval(
                model_factory=model_factory,
                scenarios=scenarios,
                mcp_url=mcp_url,
                api_key=api_key,
                max_rounds=args.max_rounds,
                on_result=lambda result: print(
                    "  [%s] %s → %s"
                    % (
                        "PASS" if result.passed else "FAIL",
                        result.scenario.scenario_id,
                        ",".join(record.tool for record in result.tool_calls) or "(无调用)",
                    )
                ),
            )
        )
    finally:
        if service is not None:
            service.stop()

    metrics = harness.aggregate(results)
    print(harness.report_text(metrics, results))

    report_path = Path(args.report) if args.report else None
    if report_path is None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / ("eval-%s.json" % time.strftime("%Y%m%d%H%M%S"))
    report_path.write_text(
        json.dumps(
            {
                "metrics": metrics,
                "scripted": args.scripted,
                "llm_model": None if args.scripted else args.llm_model,
                "mcp_url": mcp_url,
                "results": [harness.result_to_dict(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("评测报告已写入：%s" % report_path)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(_main())
