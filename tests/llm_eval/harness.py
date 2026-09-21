"""LLM 工具识别评测核心：通用 runner，不依赖任何业务代码。

设计目标是可整体迁移为独立验证平台：

- 场景是纯数据文件（话术 → 期望工具 + 关键参数子集），不 import 业务代码；
- runner 只认配置：MCP 端点、鉴权头、场景目录、LLM 接入参数；
- ChatModel / McpEndpoint 是两个注入点，自检用 ScriptedModel 无需任何外部凭据。

安全边界：默认把 preview/submit/update/void 前缀识别为写工具；只读场景里
模型误调写工具时不真实执行，直接返回结构化拦截错误并记违规，保证评测
本身不会向业务系统写入数据。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import jsonschema
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_WRITE_TOOL_SPEC = "^(?:preview|submit|update|void)"
DEFAULT_MAX_ROUNDS = 5
TOOL_RESULT_MAX_CHARS = 4000
SCRIPTED_ERP_BASE_URL = "https://127.0.0.1:1"

DEFAULT_SYSTEM_PROMPT = (
    "你是一个连接了业务工具的智能助手。根据用户的问题选择并调用合适的工具，"
    "取得结果后用简洁的中文回答用户。"
)

_HTTP_TIMEOUT = httpx.Timeout(30, read=300)


# ---------------------------------------------------------------------------
# 场景数据
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    scenario_id: str
    domain: str
    utterance: str
    expected_tool: str
    expected_params: dict[str, Any] = field(default_factory=dict)
    forbidden_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    source: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    expected_calls: list[dict[str, Any]] = field(default_factory=list)
    mock_results: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.history, list) or any(
            not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}
            for message in self.history
        ):
            raise ValueError("history 只允许 user/assistant/tool，不允许 system/developer")
        if not isinstance(self.expected_calls, list) or any(
            not isinstance(call, dict) or not isinstance(call.get("tool"), str)
            or not call["tool"].strip() or not isinstance(call.get("params", {}), dict)
            for call in self.expected_calls
        ):
            raise ValueError("expected_calls 必须为有序的 {tool, params} 数组")
        if not isinstance(self.mock_results, dict):
            raise ValueError("mock_results 必须为工具名到合成结果的对象")

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.expected_calls or [{"tool": self.expected_tool, "params": self.expected_params}]

    @property
    def metadata_only(self) -> bool:
        return "metadata_only" in self.tags

    @property
    def write_flow(self) -> bool:
        return "write_flow" in self.tags

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> "Scenario":
        missing = [
            key for key in ("scenario_id", "utterance", "expected_tool")
            if not str(data.get(key, "")).strip()
        ]
        if missing:
            raise ValueError("场景缺少必填字段 %s：%s" % (missing, source or data))
        return cls(
            scenario_id=str(data["scenario_id"]),
            domain=str(data.get("domain", "")),
            utterance=str(data["utterance"]),
            expected_tool=str(data["expected_tool"]),
            expected_params=data.get("expected_params") or {},
            forbidden_tools=list(data.get("forbidden_tools") or []),
            tags=list(data.get("tags") or ["read_only"]),
            description=str(data.get("description", "")),
            source=source,
            history=data.get("history", []),
            expected_calls=data.get("expected_calls", []),
            mock_results=data.get("mock_results", {}),
        )


def load_scenarios(
    directory: Path,
    *,
    tag_filter: list[str] | None = None,
    include_write: bool = False,
    metadata_only: bool = False,
) -> list[Scenario]:
    """加载场景目录下所有 *.json（跳过 _ 开头的模板文件）。

    普通模式始终排除 metadata_only；元数据模式只加载该标签，允许模拟
    write_flow，但不授权真实执行。普通模式仍默认丢弃 write_flow。
    """
    scenarios: list[Scenario] = []
    for path in sorted(Path(directory).glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        for item in items:
            scenarios.append(Scenario.from_dict(item, source=path.name))
    scenarios = [s for s in scenarios if s.metadata_only == metadata_only]
    if not include_write and not metadata_only:
        scenarios = [s for s in scenarios if not s.write_flow]
    if tag_filter:
        wanted = {tag.strip() for tag in tag_filter if tag.strip()}
        scenarios = [s for s in scenarios if wanted & set(s.tags)]
    return scenarios


# ---------------------------------------------------------------------------
# 模型抽象
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRequest:
    call_id: str
    name: str
    arguments: str


@dataclass
class ModelTurn:
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


class ChatModel(Protocol):
    """一轮对话：输入消息与工具定义，返回文本或工具调用请求。"""

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn: ...


class OpenAICompatibleModel:
    """OpenAI 兼容 chat/completions 客户端（DashScope/DeepSeek/Moonshot 等通用）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + self._api_key},
            )
        if response.status_code != 200:
            raise RuntimeError(
                "模型接口返回 %d：%s" % (response.status_code, response.text[:500])
            )
        message = response.json()["choices"][0]["message"]
        tool_calls = [
            ToolCallRequest(
                call_id=str(call.get("id") or "call-%s" % uuid.uuid4().hex[:8]),
                name=str(call["function"]["name"]),
                arguments=str(call["function"].get("arguments") or ""),
            )
            for call in message.get("tool_calls") or []
        ]
        return ModelTurn(content=message.get("content") or "", tool_calls=tool_calls)


