import json
from io import StringIO

import pytest
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import AssistantMsg
from agentscope.message._block import ToolResultState
from rich.console import Console

from gjp_cli.interactive import (
    ERP_BILLING_CONSOLE_PROFILE,
    InteractiveAgentConsole,
    _markdown_to_plain,
    resolve_agent_console_registration,
)
from gjp_common.errors import DomainError


class RecordingSession:
    def __init__(self) -> None:
        self.finished = False
        self.user_turns = []

    def record_user_turn(self, text):
        self.user_turns.append(text)


class RecordingAgent:
    def __init__(self, session, stop_after=2):
        self.session = session
        self.stop_after = stop_after
        self.messages = []

    async def reply(self, inputs):
        self.messages.append(inputs.get_text_content())
        if len(self.messages) >= self.stop_after:
            self.session.finished = True
        return AssistantMsg(name="agent", content="收到第%d轮" % len(self.messages))


class StreamingBillingDraftAgent:
    """模拟远程 MCP prepare_sales_order 返回，验证终端直接渲染结构化开单结果。"""

    async def reply_stream(self, inputs):
        assert inputs.get_text_content() == "土豆5斤"
        yield ReplyStartEvent(session_id="s1", reply_id="r1", name="agent")
        yield ToolCallStartEvent(
            reply_id="r1",
            tool_call_id="draft1",
            tool_call_name="mcp__erp-billing__prepare_sales_order",
        )
        yield ToolResultStartEvent(
            reply_id="r1",
            tool_call_id="draft1",
            tool_call_name="mcp__erp-billing__prepare_sales_order",
        )
        yield ToolResultTextDeltaEvent(
            reply_id="r1",
            tool_call_id="draft1",
            delta=json.dumps(
                {
                    "ok": True,
                    "confirmedProducts": [
                        {
                            "ptypeid": "P001",
                            "pfullname": "土豆",
                            "unit": "斤",
                            "quantity": 5,
                        },
                    ],
                    "recommendedProducts": [],
                    "unmatchedProducts": [],
                },
                ensure_ascii=False,
            ),
        )
        yield ToolResultEndEvent(
            reply_id="r1",
            tool_call_id="draft1",
            state=ToolResultState.SUCCESS,
        )
        yield TextBlockStartEvent(reply_id="r1", block_id="final")
        yield TextBlockDeltaEvent(reply_id="r1", block_id="final", delta="已生成开单结果")
        yield TextBlockEndEvent(reply_id="r1", block_id="final")
        yield ReplyEndEvent(session_id="s1", reply_id="r1")


def test_console_forwards_every_billing_turn_without_command_parsing():
    session = RecordingSession()
    agent = RecordingAgent(session)
    messages = iter(["土豆5斤", "土豆改成8斤，再加2斤牛肉"])
    shown = []
    console = InteractiveAgentConsole(
        agent=agent,
        session=session,
        input_fn=lambda _: next(messages),
        output_fn=shown.append,
    )

    assert console.run() == 0
    assert agent.messages == ["土豆5斤", "土豆改成8斤，再加2斤牛肉"]
    assert session.user_turns == agent.messages
    assert any("ERP Billing Agent" in line for line in shown)
    assert any("收到第2轮" in line for line in shown)


def test_console_renders_agent_markdown_with_erp_banner():
    session = RecordingSession()
    stream = StringIO()

    class MarkdownAgent:
        async def reply(self, _inputs):
            session.finished = True
            return AssistantMsg(
                name="agent",
                content="### 已完成\n\n- 商品：`土豆`\n- 数量：5斤",
            )

    ui = InteractiveAgentConsole(
        agent=MarkdownAgent(),
        session=session,
        input_fn=lambda _: "土豆5斤",
        console=Console(file=stream, force_terminal=False, width=100),
    )

    assert ui.run() == 0
    rendered = stream.getvalue()
    assert "ERP Billing Agent" in rendered
    assert "已完成" in rendered
    assert "土豆" in rendered


def test_console_renders_billing_draft_json_directly_from_tool_result():
    session = RecordingSession()
    stream = StringIO()
    ui = InteractiveAgentConsole(
        agent=StreamingBillingDraftAgent(),
        session=session,
        input_fn=lambda _: "土豆5斤",
        console=Console(file=stream, force_terminal=False, width=120),
        max_turns=1,
        profile=ERP_BILLING_CONSOLE_PROFILE,
    )

    assert ui.run() == 0
    output = stream.getvalue()
    assert "ERP AI开单" in output
    assert "confirmedProducts" in output
    assert "土豆" in output
    assert '"ok"' not in output
    assert "已生成开单结果" in output


def test_plain_output_fallback_removes_markdown_noise():
    assert _markdown_to_plain("### 已完成\n\n**商品**：`土豆`") == "已完成\n\n商品：土豆"


def test_agent_registry_only_accepts_billing_aliases():
    assert resolve_agent_console_registration("billing").key == "erp-billing"
    with pytest.raises(DomainError, match="未知 agent"):
        resolve_agent_console_registration("unknown")
