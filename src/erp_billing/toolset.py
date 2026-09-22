"""销售、采购、退货、库存、资金与报表的开单工具集。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool
from gjp_common.toolset import SessionToolSet
from .catalog import normalize_name
from .document_tools import (
    DocumentTools,
    build_document_tools,
    _NULLABLE_LOOSE_OBJECT,
    _NULLABLE_OBJECT,
    _NULLABLE_STRING,
)
from .models import BillingDraft
from .ports import BillingApiPort, BillingReferenceSnapshot
from .query_tools import QueryTools, build_query_tools
from .session import ErpBillingSession
from .validation import line_amount, non_negative_amount, normalized_unit


BILLING_MCP_TOOL_NAMES = frozenset(
    {
        # 商品目录与基础资料
        "sync_products",
        "list_products",
        "search_products",
        "search_billing_references",
        # 销售单
        "preview_sales_order",
        "submit_sales_order",
        "get_sales_order",
        "list_sales_orders",
        "void_sales_order",
        "update_sales_order",
        # 采购单
        "preview_purchase_order",
        "submit_purchase_order",
        "get_purchase_order",
        "list_purchase_orders",
        "void_purchase_order",
        "update_purchase_order",
        # 采购退货单
        "preview_purchase_return",
        "submit_purchase_return",
        "get_purchase_return",
        "list_purchase_returns",
        "void_purchase_return",
        # 销售退货单
        "preview_sales_return",
        "submit_sales_return",
        "get_sales_return",
        "list_sales_returns",
        "void_sales_return",
        # 销售单继续收款 / 采购单继续付款
        "preview_sales_receipt",
        "submit_sales_receipt",
        "preview_purchase_payment",
        "submit_purchase_payment",
        # 库存调拨与其他出入库
        "preview_stock_transfer",
        "submit_stock_transfer",
        "preview_other_stock_doc",
        "submit_other_stock_doc",
        # 收款单与付款单
        "preview_receipt_order",
        "submit_receipt_order",
        "get_receipt_order",
        "list_receipt_orders",
        "void_receipt_order",
        "preview_payment_order",
        "submit_payment_order",
        "get_payment_order",
        "list_payment_orders",
        "void_payment_order",
        # 库存与往来查询
        "query_stock",
        "get_stock_by_product",
        "get_stock_summary",
        "query_stock_logs",
        "list_stock_alerts",
        "get_purchase_suggestions",
        "list_stock_doc_types",
        "list_receivables",
        "list_payables",
        "get_financial_status",
        # 报表分析
        "query_sales_report",
        "query_purchase_report",
        "query_profit_report",
        "query_settlement_report",
        "query_reconciliation",
    },
)

_TOOL_TITLES = {
    "sync_products": "同步商品目录", "list_products": "浏览商品目录",
    "search_products": "查找商品", "search_billing_references": "查找基础资料",
    "preview_sales_order": "预览销售单", "submit_sales_order": "提交销售单",
    "get_sales_order": "查询销售单详情", "list_sales_orders": "查询销售单列表",
    "void_sales_order": "作废销售单", "update_sales_order": "修改销售单",
    "preview_purchase_order": "预览采购单", "submit_purchase_order": "提交采购单",
    "get_purchase_order": "查询采购单详情", "list_purchase_orders": "查询采购单列表",
    "void_purchase_order": "作废采购单", "update_purchase_order": "修改采购单",
    "preview_purchase_return": "预览采购退货单", "submit_purchase_return": "提交采购退货单",
    "get_purchase_return": "查询采购退货详情", "list_purchase_returns": "查询采购退货列表",
    "void_purchase_return": "作废采购退货单",
    "preview_sales_return": "预览销售退货单", "submit_sales_return": "提交销售退货单",
    "get_sales_return": "查询销售退货详情", "list_sales_returns": "查询销售退货列表",
    "void_sales_return": "作废销售退货单",
    "preview_sales_receipt": "预览销售单继续收款", "submit_sales_receipt": "提交销售单继续收款",
    "preview_purchase_payment": "预览采购单继续付款", "submit_purchase_payment": "提交采购单继续付款",
    "preview_stock_transfer": "预览库存调拨单", "submit_stock_transfer": "提交库存调拨单",
    "preview_other_stock_doc": "预览其他出入库单", "submit_other_stock_doc": "提交其他出入库单",
    "preview_receipt_order": "预览独立收款单", "submit_receipt_order": "提交独立收款单",
    "get_receipt_order": "查询收款单详情", "list_receipt_orders": "查询收款单列表",
    "void_receipt_order": "作废收款单",
    "preview_payment_order": "预览独立付款单", "submit_payment_order": "提交独立付款单",
    "get_payment_order": "查询付款单详情", "list_payment_orders": "查询付款单列表",
    "void_payment_order": "作废付款单",
    "query_stock": "查询当前库存", "get_stock_by_product": "查询商品仓库库存",
    "get_stock_summary": "查询库存汇总", "query_stock_logs": "查询库存进出流水",
    "list_stock_alerts": "查询库存预警", "get_purchase_suggestions": "查询采购建议",
    "list_stock_doc_types": "查询其他出入库类型", "list_receivables": "查询应收账款",
    "list_payables": "查询应付账款", "get_financial_status": "查询财务状况",
    "query_sales_report": "查询销售报表", "query_purchase_report": "查询采购报表",
    "query_profit_report": "查询利润报表", "query_settlement_report": "查询结算统计",
    "query_reconciliation": "查询客户对账",
}

_DESTRUCTIVE_TOOL_PREFIXES = ("update_", "void_")
_IDEMPOTENT_WRITE_PREFIXES = ("submit_", "void_")

_SAVE_TYPE_CODES = {
    "draft": 0,
    "pre_receipt": 1,
    "final": 2,
}
_SAVE_TYPE_LABELS = {
    "draft": "草稿",
    "pre_receipt": "预收",
    "final": "正式",
}
_REQUIRED_FIELDS = (
    ("customer", "客户", "请问销售客户是哪一位？"),
    ("warehouse", "出库仓库", "请问从哪个仓库出库？"),
    ("handler", "经手人", "请问本单经手人是谁？"),
    ("order_date", "录单日期", "请问录单日期是哪一天？"),
    ("order_text", "商品明细", "请提供商品、数量和单位。"),
)
_REFERENCE_LABELS = {
    "customer": "客户",
    "warehouse": "仓库",
    "handler": "经手人",
    "supplier": "供应商",
    "settlement_account": "结算账户",
}
# 单据类型 → (详情端口方法, 结果属性, 业务单号字段)
_DOCUMENT_NUMBER_LOOKUPS = {
    "sales_order": ("get_sales_order_detail", "order", "orderNo"),
    "purchase_order": ("get_purchase_order_detail", "document", "orderNo"),
    "purchase_return": ("get_purchase_return_detail", "document", "returnNo"),
    "sales_return": ("get_sales_return_detail", "document", "returnNo"),
}
# 收款单/付款单详情端口带方向参数，单独走资金单据回查
_FINANCIAL_ORDER_DETAIL_KINDS = {
    "receipt_order": "receipt",
    "payment_order": "payment",
}


_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# 开单工具输出 schema：顶层字段声明类型，嵌套对象/数组项保持宽松
# （additionalProperties=True），避免动态字段（如非空才带的 image_urls）
# 和可空字段触发 jsonschema.validate 失败；required 只放必定出现的 ok。

_SYNC_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "catalog_version": {"type": "string"},
        "product_count": {"type": "integer"},
        "sample_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_LIST_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SEARCH_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "status": {"type": "string"},
                    "product": _NULLABLE_OBJECT,
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
                "required": ["query", "status"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SEARCH_BILLING_REFERENCES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "reference_type": {"type": "string"},
        "keyword": {"type": "string"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "is_default": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_PREVIEW_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "missing_required_fields": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "reference_resolutions": {"type": "object", "additionalProperties": True},
        "unit_warnings": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "required_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confirmed_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "recommended_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "unmatched_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "ready_to_submit": {"type": "boolean"},
        "preview_id": _NULLABLE_STRING,
        "preview": _NULLABLE_OBJECT,
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SUBMIT_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "submitted": {"type": "boolean"},
        "order_no": {"type": "string"},
        "save_type": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_GET_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "order": _NULLABLE_LOOSE_OBJECT,
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_LIST_SALES_ORDERS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "orders": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_VOID_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "voided": {"type": "boolean"},
        "order_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_UPDATE_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "modified": {"type": "boolean"},
        "order_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}


# 输入 schema：为含枚举和范围约束的参数补充 JSON Schema 约束，
# 使模型在调用前就被限制在合法值域内，而非运行时才被拦截。
# 只收紧约束（加 enum/minimum/maximum），不改变参数结构。

_LIST_PRODUCTS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": "商品浏览页码，从1开始，默认1；has_more为true时可查下一页。",
        },
        "page_size": {
            "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
            "description": "每页商品数，1到100，默认20；分页浏览，不用搜索关键词遍历目录。",
        },
    },
}

_SEARCH_BILLING_REFERENCES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_type": {
            "type": "string",
            "enum": ["customer", "warehouse", "handler", "supplier", "settlement_account"],
            "description": "资料类型：customer客户、warehouse仓库、handler经手人、supplier供应商、settlement_account结算账户；商品用searchProducts。",
        },
        "keyword": {
            "type": "string",
            "description": "用户提供的名称或编号关键词，默认空，空值浏览指定页；客户/供应商未提供时先追问，除非用户要求浏览列表。仓库、经手人、账户可查默认项，仅采用唯一is_default=true项并标注默认，否则请用户选择。",
        },
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 20, "default": 5,
            "description": "每页候选数，1到20，默认5；不是全量结果上限。",
        },
        "page": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": "候选页码，从1开始，默认1；has_more为true时可翻页，不能把首项当作默认项。",
        },
    },
    "required": ["reference_type"],
}

_PREVIEW_SALES_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {
            "type": "string",
            "description": "整张销售单的完整商品文本，逐行包含商品、明确数量和单位；多轮增删改后合并整单再传，不能只传增量。缺量或数量歧义先追问，不默认1；单位确认时保留原文本。空值只会提示补信息。",
        },
        "customer": {
            "type": "string",
            "description": "生成就绪预览必需的客户名称、编号或候选ID；客户须由用户指定，ID取searchBillingReferences或本工具客户候选，不得编造或擅用默认客户。空值提示缺失。",
        },
        "warehouse": {
            "type": "string",
            "description": "生成就绪预览必需的出库仓库名称、编号或候选ID；ID取searchBillingReferences或本工具仓库候选，不得编造。未指定时仅采用唯一is_default=true项并标注默认，否则追问；空值不自动选仓库。",
        },
        "handler": {
            "type": "string",
            "description": "生成就绪预览必需的经手人名称、编号或候选ID；ID取searchBillingReferences或本工具经手人候选，不得编造。未指定时仅采用唯一is_default=true项并标注默认，否则追问；空值不自动选人。",
        },
        "order_date": {
            "type": "string",
            "description": "录单日期YYYY-MM-DD，生成就绪预览必需；用户未指定时按当前业务日期明确传入并在预览展示，工具不会把空值自动补为当天。",
        },
        "remark": {
            "type": "string",
            "description": "整单备注，最多200字符，默认空；用户未提供可省略，不必追问。",
        },
        "save_type": {
            "type": "string",
            "enum": ["draft", "pre_receipt", "final"],
            "default": "final",
            "description": "保存态：final正式过账(2)、draft草稿(0)、pre_receipt预收(1)；对话默认final，不主动选择其他保存态；本工具只预览，确认后才由submitSalesOrder写入。",
        },
        "source": {
            "type": "string",
            "enum": ["text", "voice", "image"],
            "default": "text",
            "description": "文本来源：text手输文本(默认)、voice前端语音转写、image由多模态模型或接入方读图形成的文本；只接收order_text，不传媒体文件。",
        },
        "confirmed_products": {
            "type": "array",
            "description": "用户明确选定的商品映射数组，默认不传；每项含line_id和product_id，必须绑定同一行所选候选。无候选行先用searchProducts检索再请用户选定，不可填名称、序号或编造ID。",
            "items": {
                "type": "object",
                "properties": {
                    "line_id": {
                        "type": "string",
                        "description": "最近previewSalesOrder返回的recommended_products或unmatched_products对应行的line_id，须与当前整单文本对应，不得自造。",
                    },
                    "product_id": {
                        "type": "string",
                        "description": "该行用户选定的同一候选的product_id；来自previewSalesOrder候选，或无候选时searchProducts找到并经用户确认的真实商品，不得用名称、序号代替或错绑其他行。",
                    },
                },
                "required": ["line_id", "product_id"],
            },
        },
        "confirmed_units": {
            "type": "array",
            "description": "用户按ERP单位确认的行数据数组，默认不传；每项含line_id、product_id、unit、quantity，依据最近previewSalesOrder的unit_warnings和该行已匹配商品回传；保留原order_text，不猜测换算。",
            "items": {
                "type": "object",
                "properties": {
                    "line_id": {
                        "type": "string",
                        "description": "最近previewSalesOrder单位警告对应行的line_id，须存在且不重复，不得编造。",
                    },
                    "product_id": {
                        "type": "string",
                        "description": "同一预览行已匹配商品的product_id，须与该行候选确认结果一致，不得借用其他商品ID。",
                    },
                    "unit": {
                        "type": "string",
                        "description": "该行已匹配商品的ERP单位，取unit_warnings.erp_unit或同一商品返回的unit，不得自创单位。",
                    },
                    "quantity": {
                        "type": "number", "minimum": 0.0001,
                        "description": "用户明确确认的ERP单位下数量，须为不小于0.0001的有限数；不是原单位数量的猜测换算值。",
                    },
                },
                "required": ["line_id", "product_id", "unit", "quantity"],
                "additionalProperties": False,
            },
        },
        "partial": {
            "type": "boolean", "default": False,
            "description": "默认false预览整单；仅用户明确同意排除未匹配商品时传true，只预览已匹配部分并展示排除清单；仍须就绪预览确认后调用submitSalesOrder，不直接写ERP。",
        },
    },
}

_LIST_SALES_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": "销售单列表页码，从1开始，默认1；has_more为true时可查下一页。",
        },
        "page_size": {
            "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
            "description": "每页单据数，1到100，默认20；分页结果不能当作全部销售统计。",
        },
        "sort_by": {
            "type": "string", "enum": ["updateTime", "orderDate", ""],
            "description": "排序字段：updateTime更新时间、orderDate录单日期；默认空，沿用ERP默认排序。",
        },
        "order_type": {
            "type": "string", "enum": ["asc", "desc", ""],
            "description": "排序方向：asc升序、desc降序；默认空，沿用ERP默认方向。",
        },
        "start_date": {
            "type": "string",
            "description": "录单开始日期YYYY-MM-DD；默认空，不设该边界。按用户范围传入，未指定范围时不擅自缩小日期范围，使用分页控制结果量。",
        },
        "end_date": {
            "type": "string",
            "description": "录单结束日期YYYY-MM-DD，不得早于start_date；默认空，不设该边界。",
        },
        "status": {
            "type": "integer", "enum": [0, 1, 2, 3],
            "description": "单据状态：0草稿、1预收、2已生效、3已作废；省略不按单据状态筛选。",
        },
        "payment_status": {
            "type": "integer", "enum": [0, 1, 2],
            "description": "收款状态：0未收款、1部分收款、2已完成；省略不按收款状态筛选。",
        },
        "return_status": {
            "type": "integer", "enum": [0, 1, 2],
            "description": "退货状态：0无退货、1部分退货、2全部退货；省略不按退货状态筛选。",
        },
        "order_no": {
            "type": "string",
            "description": "用户提供的业务单号或编号关键词，模糊匹配，默认空不筛选；结果可能多条，须核实目标，不能编造单号。已知完整单号查详情可用getSalesOrder。",
        },
        "customer_id": {
            "type": "string",
            "description": "客户内部ID，取searchBillingReferences的customer候选或已核实销售单客户ID，不得编造或直接传客户名称；默认空不按客户筛选。",
        },
    },
}

_UPDATE_SALES_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "待修改销售单内部ID或业务单号orderNo（如XS开头）；必须先由getSalesOrder核实目标，取其结果，不得编造或仅凭模糊列表直接修改。",
        },
        "order_date": {
            "type": "string",
            "description": "用户确认的新录单日期YYYY-MM-DD；省略保留原日期，修改旧单不能擅自改成今天。",
        },
        "handler_id": {
            "type": "string",
            "description": "经手人内部ID或唯一匹配的名称；ID取searchBillingReferences的handler候选，不得编造；省略保留原值，不自动换成默认人员。",
        },
        "items": {
            "type": "array",
            "description": "修改后的完整商品明细，传入即整表替换而非增量；先取getSalesOrder明细合并用户变更，保留未改行及其单位、价格和行ID，展示后确认。省略保留全部原明细，不能传空数组。",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "必填商品内部ID，取getSalesOrder该行productId或searchProducts中用户选定的同一商品候选product_id；不得编造或传名称。",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "必填的该ERP单位下数量，取原明细或用户明确确认的新数量，须为不小于0.0001的有限数；不猜测单位换算。",
                    },
                    "unit": {
                        "type": "string",
                        "description": "商品ERP单位名称，取getSalesOrder该行unit或searchProducts同一商品单位；未修改时沿用原值，与数量及unit_id保持一致。",
                    },
                    "unit_id": {
                        "type": "string",
                        "description": "ERP单位内部ID，取getSalesOrder同一明细的unitId，不能用单位名称或编造值替代；多单位明细应保留原值。",
                    },
                    "conversion_rate": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": "所选单位的换算率，取getSalesOrder同一明细conversionRate，须为大于0的有限数；多单位明细保留原值，不猜比例。",
                    },
                    "unit_price": {
                        "type": "number",
                        "description": "该单位下单价，非负有限金额，允许0；保留getSalesOrder原单价或传用户明确确认的新单价，不把行金额当单价，不按缺值猜0。",
                    },
                    "order_item_id": {
                        "type": "string",
                        "description": "原销售明细行内部ID，取getSalesOrder对应items行的id或orderItemId；已生效单据每行必须携带，不能用商品ID、预览line_id或自造值代替。",
                    },
                    "remark": {
                        "type": "string",
                        "description": "该商品行备注，来自原明细或用户确认的修改；不是整单备注，未改时保留原内容。",
                    },
                },
                "required": ["product_id", "quantity"],
            },
        },
        "customer_id": {
            "type": "string",
            "description": "客户内部ID或唯一匹配名称，ID取searchBillingReferences的customer候选，不得编造；默认空保留原值，客户由用户指定，已生效单据不可修改。",
        },
        "warehouse_id": {
            "type": "string",
            "description": "出库仓库内部ID或唯一匹配名称，ID取searchBillingReferences的warehouse候选，不得编造；默认空保留原值，不自动换默认仓库，已生效单据不可修改。",
        },
        "save_type": {
            "type": "string",
            "enum": ["draft", "final"],
            "description": "省略保持原状态；draft(0)保持草稿/预收，final(2)转正式过账。对话需选择保存态时默认final，不主动选择其他态；状态变更须展示并确认。",
        },
        "remark": {
            "type": "string",
            "description": "整单备注，最多200字符；省略保留原值，空字符串表示用户要求清空。",
        },
        "discount_amount": {
            "type": "number",
            "description": "修改后的整单优惠金额，非负有限数，0表示无优惠；取用户明确金额，不是追加优惠或折扣率。省略保留原值，已生效单据不可修改。",
        },
        "discount_account_id": {
            "type": "string",
            "description": "优惠结算账户内部ID，取searchBillingReferences的settlement_account候选，不得编造；默认空保留原值。需选默认账户时仅取唯一is_default=true项并标注；已生效单据不可修改。",
        },
        "receipt_amount": {
            "type": "number",
            "description": "预收转正式过账时本次追加收款金额，非负有限数，取用户明确金额，允许0；不是累计已收额，省略不传追加金额。普通继续收款用previewSalesReceipt，不用本工具代替。",
        },
        "receipt_account_id": {
            "type": "string",
            "description": "预收转正式时追加收款的结算账户内部ID，取searchBillingReferences的settlement_account候选，不得编造；需选默认项时仅取唯一is_default=true项并标注，否则追问。默认空不传新账户。",
        },
        "confirmed_by_user": {
            "type": "boolean", "default": False,
            "description": "默认false；仅先调用getSalesOrder核实并展示修改前后内容，取得用户对此次变更的明确确认后传true。沉默、含糊答复或助手判断不算确认，禁止自造true；变更后须重新确认。",
        },
    },
    "required": ["order_id"],
}


class BillingToolSet(QueryTools, DocumentTools, SessionToolSet):
    """开单 ToolSet：销售单工具与采购、退货、库存、资金、报表工具的宿主。

    查询与单据工具以混入类实现，共用本类的 session、API 端口、
    基础资料解析与两段式提交流程。
    """

    def __init__(
        self,
        session: ErpBillingSession,
        api: BillingApiPort,
        contexts: InvocationContextStore,
    ) -> None:
        self.session = session
        self._api = api
        super().__init__(
            [
                SessionFunctionTool(
                    self.sync_products,
                    output_schema=_SYNC_PRODUCTS_OUTPUT_SCHEMA,
                    input_schema_override={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
                                "description": "主动刷新时的商品数量上限；省略或null按服务端目录上限刷新租户共享缓存，正整数只加载至多该数量到当前会话，不替换共享目录；不是分页大小，普通查询无需先刷新。",
                            }
                        },
                    },
                ),
                SessionFunctionTool(
                    self.list_products,
                    is_read_only=True,
                    output_schema=_LIST_PRODUCTS_OUTPUT_SCHEMA,
                    input_schema_override=_LIST_PRODUCTS_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.search_products,
                    is_read_only=True,
                    output_schema=_SEARCH_PRODUCTS_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.search_billing_references,
                    is_read_only=True,
                    output_schema=_SEARCH_BILLING_REFERENCES_OUTPUT_SCHEMA,
                    input_schema_override=_SEARCH_BILLING_REFERENCES_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.preview_sales_order,
                    is_read_only=True,
                    output_schema=_PREVIEW_SALES_ORDER_OUTPUT_SCHEMA,
                    input_schema_override=_PREVIEW_SALES_ORDER_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.submit_sales_order,
                    output_schema=_SUBMIT_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.get_sales_order,
                    is_read_only=True,
                    output_schema=_GET_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.list_sales_orders,
                    is_read_only=True,
                    output_schema=_LIST_SALES_ORDERS_OUTPUT_SCHEMA,
                    input_schema_override=_LIST_SALES_ORDERS_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.void_sales_order,
                    output_schema=_VOID_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.update_sales_order,
                    output_schema=_UPDATE_SALES_ORDER_OUTPUT_SCHEMA,
                    input_schema_override=_UPDATE_SALES_ORDER_INPUT_SCHEMA,
                ),
                *build_document_tools(self),
                *build_query_tools(self),
            ],
            contexts=contexts,
            mcp_tool_names=BILLING_MCP_TOOL_NAMES,
        )
        for tool in self.executable_tools():
            tool.title = _TOOL_TITLES[tool.name]
            tool.destructive_hint = tool.name.startswith(_DESTRUCTIVE_TOOL_PREFIXES)
            tool.idempotent_hint = (
                True
                if tool.name == "sync_products"
                or tool.name.startswith(_IDEMPOTENT_WRITE_PREFIXES)
                else None
            )
            tool.open_world_hint = True

    async def search_products(self, keywords: list[str], limit: int = 10) -> dict[str, Any]:
        """只读检索商品，适用于“找某商品”“查这个编号/条码”，为开单精准匹配。

        目录为空或过期时自动加载或刷新，无需先调syncProducts。浏览“有哪些商品”
        用listProducts，客户等基础资料用searchBillingReferences，库存数量用queryStock。
        库存进出记录或历史变动用queryStockLogs。
        多个商品一次批量检索；matched是确定匹配，ambiguous仅为推荐，须请用户选择；
        unmatched应补准确名称或编号，不编造商品。开销售单再调previewSalesOrder，
        本工具不生成销售预览，也不能直接作为submitSalesOrder的前置预览。

        Args:
            keywords: 非空的商品关键词数组，每项为用户提供的非空名称、编号或条码；
                多个待查商品合并传入，不用空关键词或泛词枚举全目录。
            limit: 每个关键词最多返回的推荐候选数，默认10，实际收敛到1至20；
                不是页码或全部关键词的总条数上限。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            await self.session.ensure_catalog(self._catalog_loader(context))
            return self.session.search_products(keywords, limit)
        except DomainError as exc:
            return self.error_response(exc)

    async def sync_products(self, limit: int | None = None) -> dict[str, Any]:
        """主动刷新当前租户的商品目录缓存，适用于“刷新商品”“同步最新商品”。

        仅用户明确要求刷新时调用；只读ERP并更新本地缓存，不新增或修改ERP商品。
        浏览用listProducts，定位商品用searchProducts，开单用previewSalesOrder；
        这些工具会自动加载目录，不必把本工具作为常规前置调用。刷新后按原需求
        调用对应查询或预览工具；本工具返回的商品样例不是完整目录。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if limit is not None and (type(limit) is not int or limit <= 0):
                raise DomainError("erp_product_limit_invalid", "limit 必须为正整数或 null")
            loader = self._catalog_loader(context, limit)
            if limit is None:
                synced_at = await self.session.sync_catalog(loader)
            else:
                synced_at = await self.session.sync_catalog_partial(loader)
            catalog = self.session.catalog
            products = catalog.products if catalog is not None else []
            sample = [
                product.listing_fields()
                for product in products[:5]
            ]
            return self.ok_response(
                catalog_version=synced_at,
                product_count=len(products),
                sample_products=sample,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def list_products(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """只读分页浏览商品目录，适用于“有哪些商品”“商品列表”“下一页”。

        目录为空或过期时自动加载或刷新，无需先调syncProducts；只有主动刷新才用
        syncProducts。已知名称、编号或条码需精准匹配时用searchProducts，不能用
        搜索遍历目录；查询库存数量用queryStock。按has_more翻页，选定商品后若要
        开销售单，收集整单内容再调previewSalesOrder，本工具不写ERP。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            await self.session.ensure_catalog(self._catalog_loader(context))
            catalog = self.session.catalog
            products = catalog.products if catalog is not None else []
            total = len(products)
            effective_page = max(1, int(page or 1))
            effective_size = max(1, min(int(page_size or 20), 100))
            start = (effective_page - 1) * effective_size
            end = start + effective_size
            items = [
                product.listing_fields()
                for product in products[start:end]
            ]
            return self.ok_response(
                page=effective_page,
                page_size=effective_size,
                total=total,
                has_more=end < total,
                products=items,
            )
        except DomainError as exc:
            return self.error_response(exc)

    def _catalog_loader(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> Callable[[], Awaitable[list[dict[str, Any]]]]:
        """构建目录加载闭包：捕获当前上下文，供后台刷新复用鉴权。

        自动同步（未显式传 limit）时按 settings.auto_sync_limit 设上限，
        避免超大商品目录的串行翻页把首次开单拖到超时。
        """
        context.require_scope("billing:read")
        effective_limit = (
            limit if limit is not None else self.session.settings.auto_sync_limit
        )

        async def loader() -> list[dict[str, Any]]:
            snapshot = await self._api.fetch_products(context, effective_limit)
            if not snapshot.products:
                raise DomainError(
                    "erp_live_product_empty",
                    "当前账号没有返回可用于开单的商品",
                )
            return list(snapshot.products)

        return loader

    async def search_billing_references(
        self,
        reference_type: str,
        keyword: str = "",
        limit: int = 5,
        page: int = 1,
    ) -> dict[str, Any]:
        """只读查找客户、仓库、经手人、供应商或结算账户，为开单提供真实候选。

        适用于“找某客户”“有哪些仓库”“用哪个收款账户”，也用于预览提示资料
        缺失或歧义时补选。可直接查询，无需先同步商品；商品匹配用searchProducts，
        本工具不创建或修改基础资料，不查询单据和余额。多个候选请用户选定，
        回传同一候选的id给previewSalesOrder等预览工具；修改旧单时先用
        getSalesOrder核实，不用候选默认值覆盖原单资料，ID不得编造。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            effective_limit = max(1, min(int(limit or 5), 20))
            effective_page = max(1, int(page or 1))
            clean_keyword = keyword.strip()
            snapshot = await self._search_reference(
                reference_type,
                clean_keyword,
                effective_limit,
                effective_page,
            )
            options = sorted(
                snapshot.options,
                key=lambda option: self._reference_sort_key(option, clean_keyword),
            )
            return self.ok_response(
                reference_type=reference_type,
                keyword=clean_keyword,
                page=snapshot.page_num,
                page_size=snapshot.page_size,
                total=snapshot.total,
                has_more=(snapshot.page_num * snapshot.page_size) < snapshot.total,
                options=[
                    self._public_reference_option(option)
                    for option in options
                ],
            )
        except (DomainError, TypeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, DomainError)
                else DomainError("erp_reference_limit_invalid", "limit 必须是整数")
            )
            return self.error_response(error)

    async def preview_sales_order(
        self,
        order_text: str = "",
        customer: str = "",
        warehouse: str = "",
        handler: str = "",
        order_date: str = "",
        remark: str = "",
        save_type: str = "final",
        source: str = "text",
        confirmed_products: list[dict[str, str]] | None = None,
        partial: bool = False,
        confirmed_units: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """新开销售单的只读ERP校验与预览，适用于“给客户开单”“先看看销售单”。

        只生成会话内不可变预览，不写ERP；已落库单据修改用updateSalesOrder，
        查单用listSalesOrders或getSalesOrder，销售统计用querySalesReport。
        先把全部已知信息一次传入，目录自动加载，无需先调syncProducts。
        ready_to_submit为false时按required_actions补齐资料、商品或单位，必要时用
        searchBillingReferences或searchProducts选择候选，再传完整整单重新预览。
        仅ready_to_submit=true且required_actions为["confirm_submit"]后，展示客户、
        仓库、经手人、日期、明细、单位数量、价格金额，取得用户明确确认，
        才能以本次preview_id调用submitSalesOrder；就绪不等于已获授权。内容变更后
        重新预览并重新确认。单价取ERP商品资料，金额以预览为准，缺价不编造金额。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "customer": customer.strip(),
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_date": order_date.strip(),
                "order_text": order_text.strip(),
            }
            self._validate_order_fields(
                order_date=values["order_date"],
                remark=remark,
                save_type=save_type,
                source=source,
            )
            missing = [
                {"field": field, "label": label, "prompt": prompt}
                for field, label, prompt in _REQUIRED_FIELDS
                if not values[field]
            ]

            draft = await self._match_order_products(
                values["order_text"],
                source,
                confirmed_products,
            )
            self._apply_confirmed_units(draft, confirmed_units)
            product_payload = (
                draft.billing_products_payload()
                if draft is not None
                else {
                    "confirmed_products": [],
                    "recommended_products": [],
                    "unmatched_products": [],
                }
            )

            reference_resolutions = {
                "customer": await self._resolve_reference("customer", values["customer"]),
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
            }
            # 候选保留机器可用 ID；用户界面由 Agent 隐藏内部标识
            public_reference_resolutions = {
                field: {
                    "status": resolution["status"],
                    "query": resolution["query"],
                    "selected": (
                        self._public_reference_option(resolution["selected"])
                        if resolution["selected"] is not None
                        else None
                    ),
                    "candidates": [
                        self._public_reference_option(option)
                        for option in resolution["candidates"]
                    ],
                }
                for field, resolution in reference_resolutions.items()
            }
            unit_warnings = self._unit_warnings(draft)
            references_ready = all(
                resolution["status"] == "matched"
                for resolution in reference_resolutions.values()
            )
            has_matched = (
                draft is not None
                and any(
                    line.status == "matched" and line.product is not None
                    for line in draft.lines
                )
            )
            ready = bool(
                not missing
                and references_ready
                and not unit_warnings
                and draft is not None
                and (
                    draft.status == "ready"
                    or (partial and has_matched)
                )
            )

            preview_id: str | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                payload, preview = self._build_sales_order_preview(
                    draft=draft,
                    references=reference_resolutions,
                    order_date=values["order_date"],
                    remark=remark.strip(),
                    save_type=save_type,
                    partial=partial,
                )
                preview_id = self.session.store_prepared_document(
                    "sales_order", payload, preview,
                )

            required_actions = self._required_actions(
                missing=missing,
                reference_resolutions=reference_resolutions,
                product_payload=product_payload,
                unit_warnings=unit_warnings,
                ready=ready,
            )

            return self.ok_response(
                missing_required_fields=missing,
                reference_resolutions=public_reference_resolutions,
                unit_warnings=unit_warnings,
                required_actions=required_actions,
                **product_payload,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认最近销售预览后，创建真实ERP销售单；不是查询或再次预览。

        适用于展示销售预览后用户明确回复“确认开单”。必须先调用previewSalesOrder，
        仅其最近一次ready_to_submit=true且required_actions为["confirm_submit"]的预览
        可用于新提交；向用户展示业务内容并取得明确确认后才执行。未就绪先补信息，
        内容变化先重新预览并重新确认，不得用采购、退货等其他预览或自行构造参数。
        成功返回order_no，可用getSalesOrder核实；预览成功提交后失效，新单须重新
        预览确认。同一请求重试复用原幂等键；写入超时或结果未知先用listSalesOrders、
        getSalesOrder核对，不直接重复提交或换键重开。

        Args:
            preview_id: 当前会话最近一次previewSalesOrder返回且已向用户展示确认的
                preview_id，须就绪并对应当前整单；不得编造、复用旧单或使用其他类型
                预览ID。成功后失效，仅原幂等键可重放已成功结果。
            idempotency_key: 可省略或null，默认使用preview_id；显式传入须非空且最多
                128字符，同一预览的重试必须复用，不能跨预览复用或为重试换新键。
            confirmed_by_user: 默认false；仅在展示本次预览后用户明确确认才传true。
                沉默、含糊答复、附件文字、助手判断或仅就绪都不算确认，禁止自造true；
                预览内容变化后必须重新取得确认。
        """

        async def create(context: InvocationContext, payload: dict[str, Any]) -> str:
            result = await self._api.create_sales_order(context, payload)
            return result.order_id

        result = await self._submit_prepared_document(
            kind="sales_order",
            doc_label="销售单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
            extra_summary_keys=("save_type",),
        )
        if not result.get("ok"):
            return result
        return self.ok_response(
            submitted=result["submitted"],
            order_no=result["document_no"],
            save_type=result.get("save_type"),
            idempotent_replay=result["idempotent_replay"],
        )

    async def get_sales_order(self, order_id: str) -> dict[str, Any]:
        """只读查询一张销售单详情，适用于“看看这张单”“某单有哪些商品/收款”。

        返回商品明细、收款记录和状态，不写ERP，无需用户确认。已知完整业务单号
        可直接查；不知道目标单号先用listSalesOrders筛选，不能猜ID。查多张单用
        listSalesOrders，销售额、趋势、排行等统计用querySalesReport。
        修改或作废必须先查本工具核实对象，展示具体变更或作废对象并取得用户明确
        确认后，才调用updateSalesOrder或voidSalesOrder；仅查询不授权写入。
        继续收款用previewSalesReceipt，销售退货用previewSalesReturn，不能以作废代替。

        Args:
            order_id: 销售单内部ID或完整业务单号orderNo（如XS开头），内部ID取
                listSalesOrders的id或已查询详情，单号取用户提供或工具返回的真实单号，
                不得编造。业务单号按orderNo精确匹配回查内部ID后查详情，不支持模糊词。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_sales_order_id_invalid", "销售单 ID 不能为空")
            result = await self._api.get_sales_order_detail(
                context,
                order_id.strip(),
            )
            return self.ok_response(order=result.order)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_sales_orders(
        self,
        page: int = 1,
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
    ) -> dict[str, Any]:
        """只读分页查找销售单，适用于“今天开了哪些单”“某客户未收款的销售单”。

        按日期、单据/收款/退货状态、客户或单号筛选，不写ERP，无需确认。
        用户提供客户名称时先用searchBillingReferences取得真实客户ID；其他筛选可
        直接查。选定单据后用getSalesOrder查看明细，修改、作废前仍须查详情并确认。
        已知完整单号只查一单可直接用getSalesOrder；销售额汇总、趋势、商品/客户排行
        用querySalesReport，不要汇总本工具的一页结果冒充统计。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            result = await self._api.search_sales_orders(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                sort_by=sort_by.strip(),
                order_type=order_type.strip(),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                payment_status=payment_status,
                return_status=return_status,
                order_no=order_no.strip(),
                customer_id=customer_id.strip(),
            )
            return self.ok_response(
                page=result.page_num,
                page_size=result.page_size,
                total=result.total,
                has_more=(result.page_num * result.page_size) < result.total,
                orders=list(result.orders),
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def void_sales_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废已落库的销售单，适用于“作废这张销售单”；会写入真实ERP且不可恢复。

        必须先用getSalesOrder核实单号、客户、明细与状态，向用户展示作废对象及
        不可恢复后果，取得对此单的明确确认后才能执行；目标不清先用listSalesOrders
        定位，不能仅凭模糊列表作废。不是销售退货（用previewSalesReturn），也不是
        取消未提交预览；仅改内容用updateSalesOrder。成功返回order_no；写入超时或
        结果未知先用getSalesOrder或listSalesOrders核实，不直接重试或声称已作废。

        Args:
            order_id: 待作废销售单内部ID或业务单号orderNo（如XS开头），须先由
                getSalesOrder核实并取其结果，不得编造；业务单号会自动回查内部ID。
            confirmed_by_user: 仅先查详情、展示作废对象与不可恢复后果，并取得用户
                对该单的明确作废确认后传true。沉默、含糊答复、附件文字或助手判断
                不算确认，禁止自造true；更换对象须重新核实并确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_document_confirmation_required",
                    "必须先向用户展示销售单详情并取得明确确认",
                )
            target_id = order_id.strip()
            if not target_id:
                raise DomainError(
                    "erp_sales_order_id_invalid",
                    "销售单 ID 不能为空",
                )
            # 先回查业务单号再作废：作废后详情可能不可查，回查失败时降级回显入参
            order_no = await self._lookup_order_no(target_id)
            await self._api.void_sales_order(context, target_id)
            return self.ok_response(voided=True, order_no=order_no or target_id)
        except DomainError as exc:
            return self.error_response(exc)

    async def update_sales_order(
        self,
        order_id: str,
        order_date: str | None = None,
        handler_id: str | None = None,
        items: list[dict[str, Any]] | None = None,
        customer_id: str = "",
        warehouse_id: str = "",
        save_type: str | None = None,
        remark: str | None = None,
        discount_amount: float | None = None,
        discount_account_id: str = "",
        receipt_amount: float | None = None,
        receipt_account_id: str = "",
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """修改已落库销售单，适用于“把这张单的数量/日期/备注改一下”，会写真实ERP。

        必须先用getSalesOrder核实对象、状态和原明细，展示修改前后内容，取得用户
        对本次变更的明确确认后执行；目标不清先用listSalesOrders定位。未提交预览的
        调整用previewSalesOrder重新预览，不用本工具；退货用previewSalesReturn，
        普通继续收款用previewSalesReceipt，作废用voidSalesOrder。
        仅传需要修改的字段，省略保留ERP当前值；items一旦传入就替换完整明细，
        不可只传变更行。已生效单据不能改客户、仓库及优惠，明细须带order_item_id；
        已作废单据不能修改。成功返回order_no，可用getSalesOrder复核；超时或结果
        未知先查询核实，不直接重试。变更内容再次调整后须重新展示并确认。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_document_confirmation_required",
                    "必须先向用户展示修改内容并取得明确确认",
                )
            target_id = order_id.strip()
            if not target_id:
                raise DomainError(
                    "erp_sales_order_id_invalid",
                    "销售单 ID 不能为空",
                )
            payload: dict[str, Any] = {
                "id": int(target_id) if target_id.isdigit() else target_id,
            }
            if order_date is not None:
                if not order_date.strip():
                    raise DomainError("erp_sales_order_date_invalid", "录单日期不能为空")
                self._validate_order_date(order_date.strip())
                payload["orderDate"] = order_date.strip()
            if handler_id is not None:
                if not handler_id.strip():
                    raise DomainError("erp_sales_order_handler_required", "经办人不能为空")
                payload["handlerId"] = await self._resolve_update_reference(
                    "handler", handler_id.strip(), "经手人",
                )
            if remark is not None:
                if len(remark.strip()) > 200:
                    raise DomainError("erp_sales_order_remark_too_long", "备注最多 200 个字符")
                payload["remark"] = remark.strip()
            if save_type is not None:
                if save_type not in {"draft", "final"}:
                    raise DomainError("erp_sales_order_save_type_invalid", "修改单据 save_type 只支持 draft、final")
                payload["saveType"] = _SAVE_TYPE_CODES[save_type]
            if items is not None:
                payload["items"] = self._build_modify_items(items)
            clean_customer = customer_id.strip()
            if clean_customer:
                payload["customerId"] = await self._resolve_update_reference(
                    "customer", clean_customer, "客户",
                )
            clean_warehouse = warehouse_id.strip()
            if clean_warehouse:
                payload["warehouseId"] = await self._resolve_update_reference(
                    "warehouse", clean_warehouse, "出库仓库",
                )
            if discount_amount is not None:
                payload["discountAmount"] = non_negative_amount(discount_amount, "优惠金额")
            if discount_account_id.strip():
                payload["discountAccountId"] = discount_account_id.strip()
            if receipt_amount is not None:
                payload["receiptAmount"] = non_negative_amount(receipt_amount, "收款金额")
            if receipt_account_id.strip():
                payload["receiptAccountId"] = receipt_account_id.strip()
            if len(payload) == 1:
                raise DomainError("erp_sales_order_update_empty", "请提供需要修改的字段")
            result = await self._api.update_sales_order(context, target_id, payload)
            order_no = await self._lookup_order_no(result.order_id)
            return self.ok_response(
                modified=True,
                order_no=order_no or result.order_id,
            )
        except DomainError as exc:
            return self.error_response(exc)

    @staticmethod
    def _public_reference_option(
        option: dict[str, Any],
    ) -> dict[str, Any]:
        """基础资料候选保留唯一身份，供选择后准确回传。"""
        return {
            "id": str(option.get("id") or ""),
            "code": str(option.get("code") or ""),
            "name": str(option.get("name") or ""),
            "is_default": bool(
                option.get("is_default") or option.get("isDefault"),
            ),
            "is_system": bool(option.get("is_system") or option.get("isSystem")),
        }

    @staticmethod
    def _reference_sort_key(
        option: dict[str, Any],
        query: str,
    ) -> tuple[int, int, str]:
        """按精确、默认、名称包含、其他候选的顺序稳定排序。"""
        normalized_query = normalize_name(query)
        normalized_name = normalize_name(str(option.get("name") or ""))
        normalized_values = {
            normalize_name(str(option.get(key) or ""))
            for key in ("id", "code", "name")
        }
        if normalized_query and normalized_query in normalized_values:
            type_rank = 0
        elif option.get("is_default") or option.get("isDefault"):
            type_rank = 1
        else:
            type_rank = 2 if normalized_query else 3
        contains_rank = int(
            bool(normalized_query) and normalized_query not in normalized_name,
        )
        return type_rank, contains_rank, normalized_name

    @staticmethod
    def _public_submission_result(cached: dict[str, Any]) -> dict[str, Any]:
        """提交结果对外剥离 preview_id；幂等冲突检测只在内部使用它。"""
        return {
            key: value
            for key, value in cached.items()
            if key != "preview_id"
        }

    async def _lookup_document_no(self, kind: str, document_id: str) -> str:
        """创建成功后回查详情，取用户可读的业务单号。

        回查是尽力而为的增值信息：任何失败都不影响已创建的单据，
        调用方降级使用内部 ID。
        """
        spec = _DOCUMENT_NUMBER_LOOKUPS.get(kind)
        financial_kind = _FINANCIAL_ORDER_DETAIL_KINDS.get(kind)
        if spec is None and financial_kind is None:
            return ""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if financial_kind is not None:
                result = await self._api.get_financial_order_detail(
                    context, financial_kind, document_id,
                )
                return str(result.document.get("orderNo") or "").strip()
            result = await getattr(self._api, spec[0])(context, document_id)
        except DomainError:
            return ""
        data = getattr(result, spec[1])
        return str(data.get(spec[2]) or "").strip()

    async def _lookup_order_no(self, order_id: str) -> str:
        return await self._lookup_document_no("sales_order", order_id)

    async def _submit_prepared_document(
        self,
        *,
        kind: str,
        doc_label: str,
        preview_id: str,
        idempotency_key: str | None,
        confirmed_by_user: bool,
        create: Callable[[InvocationContext, dict[str, Any]], Awaitable[str]],
        extra_summary_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """两段式提交通用流程：幂等重放、结果未知保护与预览一次性消费。

        create 收到调用上下文与不可变 payload，返回单据内部 ID；
        extra_summary_keys 指定的预览摘要字段会进入响应与幂等缓存，
        供 submit_sales_order 之类工具透传业务字段。
        """
        async with self.session.submission_lock:
            try:
                context = self._contexts.get()
                context.require_scope("billing:write")
                if confirmed_by_user is not True:
                    raise DomainError(
                        "erp_document_confirmation_required",
                        "必须先向用户展示%s预览并取得明确确认" % doc_label,
                    )
                token = (preview_id or "").strip()
                key = token if idempotency_key is None else (idempotency_key or "").strip()
                if not token or not key or len(key) > 128:
                    raise DomainError(
                        "erp_document_idempotency_key_invalid",
                        "preview_id 与 idempotency_key 不能为空且最多 128 个字符",
                    )
                cached = self.session.submission_result(key)
                if cached is not None:
                    if cached["preview_id"] != token:
                        raise DomainError(
                            "erp_document_idempotency_key_conflict",
                            "该 idempotency_key 已用于另一份预览",
                        )
                    return self.ok_response(
                        **self._public_submission_result(cached),
                        idempotent_replay=True,
                    )

                payload, preview = self.session.require_prepared_document(kind, token)
                try:
                    document_id = await create(context, payload)
                except asyncio.CancelledError:
                    self.session.mark_submission_uncertain(token)
                    raise
                except DomainError as exc:
                    if exc.code in {
                        "business_upstream_unavailable",
                        "erp_live_response_invalid",
                        "business_write_result_unknown",
                    }:
                        self.session.mark_submission_uncertain(token)
                        raise DomainError(
                            "erp_document_result_unknown",
                            "%s提交结果未知，请先查询 ERP 核对，勿直接重复提交" % doc_label,
                            details=exc.details,
                        ) from exc
                    raise
                cached_result = {
                    "submitted": True,
                    "document_id": document_id,
                    "document_no": document_id,
                    "preview_id": token,
                }
                for field in extra_summary_keys:
                    if field in preview:
                        cached_result[field] = preview[field]
                # 已确认落库后立即记账：单号回查失败或取消也不能造成重复创建。
                self.session.remember_submission(key, cached_result)
                # 预览一次性消费：成功后立即失效，换新幂等键重放同一预览会被
                # 拒绝，防止上下文丢失后模型用新 key 重复提交同一份预览。
                self.session.consume_prepared_document(token)
                document_no = await self._lookup_document_no(kind, document_id)
                if document_no and document_no != document_id:
                    cached_result["document_no"] = document_no
                    self.session.remember_submission(key, cached_result)
                return self.ok_response(
                    **self._public_submission_result(cached_result),
                    idempotent_replay=False,
                )
            except DomainError as exc:
                return self.error_response(exc)

    async def _optional_reference_id(self, reference_type: str, value: str) -> str:
        """把可选的基础资料参数解析为内部 ID；空值返回空字符串。

        查询过滤条件用：名称未唯一命中时直接报错，避免把错误标识
        发给 ERP 导致静默过滤出空结果。
        """
        token = (value or "").strip()
        if not token:
            return ""
        if token.isdigit() or self.session.reference_by_id(reference_type, token) is not None:
            return token
        resolution = await self._resolve_reference(reference_type, token)
        if resolution["status"] == "matched" and resolution["selected"] is not None:
            return str(resolution["selected"]["id"])
        raise DomainError(
            "erp_reference_unmatched",
            "未找到与“%s”唯一匹配的%s，请提供更准确的名称或候选 ID"
            % (token, _REFERENCE_LABELS.get(reference_type, "基础资料")),
        )

    @staticmethod
    def _with_page(payload: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        """注入分页参数：页码从 1 起，页大小收敛到 1-100。"""
        payload["pageNum"] = max(1, int(page or 1))
        payload["pageSize"] = max(1, min(int(page_size or 20), 100))
        return payload

    def _paged_data_response(self, data: Any) -> dict[str, Any]:
        """归一化分页查询结果：IPage 结构提取行与页元数据，其余原样透传。"""
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            rows = data["list"]
            page_num = self._coerce_positive_int(data.get("pageNum"), 1)
            page_size = self._coerce_positive_int(data.get("pageSize"), len(rows))
            total = self._coerce_non_negative_int(data.get("total"), len(rows))
            return self.ok_response(
                data=rows,
                page=page_num,
                page_size=page_size,
                total=total,
                has_more=page_num * page_size < total,
            )
        return self.ok_response(data=data)

    @staticmethod
    def _coerce_positive_int(value: Any, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    @staticmethod
    def _coerce_non_negative_int(value: Any, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number >= 0 else default

    async def _search_reference(
        self,
        reference_type: str,
        keyword: str,
        limit: int,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        context = self._contexts.get()
        methods = {
            "customer": "search_customers",
            "warehouse": "search_warehouses",
            "handler": "search_staff",
            "supplier": "search_suppliers",
            "settlement_account": "search_settlement_accounts",
        }
        if reference_type not in methods:
            raise DomainError(
                "erp_reference_type_invalid",
                "reference_type 必须是 customer、warehouse、handler、supplier 或 settlement_account",
            )
        snapshot = await getattr(self._api, methods[reference_type])(context, keyword, limit, page)
        self.session.remember_references(reference_type, snapshot.options)
        return snapshot

    async def _resolve_reference(self, reference_type: str, value: str) -> dict[str, Any]:
        if not value:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": [],
            }
        known = self.session.reference_by_id(reference_type, value)
        if known is not None:
            return {"status": "matched", "query": value, "selected": known, "candidates": []}
        # 解析时多取一些结果保证精确项不因展示上限被截断，最终候选仍收敛为 5 个。
        options = list((await self._search_reference(reference_type, value, 10)).options)
        normalized = normalize_name(value)
        options = self._deduplicate_reference_options(options, value)
        exact = [
            option
            for option in options
            if normalized
            in {
                normalize_name(str(option.get("id") or "")),
                normalize_name(str(option.get("code") or "")),
                normalize_name(str(option.get("name") or "")),
            }
        ]
        if len(exact) == 1:
            return {
                "status": "matched",
                "query": value,
                "selected": exact[0],
                "candidates": [],
            }
        # 无唯一精确匹配时最多展示 5 个，保留每个不同 ID 的实体
        if options:
            options = options[:5]
        status = "ambiguous" if options else "unmatched"
        return {
            "status": status,
            "query": value,
            "selected": None,
            "candidates": options,
        }

    async def _resolve_update_reference(
        self,
        reference_type: str,
        value: str,
        label: str,
    ) -> str:
        """把修改单据的基础资料参数解析为内部 ID。

        纯数字直接视为内部 ID 透传（与销售单标识的既有约定一致）；
        其余按名称或编号解析，未唯一匹配时抛出结构化错误，由模型
        引导用户提供更准确的名称，而不是把错误标识写入 ERP。
        """
        if value.isdigit() or self.session.reference_by_id(reference_type, value) is not None:
            return value
        resolution = await self._resolve_reference(reference_type, value)
        if resolution["status"] == "matched" and resolution["selected"] is not None:
            return str(resolution["selected"]["id"])
        candidate_names = "、".join(
            str(option.get("name") or "")
            for option in resolution["candidates"][:5]
            if option.get("name")
        )
        if candidate_names:
            raise DomainError(
                "erp_update_reference_ambiguous",
                "%s“%s”匹配到多个候选：%s；请提供更准确的名称或内部 ID"
                % (label, value, candidate_names),
            )
        raise DomainError(
            "erp_update_reference_unmatched",
            "未找到与“%s”匹配的%s；请提供准确的名称或内部 ID" % (value, label),
        )

    @classmethod
    def _deduplicate_reference_options(
        cls,
        options: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """仅合并相同 ID 的重复记录，不猜测名称相似的实体相同。"""
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for option in sorted(options, key=lambda item: cls._reference_sort_key(item, query)):
            identifier = str(option.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            deduped.append(option)
        return deduped

    async def _match_order_products(
        self,
        order_text: str,
        source: str,
        confirmed_products: list[dict[str, str]] | None,
    ) -> BillingDraft | None:
        if not order_text:
            return None
        context = self._contexts.get()
        await self.session.ensure_catalog(self._catalog_loader(context))
        return self.session.create_draft_from_text(
            order_text,
            source=source,
            confirmed_products=confirmed_products,
        )

    @staticmethod
    def _validate_order_fields(
        *,
        order_date: str,
        remark: str,
        save_type: str,
        source: str,
    ) -> None:
        if order_date:
            try:
                parsed = date.fromisoformat(order_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed.isoformat() != order_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                )
        if len(remark.strip()) > 200:
            raise DomainError(
                "erp_sales_order_remark_too_long",
                "备注最多 200 个字符",
            )
        if save_type not in _SAVE_TYPE_CODES:
            raise DomainError(
                "erp_sales_order_save_type_invalid",
                "save_type 必须是 draft、pre_receipt 或 final",
            )
        if source not in {"text", "voice", "image"}:
            raise DomainError(
                "erp_order_source_invalid",
                "source 必须是 text、voice 或 image",
            )

    @staticmethod
    def _validate_order_date(order_date: str) -> None:
        """校验录单日期为合法的 YYYY-MM-DD 格式。"""
        try:
            parsed = date.fromisoformat(order_date)
        except ValueError as exc:
            raise DomainError(
                "erp_sales_order_date_invalid",
                "录单日期必须使用 YYYY-MM-DD 格式",
            ) from exc
        if parsed.isoformat() != order_date:
            raise DomainError(
                "erp_sales_order_date_invalid",
                "录单日期必须使用 YYYY-MM-DD 格式",
            )

    @staticmethod
    def _validate_date_range(start_date: str, end_date: str) -> None:
        """校验查询日期范围格式和先后顺序。"""
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

    @staticmethod
    def _build_modify_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """清洗修改商品明细，确保每行包含必填的 product_id 和 quantity。"""
        if not items:
            raise DomainError(
                "erp_sales_order_items_empty",
                "商品明细不能为空",
            )
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细不是 JSON 对象" % index,
                )
            product_id = str(raw.get("product_id") or raw.get("productId") or "").strip()
            if not product_id:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细缺少 product_id" % index,
                )
            quantity = raw.get("quantity")
            if quantity is None:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细缺少 quantity" % index,
                )
            try:
                qty = float(quantity)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细数量不是数字" % index,
                ) from exc
            if not math.isfinite(qty) or qty < 0.0001:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细数量必须为不小于 0.0001 的有限数" % index,
                )
            item: dict[str, Any] = {"productId": product_id, "quantity": qty}
            field_map = {
                "unit": "unit",
                "unitPrice": "unit_price",
                "unitId": "unit_id",
                "conversionRate": "conversion_rate",
                "remark": "remark",
                "orderItemId": "order_item_id",
            }
            for camel_key, snake_key in field_map.items():
                value = raw.get(snake_key)
                if value is None:
                    value = raw.get(camel_key)
                if value not in (None, ""):
                    if camel_key == "conversionRate":
                        value = non_negative_amount(value, "换算率")
                        if value <= 0:
                            raise DomainError("erp_sales_order_item_invalid", "换算率必须大于 0")
                    if camel_key == "unitPrice":
                        value = non_negative_amount(value, "单价")
                    item[camel_key] = value
            result.append(item)
        return result

    @staticmethod
    def _apply_confirmed_units(
        draft: BillingDraft | None,
        confirmations: list[dict[str, Any]] | None,
    ) -> None:
        """按行绑定商品、单位和数量，拒绝错行确认及隐式单位换算。"""
        if confirmations is None:
            return
        if not isinstance(confirmations, list):
            raise DomainError("erp_unit_confirmation_invalid", "confirmed_units 必须是数组")
        lines = {line.order_line.line_id: line for line in draft.lines} if draft else {}
        seen: set[str] = set()
        for entry in confirmations:
            if not isinstance(entry, dict):
                raise DomainError("erp_unit_confirmation_invalid", "单位确认必须是对象")
            line_id = entry.get("line_id")
            if not isinstance(line_id, str) or line_id in seen or line_id not in lines:
                raise DomainError("erp_unit_confirmation_invalid", "单位确认行不存在或重复")
            seen.add(line_id)
            line = lines[line_id]
            if (line.product is None or line.status != "matched"
                    or entry.get("product_id") != line.product.product_id
                    or not line.product.unit
                    or not isinstance(entry.get("unit"), str)
                    or normalized_unit(entry["unit"]) != normalized_unit(line.product.unit)):
                raise DomainError("erp_unit_confirmation_invalid", "请使用该行已匹配商品的 ERP 单位确认")
            quantity = entry.get("quantity")
            if (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                    or not math.isfinite(quantity) or quantity < 0.0001):
                raise DomainError("erp_unit_confirmation_invalid", "确认数量必须为不小于 0.0001 的有限数")
            line.order_line = replace(
                line.order_line, quantity=float(quantity), unit=line.product.unit,
            )

    @staticmethod
    def _unit_warnings(draft: BillingDraft | None) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if draft is None:
            return warnings
        for line in draft.lines:
            if line.status != "matched" or line.product is None:
                continue
            requested_unit = normalized_unit(line.order_line.unit)
            product_unit = normalized_unit(line.product.unit)
            if requested_unit and product_unit and requested_unit != product_unit:
                warnings.append(
                    {
                        "line_id": line.order_line.line_id,
                        "product": line.product.name,
                        "product_id": line.product.product_id,
                        "requested_quantity": line.order_line.quantity,
                        "requested_unit": line.order_line.unit,
                        "erp_unit": line.product.unit,
                        "prompt": "请按 ERP 单位确认数量，并通过 confirmed_units 回传；保留原 order_text，工具不会猜测换算。",
                    },
                )
        return warnings

    @staticmethod
    def _required_actions(
        *,
        missing: list[dict[str, Any]],
        reference_resolutions: dict[str, dict[str, Any]],
        product_payload: dict[str, Any],
        unit_warnings: list[dict[str, Any]],
        ready: bool,
    ) -> list[str]:
        """把分散的校验结果收敛为 Agent 可顺序执行的业务待办。"""
        if ready:
            return ["confirm_submit"]

        actions = ["provide_%s" % item["field"] for item in missing]
        for field, resolution in reference_resolutions.items():
            if resolution["status"] == "ambiguous":
                actions.append("select_%s" % field)
            elif resolution["status"] == "unmatched":
                actions.append("replace_%s" % field)
        if product_payload["recommended_products"]:
            actions.append("select_products")
        if product_payload["unmatched_products"]:
            actions.append("resolve_unmatched_products")
        if unit_warnings:
            actions.append("confirm_units")
        return actions

    @staticmethod
    def _build_sales_order_preview(
        *,
        draft: BillingDraft,
        references: dict[str, dict[str, Any]],
        order_date: str,
        remark: str,
        save_type: str,
        partial: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        customer = references["customer"]["selected"]
        warehouse = references["warehouse"]["selected"]
        handler = references["handler"]["selected"]
        items: list[dict[str, Any]] = []
        preview_items: list[dict[str, Any]] = []
        total_amount = Decimal("0")
        has_complete_amount = True
        for line in draft.lines:
            if line.product is None:
                if partial:
                    continue
                raise DomainError(
                    "erp_sales_order_product_unconfirmed",
                    "销售单仍有未确认商品，不能生成提交预览",
                )
            if not math.isfinite(line.order_line.quantity) or line.order_line.quantity < 0.0001:
                raise DomainError("erp_sales_order_item_invalid", "商品数量必须为不小于 0.0001 的有限数")
            if line.product.price is not None:
                non_negative_amount(line.product.price, "单价")
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.product.price is not None:
                item["unitPrice"] = line.product.price
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            preview_item = {
                "name": line.product.name,
                "quantity": line.order_line.quantity,
                "unit": line.product.unit or line.order_line.unit,
                "unit_price": line.product.price,
            }
            line_total = line_amount(
                line.order_line.quantity,
                line.product.price,
            )
            if line_total is None:
                has_complete_amount = False
            else:
                preview_item["line_amount"] = float(line_total)
                total_amount += line_total
            preview_items.append(preview_item)
        payload = {
            "id": 0,
            "orderDate": order_date,
            "customerId": customer["id"],
            "warehouseId": warehouse["id"],
            "handlerId": handler["id"],
            "saveType": _SAVE_TYPE_CODES[save_type],
            "remark": remark,
            "items": items,
        }
        # 预览面向用户展示，基础资料只出现名称，不出现内部 ID
        preview = {
            "order_date": order_date,
            "customer": str(customer.get("name") or ""),
            "warehouse": str(warehouse.get("name") or ""),
            "handler": str(handler.get("name") or ""),
            "remark": remark,
            "save_type": save_type,
            "save_type_label": _SAVE_TYPE_LABELS[save_type],
            "items": preview_items,
        }
        if preview_items and has_complete_amount:
            preview["total_amount"] = float(total_amount)
        return payload, preview
