"""缺陷挖掘复现测试集（bug hunt，2026-08-28 全方位测试轮次）。

约定：
- ``xfail`` 用例断言【正确行为】，当前实现存在缺陷时按 xfail 记录；
  修复后用例 XPASS，应把 ``xfail`` 标记移除转为常规回归测试。
- 标注「当前行为记录」的用例断言现有行为，固化低危缺陷或设计取舍，
  供缺陷报告引用。
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from erp_billing.app import (
    ApiKeyIdentityResolver,
    SessionCredentialStore,
    _conversation_id_from_mcp_context,
)
from erp_billing.config import ErpBillingSettings
from erp_billing.ports import (
    BillingProductSnapshot,
    BillingReferenceSnapshot,
    BillingSalesOrderDetailResult,
    BillingSalesOrderResult,
)
from erp_billing.session import ErpBillingSession, _normalize_confirmed_products
from erp_billing.session import parse_order_text
from erp_billing.toolset import BillingToolSet
from gjp_common.context import InvocationContext, InvocationContextStore


def _settings(tmp_path):
    return ErpBillingSettings(
        product_catalog_path=tmp_path / "catalog.json",
        alias_path=None,
        use_default_fresh_aliases=True,
        category_path=None,
        use_default_categories=True,
        recommendation_score=0.60,
        auto_sync_limit=10000,
    )


_CATALOG_PRODUCTS = [
    {"productId": "P001", "name": "土豆", "unit": "斤", "price": 8.5},
    {"productId": "P002", "name": "书本", "unit": "本", "price": 3.0},
]


def _toolset(tmp_path, api):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"products": _CATALOG_PRODUCTS}, ensure_ascii=False),
        encoding="utf-8",
    )
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )
    context = InvocationContext(
        tenant_id="tenant-bug-hunt",
        subject_id="user-bug-hunt",
        account_id="billing-bug-hunt",
        session_id="session-bug-hunt",
        scopes=frozenset({"billing:read", "billing:write"}),
    )
    return BillingToolSet(session, api, InvocationContextStore(default=context))


class RecordingBillingApi:
    """完整记录调用的假 ERP API，用于缺陷复现。"""

    def __init__(self) -> None:
        self.created_payloads: list[dict] = []
        self.detail_queries: list[str] = []

    @staticmethod
    def _reference(options, keyword, limit, page):
        return BillingReferenceSnapshot(
            options=tuple(options),
            total=len(options),
            page_num=page,
            page_size=limit,
        )

    async def fetch_products(self, context, limit=None):
        # 与真实 adapter 行为一致：limit=0 返回空快照，由 loader 报空目录错误
        products = (
            _CATALOG_PRODUCTS
            if limit is None
            else _CATALOG_PRODUCTS[: max(0, limit)]
        )
        return BillingProductSnapshot(products=tuple(products))

    async def search_customers(self, context, keyword, limit=5, page=1):
        return self._reference([{"id": "C001", "name": "客户甲"}], keyword, limit, page)

    async def search_warehouses(self, context, keyword, limit=5, page=1):
        return self._reference([{"id": "W001", "name": "一号仓"}], keyword, limit, page)

    async def search_staff(self, context, keyword, limit=5, page=1):
        return self._reference([{"id": "H001", "name": "张三"}], keyword, limit, page)

    async def create_sales_order(self, context, payload):
        # 让出事件循环放大并发窗口，模拟真实 ERP 网络往返
        await asyncio.sleep(0.01)
        self.created_payloads.append(deepcopy(payload))
        return BillingSalesOrderResult(order_id="1001")

    async def get_sales_order_detail(self, context, order_id):
        self.detail_queries.append(order_id)
        return BillingSalesOrderDetailResult(order={"orderNo": "XS1"})

    async def search_sales_orders(self, context, **kwargs):
        from erp_billing.ports import BillingSalesOrderPageResult

        return BillingSalesOrderPageResult(
            total=0, page_num=1, page_size=20, orders=(),
        )

    async def void_sales_order(self, context, order_id):
        return None

    async def update_sales_order(self, context, order_id, payload):
        return BillingSalesOrderResult(order_id=order_id)


def _ready_preview_arguments(order_text: str) -> dict:
    return {
        "order_text": order_text,
        "customer": "客户甲",
        "warehouse": "一号仓",
        "handler": "张三",
        "order_date": "2026-08-28",
        "save_type": "final",
    }


# ---------------------------------------------------------------------------
# BUG-B1：行间分隔符只认内置单位白名单，自定义单位行会把下一行吞进来
# ---------------------------------------------------------------------------


def test_separator_splits_builtin_unit_line():
    """白名单单位（斤）结尾 + 空格时正确拆分为两行（正常路径）。"""
    lines = parse_order_text("土豆3斤 书本2本")

    assert [(line.requested_name, line.quantity) for line in lines] == [
        ("土豆", 3.0),
        ("书本", 2.0),
    ]


@pytest.mark.xfail(
    reason="BUG-B1: _DIGIT_UNIT_SPACE_RE 只认内置单位白名单；自定义单位（本）在前时"
    "两行合并成一行垃圾数据（name='书本2本 土豆', qty=3, unit='斤'）",
    strict=False,
)
def test_separator_splits_custom_unit_line():
    """自定义单位（本）结尾 + 空格时同样应拆分为两行。"""
    lines = parse_order_text("书本2本 土豆3斤")

    assert [(line.requested_name, line.quantity) for line in lines] == [
        ("书本", 2.0),
        ("土豆", 3.0),
    ]


# ---------------------------------------------------------------------------
# BUG-B2：数量 0 未在预览链路校验，可生成 0 数量可提交预览
# ---------------------------------------------------------------------------


def test_preview_rejects_zero_quantity(tmp_path):
    """数量为 0 的商品行应被结构化拒绝，不得生成可提交预览。"""
    result = asyncio.run(
        _toolset(tmp_path, RecordingBillingApi()).preview_sales_order(
            **_ready_preview_arguments("土豆0斤"),
        ),
    )

    assert result["ok"] is False


def test_update_rejects_zero_quantity_for_contrast(tmp_path):
    """对照：update 链路对数量 0 有结构化拦截（证明两链路不一致）。"""
    result = asyncio.run(
        _toolset(tmp_path, RecordingBillingApi()).update_sales_order(
            order_id="1001",
            order_date="2026-08-28",
            handler_id="张三",
            items=[{"product_id": "P001", "quantity": 0}],
            confirmed_by_user=True,
        ),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_item_invalid"


# ---------------------------------------------------------------------------
# BUG-B3：负号静默并入商品名，数量符号丢失
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="BUG-B3: '土豆-2斤' 解析为 name='土豆-', qty=2（正数）；负号被静默"
    "并入商品名且数量符号丢失，语义静默改变",
    strict=False,
)
def test_negative_sign_not_swallowed_into_name():
    """负号不应被静默并入商品名（数量符号不得丢失）。"""
    lines = parse_order_text("土豆-2斤")

    assert lines[0].requested_name == "土豆"


# ---------------------------------------------------------------------------
# BUG-B4：超大数量触发 decimal.InvalidOperation 未捕获，工具协议级崩溃
# ---------------------------------------------------------------------------


def test_huge_quantity_preview_returns_structured_response(tmp_path):
    """超大数量应返回结构化错误（或成功），不得让异常逃逸成协议错误。"""
    result = asyncio.run(
        _toolset(tmp_path, RecordingBillingApi()).preview_sales_order(
            **_ready_preview_arguments("土豆" + "9" * 26 + "斤"),
        ),
    )

    assert isinstance(result, dict) and "ok" in result


# ---------------------------------------------------------------------------
# BUG-B5：同一预览并发提交无互斥保护，幂等机制被并发击穿
# ---------------------------------------------------------------------------


def _prepare_preview(toolset) -> str:
    result = asyncio.run(
        toolset.preview_sales_order(**_ready_preview_arguments("土豆2斤")),
    )
    assert result["ready_to_submit"] is True, result
    return result["preview_id"]


def test_concurrent_submit_same_idempotency_key_writes_once(tmp_path):
    """同一幂等键并发提交两次，ERP 只应写入一张单。"""
    api = RecordingBillingApi()
    toolset = _toolset(tmp_path, api)
    preview_id = _prepare_preview(toolset)

    async def _submit(key: str):
        return await toolset.submit_sales_order(
            preview_id=preview_id,
            idempotency_key=key,
            confirmed_by_user=True,
        )

    async def _run():
        return await asyncio.gather(_submit("bug-hunt-key"), _submit("bug-hunt-key"))

    results = asyncio.run(_run())

    assert len(api.created_payloads) == 1, (
        "同一幂等键并发提交应只产生一次 ERP 写入，实际 %d 次"
        % len(api.created_payloads)
    )
    assert all(result["ok"] is True for result in results)


def test_concurrent_submit_different_keys_writes_once(tmp_path):
    """预览一次性消费应在并发下生效：同一预览只允许提交一次。"""
    api = RecordingBillingApi()
    toolset = _toolset(tmp_path, api)
    preview_id = _prepare_preview(toolset)

    async def _submit(key: str):
        return await toolset.submit_sales_order(
            preview_id=preview_id,
            idempotency_key=key,
            confirmed_by_user=True,
        )

    async def _run():
        return await asyncio.gather(
            _submit("bug-hunt-key-a"), _submit("bug-hunt-key-b"),
        )

    results = asyncio.run(_run())

    assert len(api.created_payloads) == 1, (
        "同一预览并发提交应只产生一次 ERP 写入，实际 %d 次"
        % len(api.created_payloads)
    )
    assert [result["ok"] for result in results].count(True) == 1


# ---------------------------------------------------------------------------
# BUG-B6："各"模式对偶数长度名称串盲切双字，跨词产出垃圾商品名
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="BUG-B6: _EACH_LIST_RE 对偶数长度名称串按双字硬切，"
    "'火龙果猕猴桃各5斤' 产出 ['火龙','果猕','猴桃'] 跨词垃圾名",
    strict=False,
)
def test_each_list_does_not_split_across_word_boundary():
    """"各"模式不得产出跨词边界的垃圾商品名。"""
    lines = parse_order_text("火龙果猕猴桃各5斤")

    names = [line.requested_name for line in lines]
    assert "果猕" not in names, "跨词双字切分产出垃圾名：%s" % names


def test_each_list_splits_two_char_names():
    """对照：双字商品名串（设计目标场景）切分正确。"""
    lines = parse_order_text("苹果香蕉各5斤")

    assert [line.requested_name for line in lines] == ["苹果", "香蕉"]


# ---------------------------------------------------------------------------
# BUG-B7：getSalesOrder 对空单号无校验，与 voidSalesOrder 行为不一致
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="BUG-B7: get_sales_order 不校验空白单号，把空串直接发给 ERP；"
    "void_sales_order 有 erp_sales_order_id_invalid 拦截，两工具行为不一致",
    strict=False,
)
def test_get_sales_order_rejects_blank_order_id(tmp_path):
    """空白单号应在工具层结构化拒绝，不产生 ERP 调用。"""
    api = RecordingBillingApi()
    result = asyncio.run(
        _toolset(tmp_path, api).get_sales_order(order_id="   "),
    )

    assert result["ok"] is False
    assert api.detail_queries == [], "空白单号不应触发 ERP 查询"


# ---------------------------------------------------------------------------
# 当前行为记录（低危缺陷 / 设计取舍，缺陷报告引用）
# ---------------------------------------------------------------------------


def test_current_behavior_conversation_id_truncation_collision():
    """低危：会话标识截断到 64 字符，前缀相同的长对话 ID 会共享会话。

    两个不同的对话（仅第 65 字符起不同）被映射到同一 session_id，
    预览与幂等缓存跨对话共享。
    """
    header = "conv-" + "x" * 60

    def _context(conversation_id: str) -> str:
        mcp_context = SimpleNamespace(
            request=SimpleNamespace(
                headers={"x-conversation-id": conversation_id},
            ),
        )
        return _conversation_id_from_mcp_context(mcp_context)

    first = _context(header + "AAAA")
    second = _context(header + "BBBB")

    assert first == second, "前 64 字符相同的不同对话 ID 被截断为同一会话键"


def test_current_behavior_api_key_session_isolation():
    """对照：X-API-Key 无对话标识时按 key 隔离；传不同对话 ID 则隔离。"""
    store = SessionCredentialStore()
    resolver = ApiKeyIdentityResolver(store)

    def _resolve(conversation_id: str) -> str:
        headers = {"x-api-key": "ak-test"}
        if conversation_id:
            headers["x-conversation-id"] = conversation_id
        context = resolver.resolve(
            SimpleNamespace(request=SimpleNamespace(headers=headers)),
        )
        return context.session_id

    assert _resolve("c1") != _resolve("c2")
    assert _resolve("c1") == _resolve("c1")


def test_current_behavior_duplicate_line_id_last_wins():
    """低危：confirmed_products 中重复 line_id 静默后者覆盖前者。"""
    normalized = _normalize_confirmed_products([
        {"line_id": "L001", "product_id": "P001"},
        {"line_id": "L001", "product_id": "P002"},
    ])

    assert normalized == {"L001": "P002"}


def test_current_behavior_sync_products_zero_limit_error_message(tmp_path):
    """低危：syncProducts 传 limit=0（或负数）时报"没有可用于开单的商品"，
    报错语义误导（实际是参数无效），且输入 Schema 未约束 limit 下界。"""
    result = asyncio.run(
        _toolset(tmp_path, RecordingBillingApi()).sync_products(limit=0),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_live_product_empty"