class ScriptedModel:
    """按 expected_calls 逐轮回放，仅自检链路与断言，不代表模型识别准确率。"""

    def __init__(self, scenario: Scenario):
        self._scenario = scenario
        self._round = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        if self._round < len(self._scenario.calls):
            call = self._scenario.calls[self._round]
            self._round += 1
            return ModelTurn(
                tool_calls=[
                    ToolCallRequest(
                        call_id="scripted-%s" % uuid.uuid4().hex[:8],
                        name=call["tool"],
                        arguments=json.dumps(call.get("params", {}), ensure_ascii=False),
                    )
                ]
            )
        return ModelTurn(content="（脚本化回放，非模型实测）已按场景期望生成调用请求。")


# ---------------------------------------------------------------------------
# MCP 端点
# ---------------------------------------------------------------------------


class McpEndpoint:
    """一次 MCP 会话的包装：list_tools / call_tool，结果统一解包为 dict。"""

    def __init__(self, server_url: str, headers: dict[str, str]):
        self._url = server_url.rstrip("/") + "/mcp"
        self._headers = headers
        self._client: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpEndpoint":
        self._client = httpx.AsyncClient(headers=self._headers, timeout=_HTTP_TIMEOUT)
        stream = streamable_http_client(self._url, http_client=self._client)
        self._read, self._write, _ = await stream.__aenter__()
        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._stream = stream
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        assert self._session is not None and self._client is not None
        await self._session.__aexit__(*exc_info)
        await self._stream.__aexit__(*exc_info)
        await self._client.aclose()

    async def list_openai_tools(self) -> list[dict[str, Any]]:
        """tools/list 转换为 OpenAI function calling 工具定义。"""
        assert self._session is not None
        result = await self._session.list_tools()
        definitions = []
        for tool in result.tools:
            schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return definitions

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行工具调用；协议级错误与不可解析结果统一转为 ok=False 的结构化 dict。"""
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            text = " ".join(
                getattr(block, "text", "") for block in result.content or []
            )
            return {
                "ok": False,
                "error": {"code": "mcp_protocol_error", "message": text[:500]},
            }
        payload = getattr(result, "structuredContent", None)
        if isinstance(payload, dict):
            return payload
        for block in result.content or []:
            text = getattr(block, "text", "")
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {
            "ok": False,
            "error": {"code": "eval_tool_result_unparsable", "message": "工具结果无 structuredContent 或 JSON 文本"},
        }


def conversation_headers(api_key: str, scenario: Scenario, run_id: str) -> dict[str, str]:
    """每个场景独立会话头，避免跨场景共享服务端会话状态。"""
    return {
        "X-API-Key": api_key,
        "X-Conversation-Id": "eval-%s-%s" % (scenario.scenario_id, run_id),
    }


def load_env_defaults(path, prefix: str = "") -> None:
    """把 KEY=VALUE 文件中的配置注入 os.environ 缺省值；已设置的变量优先。

    评测凭据（X-API-Key、模型 Key 等）可固化在本地 env 文件而不必每次
    export；只有带 prefix 的键会被注入，避免影响其他配置。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if prefix and not key.startswith(prefix):
            continue
        if key and value:
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# 评测执行
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any] | None
    executed: bool
    ok: bool | None
    error_code: str = ""
    simulated: bool = False


