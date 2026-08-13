"""销售单列表查询的 adapter 层参数校验测试。

覆盖 ErpAuthenticatedHttpAdapter.search_sales_orders 在协议层对日期
格式和先后顺序的校验，确保非法参数在发往 ERP 前被拦截，避免后端
类型转换异常泄露技术栈错误给调用方。
"""

import asyncio

import pytest

from erp_billing.adapters import ErpAuthenticatedHttpAdapter
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError

_CONTEXT = InvocationContext(
    tenant_id="tenant-test",
    subject_id="user-test",
    account_id="billing-test",
)


class _FakeHttp:
    """仅记录 GET 调用的最小已鉴权 JSON 客户端。"""

    def __init__(self):
        self.get_calls = []

    async def get_json(self, context, path, params=None):
        self.get_calls.append((path, dict(params or {})))
        return {
            "code": "A00000",
            "data": {"total": 0, "pageNum": 1, "pageSize": 20, "list": []},
        }

    async def post_json(self, context, path, payload):
        return {"code": "A00000", "data": ""}

    async def put_json(self, context, path, payload=None):
        return {"code": "A00000", "data": ""}


def _adapter():
    http = _FakeHttp()
    return http, ErpAuthenticatedHttpAdapter(http)


def test_invalid_start_date_rejected_before_request():
    """斜杠等非法日期格式在发往 ERP 前被拦截，不泄露后端异常。"""
    http, adapter = _adapter()

    with pytest.raises(DomainError) as exc:
        asyncio.run(adapter.search_sales_orders(_CONTEXT, start_date="2026/06/01"))

    assert exc.value.code == "erp_sales_order_date_invalid"
    assert not http.get_calls


def test_invalid_end_date_rejected_before_request():
    http, adapter = _adapter()

    with pytest.raises(DomainError) as exc:
        asyncio.run(adapter.search_sales_orders(_CONTEXT, end_date="2026-06-31"))

    assert exc.value.code == "erp_sales_order_date_invalid"
    assert not http.get_calls


def test_end_before_start_rejected_before_request():
    http, adapter = _adapter()

    with pytest.raises(DomainError) as exc:
        asyncio.run(
            adapter.search_sales_orders(
                _CONTEXT,
                start_date="2026-08-04",
                end_date="2026-07-04",
            )
        )

    assert exc.value.code == "erp_sales_order_date_invalid"
    assert "结束日期不能早于开始日期" in exc.value.message
    assert not http.get_calls


def test_valid_dates_proceed_to_request():
    """合法日期格式通过校验，正常发往 ERP。"""
    http, adapter = _adapter()

    result = asyncio.run(
        adapter.search_sales_orders(
            _CONTEXT,
            start_date="2026-06-01",
            end_date="2026-06-30",
        )
    )

    assert http.get_calls
    assert result.total == 0
