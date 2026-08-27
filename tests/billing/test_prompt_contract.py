"""ERP 开单提示词的用户输出契约回归测试。"""

import erp_billing.prompt as prompt_module
from erp_billing.prompt import (
    ERP_BILLING_MCP_INSTRUCTIONS,
    ERP_BILLING_SYSTEM_PROMPT,
)


def test_prompt_module_exposes_only_two_billing_prompt_constants() -> None:
    """提示词入口保持简单，避免重新引入需要人工选择的第三个常量。"""
    names = {name for name in vars(prompt_module) if name.startswith("ERP_BILLING_")}
    assert names == {
        "ERP_BILLING_MCP_INSTRUCTIONS",
        "ERP_BILLING_SYSTEM_PROMPT",
    }


def test_system_prompt_remains_compact_and_lists_every_tool() -> None:
    """完整提示词应控制体积，同时保留十个工具的准确名称。"""
    assert len(ERP_BILLING_SYSTEM_PROMPT) <= 5000
    tool_names = {
        "syncProducts",
        "listProducts",
        "searchProducts",
        "searchBillingReferences",
        "previewSalesOrder",
        "submitSalesOrder",
        "getSalesOrder",
        "listSalesOrders",
        "voidSalesOrder",
        "updateSalesOrder",
    }
    for tool_name in tool_names:
        assert tool_name in ERP_BILLING_SYSTEM_PROMPT


def test_mcp_instructions_keep_minimum_output_constraints() -> None:
    """MCP initialize 应保留必要的静默、表格和金额约束。"""
    assert "生成预览期间保持静默" in ERP_BILLING_MCP_INSTRUCTIONS
    assert "使用纵向 Markdown 表格" in ERP_BILLING_MCP_INSTRUCTIONS
    assert "只展示系统返回的金额" in ERP_BILLING_MCP_INSTRUCTIONS
    assert "required_actions 顺序处理" in ERP_BILLING_MCP_INSTRUCTIONS
    assert "客户未匹配时不得用空关键词枚举客户" in ERP_BILLING_MCP_INSTRUCTIONS


def test_response_contract_requires_structured_sales_order_header() -> None:
    """销售单头必须使用表格，不能回退到分行文字。"""
    assert "销售单单头是强制例外" in ERP_BILLING_SYSTEM_PROMPT
    assert "| 客户 | 【客户名称或—】 |" in ERP_BILLING_SYSTEM_PROMPT
    assert "| 出库仓库 | 【仓库名称或—】 |" in ERP_BILLING_SYSTEM_PROMPT
    assert "| 经手人 | 【经手人名称或—】 |" in ERP_BILLING_SYSTEM_PROMPT
    assert "单头信息分行展示" not in ERP_BILLING_SYSTEM_PROMPT
    assert "不必强行套表格" not in ERP_BILLING_SYSTEM_PROMPT


def test_response_contract_suppresses_process_narration_and_guessed_totals() -> None:
    """提示词必须抑制过程旁白，并禁止自行补算 ERP 金额。"""
    assert "连续调用工具期间保持静默" in ERP_BILLING_SYSTEM_PROMPT
    assert "系统未返回时不得自行计算" in ERP_BILLING_SYSTEM_PROMPT
    assert "末行展示合计金额" not in ERP_BILLING_SYSTEM_PROMPT


def test_billing_flow_follows_server_actions_without_enumerating_customers() -> None:
    """Agent 必须服从 MCP 待办顺序，并保护客户资料不被无条件枚举。"""
    assert "严格按 required_actions 的返回顺序" in ERP_BILLING_SYSTEM_PROMPT
    assert "只有 confirm_submit 才进入提交确认" in ERP_BILLING_MCP_INSTRUCTIONS
    assert "不得用空关键词查询完整客户列表" in ERP_BILLING_SYSTEM_PROMPT