@dataclass
class ScenarioResult:
    scenario: Scenario
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    rounds: int = 0
    final_answer: str = ""
    metadata_only: bool = False
    scripted: bool = False

    @property
    def selection_ok(self) -> bool:
        if self.metadata_only:
            return [record.tool for record in self.tool_calls] == [call["tool"] for call in self.scenario.calls]
        return any(record.tool == self.scenario.expected_tool for record in self.tool_calls)

    @property
    def params_ok(self) -> bool:
        if self.metadata_only:
            return self.selection_ok and all(
                _params_match(call.get("params", {}), record.arguments)
                for call, record in zip(self.scenario.calls, self.tool_calls)
            )
        if not self.scenario.expected_params:
            return self.selection_ok
        for record in self.tool_calls:
            if record.tool == self.scenario.expected_tool and _params_match(
                self.scenario.expected_params, record.arguments
            ):
                return True
        return False

    @property
    def write_violation(self) -> bool:
        return any(
            record.tool in self.scenario.forbidden_tools
            or (
                not self.scenario.write_flow
                and _is_write_tool(record.tool, WRITE_PATTERNS)
            )
            for record in self.tool_calls
        )

    @property
    def schema_ok(self) -> bool:
        return not any(record.error_code in {
            "eval_arguments_invalid", "eval_unknown_tool", "eval_schema_invalid",
        } for record in self.tool_calls)

    @property
    def passed(self) -> bool:
        return (
            self.selection_ok and self.params_ok and not self.write_violation
            and (not self.metadata_only or (
                self.schema_ok and not any(record.error_code for record in self.tool_calls)
            ))
        )


WRITE_PATTERNS: list[re.Pattern[str]] = []


def set_write_tool_spec(spec: str) -> None:
    """设置写工具识别规则（逗号分隔的正则），默认 preview/submit/update/void 前缀。"""
    global WRITE_PATTERNS
    WRITE_PATTERNS = [re.compile(item.strip()) for item in spec.split(",") if item.strip()]


set_write_tool_spec(DEFAULT_WRITE_TOOL_SPEC)


def _is_write_tool(name: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(name) for pattern in patterns)


def _params_match(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    """期望参数是子集匹配；字符串去首尾空白比较，其余类型要求相等。"""
    if actual is None:
        return False
    for key, want in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if isinstance(want, str) and isinstance(got, str):
            if want.strip() != got.strip():
                return False
        elif want != got:
            return False
    return True


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def run_scenario(
    model: ChatModel,
    endpoint: McpEndpoint,
    scenario: Scenario,
    *,
    tools: list[dict[str, Any]],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    metadata_only: bool = False,
) -> ScenarioResult:
    """单场景评测；metadata_only 仅消费 tools/list 与合成结果，绝不调用 ERP。

    元数据模式不发送任何 system 消息（包括调用方传入的业务 prompt），
    历史、用户话术和发布工具定义以外的期望断言不会送给真实模型。
    """
    if scenario.metadata_only and not metadata_only:
        raise ValueError("metadata_only 场景不能由 live runner 执行")
    result = ScenarioResult(scenario=scenario, metadata_only=metadata_only, scripted=isinstance(model, ScriptedModel))
    messages: list[dict[str, Any]] = []
    if system_prompt and not metadata_only:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(deepcopy(scenario.history))
    messages.append({"role": "user", "content": scenario.utterance})
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    for _ in range(max_rounds):
        result.rounds += 1
        turn = await model.chat(messages, tools)
        if not turn.tool_calls:
            result.final_answer = turn.content
            return result
        messages.append(
            {
                "role": "assistant",
                "content": turn.content or None,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in turn.tool_calls
                ],
            }
        )
        for call in turn.tool_calls:
            arguments = _parse_arguments(call.arguments)
            executed = False
            simulated = False
            ok: bool | None = None
            code = ""
            message = ""
            # 无论模式与 write_flow 标签如何，显式禁止的工具都先拦截。
            if call.name in scenario.forbidden_tools:
                code, message = "eval_forbidden_tool", "场景禁止此工具，本次调用已被拦截"
            elif not scenario.write_flow and _is_write_tool(call.name, WRITE_PATTERNS):
                code, message = "eval_write_blocked", "只读评测场景禁止执行写工具，本次调用已被拦截"
            elif arguments is None:
                code, message = "eval_arguments_invalid", "工具参数不是合法 JSON 对象"
            elif call.name not in schemas:
                code, message = "eval_unknown_tool", "工具未发布在 MCP tools/list 中"
            elif metadata_only:
                try:
                    jsonschema.validate(arguments, schemas[call.name])
                except (jsonschema.ValidationError, jsonschema.SchemaError):
                    code, message = "eval_schema_invalid", "工具参数不满足发布的 inputSchema"
                if not code and call.name not in scenario.mock_results:
                    code, message = "eval_mock_missing", "未配置该工具的合成结果；不会回退执行 ERP"
            if code:
                payload = {"ok": False, "error": {"code": code, "message": message}}
            elif metadata_only:
                simulated = True
                payload = deepcopy(scenario.mock_results[call.name])
            else:
                executed = True
                payload = await endpoint.call_tool(call.name, arguments)
                ok = bool(payload.get("ok"))
            error = payload.get("error") or {} if isinstance(payload, dict) else {}
            result.tool_calls.append(
                ToolCallRecord(
                    tool=call.name,
                    arguments=arguments,
                    executed=executed,
                    ok=ok,
                    error_code=str(error.get("code", "")) if isinstance(error, dict) else "",
                    simulated=simulated,
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": json.dumps(payload, ensure_ascii=False)[:TOOL_RESULT_MAX_CHARS],
                }
            )
    return result


