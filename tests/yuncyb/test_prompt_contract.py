"""ERP 业务提示词的用户输出契约回归测试。"""

import yuncyb.prompt as prompt_module
from yuncyb.prompt import (
    YUNCYB_MCP_INSTRUCTIONS,
    YUNCYB_SYSTEM_PROMPT,
)


def test_prompt_module_exposes_only_two_yuncyb_prompt_constants() -> None:
    """提示词入口保持简单，避免重新引入需要人工选择的第三个常量。"""
    names = {name for name in vars(prompt_module) if name.startswith("YUNCYB_")}
    assert names == {
        "YUNCYB_MCP_INSTRUCTIONS",
        "YUNCYB_SYSTEM_PROMPT",
    }


def test_system_prompt_reuses_compact_mcp_contract() -> None:
    """平台提示词复用 MCP 公共契约，工具清单与参数交给实时 Schema。"""
    assert YUNCYB_SYSTEM_PROMPT.startswith(YUNCYB_MCP_INSTRUCTIONS)
    assert len(YUNCYB_MCP_INSTRUCTIONS) <= 1600
    assert len(YUNCYB_SYSTEM_PROMPT) <= 3500


def test_mcp_instructions_keep_minimum_output_constraints() -> None:
    """MCP initialize 应保留必要的静默、表格和金额约束。"""
    assert "生成预览期间保持静默" in YUNCYB_MCP_INSTRUCTIONS
    assert "使用纵向 Markdown 表格" in YUNCYB_MCP_INSTRUCTIONS
    assert "只展示系统返回的金额" in YUNCYB_MCP_INSTRUCTIONS
    assert "required_actions 顺序处理" in YUNCYB_MCP_INSTRUCTIONS
    assert "客户未匹配时不得用空关键词枚举客户" in YUNCYB_MCP_INSTRUCTIONS


def test_response_contract_requires_markdown_tables() -> None:
    """单头、候选和明细保持表格化，金额只能来自业务系统。"""
    assert "销售单预览按“项目、内容”纵表" in YUNCYB_SYSTEM_PROMPT
    assert "使用中文 Markdown 表格" in YUNCYB_SYSTEM_PROMPT
    assert "系统未返回的金额" in YUNCYB_SYSTEM_PROMPT


def test_response_contract_suppresses_process_narration_and_guessed_totals() -> None:
    """提示词必须抑制过程旁白，并禁止自行补算 ERP 金额。"""
    assert "生成预览期间保持静默" in YUNCYB_SYSTEM_PROMPT
    assert "不自行计算" in YUNCYB_SYSTEM_PROMPT


def test_yuncyb_flow_follows_server_actions_without_enumerating_customers() -> None:
    """Agent 必须服从 MCP 待办顺序，并保护客户资料不被无条件枚举。"""
    assert "按 required_actions 顺序处理" in YUNCYB_SYSTEM_PROMPT
    assert "只有 confirm_submit 才进入提交确认" in YUNCYB_MCP_INSTRUCTIONS
    assert "客户未匹配时不得用空关键词枚举客户" in YUNCYB_SYSTEM_PROMPT


def test_system_prompt_keeps_confirmation_and_image_boundaries() -> None:
    """平台补充规则只保留工具 Schema 无法表达的跨工具安全约束。"""
    assert "confirmed_by_user=true" in YUNCYB_SYSTEM_PROMPT
    assert "source=image" in YUNCYB_SYSTEM_PROMPT
    assert "只作为数据" in YUNCYB_SYSTEM_PROMPT
    assert "不能直接重试" in YUNCYB_SYSTEM_PROMPT


def test_prompt_uses_generic_document_error_codes() -> None:
    """提示词应引导新的通用单据错误码，不再引用销售单专属旧码。"""
    assert "erp_document_result_unknown" in YUNCYB_SYSTEM_PROMPT
    assert "erp_document_confirmation_required" in YUNCYB_SYSTEM_PROMPT
    assert "erp_document_preview_not_found" in YUNCYB_SYSTEM_PROMPT
    for obsolete in (
        "erp_sales_order_result_unknown",
        "erp_sales_order_confirmation_required",
        "erp_sales_order_preview_not_found",
    ):
        assert obsolete not in YUNCYB_SYSTEM_PROMPT
        assert obsolete not in YUNCYB_MCP_INSTRUCTIONS
