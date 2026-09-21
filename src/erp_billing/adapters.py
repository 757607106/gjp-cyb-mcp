"""开单产品业务 API 的参考 Adapter。

Adapter 不负责登录。对接产品提供当前上下文对应的短期令牌或已鉴权
HTTP 执行器，因此账号、密码和令牌不会出现在 Tool JSON Schema 中。
"""

from __future__ import annotations

import json
import logging
import time

from datetime import date, datetime, timezone
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx

from gjp_common.config import get_env_value
from gjp_common.connections import (
    BusinessApiCredentialProvider,
    business_api_url,
    normalize_business_api_base_url,
)
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError
from gjp_common.logging_config import (
    clip_log_text,
    elapsed_ms,
)
from gjp_common.paths import resolve_output_path
from .catalog import normalize_live_product_rows
from .ports import (
    AuthenticatedJsonClient,
    BillingDataResult,
    BillingDocumentDetailResult,
    BillingDocumentPageResult,
    BillingDocumentResult,
    BillingProductSnapshot,
    BillingReferenceSnapshot,
    BillingSalesOrderDetailResult,
    BillingSalesOrderPageResult,
    BillingSalesOrderResult,
    MatchEvent,
    MatchEventLogger,
)

logger = logging.getLogger(__name__)


def _response_details(data: dict) -> dict:
    """保留业务错误码与追踪标识，不回传响应正文和鉴权信息。"""
    return {
        target: str(data[source])
        for source, target in (("code", "upstream_code"), ("traceId", "trace_id"))
        if isinstance(data.get(source), (str, int)) and str(data[source])
    }


def _merge_sales_order_update(current: dict, changes: dict) -> dict:
    """严格按 SalesOrderUpdateDTO 合并，保留金额、多单位和明细行身份。"""
    status = current.get("status")
    if status == 3:
        raise DomainError("erp_sales_order_state_invalid", "已作废销售单不能修改")
    if status == 2:
        immutable = {"customerId": "客户", "warehouseId": "仓库",
                     "discountAmount": "优惠金额", "discountAccountId": "优惠账户"}
        for field, label in immutable.items():
            if field not in changes:
                continue
            unchanged = (
                changes[field] == current.get(field)
                if field == "discountAmount"
                else str(changes[field]) == str(current.get(field))
            )
            if not unchanged:
                raise DomainError("erp_sales_order_field_locked", "已生效销售单不能修改" + label)
        if "items" in changes and any(not item.get("orderItemId") for item in changes["items"]):
            raise DomainError("erp_sales_order_item_id_required", "编辑已生效明细必须提供 order_item_id")
    fields = ("orderDate", "customerId", "warehouseId", "handlerId",
              "discountAmount", "discountAccountId", "remark")
    payload = {key: current[key] for key in fields if current.get(key) is not None}
    if "items" not in changes:
        rows = current.get("items")
        if not isinstance(rows, list) or not rows:
            raise DomainError("erp_live_response_invalid", "ERP 详情缺少商品明细，无法保留原单据")
        item_fields = ("productId", "unitId", "unit", "conversionRate", "quantity", "unitPrice", "remark")
        payload["items"] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("productId") or row.get("quantity") is None:
                raise DomainError("erp_live_response_invalid", "ERP 详情商品明细不完整")
            item = {key: row[key] for key in item_fields if row.get(key) is not None}
            if row.get("id"):
                item["orderItemId"] = row["id"]
            payload["items"].append(item)
    payload.update(changes)
    if not payload.get("orderDate") or not payload.get("handlerId"):
        raise DomainError("erp_live_response_invalid", "ERP 详情缺少日期或经手人，无法完成修改")
    if payload.get("saveType", 0) not in {0, 2}:
        raise DomainError("erp_sales_order_save_type_invalid", "修改单据只支持保持状态或转正式过账")
    return payload


def _merge_purchase_order_update(current: dict, changes: dict) -> dict:
    """严格按 PurchaseOrderUpdateDTO 合并，保留金额、多单位和明细行身份。"""
    status = current.get("status")
    if status == 3:
        raise DomainError("erp_purchase_order_state_invalid", "已作废采购单不能修改")
    if status == 2:
        immutable = {"supplierId": "供应商", "warehouseId": "入库仓库",
                     "discountAmount": "优惠金额", "discountAccountId": "优惠账户"}
        for field, label in immutable.items():
            if field not in changes:
                continue
            unchanged = (
                changes[field] == current.get(field)
                if field == "discountAmount"
                else str(changes[field]) == str(current.get(field))
            )
            if not unchanged:
                raise DomainError("erp_purchase_order_field_locked", "已生效采购单不能修改" + label)
        if "items" in changes and any(not item.get("orderItemId") for item in changes["items"]):
            raise DomainError("erp_purchase_order_item_id_required", "编辑已生效明细必须提供 order_item_id")
    fields = ("orderDate", "supplierId", "warehouseId", "handlerId",
              "discountAmount", "discountAccountId", "paymentAmount",
              "paymentAccountId", "remark")
    payload = {key: current[key] for key in fields if current.get(key) is not None}
    if "items" not in changes:
        rows = current.get("items")
        if not isinstance(rows, list) or not rows:
            raise DomainError("erp_live_response_invalid", "ERP 详情缺少商品明细，无法保留原单据")
        item_fields = ("productId", "unitId", "unit", "conversionRate", "quantity", "unitPrice", "remark")
        payload["items"] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("productId") or row.get("quantity") is None:
                raise DomainError("erp_live_response_invalid", "ERP 详情商品明细不完整")
            item = {key: row[key] for key in item_fields if row.get(key) is not None}
            if row.get("id"):
                item["orderItemId"] = row["id"]
            payload["items"].append(item)
    payload.update(changes)
    if not payload.get("orderDate") or not payload.get("handlerId"):
        raise DomainError("erp_live_response_invalid", "ERP 详情缺少日期或经办人，无法完成修改")
    if payload.get("saveType", 0) not in {0, 1, 2}:
        raise DomainError("erp_purchase_order_save_type_invalid", "修改单据只支持保持状态或转正式过账")
    return payload


