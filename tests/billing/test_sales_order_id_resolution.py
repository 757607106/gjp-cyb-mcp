"""销售单标识归一化测试。

ERP 详情、作废和修改接口的路径参数是内部数字 ID，而工具入参 order_id
可能拿到业务单号 orderNo（如 XS 开头）。这些测试覆盖
ErpAuthenticatedHttpAdapter 的 _resolve_order_id 归一化路径，确保
两种标识都能正确落到内部 ID。
"""

import pytest

from erp_billing.adapters import ErpAuthenticatedHttpAdapter
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError

_CONTEXT = InvocationContext(
    tenant_id="tenant-test",
    subject_id="user-test",
    account_id="billing-test",
)

_ORDER_NO = "XS202606120005"
_INTERNAL_ID = "2065262048677449729"


class _FakeHttp:
    """记录调用的最小已鉴权 JSON 客户端。"""

    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.put_calls = []
        self.get_responses = {}

    def get_json(self, context, path, params=None):
        self.get_calls.append((path, dict(params or {})))
        return self.get_responses.get(path, {"code": "A00000", "data": {}})

    def post_json(self, context, path, payload):
        self.post_calls.append((path, payload))
        return {"code": "A00000", "data": "NEW-ID"}

    def put_json(self, context, path, payload=None):
        self.put_calls.append((path, payload))
        return {"code": "A00000", "data": ""}


def _page_response(orders):
    return {
        "code": "A00000",
        "data": {
            "total": len(orders),
            "pageNum": 1,
            "pageSize": 20,
            "list": orders,
        },
    }


def _detail_response():
    return {
        "code": "A00000",
        "data": {"id": _INTERNAL_ID, "orderNo": _ORDER_NO},
    }


def _adapter_with_order_no_lookup():
    http = _FakeHttp()
    http.get_responses["/sales/orders/page"] = _page_response(
        [{"id": _INTERNAL_ID, "orderNo": _ORDER_NO}],
    )
    http.get_responses["/sales/orders/%s" % _INTERNAL_ID] = _detail_response()
    return http, ErpAuthenticatedHttpAdapter(http)


def test_numeric_id_skips_lookup():
    """纯数字内部 ID 直接请求详情，不触发列表回查。"""
    http = _FakeHttp()
    http.get_responses["/sales/orders/%s" % _INTERNAL_ID] = _detail_response()
    adapter = ErpAuthenticatedHttpAdapter(http)

    result = adapter.get_sales_order_detail(_CONTEXT, _INTERNAL_ID)

    paths = [call[0] for call in http.get_calls]
    assert paths == ["/sales/orders/%s" % _INTERNAL_ID]
    assert not any(path == "/sales/orders/page" for path in paths)
    assert result.order["orderNo"] == _ORDER_NO


def test_order_no_resolves_via_lookup():
    """业务单号通过列表精确匹配取回内部 ID 后再查详情。"""
    http, adapter = _adapter_with_order_no_lookup()

    result = adapter.get_sales_order_detail(_CONTEXT, _ORDER_NO)

    assert http.get_calls[0][0] == "/sales/orders/page"
    assert http.get_calls[0][1]["orderNo"] == _ORDER_NO
    assert http.get_calls[1][0] == "/sales/orders/%s" % _INTERNAL_ID
    assert result.order["orderNo"] == _ORDER_NO


def test_lookup_requires_exact_order_no_match():
    """列表 orderNo 模糊匹配可能返回多条，必须用完全相等的那条。"""
    http = _FakeHttp()
    http.get_responses["/sales/orders/page"] = _page_response(
        [
            {"id": "111", "orderNo": _ORDER_NO + "0"},  # 仅前缀匹配
            {"id": _INTERNAL_ID, "orderNo": _ORDER_NO},
        ],
    )
    http.get_responses["/sales/orders/%s" % _INTERNAL_ID] = _detail_response()
    adapter = ErpAuthenticatedHttpAdapter(http)

    result = adapter.get_sales_order_detail(_CONTEXT, _ORDER_NO)

    assert result.order["id"] == _INTERNAL_ID
    assert http.get_calls[1][0] == "/sales/orders/%s" % _INTERNAL_ID


def test_void_resolves_order_no():
    """作废接口同样接受业务单号并归一化为内部 ID。"""
    http = _FakeHttp()
    http.get_responses["/sales/orders/page"] = _page_response(
        [{"id": _INTERNAL_ID, "orderNo": _ORDER_NO}],
    )
    adapter = ErpAuthenticatedHttpAdapter(http)

    adapter.void_sales_order(_CONTEXT, _ORDER_NO)

    assert http.put_calls[0][0] == "/sales/orders/%s/void" % _INTERNAL_ID


def test_update_resolves_order_no_and_injects_id():
    """修改接口归一化内部 ID 并将其注入 payload，避免 int(XS) 崩溃。"""
    http = _FakeHttp()
    http.get_responses["/sales/orders/page"] = _page_response(
        [{"id": _INTERNAL_ID, "orderNo": _ORDER_NO}],
    )
    adapter = ErpAuthenticatedHttpAdapter(http)

    result = adapter.update_sales_order(
        _CONTEXT,
        _ORDER_NO,
        {"orderDate": "2026-08-04"},
    )

    path, payload = http.put_calls[0]
    assert path == "/sales/orders/%s" % _INTERNAL_ID
    assert payload["id"] == int(_INTERNAL_ID)
    assert payload["orderDate"] == "2026-08-04"
    assert result.order_id == _INTERNAL_ID


def test_empty_id_rejected():
    """空入参返回格式非法错误，不发请求。"""
    adapter = ErpAuthenticatedHttpAdapter(_FakeHttp())

    with pytest.raises(DomainError) as exc:
        adapter.get_sales_order_detail(_CONTEXT, "")

    assert exc.value.code == "erp_sales_order_id_invalid"
    assert not adapter._http.get_calls  # type: ignore[attr-defined]


def test_unknown_order_no_not_found():
    """业务单号在列表中无精确匹配时返回销售单不存在。"""
    http = _FakeHttp()
    http.get_responses["/sales/orders/page"] = _page_response([])
    adapter = ErpAuthenticatedHttpAdapter(http)

    with pytest.raises(DomainError) as exc:
        adapter.get_sales_order_detail(_CONTEXT, "XS999999999")

    assert exc.value.code == "erp_sales_order_not_found"
    assert "XS999999999" in exc.value.message