# ---------------------------------------------------------------------------
# 指标与报告
# ---------------------------------------------------------------------------


def aggregate(results: list[ScenarioResult]) -> dict[str, Any]:
    selection_hits = [r for r in results if r.selection_ok]
    return {
        "metadata_only": bool(results) and all(r.metadata_only for r in results),
        "scripted": bool(results) and all(r.scripted for r in results),
        "metric_kind": "scripted_contract_check" if any(r.scripted for r in results) else "model_recognition",
        "executed_calls": sum(c.executed for r in results for c in r.tool_calls),
        "simulated_calls": sum(c.simulated for r in results for c in r.tool_calls),
        "schema_errors": sum(not r.schema_ok for r in results),
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "selection_hits": len(selection_hits),
        "selection_accuracy": round(len(selection_hits) / len(results), 4) if results else 0.0,
        "param_hits": sum(1 for r in selection_hits if r.params_ok),
        "param_accuracy": round(
            sum(1 for r in selection_hits if r.params_ok) / len(selection_hits), 4
        )
        if selection_hits
        else 0.0,
        "write_misfires": sum(1 for r in results if r.write_violation),
        "tool_errors": sum(
            1 for r in results for c in r.tool_calls if c.executed and c.ok is False
        ),
        "avg_rounds": round(sum(r.rounds for r in results) / len(results), 2) if results else 0.0,
    }


def result_to_dict(result: ScenarioResult) -> dict[str, Any]:
    return {
        "scenario_id": result.scenario.scenario_id,
        "domain": result.scenario.domain,
        "utterance": result.scenario.utterance,
        "expected_tool": result.scenario.expected_tool,
        "expected_calls": result.scenario.calls,
        "metadata_only": result.metadata_only,
        "scripted": result.scripted,
        "schema_ok": result.schema_ok,
        "called_tools": [
            {
                "tool": record.tool,
                "arguments": record.arguments,
                "executed": record.executed,
                "simulated": record.simulated,
                "ok": record.ok,
                "error_code": record.error_code,
            }
            for record in result.tool_calls
        ],
        "selection_ok": result.selection_ok,
        "params_ok": result.params_ok,
        "write_violation": result.write_violation,
        "rounds": result.rounds,
        "final_answer": result.final_answer,
        "passed": result.passed,
    }