class ErpAuthenticatedHttpAdapter:
    """通过服务端已鉴权 HTTP 执行器访问云创业版商品分页 API。"""

    def __init__(
        self,
        http: AuthenticatedJsonClient,
        page_size: int = 100,
    ) -> None:
        if page_size <= 0:
            raise ValueError("商品分页大小必须大于 0")
        self._http = http
        self._page_size = page_size

    async def _get(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, object],
    ) -> dict:
        data = await self._http.get_json(
            context,
            "/" + path.lstrip("/"),
            params,
        )
        return self._ensure_success(data)

    async def _post(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        data = await self._http.post_json(
            context,
            "/" + path.lstrip("/"),
            payload,
        )
        return self._ensure_success(data)

    async def _put(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict:
        data = await self._http.put_json(
            context,
            "/" + path.lstrip("/"),
            payload,
        )
        return self._ensure_success(data)

    @staticmethod
    def _ensure_success(data: dict) -> dict:
        code = str(data.get("code") or "")
        if code == "A10006":
            raise DomainError("business_reauth_required", "当前业务系统授权已失效", details=_response_details(data))
        if code != "A00000":
            raise DomainError(
                "erp_live_request_failed",
                str(data.get("message") or "ERP 接口失败"),
                details=_response_details(data),
            )
        return data

    async def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> BillingProductSnapshot:
        wanted = None if limit is None else max(0, limit)
        if wanted == 0:
            return BillingProductSnapshot(products=())

        rows: list[object] = []
        page_num = 1
        while True:
            page_size = self._page_size
            if wanted is not None:
                page_size = min(page_size, wanted - len(rows))
            data = await self._get(
                context,
                "/product/page",
                {
                    "pageNum": page_num,
                    "pageSize": page_size,
                    "status": 1,
                },
            )
            page = data.get("data")
            if not isinstance(page, dict):
                raise DomainError("erp_live_response_invalid", "ERP 商品分页数据不是对象")
            page_rows = page.get("list")
            if not isinstance(page_rows, list):
                raise DomainError("erp_live_response_invalid", "ERP 商品列表不是数组")
            total = _non_negative_int(page.get("total"), "ERP 商品总数无效")
            rows.extend(page_rows)
            if (
                not page_rows
                or len(rows) >= total
                or len(page_rows) < page_size
                or (wanted is not None and len(rows) >= wanted)
            ):
                break
            page_num += 1

        products = normalize_live_product_rows(rows, leaf_only=False)
        if wanted is not None:
            products = products[:wanted]
        return BillingProductSnapshot(products=tuple(products))

    async def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        return await self._search_reference(
            context,
            "/customer/page",
            keyword,
            limit,
            page,
        )

    async def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        return await self._search_reference(
            context,
            "/warehouse/page",
            keyword,
            limit,
            page,
        )

    async def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        return await self._search_reference(
            context,
            "/staff/page",
            keyword,
            limit,
            page,
        )

    async def _search_reference(
        self,
        context: InvocationContext,
        path: str,
        keyword: str,
        limit: int,
        page: int,
        status: object = 1,
    ) -> BillingReferenceSnapshot:
        effective_limit = max(1, min(int(limit or 5), 20))
        effective_page = max(1, int(page or 1))
        params: dict[str, object] = {
            "pageNum": effective_page,
            "pageSize": effective_limit,
            "status": status,
        }
        if keyword.strip():
            params["keyword"] = keyword.strip()
        data = await self._get(context, path, params)
        page_data = data.get("data")
        if not isinstance(page_data, dict):
            raise DomainError("erp_live_response_invalid", "ERP 基础资料分页数据不是对象")
        rows = page_data.get("list")
        if not isinstance(rows, list):
            raise DomainError("erp_live_response_invalid", "ERP 基础资料列表不是数组")
        total = _non_negative_int(page_data.get("total"), "ERP 基础资料总数无效")
        options = tuple(
            option
            for row in rows
            if isinstance(row, dict)
            and (option := _reference_option(row)) is not None
        )
        return BillingReferenceSnapshot(
            options=options,
            total=total,
            page_num=effective_page,
            page_size=effective_limit,
        )

    async def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        data = await self._post(context, "/sales/orders", payload)
        order_id = str(data.get("data") or "").strip()
        if not order_id:
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 新增销售单成功但未返回单据 ID",
            )
        return BillingSalesOrderResult(order_id=order_id)

    async def _resolve_order_id(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> str:
        """归一化销售单标识为内部数字 ID。

        详情、作废和修改接口的路径参数是内部数字 ID，不是业务单号
        orderNo。入参为纯数字串时视为内部 ID 直接返回；非纯数字（业务
        单号，如 XS 开头的 orderNo）则通过列表按 orderNo 精确匹配取回
        内部 ID，使工具对两种标识都可用。
        """
        token = (order_id or "").strip()
        if not token:
            raise DomainError(
                "erp_sales_order_id_invalid",
                "销售单 ID 不能为空",
            )
        if token.isdigit():
            return token
        page = await self.search_sales_orders(
            context,
            order_no=token,
            page_size=20,
        )
        for order in page.orders:
            if str(order.get("orderNo") or "").strip() == token:
                resolved = str(order.get("id") or "").strip()
                if resolved:
                    return resolved
        raise DomainError(
            "erp_sales_order_not_found",
            "销售单不存在：%s" % token,
        )

    async def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingSalesOrderDetailResult:
        resolved = await self._resolve_order_id(context, order_id)
        data = await self._get(
            context,
            "/sales/orders/%s" % _path_segment(resolved),
            {},
        )
        order = data.get("data")
        if not isinstance(order, dict):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 销售单详情数据不是对象",
            )
        return BillingSalesOrderDetailResult(order=order)

    async def search_sales_orders(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        sort_by: str = "",
        order_type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        customer_id: str = "",
    ) -> BillingSalesOrderPageResult:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, object] = {
            "pageNum": max(1, page_num),
            "pageSize": max(1, min(page_size, 100)),
        }
        if sort_by.strip():
            params["sortBy"] = sort_by.strip()
        if order_type.strip():
            params["orderType"] = order_type.strip()
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if payment_status is not None:
            params["paymentStatus"] = int(payment_status)
        if return_status is not None:
            params["returnStatus"] = int(return_status)
        if order_no.strip():
            params["orderNo"] = order_no.strip()
        if customer_id.strip():
            params["customerId"] = customer_id.strip()
        data = await self._get(context, "/sales/orders/page", params)
        page = data.get("data")
        if not isinstance(page, dict):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 销售单分页数据不是对象",
            )
        rows = page.get("list")
        if not isinstance(rows, list):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 销售单列表不是数组",
            )
        return BillingSalesOrderPageResult(
            total=_non_negative_int(
                page.get("total"), "ERP 销售单总数无效",
            ),
            page_num=_non_negative_int(
                page.get("pageNum"), "ERP 销售单页码无效",
            ),
            page_size=_non_negative_int(
                page.get("pageSize"), "ERP 销售单页大小无效",
            ),
            orders=tuple(
                row for row in rows if isinstance(row, dict)
            ),
        )

    @staticmethod
    def _validate_date_range(start_date: str, end_date: str) -> None:
        """校验查询日期格式为 YYYY-MM-DD 且结束不早于开始。

        ERP 的 startDate/endDate 要求 LocalDate，非法格式会触发后端
        类型转换异常并泄露技术栈错误；在协议层提前拦截，返回友好错误。
        """
        parsed_start = None
        if start_date:
            try:
                parsed_start = date.fromisoformat(start_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "开始日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed_start.isoformat() != start_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "开始日期必须使用 YYYY-MM-DD 格式",
                )
        if end_date:
            try:
                parsed_end = date.fromisoformat(end_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed_end.isoformat() != end_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期必须使用 YYYY-MM-DD 格式",
                )
            if parsed_start is not None and parsed_end < parsed_start:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期不能早于开始日期",
                )

    async def void_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        resolved = await self._resolve_order_id(context, order_id)
        await self._put(
            context,
            "/sales/orders/%s/void" % _path_segment(resolved),
            {},
        )

    async def update_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        resolved = await self._resolve_order_id(context, order_id)
        # ERP 是完整 PUT 契约；局部修改在此映射为最新详情的完整请求。
        # 上游无版本条件，GET/PUT 之间的外部并发仍需 ERP 原子更新支持。
        current = (await self.get_sales_order_detail(context, resolved)).order
        payload = _merge_sales_order_update(current, payload)
        payload["id"] = int(resolved)
        data = await self._put(
            context,
            "/sales/orders/%s" % _path_segment(resolved),
            payload,
        )
        result_id = str(data.get("data") or "").strip()
        if not result_id:
            result_id = resolved
        return BillingSalesOrderResult(order_id=result_id)

    # ------------------------------------------------------------------
    # 通用单据辅助：ID 解析、分页解析、创建/详情/作废
    # ------------------------------------------------------------------

    async def _resolve_document_id(
        self,
        context: InvocationContext,
        token: str,
        *,
        search: Callable[[str], Awaitable[BillingDocumentPageResult]],
        number_field: str,
        code_prefix: str,
        doc_label: str,
    ) -> str:
        """归一化单据标识：纯数字视为内部 ID，其余按业务单号精确匹配。"""
        clean = (token or "").strip()
        if not clean:
            raise DomainError(code_prefix + "_id_invalid", "%s标识不能为空" % doc_label)
        if clean.isdigit():
            return clean
        page = await search(clean)
        for row in page.rows:
            if str(row.get(number_field) or "").strip() == clean:
                resolved = str(row.get("id") or "").strip()
                if resolved:
                    return resolved
        raise DomainError(code_prefix + "_not_found", "%s不存在：%s" % (doc_label, clean))

    async def _get_document(
        self,
        context: InvocationContext,
        path: str,
        code_prefix: str,
        doc_label: str,
    ) -> BillingDocumentDetailResult:
        data = await self._get(context, path, {})
        document = data.get("data")
        if not isinstance(document, dict):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP %s详情数据不是对象" % doc_label,
            )
        return BillingDocumentDetailResult(document=document)

    async def _create_document(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object],
        doc_label: str,
    ) -> BillingDocumentResult:
        data = await self._post(context, path, payload)
        document_id = str(data.get("data") or "").strip()
        if not document_id:
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 新增%s成功但未返回单据 ID" % doc_label,
            )
        return BillingDocumentResult(document_id=document_id)

    async def _void_document(
        self,
        context: InvocationContext,
        path: str,
    ) -> None:
        await self._put(context, path, {})

    def _parse_document_page(
        self,
        data: dict,
        code_prefix: str,
        doc_label: str,
    ) -> BillingDocumentPageResult:
        page = data.get("data")
        if not isinstance(page, dict):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP %s分页数据不是对象" % doc_label,
            )
        rows = page.get("list")
        if not isinstance(rows, list):
            raise DomainError(
                "erp_live_response_invalid",
                "ERP %s列表不是数组" % doc_label,
            )
        return BillingDocumentPageResult(
            total=_non_negative_int(
                page.get("total"), "ERP %s总数无效" % doc_label,
            ),
            page_num=_non_negative_int(
                page.get("pageNum"), "ERP %s页码无效" % doc_label,
            ),
            page_size=_non_negative_int(
                page.get("pageSize"), "ERP %s页大小无效" % doc_label,
            ),
            rows=tuple(row for row in rows if isinstance(row, dict)),
        )

    @staticmethod
    def _data_result(data: dict, label: str) -> BillingDataResult:
        payload = data.get("data")
        if payload is None:
            raise DomainError("erp_live_response_invalid", "ERP %s数据缺失" % label)
        return BillingDataResult(data=payload)

    @staticmethod
    def _page_params(
        page_num: int,
        page_size: int,
    ) -> dict[str, object]:
        return {
            "pageNum": max(1, int(page_num or 1)),
            "pageSize": max(1, min(int(page_size or 20), 100)),
        }

    # ------------------------------------------------------------------
    # 基础资料扩展：供应商与结算账户
    # ------------------------------------------------------------------

    async def search_suppliers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        return await self._search_reference(
            context,
            "/supplier/page",
            keyword,
            limit,
            page,
            status="1",
        )

    async def search_settlement_accounts(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
        account_type: int | None = None,
    ) -> BillingReferenceSnapshot:
        effective_limit = max(1, min(int(limit or 5), 20))
        effective_page = max(1, int(page or 1))
        params: dict[str, object] = {
            "pageNum": effective_page,
            "pageSize": effective_limit,
            "status": 1,
        }
        if keyword.strip():
            params["keyword"] = keyword.strip()
        if account_type is not None:
            params["accountType"] = int(account_type)
        data = await self._get(context, "/settlement-account/page", params)
        page_data = data.get("data")
        if not isinstance(page_data, dict):
            raise DomainError("erp_live_response_invalid", "ERP 结算账户分页数据不是对象")
        rows = page_data.get("list")
        if not isinstance(rows, list):
            raise DomainError("erp_live_response_invalid", "ERP 结算账户列表不是数组")
        total = _non_negative_int(page_data.get("total"), "ERP 结算账户总数无效")
        options = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            option_id = str(row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            if not option_id or not name:
                continue
            options.append(
                {
                    "id": option_id,
                    "code": str(row.get("code") or "").strip(),
                    "name": name,
                    "is_default": False,
                    "account_type": row.get("accountType"),
                    "balance": row.get("balance"),
                },
            )
        return BillingReferenceSnapshot(
            options=tuple(options),
            total=total,
            page_num=effective_page,
            page_size=effective_limit,
        )

    async def search_fund_types(
        self,
        context: InvocationContext,
        direction: int,
    ) -> BillingReferenceSnapshot:
        """查询某资金方向的全部款项类型，列表小而稳定，由调用方精确匹配。"""
        effective_direction = int(direction)
        if effective_direction not in (1, 2):
            raise DomainError("erp_fund_type_direction_invalid", "款项类型 direction 必须是 1 或 2")
        data = await self._get(
            context,
            "/financial/payment-receipt-type/list-by-direction",
            {"direction": effective_direction},
        )
        rows = data.get("data")
        if not isinstance(rows, list):
            raise DomainError("erp_live_response_invalid", "ERP 款项类型列表不是数组")
        options = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            option_id = str(row.get("id") or "").strip()
            name = str(row.get("typeName") or "").strip()
            if not option_id or not name:
                continue
            options.append(
                {
                    "id": option_id,
                    "code": str(row.get("typeCode") or "").strip(),
                    "name": name,
                    "is_default": False,
                    "is_system": bool(row.get("isSystem")),
                },
            )
        return BillingReferenceSnapshot(
            options=tuple(options),
            total=len(options),
            page_num=1,
            page_size=len(options),
        )

    # ------------------------------------------------------------------
    # 采购单
    # ------------------------------------------------------------------

    async def _resolve_purchase_order_id(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> str:
        async def search(token: str) -> BillingDocumentPageResult:
            return await self.search_purchase_orders(context, order_no=token, page_size=20)

        return await self._resolve_document_id(
            context,
            order_id,
            search=search,
            number_field="orderNo",
            code_prefix="erp_purchase_order",
            doc_label="采购单",
        )

    async def create_purchase_order(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/purchase/orders", payload, "采购单")

    async def get_purchase_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        resolved = await self._resolve_purchase_order_id(context, order_id)
        return await self._get_document(
            context,
            "/purchase/orders/%s" % _path_segment(resolved),
            "erp_purchase_order",
            "采购单",
        )

    async def search_purchase_orders(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        supplier_id: str = "",
    ) -> BillingDocumentPageResult:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, object] = self._page_params(page_num, page_size)
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if payment_status is not None:
            params["paymentStatus"] = int(payment_status)
        if return_status is not None:
            params["returnStatus"] = int(return_status)
        if order_no.strip():
            params["orderNo"] = order_no.strip()
        if supplier_id.strip():
            params["supplierId"] = supplier_id.strip()
        data = await self._get(context, "/purchase/orders/page", params)
        return self._parse_document_page(data, "erp_purchase_order", "采购单")

    async def void_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        resolved = await self._resolve_purchase_order_id(context, order_id)
        await self._void_document(
            context, "/purchase/orders/%s/void" % _path_segment(resolved),
        )

    async def update_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, object],
    ) -> BillingDocumentResult:
        resolved = await self._resolve_purchase_order_id(context, order_id)
        current = (await self.get_purchase_order_detail(context, resolved)).document
        merged = _merge_purchase_order_update(current, payload)
        merged["id"] = int(resolved)
        data = await self._put(
            context,
            "/purchase/orders/%s" % _path_segment(resolved),
            merged,
        )
        result_id = str(data.get("data") or "").strip() or resolved
        return BillingDocumentResult(document_id=result_id)

    async def pay_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, object],
    ) -> None:
        resolved = await self._resolve_purchase_order_id(context, order_id)
        body = dict(payload)
        body["orderId"] = resolved
        await self._post(context, "/purchase/orders/payment", body)

    async def get_purchase_order_quick_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        resolved = await self._resolve_purchase_order_id(context, order_id)
        return await self._get_document(
            context,
            "/purchase/orders/%s/quick-return" % _path_segment(resolved),
            "erp_purchase_order",
            "采购单快捷退货数据",
        )

    # ------------------------------------------------------------------
    # 采购退货单
    # ------------------------------------------------------------------

    async def _resolve_purchase_return_id(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> str:
        async def search(token: str) -> BillingDocumentPageResult:
            return await self.search_purchase_returns(context, return_no=token, page_size=20)

        return await self._resolve_document_id(
            context,
            order_id,
            search=search,
            number_field="returnNo",
            code_prefix="erp_purchase_return",
            doc_label="采购退货单",
        )

    async def create_purchase_return(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/purchase/returns", payload, "采购退货单")

    async def get_purchase_return_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        resolved = await self._resolve_purchase_return_id(context, order_id)
        return await self._get_document(
            context,
            "/purchase/returns/%s" % _path_segment(resolved),
            "erp_purchase_return",
            "采购退货单",
        )

    async def search_purchase_returns(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        return_no: str = "",
        supplier_id: str = "",
    ) -> BillingDocumentPageResult:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, object] = self._page_params(page_num, page_size)
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if return_no.strip():
            params["returnNo"] = return_no.strip()
        if supplier_id.strip():
            params["supplierId"] = supplier_id.strip()
        data = await self._get(context, "/purchase/returns/page", params)
        return self._parse_document_page(data, "erp_purchase_return", "采购退货单")

    async def void_purchase_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        resolved = await self._resolve_purchase_return_id(context, order_id)
        await self._void_document(
            context, "/purchase/returns/%s/void" % _path_segment(resolved),
        )

    # ------------------------------------------------------------------
    # 销售退货单
    # ------------------------------------------------------------------

    async def _resolve_sales_return_id(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> str:
        async def search(token: str) -> BillingDocumentPageResult:
            return await self.search_sales_returns(context, return_no=token, page_size=20)

        return await self._resolve_document_id(
            context,
            order_id,
            search=search,
            number_field="returnNo",
            code_prefix="erp_sales_return",
            doc_label="销售退货单",
        )

    async def get_sales_order_quick_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        resolved = await self._resolve_order_id(context, order_id)
        return await self._get_document(
            context,
            "/sales/orders/%s/quick-return" % _path_segment(resolved),
            "erp_sales_order",
            "销售单快捷退货数据",
        )

    async def create_sales_return(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/sales/returns", payload, "销售退货单")

    async def get_sales_return_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        resolved = await self._resolve_sales_return_id(context, order_id)
        return await self._get_document(
            context,
            "/sales/returns/%s" % _path_segment(resolved),
            "erp_sales_return",
            "销售退货单",
        )

    async def search_sales_returns(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        refund_status: int | None = None,
        return_no: str = "",
        customer_id: str = "",
    ) -> BillingDocumentPageResult:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, object] = self._page_params(page_num, page_size)
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if refund_status is not None:
            params["refundStatus"] = int(refund_status)
        if return_no.strip():
            params["returnNo"] = return_no.strip()
        if customer_id.strip():
            params["customerId"] = customer_id.strip()
        data = await self._get(context, "/sales/returns/page", params)
        return self._parse_document_page(data, "erp_sales_return", "销售退货单")

    async def void_sales_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        resolved = await self._resolve_sales_return_id(context, order_id)
        await self._void_document(
            context, "/sales/returns/%s/void" % _path_segment(resolved),
        )

    # ------------------------------------------------------------------
    # 库存查询与库存写单据
    # ------------------------------------------------------------------

    async def query_stock_page(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDataResult:
        body = dict(payload)
        body.update(self._page_params(body.pop("pageNum", 1), body.pop("pageSize", 20)))
        data = await self._post(context, "/inventory/page", body)
        return self._data_result(data, "库存列表")

    async def get_stock_by_product(
        self,
        context: InvocationContext,
        product_id: str,
    ) -> BillingDataResult:
        token = (product_id or "").strip()
        if not token:
            raise DomainError("erp_stock_query_invalid", "商品 ID 不能为空")
        data = await self._get(
            context,
            "/inventory/detail-by-product",
            {"productId": _path_segment(token)},
        )
        return self._data_result(data, "商品库存")

    async def get_stock_summary(
        self,
        context: InvocationContext,
        params: dict[str, Any],
    ) -> BillingDataResult:
        data = await self._get(
            context,
            "/inventory/summary",
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "库存汇总")

    async def query_stock_logs(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDataResult:
        body = dict(payload)
        body.update(self._page_params(body.pop("pageNum", 1), body.pop("pageSize", 20)))
        data = await self._post(context, "/inventory/logs/page", body)
        return self._data_result(data, "库存流水")

    async def list_stock_alerts(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDataResult:
        body = dict(payload)
        body.update(self._page_params(body.pop("pageNum", 1), body.pop("pageSize", 20)))
        data = await self._post(context, "/inventory/alerts/list", body)
        return self._data_result(data, "库存预警")

    async def get_purchase_suggestions(
        self,
        context: InvocationContext,
    ) -> BillingDataResult:
        data = await self._get(context, "/inventory/alerts/purchase-suggestions", {})
        return self._data_result(data, "采购建议")

    async def list_stock_doc_types(
        self,
        context: InvocationContext,
        kind: str,
    ) -> BillingDataResult:
        paths = {
            "inbound": "/inventory/inbound-type/list",
            "outbound": "/inventory/outbound-type/list",
        }
        if kind not in paths:
            raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
        data = await self._get(context, paths[kind], {})
        return self._data_result(data, "出入库类型")

    async def create_stock_transfer(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/inventory/transfers", payload, "调拨单")

    async def get_stock_transfer_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        token = (order_id or "").strip()
        if not token:
            raise DomainError("erp_stock_transfer_id_invalid", "调拨单 ID 不能为空")
        return await self._get_document(
            context,
            "/inventory/transfers/%s" % _path_segment(token),
            "erp_stock_transfer",
            "调拨单",
        )

    async def create_other_stock_doc(
        self,
        context: InvocationContext,
        kind: str,
        payload: dict[str, Any],
    ) -> BillingDocumentResult:
        paths = {
            "inbound": "/inventory/other-inbounds",
            "outbound": "/inventory/other-outbounds",
        }
        if kind not in paths:
            raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
        return await self._create_document(context, paths[kind], payload, "其他出入库单")

    async def get_other_stock_doc_detail(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        if kind not in {"inbound", "outbound"}:
            raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
        token = (order_id or "").strip()
        if not token:
            raise DomainError("erp_stock_doc_id_invalid", "其他出入库单 ID 不能为空")
        path = "/inventory/other-inbounds" if kind == "inbound" else "/inventory/other-outbounds"
        return await self._get_document(
            context,
            "%s/%s" % (path, _path_segment(token)),
            "erp_other_stock_doc",
            "其他出入库单",
        )

    # ------------------------------------------------------------------
    # 资金往来：收款、付款、应收应付
    # ------------------------------------------------------------------

    async def receive_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, Any],
    ) -> None:
        resolved = await self._resolve_order_id(context, order_id)
        body = dict(payload)
        body["orderId"] = resolved
        await self._post(context, "/sales/orders/receipt", body)

    async def create_receipt_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/financial/receipt-orders", payload, "收款单")

    async def create_payment_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingDocumentResult:
        return await self._create_document(context, "/financial/payment-orders", payload, "付款单")

    async def get_financial_order_detail(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> BillingDocumentDetailResult:
        if kind not in {"receipt", "payment"}:
            raise DomainError("erp_financial_kind_invalid", "kind 必须是 receipt 或 payment")
        token = (order_id or "").strip()
        if not token:
            raise DomainError("erp_financial_order_id_invalid", "收付款单 ID 不能为空")
        path = "/financial/receipt-orders" if kind == "receipt" else "/financial/payment-orders"
        label = "收款单" if kind == "receipt" else "付款单"
        resolved = await self._resolve_financial_order_id(context, kind, token)
        return await self._get_document(
            context,
            "%s/%s" % (path, _path_segment(resolved)),
            "erp_financial_order",
            label,
        )

    async def list_financial_orders(
        self,
        context: InvocationContext,
        kind: str,
        params: dict[str, Any],
    ) -> BillingDocumentPageResult:
        if kind not in {"receipt", "payment"}:
            raise DomainError("erp_financial_kind_invalid", "kind 必须是 receipt 或 payment")
        data = await self._get(
            context,
            "/financial/%s-orders/page" % ("receipt" if kind == "receipt" else "payment"),
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._parse_document_page(data, "erp_financial_order", "收付款单")

    async def void_financial_order(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> None:
        if kind not in {"receipt", "payment"}:
            raise DomainError("erp_financial_kind_invalid", "kind 必须是 receipt 或 payment")
        resolved = await self._resolve_financial_order_id(context, kind, order_id)
        prefix = "receipt" if kind == "receipt" else "payment"
        await self._void_document(
            context, "/financial/%s-orders/%s/void" % (prefix, _path_segment(resolved)),
        )

    async def _resolve_financial_order_id(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> str:
        doc_label = "收款单" if kind == "receipt" else "付款单"
        code_prefix = "erp_receipt_order" if kind == "receipt" else "erp_payment_order"

        async def search(token: str) -> BillingDocumentPageResult:
            return await self.list_financial_orders(
                context, kind, {"pageNum": 1, "pageSize": 20, "orderNo": token},
            )

        return await self._resolve_document_id(
            context,
            order_id,
            search=search,
            number_field="orderNo",
            code_prefix=code_prefix,
            doc_label=doc_label,
        )

    async def list_receivables(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        if view not in {"summary", "details"}:
            raise DomainError("erp_receivable_view_invalid", "view 必须是 summary 或 details")
        data = await self._get(
            context,
            "/financial/receivables/%s" % view,
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "应收%s" % ("汇总" if view == "summary" else "明细"))

    async def list_payables(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        if view not in {"summary", "details"}:
            raise DomainError("erp_payable_view_invalid", "view 必须是 summary 或 details")
        data = await self._get(
            context,
            "/financial/payables/%s" % view,
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "应付%s" % ("汇总" if view == "summary" else "明细"))

    # ------------------------------------------------------------------
    # 报表分析
    # ------------------------------------------------------------------

    _SALES_REPORT_PATHS = {
        "analysis": "/sales/analysis",
        "details": "/sales/details/page",
        "details_summary": "/sales/details/summary",
        "ranking_product": "/sales/ranking/product",
        "ranking_customer": "/sales/ranking/customer",
    }

    _PURCHASE_REPORT_PATHS = {
        "statistics": "/purchase/statistics",
        "details": "/purchase/details/page",
        "details_summary": "/purchase/details/summary",
    }

    _PROFIT_REPORT_PATHS = {
        "summary": "/financial/profits/summary",
        "by_customer": "/financial/profits/by-customer",
        "by_product": "/financial/profits/by-product",
    }

    async def query_sales_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        path = self._SALES_REPORT_PATHS.get(view)
        if path is None:
            raise DomainError(
                "erp_report_view_invalid",
                "销售报表 view 必须是 %s" % "、".join(self._SALES_REPORT_PATHS),
            )
        data = await self._get(
            context,
            path,
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "销售报表")

    async def query_purchase_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        path = self._PURCHASE_REPORT_PATHS.get(view)
        if path is None:
            raise DomainError(
                "erp_report_view_invalid",
                "采购报表 view 必须是 %s" % "、".join(self._PURCHASE_REPORT_PATHS),
            )
        data = await self._get(
            context,
            path,
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "采购报表")

    async def query_profit_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        path = self._PROFIT_REPORT_PATHS.get(view)
        if path is None:
            raise DomainError(
                "erp_report_view_invalid",
                "利润报表 view 必须是 %s" % "、".join(self._PROFIT_REPORT_PATHS),
            )
        data = await self._get(
            context,
            path,
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "利润报表")

    async def get_financial_status(
        self,
        context: InvocationContext,
        biz_date: str = "",
    ) -> BillingDataResult:
        params: dict[str, object] = {}
        if biz_date.strip():
            params["bizDate"] = biz_date.strip()
        data = await self._get(context, "/financial/status", params)
        return self._data_result(data, "财务状况")

    async def query_settlement_report(
        self,
        context: InvocationContext,
        params: dict[str, Any],
    ) -> BillingDataResult:
        data = await self._get(
            context,
            "/financial/settlements/statistics",
            {key: value for key, value in params.items() if value not in (None, "")},
        )
        return self._data_result(data, "结算统计")

    async def query_reconciliation(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> BillingDataResult:
        paths = {
            "summary": ("/financial/reconciliation/summary/page", "客户对账报表"),
            "statement": ("/financial/reconciliation/statement/page", "客户对账单"),
        }
        if view not in paths:
            raise DomainError("erp_reconciliation_view_invalid", "view 必须是 summary 或 statement")
        path, label = paths[view]
        body = dict(params)
        body.update(self._page_params(body.pop("pageNum", 1), body.pop("pageSize", 20)))
        data = await self._post(context, path, body)
        return self._data_result(data, label)


class BusinessAuthenticatedJsonClient:
    """使用当前会话中的地址和 Bearer 调用业务 JSON API。"""

    def __init__(
        self,
        base_url: str,
        credential_provider: BusinessApiCredentialProvider,
        timeout_seconds: float = 30,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("业务 API 超时时间必须大于 0")
        self._base_url = normalize_business_api_base_url(base_url)
        self._credential_provider = credential_provider
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """延迟创建 httpx.AsyncClient，避免未使用的 ToolSet 占用连接池。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                verify=True,
            )
        return self._client

    async def get_json(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(context, "GET", path, params=params)

    async def post_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        return await self._request_json(context, "POST", path, payload=payload)

    async def put_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(context, "PUT", path, payload=payload)

    async def _request_json(
        self,
        context: InvocationContext,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        credential = self._credential_provider.resolve(context)
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if credential.kind == "bearer":
            headers["Authorization"] = "Bearer " + credential.value
        elif credential.kind == "api_key":
            headers["X-API-Key"] = credential.value
        else:
            raise DomainError("billing_api_unauthorized", "当前开单会话的鉴权类型无效")
        url = business_api_url(self._base_url, path)
        body_bytes = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ERP 请求开始 method=%s url=%s headers=%s body=%s",
                method,
                url,
                "<已脱敏>",
                clip_log_text(json.dumps(payload, ensure_ascii=False))
                if payload is not None
                else "<无>",
            )
        started = time.perf_counter()
        try:
            response = await self._ensure_client().request(
                method,
                url,
                params=params,
                content=body_bytes,
                headers=headers,
            )
            response.raise_for_status()
            body = response.text
        except httpx.HTTPStatusError as exc:
            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = {}
            if not isinstance(error_body, dict):
                error_body = {}
            details = _response_details(error_body)
            details["http_status"] = exc.response.status_code
            if exc.response.status_code == 401:
                raise DomainError("business_reauth_required", "当前业务系统授权已失效", details=details) from exc
            if exc.response.status_code == 403:
                raise DomainError("business_forbidden", "当前账号无权执行该操作", details=details) from exc
            raise DomainError(
                "business_write_result_unknown"
                if method != "GET" and exc.response.status_code >= 500
                else "erp_live_request_failed",
                str(error_body.get("message") or "ERP 接口返回 HTTP %s" % exc.response.status_code),
                details=details,
            ) from exc
        except httpx.TransportError as exc:
            code = "business_upstream_unavailable" if method == "GET" else "business_write_result_unknown"
            message = (
                "当前业务系统不可用或请求超时" if method == "GET"
                else "业务写入结果未知，请先查询 ERP 核对，勿直接重复操作"
            )
            raise DomainError(code, message) from exc
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ERP 请求完成 url=%s elapsed=%dms body=%s",
                url,
                elapsed_ms(started),
                clip_log_text(body),
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DomainError("erp_live_response_invalid", "ERP 接口返回的不是 JSON") from exc
        if not isinstance(data, dict):
            raise DomainError("erp_live_response_invalid", "ERP 接口响应顶层不是对象")
        return data

    async def close(self) -> None:
        """关闭内部 HTTP 连接池，供服务停机或会话淘汰时调用。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class UnavailableBillingApi:
    """本地参考客户端未注入开单产品 Adapter 时返回明确错误。"""

    @staticmethod
    def _raise() -> NoReturn:
        raise DomainError("billing_api_not_configured", "开单服务尚未注入已鉴权 BillingApiPort")

    def __getattr__(self, name: str) -> Any:
        """未显式声明的端口方法统一返回未注入错误，免于逐个方法样板。"""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _method(*args: Any, **kwargs: Any) -> Any:
            self._raise()

        return _method

    async def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> BillingProductSnapshot:
        self._raise()

    async def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        self._raise()

    async def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        self._raise()

    async def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        self._raise()

    async def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        self._raise()

    async def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingSalesOrderDetailResult:
        self._raise()

    async def search_sales_orders(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        sort_by: str = "",
        order_type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        customer_id: str = "",
    ) -> BillingSalesOrderPageResult:
        self._raise()

    async def void_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        self._raise()

    async def update_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        self._raise()


def _non_negative_int(value: object, message: str) -> int:
    """解析上游分页数字，拒绝缺失、布尔值和负数。"""
    if isinstance(value, bool):
        raise DomainError("erp_live_response_invalid", message)
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DomainError("erp_live_response_invalid", message) from exc
    if parsed < 0:
        raise DomainError("erp_live_response_invalid", message)
    return parsed


def _path_segment(value: str) -> str:
    """清洗 URL 路径段，阻止斜杠注入。"""
    cleaned = str(value or "").strip()
    if not cleaned or "/" in cleaned:
        raise DomainError(
            "erp_sales_order_id_invalid",
            "销售单 ID 不能为空且不能包含斜杠",
        )
    return cleaned


def _reference_option(row: dict[str, object]) -> dict[str, object] | None:
    """把客户、仓库、职员列表项收敛为统一的最小候选结构。"""
    option_id = str(row.get("id") or "").strip()
    name = str(row.get("name") or "").strip()
    if not option_id or not name:
        return None
    return {
        "id": option_id,
        "name": name,
        "is_default": bool(row.get("isDefault")),
    }


class JsonlMatchEventLogger:
    """把匹配确认事件追加写入 JSONL 文件，供离线统计同义词候选。

    记录失败只输出警告日志，不影响开单主流程；匹配主流程本身不依赖
    本日志的写入结果。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, event: MatchEvent) -> None:
        line = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": event.source,
                "requestedName": event.requested_name,
                "productId": event.product_id,
                "productName": event.product_name,
                "matchType": event.match_type,
            },
            ensure_ascii=False,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("写入匹配事件日志失败 path=%s err=%s", self._path, exc)


class NullMatchEventLogger:
    """默认空实现：不记录任何事件，保持匹配主流程零副作用。"""

    def record(self, event: MatchEvent) -> None:
        return None


def create_match_logger_from_env() -> MatchEventLogger:
    """按 ERP_BILLING_MATCH_LOG 环境变量构建匹配事件日志器。

    留空时返回 NullMatchEventLogger，不产生任何文件 IO；非空时按
    resolve_output_path 解析为项目根相对路径并返回 JsonlMatchEventLogger。
    """
    value = get_env_value("ERP_BILLING_MATCH_LOG").strip()
    if not value:
        return NullMatchEventLogger()
    return JsonlMatchEventLogger(resolve_output_path(value))
