"""开单产品业务 API 的参考 Adapter。

Adapter 不负责登录。对接产品提供当前上下文对应的短期令牌或已鉴权
HTTP 执行器，因此账号、密码和令牌不会出现在 Tool JSON Schema 中。
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import date, datetime, timezone
from pathlib import Path
from typing import NoReturn

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
    credential_dump_enabled,
    elapsed_ms,
)
from gjp_common.paths import resolve_output_path
from .catalog import normalize_live_product_rows
from .ports import (
    AuthenticatedJsonClient,
    BillingProductSnapshot,
    BillingReferenceSnapshot,
    BillingSalesOrderDetailResult,
    BillingSalesOrderPageResult,
    BillingSalesOrderResult,
    MatchEvent,
    MatchEventLogger,
)

logger = logging.getLogger(__name__)


class ErpAuthenticatedHttpAdapter:
    """通过服务端已鉴权 HTTP 执行器访问云创业版商品分页 API。"""

    def __init__(
        self,
        http: AuthenticatedJsonClient,
        page_size: int = 20,
    ) -> None:
        if page_size <= 0:
            raise ValueError("商品分页大小必须大于 0")
        self._http = http
        self._page_size = page_size

    def _get(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, object],
    ) -> dict:
        data = self._http.get_json(
            context,
            "/" + path.lstrip("/"),
            params,
        )
        return self._ensure_success(data)

    def _post(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        data = self._http.post_json(
            context,
            "/" + path.lstrip("/"),
            payload,
        )
        return self._ensure_success(data)

    def _put(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict:
        data = self._http.put_json(
            context,
            "/" + path.lstrip("/"),
            payload,
        )
        return self._ensure_success(data)

    @staticmethod
    def _ensure_success(data: dict) -> dict:
        code = str(data.get("code") or "")
        if code == "A10006":
            raise DomainError("business_reauth_required", "当前业务系统授权已失效")
        if code != "A00000":
            raise DomainError(
                "erp_live_request_failed",
                str(data.get("message") or "ERP 接口失败"),
            )
        return data

    def fetch_products(
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
            data = self._get(
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

    def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        return self._search_reference(
            context,
            "/customer/page",
            keyword,
            limit,
        )

    def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        return self._search_reference(
            context,
            "/warehouse/page",
            keyword,
            limit,
        )

    def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        return self._search_reference(
            context,
            "/staff/page",
            keyword,
            limit,
        )

    def _search_reference(
        self,
        context: InvocationContext,
        path: str,
        keyword: str,
        limit: int,
    ) -> BillingReferenceSnapshot:
        effective_limit = max(1, min(int(limit or 10), 20))
        params: dict[str, object] = {
            "pageNum": 1,
            "pageSize": effective_limit,
            "status": 1,
        }
        if keyword.strip():
            params["keyword"] = keyword.strip()
        data = self._get(context, path, params)
        page = data.get("data")
        if not isinstance(page, dict):
            raise DomainError("erp_live_response_invalid", "ERP 基础资料分页数据不是对象")
        rows = page.get("list")
        if not isinstance(rows, list):
            raise DomainError("erp_live_response_invalid", "ERP 基础资料列表不是数组")
        options = tuple(
            option
            for row in rows
            if isinstance(row, dict)
            and (option := _reference_option(row)) is not None
        )
        return BillingReferenceSnapshot(options=options)

    def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        data = self._post(context, "/sales/orders", payload)
        order_id = str(data.get("data") or "").strip()
        if not order_id:
            raise DomainError(
                "erp_live_response_invalid",
                "ERP 新增销售单成功但未返回单据 ID",
            )
        return BillingSalesOrderResult(order_id=order_id)

    def _resolve_order_id(
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
        page = self.search_sales_orders(
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

    def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingSalesOrderDetailResult:
        resolved = self._resolve_order_id(context, order_id)
        data = self._get(
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

    def search_sales_orders(
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
        data = self._get(context, "/sales/orders/page", params)
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

    def void_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        resolved = self._resolve_order_id(context, order_id)
        self._put(
            context,
            "/sales/orders/%s/void" % _path_segment(resolved),
            {},
        )

    def update_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        resolved = self._resolve_order_id(context, order_id)
        payload = dict(payload)
        payload["id"] = int(resolved)
        data = self._put(
            context,
            "/sales/orders/%s" % _path_segment(resolved),
            payload,
        )
        result_id = str(data.get("data") or "").strip()
        if not result_id:
            result_id = resolved
        return BillingSalesOrderResult(order_id=result_id)


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

    def get_json(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict:
        return self._request_json(context, "GET", path, params=params)

    def post_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        return self._request_json(context, "POST", path, payload=payload)

    def put_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict:
        return self._request_json(context, "PUT", path, payload=payload)

    def _request_json(
        self,
        context: InvocationContext,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict:
        credential = self._credential_provider.resolve(context)
        headers = {
            "Accept": "application/json",
        }
        if credential.kind == "bearer":
            headers["Authorization"] = "Bearer " + credential.value
        else:
            raise DomainError("billing_api_unauthorized", "当前开单会话的鉴权类型无效")
        url = business_api_url(self._base_url, path)
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        body_bytes = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ERP 请求开始 method=%s url=%s headers=%s body=%s",
                method,
                url,
                json.dumps(headers, ensure_ascii=False)
                if credential_dump_enabled()
                else "<已脱敏>",
                clip_log_text(json.dumps(payload, ensure_ascii=False))
                if payload is not None
                else "<无>",
            )
        started = time.perf_counter()
        request = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise DomainError("business_reauth_required", "当前业务系统授权已失效") from exc
            raise DomainError(
                "erp_live_request_failed",
                "ERP 接口返回 HTTP %s" % exc.code,
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise DomainError("business_upstream_unavailable", "当前业务系统不可用或请求超时") from exc
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


class UnavailableBillingApi:
    """本地参考客户端未注入开单产品 Adapter 时返回明确错误。"""

    @staticmethod
    def _raise() -> NoReturn:
        raise DomainError("billing_api_not_configured", "开单服务尚未注入已鉴权 BillingApiPort")

    def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> BillingProductSnapshot:
        self._raise()

    def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        self._raise()

    def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        self._raise()

    def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        self._raise()

    def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, object],
    ) -> BillingSalesOrderResult:
        self._raise()

    def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingSalesOrderDetailResult:
        self._raise()

    def search_sales_orders(
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

    def void_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        self._raise()

    def update_sales_order(
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
        "code": str(row.get("code") or "").strip(),
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