def report_text(metrics: dict[str, Any], results: list[ScenarioResult]) -> str:
    rate_label = "脚本符合率" if metrics.get("scripted") else "准确率"
    lines = [
        "====== LLM 工具识别评测汇总 ======",
        "运行类型：%s；%s" % (
            "元数据模拟（无 ERP 执行）" if metrics.get("metadata_only") else "live 工具执行",
            "脚本化回放，非模型实测，不代表模型准确率" if metrics.get("scripted") else "模型工具识别",
        ),
        "场景总数：%d" % metrics["total"],
        "工具选择命中：%d/%d（%s %.1f%%）"
        % (metrics["selection_hits"], metrics["total"], rate_label, metrics["selection_accuracy"] * 100),
        "参数抽取正确：%d/%d（%s %.1f%%）"
        % (metrics["param_hits"], metrics["selection_hits"], rate_label, metrics["param_accuracy"] * 100),
        "写工具误触发：%d" % metrics["write_misfires"],
        "工具执行错误：%d 次" % metrics["tool_errors"],
        "Schema 错误场景：%d；通过：%d/%d" % (
            metrics.get("schema_errors", 0), metrics.get("passed", 0), metrics["total"],
        ),
        "真实执行：%d 次；合成响应：%d 次" % (
            metrics.get("executed_calls", 0), metrics.get("simulated_calls", 0),
        ),
        "平均交互轮次：%.2f" % metrics["avg_rounds"],
        "------ 明细 ------",
    ]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        called = ",".join(record.tool for record in result.tool_calls) or "(无调用)"
        lines.append(
            "[%s] %s | 期望 %s | 实际调用 %s | 轮次 %d"
            % (status, result.scenario.scenario_id, result.scenario.expected_tool, called, result.rounds)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 服务拉起（供 CLI 与 pytest 入口复用）
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SpawnedService:
    """本地 uvicorn 子进程：构造后调用 start()，用完调用 stop()。"""

    def __init__(
        self,
        *,
        project_root: Path,
        app: str = "erp_billing.app:app",
        erp_base_url: str,
        env_extras: dict[str, str] | None = None,
        ready_timeout: float = 30.0,
    ):
        self._project_root = project_root
        self._app = app
        self._erp_base_url = erp_base_url
        self._env_extras = env_extras or {}
        self._ready_timeout = ready_timeout
        self._process: subprocess.Popen | None = None
        self._log_path: Path | None = None
        self.url = ""

    def start(self) -> str:
        port = free_port()
        env = os.environ.copy()
        env.update(self._env_extras)
        env["ERP_BILLING_BASE_URL"] = self._erp_base_url
        env["GJP_ENV"] = "local"
        log_file = tempfile.NamedTemporaryFile(
            prefix="eval-uvicorn-", suffix=".log", delete=False
        )
        self._process = subprocess.Popen(
            [
                "uv", "run", "uvicorn", self._app,
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=self._project_root,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self._log_path = Path(log_file.name)
        self.url = "http://127.0.0.1:%d" % port
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._raise_not_ready("服务进程提前退出")
            try:
                if httpx.get(self.url + "/healthz", timeout=2).status_code == 200:
                    return self.url
            except httpx.HTTPError:
                time.sleep(0.2)
        self._raise_not_ready("服务未在 %.0f 秒内就绪" % self._ready_timeout)
        return self.url

    def _raise_not_ready(self, reason: str) -> None:
        tail = ""
        if self._log_path and self._log_path.exists():
            tail = self._log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            self._log_path.unlink(missing_ok=True)
        raise RuntimeError("MCP 服务启动失败：%s\n服务日志尾部：\n%s" % (reason, tail))

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        if self._log_path:
            self._log_path.unlink(missing_ok=True)


async def run_eval(
    *,
    model_factory,
    scenarios: list[Scenario],
    mcp_url: str,
    api_key: str,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    on_result=None,
    metadata_only: bool = False,
) -> list[ScenarioResult]:
    """依次执行场景：每个场景独立 MCP 会话与独立模型实例。

    model_factory(scenario) 返回 ChatModel，真实评测每次返回同一模型配置，
    脚本化模式返回扮演该场景期望的 ScriptedModel。
    """
    run_id = "%s-%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6])
    results = []
    for scenario in scenarios:
        headers = conversation_headers(api_key, scenario, run_id)
        async with McpEndpoint(mcp_url, headers) as endpoint:
            tools = await endpoint.list_openai_tools()
            result = await run_scenario(
                model_factory(scenario),
                endpoint,
                scenario,
                tools=tools,
                max_rounds=max_rounds,
                system_prompt=system_prompt,
                metadata_only=metadata_only,
            )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results
