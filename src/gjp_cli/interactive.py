"""AgentScope 多轮 CLI 传输层。

这个文件只负责 ERP 开单 Agent 的终端输入输出、流式事件渲染和工具结果展示。
"""

import asyncio
import json
import logging
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

# 显式导入 readline，让 input() 在 macOS 等环境中保留方向键、退格和历史记录。
try:
    import readline  # noqa: F401
except ImportError:
    pass

from agentscope.event import (
    ConfirmResult,
    EventType,
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import AssistantMsg
from agentscope.message import UserMsg
from agentscope.message._block import TextBlock, ToolResultBlock, ToolResultState
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from gjp_common.config import get_env_value
from erp_billing.adapters import UnavailableBillingApi
from gjp_common.errors import DomainError
from erp_billing.toolset import BillingToolSet
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.toolset import LocalToolProvider
from .agent import (
    AgentSpec,
    ERP_BILLING_AGENT_SPEC,
    build_agent,
    load_agent_state,
    save_agent_state,
)
from .model_runtime import LLMSettings


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class AgentConsoleProfile:
    """一个业务 Agent 在终端中的展示配置。"""

    title: str
    summary: str
    detail: str
    prompt_hint: str
    display_title: str
    tool_labels: dict[str, str]
    json_result_tools: frozenset[str] = frozenset()
    border_style: str = "bright_blue"


ERP_BILLING_CONSOLE_PROFILE = AgentConsoleProfile(
    title="ERP Billing Agent",
    summary="自然语言生成销售单 JSON，并匹配真实 ERP 商品目录。",
    detail="支持商品同步、语义查询、完整文本开单，也可直接输入下单图片路径。",
    prompt_hint="直接描述客户下单或输入图片路径",
    display_title="ERP AI开单",
    json_result_tools=frozenset({"create_draft"}),
    tool_labels={
        "sync_products": "同步线上商品",
        "search_products": "查询系统商品",
        "create_draft": "生成开单 JSON",
    },
)


@dataclass(frozen=True)
class AgentConsoleBuildOptions:
    """CLI 传入的通用 Agent 构建选项。"""

    resume: bool = False


@dataclass(frozen=True)
class AgentConsoleRuntime:
    """某个业务 Agent 的运行时资源，由注册表 builder 负责创建。"""

    session: Any
    tool_provider: Any
    agent_state_path: Path | None
    session_state_path: Path | None
    max_turns: Optional[int]


@dataclass(frozen=True)
class AgentConsoleRegistration:
    """可插拔 Agent 在统一 CLI 中的注册项；本地 chat 与远程 MCP 控制台共用。"""

    key: str
    aliases: frozenset[str]
    agent_spec: AgentSpec
    profile: AgentConsoleProfile
    runtime_builder: Callable[[AgentConsoleBuildOptions], AgentConsoleRuntime]
    mcp_name: str
    user_input_transformer: Callable[[str], Awaitable[str]] | None = None


class ConversationAgent(Protocol):
    """CLI 与测试替身共同依赖的 AgentScope 回复接口：reply / reply_stream。"""

    async def reply(self, inputs: UserMsg) -> Any:
        ...

    async def reply_stream(self, inputs: Any) -> Any:
        ...


class InteractiveAgentConsole:
    """统一 CLI 对话控制台：把每轮自然语言输入转发给持久化的 AgentScope Agent，
    并负责流式事件渲染、工具结果展示、HITL 澄清和会话状态保存。"""

    def __init__(
        self,
        agent: ConversationAgent,
        session: Any,
        input_fn: Callable[[str], str] = input,
        output_fn: Optional[Callable[[str], None]] = None,
        console: Optional[Console] = None,
        max_turns: Optional[int] = None,
        model_label: Optional[str] = None,
        stream_enabled: bool = True,
        profile: AgentConsoleProfile = ERP_BILLING_CONSOLE_PROFILE,
        user_input_transformer: Callable[[str], Awaitable[str]] | None = None,
        agent_state_path: Optional[Path] = None,
        session_state_path: Optional[Path] = None,
        startup_notices: tuple[str, ...] = (),
    ) -> None:
        self.agent = agent
        self.session = session
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.console = console or Console()
        self.rich_output = output_fn is None
        self.max_turns = max_turns
        self.model_label = model_label
        self.stream_enabled = stream_enabled
        self._active_statuses: dict[str, Any] = {}
        self._stream_buffer = ""
        self._stream_live: Optional[Live] = None
        self._state_path = agent_state_path
        self._session_state_path = session_state_path
        self._startup_notices = startup_notices
        self._tool_names: dict[str, str] = {}
        self.profile = profile
        self.user_input_transformer = user_input_transformer

    async def run_async(self) -> int:
        """运行多轮对话循环，不做任何用户短语解释，纯传输层。"""
        use_clear = self.rich_output and self.console.is_terminal
        if use_clear:
            self.console.clear()
        resize_registered = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and hasattr(signal, "SIGWINCH"):
            try:
                loop.add_signal_handler(signal.SIGWINCH, self._on_terminal_resize)
                resize_registered = True
            except (NotImplementedError, RuntimeError):
                pass
        try:
            self._show_banner()
            # 启动提示在清屏和横幅之后输出，保证真终端上可见。
            for notice in self._startup_notices:
                self._system(notice)
            turn = 0
            while not bool(getattr(self.session, "finished", False)):
                if self.max_turns is not None and turn >= self.max_turns:
                    self._system("已达到当前会话的最大轮数，请重新启动会话继续。")
                    return 0
                try:
                    text = self._read_user_input().strip()
                except (EOFError, KeyboardInterrupt):
                    self._system("会话已结束。")
                    return 0
                if not text:
                    continue
                if text.lower() in ("exit", "quit", "bye", "/exit", "/quit", "/bye"):
                    self._system("会话已结束。")
                    return 0
                if self.user_input_transformer is not None:
                    try:
                        transformed = (await self.user_input_transformer(text)).strip()
                    except DomainError as exc:
                        self._system(str(exc))
                        continue
                    if not transformed:
                        continue
                    if transformed != text:
                        self._system("输入已识别为：\n%s" % transformed)
                    text = transformed
                turn += 1
                record_user_turn = getattr(self.session, "record_user_turn", None)
                if callable(record_user_turn):
                    record_user_turn(text)
                agent_task = asyncio.create_task(
                    self._agent_turn(UserMsg(name="user", content=text))
                )
                sigint_installed = False
                if loop is not None and hasattr(signal, "SIGINT"):
                    try:
                        loop.add_signal_handler(
                            signal.SIGINT, agent_task.cancel,
                        )
                        sigint_installed = True
                    except (NotImplementedError, RuntimeError):
                        pass
                try:
                    await agent_task
                except asyncio.CancelledError:
                    self._system("已取消当前回复。")
                finally:
                    if sigint_installed and loop is not None:
                        try:
                            loop.remove_signal_handler(signal.SIGINT)
                        except (NotImplementedError, RuntimeError, ValueError):
                            pass
                    for tool_call_id in list(self._active_statuses):
                        self._finish_tool_status(tool_call_id)
                    if self._stream_live is not None:
                        try:
                            self._stream_live.stop()
                        except Exception:
                            pass
                        self._stream_live = None
            return 0
        finally:
            if resize_registered and loop is not None:
                try:
                    loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            if self._session_state_path is not None:
                _save_session_snapshot(self.session, self._session_state_path)
            if self._state_path is not None:
                save_agent_state(self.agent, self._state_path)

    def _on_terminal_resize(self) -> None:
        """终端尺寸变化时重新渲染流式 Live 组件。"""
        if self._stream_live is not None:
            self._stream_live.update(Markdown(self._stream_buffer), refresh=True)

    def _read_user_input(self) -> str:
        if self.input_fn is input and self.rich_output:
            # Pass the styled prompt directly to input() so readline treats
            # it as non-editable.  \x01/\x02 wrap ANSI escape codes so
            # readline calculates the prompt's visible length correctly and
            # backspace can never erase the "user" label.
            return input("\x01\033[1;32m\x02user\x01\033[0m\x02 › ")
        return self.input_fn("user › ")

    def run(self) -> int:
        """同步入口：CLI 命令安装后的调用点，内部用 asyncio.run 驱动异步循环。"""
        try:
            return asyncio.run(self.run_async())
        except KeyboardInterrupt:
            # Python's asyncio runner may re-raise SIGINT after the coroutine
            # has already handled the prompt interruption.  Keep CLI shutdown
            # clean and never expose an asyncio traceback to terminal users.
            return 0

    def _show_banner(self) -> None:
        if self.rich_output:
            lines: list[Any] = [
                Text(self.profile.title, style="bold cyan"),
                Text(""),
                Text(" %s" % self.profile.summary),
                Text(" %s" % self.profile.detail, style="dim"),
            ]
            if self.model_label:
                stream_on = self.stream_enabled
                stream_label = "ON" if stream_on else "OFF"
                stream_style = "bold green" if stream_on else "bold yellow"
                lines.append(Text(""))
                lines.append(
                    Text.assemble(
                        ("   ⚙  ", "dim"),
                        ("模型  ", "bold dim"),
                        (self.model_label, "cyan"),
                    )
                )
                lines.append(
                    Text.assemble(
                        ("   ⚡ ", "dim"),
                        ("流式  ", "bold dim"),
                        (stream_label, stream_style),
                    )
                )
            lines.append(Text(""))
            lines.append(
                Text.assemble(
                    ("   %s  " % self.profile.prompt_hint, "dim"),
                    ("Ctrl+C", "bold dim"),
                    (" 取消  ", "dim"),
                    ("exit", "bold dim"),
                    (" 结束会话", "dim"),
                )
            )
            self.console.print(
                Panel.fit(
                    Group(*lines),
                    border_style=self.profile.border_style,
                    padding=(0, 1),
                ),
            )
            self.console.print()
            return
        self._plain(self.profile.title)
        self._plain(self.profile.summary)
        if self.model_label:
            self._plain(
                "模型：%s；流式：%s"
                % (self.model_label, "开启" if self.stream_enabled else "关闭"),
            )

    def _assistant(self, text: str) -> None:
        if self.rich_output:
            self.console.print("agent", style="bold blue")
            self.console.print(Markdown(text))
            self.console.print()
            return
        self._plain("agent")
        rendered = _markdown_to_plain(text)
        if rendered:
            self._plain(rendered)

    async def _agent_turn(self, user_msg: UserMsg) -> None:
        if hasattr(self.agent, "reply_stream"):
            await self._stream_agent_turn(user_msg)
            return
        reply = await self.agent.reply(user_msg)
        rendered_previews: set[str] = set()
        self._render_message_tool_displays(reply, rendered_previews)
        response_text = reply.get_text_content() if reply is not None else None
        if response_text:
            self._assistant(response_text)

    async def _stream_agent_turn(self, user_msg: UserMsg) -> None:
        message = None
        tool_outputs: dict[str, str] = {}
        rendered_previews: set[str] = set()
        final_text_parts: list[str] = []
        streamed_text = False
        stream_open = False
        next_input: Any = user_msg
        try:
            while next_input is not None and not bool(
                getattr(self.session, "finished", False),
            ):
                current_input = next_input
                next_input = None
                async for event in self.agent.reply_stream(current_input):
                    if event.type == EventType.REPLY_START:
                        message = AssistantMsg(
                            name=event.name,
                            content=[],
                            id=event.reply_id,
                        )
                    if message is not None:
                        message.append_event(event)
                    if event.type == EventType.MODEL_CALL_START:
                        self._start_model_status(event.model_name)
                    elif event.type == EventType.MODEL_CALL_END:
                        self._finish_tool_status("__model__")
                    elif event.type == EventType.TOOL_CALL_START:
                        self._finish_tool_status("__model__")
                        if stream_open:
                            self._assistant_stream_finish(discard=True)
                            stream_open = False
                        final_text_parts.clear()
                        self._tool_names[event.tool_call_id] = event.tool_call_name
                        self._start_tool_status(event.tool_call_id, event.tool_call_name)
                    elif event.type == EventType.REQUIRE_EXTERNAL_EXECUTION:
                        next_input = self._handle_external_execution_request(event)
                    elif event.type == EventType.REQUIRE_USER_CONFIRM:
                        next_input = self._handle_user_confirm_request(event)
                    elif event.type == EventType.TOOL_RESULT_TEXT_DELTA:
                        tool_outputs[event.tool_call_id] = (
                            tool_outputs.get(event.tool_call_id, "") + event.delta
                        )
                    elif event.type == EventType.TOOL_RESULT_DATA_DELTA and event.data:
                        tool_outputs[event.tool_call_id] = (
                            tool_outputs.get(event.tool_call_id, "") + event.data
                        )
                    elif event.type == EventType.TOOL_RESULT_END:
                        self._finish_tool_status(event.tool_call_id)
                        output = tool_outputs.get(event.tool_call_id, "")
                        shown = self._render_tool_output_displays(output, rendered_previews)
                        if not shown:
                            tool_name = self._tool_names.get(event.tool_call_id, event.tool_call_id)
                            base_name = _base_tool_name(tool_name)
                            if (
                                not shown
                                and base_name in self.profile.json_result_tools
                                and _parse_ok(output)
                            ):
                                # 白名单工具的 JSON 结果由终端直接呈现，避免模型逐 token 复述
                                shown = self._render_json_result(output)
                            if not shown:
                                self._tool_result_summary(tool_name, output)
                    elif event.type == EventType.TEXT_BLOCK_DELTA:
                        self._finish_tool_status("__model__")
                        final_text_parts.append(event.delta)
                        if not stream_open:
                            self._assistant_stream_start()
                            stream_open = True
                        self._assistant_stream_delta(event.delta)
                        streamed_text = True
        finally:
            for tool_call_id in list(self._active_statuses):
                self._finish_tool_status(tool_call_id)
            if stream_open:
                self._assistant_stream_finish()
        if bool(getattr(self.session, "finished", False)):
            return
        if message is not None:
            self._render_message_tool_displays(message, rendered_previews)
            response_text = "".join(final_text_parts).strip() or _last_text_content(message)
            if response_text and not streamed_text:
                self._assistant(response_text)

    def _assistant_stream_start(self) -> None:
        self._stream_buffer = ""
        self._stream_live = None
        if self.rich_output:
            self.console.print("agent", style="bold blue")
            if self.console.is_terminal:
                self._stream_live = Live(
                    Markdown(""),
                    console=self.console,
                    refresh_per_second=20,
                    transient=True,
                )
                self._stream_live.start()
            return

    def _assistant_stream_delta(self, text: str) -> None:
        if not text:
            return
        self._stream_buffer += text
        if self._stream_live is not None:
            self._stream_live.update(Markdown(self._stream_buffer), refresh=True)

    def _assistant_stream_finish(self, discard: bool = False) -> None:
        if self._stream_live is not None:
            self._stream_live.stop()
            self._stream_live = None
        if not discard:
            if self._stream_buffer:
                if self.rich_output:
                    self.console.print(Markdown(self._stream_buffer))
                else:
                    self._plain(_markdown_to_plain(self._stream_buffer))
            if self.rich_output:
                self.console.print()
        self._stream_buffer = ""

    def _handle_external_execution_request(
        self,
        event: RequireExternalExecutionEvent,
    ) -> Optional[ExternalExecutionResultEvent]:
        results: list[ToolResultBlock] = []
        for tool_call in event.tool_calls:
            self._finish_tool_status(tool_call.id)
            tool_input = _parse_tool_input(tool_call.input)
            if tool_call.name != "clarify":
                output = {
                    "ok": False,
                    "error": {
                        "code": "UNSUPPORTED_EXTERNAL_TOOL",
                        "message": "CLI 不支持此外部工具：%s" % tool_call.name,
                    },
                }
                results.append(
                    ToolResultBlock(
                        id=tool_call.id,
                        name=tool_call.name,
                        output=[TextBlock(text=json.dumps(output, ensure_ascii=False))],
                        state=ToolResultState.ERROR,
                    ),
                )
                continue
            result = self._ask_clarification(tool_call.id, tool_input)
            if result is None:
                if hasattr(self.session, "finished"):
                    self.session.finished = True
                return None
            results.append(
                ToolResultBlock(
                    id=tool_call.id,
                    name=tool_call.name,
                    output=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                ),
            )
        return ExternalExecutionResultEvent(
            reply_id=event.reply_id,
            execution_results=results,
        )

    def _ask_clarification(
        self,
        tool_call_id: str,
        tool_input: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        question = str(tool_input.get("question") or "请补充需要确认的业务要求。").strip()
        raw_options = tool_input.get("options")
        options = [item for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
        allow_free_text = bool(tool_input.get("allowFreeText", True))
        self.session.begin_clarification(tool_call_id, tool_input)
        self._clarification_prompt(question, options, allow_free_text)
        while True:
            try:
                raw_answer = self.input_fn("请选择> ").strip()
            except (EOFError, KeyboardInterrupt, StopIteration):
                self.session.cancel_clarification(tool_call_id)
                self._system("会话已结束，未执行需要澄清的修改。")
                return None
            selected = _match_clarification_option(raw_answer, options)
            if selected is not None:
                answer = {
                    "ok": True,
                    "answer": str(selected.get("label") or raw_answer),
                    "selectedOption": selected,
                }
                self.session.resolve_clarification(tool_call_id, answer)
                return answer
            if raw_answer and allow_free_text:
                answer = {"ok": True, "answer": raw_answer, "selectedOption": None}
                self.session.resolve_clarification(tool_call_id, answer)
                return answer
            self._system("请输入选项序号，或按提示补充描述。")

    def _clarification_prompt(
        self,
        question: str,
        options: list[dict[str, Any]],
        allow_free_text: bool,
    ) -> None:
        if not self.rich_output:
            self._plain("ERP 开单")
            self._plain(question)
            for index, option in enumerate(options, start=1):
                label = str(option.get("label") or option.get("id") or index)
                description = str(option.get("description") or "").strip()
                suffix = " - %s" % description if description else ""
                self._plain("%d. %s%s" % (index, label, suffix))
            if allow_free_text:
                self._plain("也可以直接输入你的具体要求。")
            return
        panel_lines: list[Any] = [Text(question), Text("")]
        for index, option in enumerate(options, start=1):
            label = str(option.get("label") or option.get("id") or index)
            description = str(option.get("description") or "").strip()
            panel_lines.append(Text("%d. %s" % (index, label)))
            if description:
                panel_lines.append(Text("   %s" % description, style="dim"))
        if allow_free_text:
            panel_lines.append(Text(""))
            panel_lines.append(Text("也可以直接输入你的具体要求。", style="dim"))
        self.console.print(
            Panel(
                Group(*panel_lines),
                title=Text("ERP 开单", style="bold cyan"),
                border_style="cyan",
                padding=(0, 1),
            ),
        )

    def _handle_user_confirm_request(
        self,
        event: RequireUserConfirmEvent,
    ) -> Optional[UserConfirmResultEvent]:
        results: list[ConfirmResult] = []
        for tool_call in event.tool_calls:
            self._finish_tool_status(tool_call.id)
            label = self._tool_label(tool_call.name)
            if self.rich_output:
                self.console.print("需要确认", style="bold yellow")
                self.console.print(Text("是否执行：%s" % label))
            else:
                self._plain("需要确认：是否执行 %s" % label)
            try:
                answer = self.input_fn("确认执行？[y/N] ").strip().casefold()
            except (EOFError, KeyboardInterrupt, StopIteration):
                answer = ""
            results.append(
                ConfirmResult(
                    confirmed=answer in {"y", "yes", "是", "确认", "执行"},
                    tool_call=tool_call,
                ),
            )
        return UserConfirmResultEvent(reply_id=event.reply_id, confirm_results=results)

    def _tool_label(self, tool_name: str) -> str:
        base_name = _base_tool_name(tool_name)
        return self.profile.tool_labels.get(base_name, base_name)

    def _tool_result_summary(self, tool_name: str, output: str) -> None:
        """工具完成且无结构化展示时，打印一行摘要（成功/失败状态）。"""
        label = self._tool_label(tool_name)
        try:
            data = json.loads(output) if output else {}
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict) and data.get("ok") is False:
            error = data.get("error") or {}
            msg = str(error.get("message") or label)
            if self.rich_output:
                self.console.print(Text("✗ %s" % msg, style="red"))
            else:
                self._plain("✗ %s" % msg)
        elif isinstance(data, dict) and data.get("message"):
            msg = str(data["message"])
            if self.rich_output:
                self.console.print(Text("✓ %s" % msg, style="green"))
            else:
                self._plain("✓ %s" % msg)
        else:
            if self.rich_output:
                self.console.print(Text("✓ %s" % label, style="green"))
            else:
                self._plain("✓ %s" % label)

    def _error(self, text: str) -> None:
        """以红色面板渲染错误信息，提升可见性。"""
        if self.rich_output:
            self.console.print(
                Panel(Text(text), border_style="red", style="red", padding=(0, 1)),
            )
            return
        self._plain("错误：%s" % text)

    def _start_tool_status(self, tool_call_id: str, tool_name: str) -> None:
        label = self._tool_label(tool_name)
        if self.rich_output and self.console.is_terminal:
            status = self.console.status("%s…" % label, spinner="dots", spinner_style="cyan")
            status.start()
            self._active_statuses[tool_call_id] = status
        elif not self.rich_output:
            self._plain("系统> %s…" % label)

    def _start_model_status(self, model_name: str) -> None:
        if self.rich_output and self.console.is_terminal:
            status = self.console.status(
                "正在思考 · %s…" % model_name,
                spinner="dots",
                spinner_style="cyan",
            )
            status.start()
            self._active_statuses["__model__"] = status

    def _finish_tool_status(self, tool_call_id: str) -> None:
        status = self._active_statuses.pop(tool_call_id, None)
        if status is not None:
            status.stop()

    def _render_message_tool_displays(self, message: Any, rendered: set[str]) -> set[str]:
        shown: set[str] = set()
        if message is None or not hasattr(message, "get_content_blocks"):
            return shown
        for block in message.get_content_blocks("tool_result"):
            shown.update(
                self._render_tool_output_displays(_tool_output_text(block.output), rendered),
            )
        return shown

    def _render_tool_output_displays(self, output: str, rendered: set[str]) -> set[str]:
        shown: set[str] = set()
        for display in _iter_terminal_displays(output):
            key = json.dumps(display, ensure_ascii=False, sort_keys=True)
            if key in rendered:
                continue
            rendered.add(key)
            if display.get("kind") == "artifact_list":
                self._artifact_list(display)
            else:
                self._terminal_canvas(str(display.get("content") or ""))
            shown.add(str(display.get("kind") or "unknown"))
        return shown

    def _artifact_list(self, display: dict[str, Any]) -> None:
        title = str(display.get("title") or "文件已生成")
        items = display.get("items") if isinstance(display.get("items"), list) else []
        if not self.rich_output:
            self._plain(title)
            for item in items:
                if isinstance(item, dict):
                    self._plain("%s：%s" % (item.get("label") or "文件", item.get("path") or ""))
            return
        self.console.print(title, style="bold green")
        for item in items:
            if not isinstance(item, dict):
                continue
            self.console.print(
                Text.assemble(
                    ("  %-8s" % (str(item.get("label") or "文件") + "："), "dim"),
                    str(item.get("path") or ""),
                ),
                soft_wrap=True,
            )

    def _terminal_canvas(self, content: str) -> None:
        if self.rich_output:
            self.console.print(self.profile.display_title, style="bold cyan")
            self.console.print(Text(content))
            self.console.print()
            return
        self._plain(self.profile.display_title)
        self._plain(content)

    def _render_json_result(self, output: str) -> bool:
        """把工具返回的 JSON 结果直接渲染到终端，避免模型复述。"""
        try:
            data = json.loads(output) if output else {}
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        payload = {key: value for key, value in data.items() if key != "ok"}
        if not payload:
            return False
        self._terminal_canvas(json.dumps(payload, ensure_ascii=False, indent=2))
        return True

    def _system(self, text: str) -> None:
        if self.rich_output:
            self.console.print("系统> %s" % text, style="dim")
            return
        self._plain("系统> %s" % text)

    def _plain(self, text: str) -> None:
        if self.output_fn is None:
            print(text)
            return
        self.output_fn(text)


def _markdown_to_plain(text: str) -> str:
    """Markdown 转纯文本的轻量回退，供测试或注入 output_fn 的调用方使用。"""
    lines: list[str] = []
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if set(stripped) <= {"-", "_", "*"} and len(stripped) >= 3:
            continue
        while stripped.startswith("#"):
            stripped = stripped[1:].lstrip()
        stripped = stripped.replace("**", "").replace("__", "").replace("`", "")
        lines.append(stripped)
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def _parse_tool_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _match_clarification_option(
    answer: str,
    options: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    normalized = answer.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(options):
            return options[index]
    folded = normalized.casefold()
    for option in options:
        identifiers = [
            str(option.get("id") or ""),
            str(option.get("label") or ""),
        ]
        if folded in {item.casefold() for item in identifiers if item}:
            return option
    return None


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if getattr(item, "type", None) == "text":
                parts.append(str(getattr(item, "text", "")))
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _last_text_content(message: Any) -> str:
    if message is None or not hasattr(message, "get_content_blocks"):
        return ""
    blocks = message.get_content_blocks("text")
    if not blocks:
        return ""
    return str(getattr(blocks[-1], "text", "") or "").strip()


def _iter_terminal_displays(value: Any):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _iter_terminal_displays(parsed)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_terminal_displays(item)
        return
    if not isinstance(value, dict):
        return
    display = value.get("display")
    has_display = False
    if isinstance(display, dict) and display.get("kind") in {
        "terminal_canvas",
        "artifact_list",
    }:
        content = display.get("content")
        if display.get("kind") == "artifact_list" or (
            isinstance(content, str) and content.strip()
        ):
            yield display
            has_display = True
    preview = value.get("terminalPreview")
    if not has_display and isinstance(preview, str) and preview.strip():
        yield {"kind": "terminal_canvas", "content": preview, "width": None}
    for item in value.values():
        if isinstance(item, (dict, list)):
            yield from _iter_terminal_displays(item)


def build_agent_console(
    agent_key: str = "erp-billing",
    resume: bool = False,
) -> InteractiveAgentConsole:
    """按 agent_key 构建一个可插拔业务 Agent 对话控制台。"""
    options = AgentConsoleBuildOptions(
        resume=resume,
    )
    registration = resolve_agent_console_registration(agent_key)
    runtime = registration.runtime_builder(options)
    session_restored = (
        _load_session_snapshot(runtime.session, runtime.session_state_path)
        if options.resume
        else False
    )
    settings = LLMSettings.from_env()
    agent_state = (
        load_agent_state(runtime.agent_state_path)
        if options.resume and runtime.agent_state_path is not None
        else None
    )
    agent = build_agent(
        runtime.tool_provider,
        settings,
        registration.agent_spec,
        state=agent_state,
    )
    startup_notices: tuple[str, ...] = ()
    if options.resume and (agent_state is not None or session_restored):
        startup_notices = ("已恢复上一轮 %s 会话上下文。" % registration.key,)
    return InteractiveAgentConsole(
        agent=agent,
        session=runtime.session,
        max_turns=runtime.max_turns,
        model_label="%s / %s" % (settings.provider, settings.model_name),
        stream_enabled=settings.stream,
        profile=registration.profile,
        user_input_transformer=registration.user_input_transformer,
        agent_state_path=runtime.agent_state_path,
        session_state_path=runtime.session_state_path,
        startup_notices=startup_notices,
    )


def _build_erp_billing_agent_runtime(options: AgentConsoleBuildOptions) -> AgentConsoleRuntime:
    """构建开单参考客户端；生产开单服务使用 billing.mcp_service 独立部署。"""
    from erp_billing.adapters import create_match_logger_from_env
    from erp_billing.config import ErpBillingSettings
    from erp_billing.session import ErpBillingSession

    settings = ErpBillingSettings.from_env()
    session = ErpBillingSession.from_settings(
        settings,
        allow_missing_catalog=True,
        match_logger=create_match_logger_from_env(),
    )
    context = _local_invocation_context()
    contexts = InvocationContextStore(default=context)
    toolset = BillingToolSet(
        session,
        UnavailableBillingApi(),
        contexts,
    )
    max_turns = _env_max_turns(
        "ERP_BILLING_CHAT_MAX_TURNS",
        "ERP_BILLING_CONFIG_INVALID",
    )
    return AgentConsoleRuntime(
        session=session,
        tool_provider=LocalToolProvider(toolset),
        agent_state_path=None,
        session_state_path=None,
        max_turns=max_turns,
    )


async def erp_billing_user_input_transformer(text: str) -> str:
    """开单会话输入预处理：整行图片路径先经多模态模型识别为订单文本。

    本地 chat 与远程 MCP 对话控制台共用，保证两条链路行为一致；
    在主事件循环内直接 await，模型 HTTP 连接随主循环统一清理。
    """
    from .image_order import maybe_order_text_from_image_input

    converted = await maybe_order_text_from_image_input(text)
    return converted if converted is not None else text


def _local_invocation_context() -> InvocationContext:
    """为单用户参考 CLI 创建上下文；生产服务必须从认证结果构造。"""
    return InvocationContext(
        tenant_id=get_env_value("CAPABILITY_TENANT_ID", "local").strip() or "local",
        subject_id=get_env_value("CAPABILITY_SUBJECT_ID", "local-user").strip() or "local-user",
        account_id=get_env_value("ERP_BILLING_ACCOUNT_ID", "local-account").strip()
        or "local-account",
        session_id="local-cli",
        scopes=frozenset({"billing:read"}),
    )


_AGENT_CONSOLE_REGISTRY = (
    AgentConsoleRegistration(
        key="erp-billing",
        aliases=frozenset({"erp", "erp-billing", "billing"}),
        agent_spec=ERP_BILLING_AGENT_SPEC,
        profile=ERP_BILLING_CONSOLE_PROFILE,
        runtime_builder=_build_erp_billing_agent_runtime,
        mcp_name="erp-billing",
        user_input_transformer=erp_billing_user_input_transformer,
    ),
)
"""统一 CLI 的 Agent 注册表；新增 Agent 时追加一个注册项即可接入本地与远程两条链路。"""


def resolve_agent_console_registration(agent_key: str) -> AgentConsoleRegistration:
    """按 key 或别名解析注册项；本地 chat 与远程 MCP 控制台共用同一入口。"""
    normalized = agent_key.strip().casefold().replace("_", "-")
    for registration in _AGENT_CONSOLE_REGISTRY:
        if normalized == registration.key or normalized in registration.aliases:
            return registration
    raise DomainError("AGENT_NOT_FOUND", "未知 agent：%s" % agent_key)


def _load_session_snapshot(session: Any, state_path: Path | None) -> bool:
    restore = getattr(session, "restore_snapshot", None)
    if restore is None or state_path is None or not state_path.is_file():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        restore(payload)
        logger.info("业务会话状态已恢复 path=%s", state_path)
        return True
    except Exception as exc:
        logger.warning("业务会话状态恢复失败，将只恢复 AgentState: %s", exc)
        return False


def _save_session_snapshot(session: Any, state_path: Path) -> None:
    snapshot = getattr(session, "snapshot", None)
    if snapshot is None:
        return
    try:
        payload = snapshot()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        logger.info("业务会话状态已保存 path=%s", state_path)
    except Exception as exc:
        logger.warning("业务会话状态保存失败: %s", exc)


def _env_max_turns(name: str, error_code: str) -> Optional[int]:
    value = get_env_value(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DomainError(error_code, "%s 必须是正整数或留空" % name) from exc
    if parsed < 1:
        raise DomainError(error_code, "%s 必须是正整数或留空" % name)
    return parsed


def _parse_ok(output: str) -> bool:
    """检查工具输出 JSON 是否表示成功（ok=true）。"""
    try:
        parsed = json.loads(output) if output else {}
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("ok") is True


def _base_tool_name(tool_name: str) -> str:
    """去掉远程 MCP 工具名前缀（如 mcp__erp-billing__create_draft）得到业务工具名。"""
    return tool_name.rsplit("__", 1)[-1]
