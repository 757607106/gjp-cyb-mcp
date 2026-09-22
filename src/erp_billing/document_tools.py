"""采购、退货、库存写单据与资金单据的两段式工具。

所有写单据沿用 preview → submit 确认模式：预览生成不可变 payload 并
返回 preview_id，用户明确确认后 submit 才写入真实 ERP；统一 saveType=2
保存过账，不暴露草稿态。实现为 BillingToolSet 的混入类，与销售单工具
共用 session、API 端口、基础资料解析与通用提交逻辑。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

from gjp_common.context import InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool

from .catalog import normalize_name
from .ports import BillingApiPort
from .session import ErpBillingSession
from .validation import line_amount, non_negative_amount, positive_amount

_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# 可空字段的统一写法：anyOf 单类型分支。type 数组形式（如
# ["object", "null"]）是合法 JSON Schema，但部分 MCP 客户端只读取
# 单字符串 type，会拒绝工具或丢弃约束。
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_OBJECT = {"anyOf": [{"type": "object"}, {"type": "null"}]}
_NULLABLE_LOOSE_OBJECT = {
    "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]
}

# ---------------------------------------------------------------------------
# 输出 schema：顶层字段声明类型，嵌套对象保持宽松（additionalProperties=True）
# ---------------------------------------------------------------------------

_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA = {
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
        "price_warnings": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "required_actions": {"type": "array", "items": {"type": "string"}},
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

_RETURN_PREVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "source_order": _NULLABLE_LOOSE_OBJECT,
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "required_actions": {"type": "array", "items": {"type": "string"}},
        "ready_to_submit": {"type": "boolean"},
        "preview_id": _NULLABLE_STRING,
        "preview": _NULLABLE_OBJECT,
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_MONEY_PREVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "order": _NULLABLE_LOOSE_OBJECT,
        "reference_resolutions": {"type": "object", "additionalProperties": True},
        "required_actions": {"type": "array", "items": {"type": "string"}},
        "ready_to_submit": {"type": "boolean"},
        "preview_id": _NULLABLE_STRING,
        "preview": _NULLABLE_OBJECT,
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_SUBMIT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "submitted": {"type": "boolean"},
        "document_id": {"type": "string"},
        "document_no": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_GET_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "document": _NULLABLE_LOOSE_OBJECT,
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_LIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "documents": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_VOID_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "voided": {"type": "boolean"},
        "document_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_UPDATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "modified": {"type": "boolean"},
        "document_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

# ---------------------------------------------------------------------------
# 输入 schema：为枚举和范围参数补充 JSON Schema 约束
# ---------------------------------------------------------------------------

_CONFIRMED_PRODUCTS_INPUT = {
    "type": "array",
    "description": "用户从预览或 searchProducts 候选中选定的商品，绑定当前完整商品文本的行后重新预览；不是提交确认。",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string", "description": "当前同类预览返回的商品行 line_id（如 L001），不是 ERP 单据明细 ID；不得自造或错行。"},
            "product_id": {"type": "string", "description": "用户选定的 ERP 商品内部 ID，取自预览商品候选或 searchProducts；不是商品名称或编号。"},
        },
        "required": ["line_id", "product_id"],
    },
}

_CONFIRMED_UNITS_INPUT = {
    "type": "array",
    "description": "针对预览 unit_warnings，由用户按该行已匹配商品的 ERP 单位确认数量后重新预览；不得自行换算。",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string", "description": "当前预览中待确认单位的商品行 line_id，必须存在且不能重复；不是 ERP 明细 ID。"},
            "product_id": {"type": "string", "description": "该行预览已匹配的 ERP 商品内部 ID，必须与 line_id 对应商品一致。"},
            "unit": {"type": "string", "description": "该商品预览或 searchProducts 返回的 ERP 单位，必须与已匹配商品单位一致，不能自造包装单位。"},
            "quantity": {"type": "number", "minimum": 0.0001, "description": "用户按上述 ERP 单位明确确认的数量，至少 0.0001；覆盖该行数量，不做隐式单位换算。"},
        },
        "required": ["line_id", "product_id", "unit", "quantity"],
        "additionalProperties": False,
    },
}

_CONFIRMED_PRICES_INPUT = {
    "type": "array",
    "description": "用户确认的采购单价，覆盖目录最近采购价；根据 previewPurchaseOrder 的 price_warnings 补价后重新预览。",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string", "description": "previewPurchaseOrder 返回的当前商品行 line_id，必须存在且不能重复；不是 ERP 明细 ID。"},
            "product_id": {"type": "string", "description": "该采购行预览已匹配的 ERP 商品内部 ID，须与 line_id 对应商品一致。"},
            "unit_price": {"type": "number", "exclusiveMinimum": 0, "description": "用户明确提供或确认的该商品 ERP 单位采购单价，必须大于 0；不能用销售价代替或自行估价。"},
        },
        "required": ["line_id", "product_id", "unit_price"],
        "additionalProperties": False,
    },
}

_RETURN_ITEMS_INPUT = {
    "type": "array",
    "description": "从原销售单或原采购单选择退货商品；省略明细时使用快捷退货预填的全部商品及可退数量。",
    "items": {
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "原销售/采购单明细或对应退货预览 items 返回的 ERP 商品内部 ID，必须在源单可退明细中；不是退货单 ID。"},
            "quantity": {"type": "number", "minimum": 0.0001, "description": "用户指定的本次退货数量，沿用源单单位，须大于 0 且不超过快捷退货预填的可退数量（已扣历史退货）。"},
            "unit_price": {"type": "number", "minimum": 0, "description": "可选的本次退货单价，用户确认后覆盖源单单价；省略沿用快捷退货预填单价，允许为 0。"},
        },
        "required": ["product_id", "quantity"],
        "additionalProperties": False,
    },
}

_WRITEOFF_DETAILS_INPUT = {
    "type": "array",
    "description": "独立收付款单的业务单据核销分配，可列多单；先查询对应单据核实往来单位和金额，不是商品明细。",
    "items": {
        "type": "object",
        "properties": {
            "biz_type": {
                "type": "string",
                "enum": ["sales_order", "purchase_order", "sales_return", "purchase_return"],
                "description": "被核销单据类型：sales_order=销售单、purchase_order=采购单、sales_return=销售退货单、purchase_return=采购退货单；收款通常核销销售单/采购退货，付款通常核销采购单/销售退货。",
            },
            "biz_id": {"type": "string", "description": "与 biz_type 对应的业务单据内部 ID，取自 getSalesOrder、getPurchaseOrder、getSalesReturn 或 getPurchaseReturn 返回详情；不是业务单号、商品 ID、源单 ID 或收付款单 ID，不做单号解析。"},
            "writeoff_amount": {"type": "number", "minimum": 0, "description": "用户指定分配到该业务单据的核销金额；实现要求大于 0，所有行合计不能超过本次收/付款总额（不含优惠）。"},
        },
        "required": ["biz_type", "biz_id", "writeoff_amount"],
        "additionalProperties": False,
    },
}

_PREVIEW_PURCHASE_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {"type": "string", "description": "向供应商采购入库的完整商品文本，须含商品和数量；多轮修改仍传完整明细，商品候选用 searchProducts 查询。"},
        "supplier": {"type": "string", "description": "采购供应商，业务必填；名称、编号或 searchBillingReferences 返回的供应商 ID。"},
        "warehouse": {"type": "string", "description": "采购入库仓库，业务必填；名称、编号或 searchBillingReferences 返回的仓库 ID。"},
        "handler": {"type": "string", "description": "采购经手人，业务必填；名称、编号或 searchBillingReferences 返回的经手人 ID。"},
        "order_date": {"type": "string", "description": "采购录单日期 YYYY-MM-DD，业务必填，不默认当天。"},
        "remark": {"type": "string", "description": "可选的采购整单备注，最多 200 个字符。"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "confirmed_prices": _CONFIRMED_PRICES_INPUT,
        "partial": {"type": "boolean", "default": False, "description": "默认要求全部商品就绪；仅在用户同意跳过未匹配行时传 true，生成仅含已匹配商品的预览，不直接提交。"},
    },
}

_LIST_PURCHASE_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "采购单列表页码，从 1 开始；has_more 为 true 时可继续翻页。"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页采购单数量，1 到 100，默认 20；不是统计范围。"},
        "start_date": {"type": "string", "description": "采购单筛选开始日期 YYYY-MM-DD；留空不限制起始日期。"},
        "end_date": {"type": "string", "description": "采购单筛选结束日期 YYYY-MM-DD，不早于开始日期；留空不限制结束日期。"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3], "description": "采购单状态：0=草稿、1=预付、2=已生效、3=已作废；省略不按状态筛选。"},
        "payment_status": {"type": "integer", "enum": [0, 1, 2], "description": "采购付款状态：0=未付款、1=部分付款、2=已完成；省略不筛选。"},
        "return_status": {"type": "integer", "enum": [0, 1, 2], "description": "采购退货状态：0=无退货、1=部分退货、2=全部退货；省略不筛选。"},
        "order_no": {"type": "string", "description": "采购业务单号的模糊匹配关键词，不是内部 ID；留空不按单号筛选。"},
        "supplier_id": {"type": "string", "description": "供应商内部 ID 或名称，候选通过 searchBillingReferences 查询；留空查询所有供应商。"},
    },
}

_UPDATE_PURCHASE_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "先经 getPurchaseOrder 核实的采购单内部 ID 或业务单号 orderNo，不是采购退货单 ID。"},
        "order_date": {"type": "string", "description": "修改后的采购录单日期 YYYY-MM-DD；省略保留原值，传入时不能为空。"},
        "handler_id": {"type": "string", "description": "新经手人的内部 ID 或名称，取自 searchBillingReferences；省略保留，传入时不能为空。"},
        "supplier_id": {"type": "string", "description": "新供应商的内部 ID 或名称，取自 searchBillingReferences；留空保留原供应商。"},
        "warehouse_id": {"type": "string", "description": "新入库仓库的内部 ID 或名称，取自 searchBillingReferences；留空保留原仓库。"},
        "items": {
            "type": "array",
            "description": "修改后的完整采购明细，传入即整体替换而非增量更新，不能为空数组；基于 getPurchaseOrder 保留未改行，省略保留全部原明细。",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "ERP 商品内部 ID，原行取自 getPurchaseOrder，新商品通过 searchProducts 确认；不是商品名称或单据 ID。"},
                    "quantity": {"type": "number", "description": "修改后该行采购数量，按该行单位计，必须大于 0；不是本次增减数量。"},
                    "unit": {"type": "string", "description": "该行采购单位，取自 getPurchaseOrder 或 searchProducts 的商品单位并由用户核实；不自动换算。"},
                    "unit_price": {"type": "number", "description": "该行采购单价，必填；保留原价或传用户确认的新价。此修改接口校验非负数，与新建采购必须正价不同。"},
                    "order_item_id": {"type": "string", "description": "可选的原采购明细行标识，保留原行时取自 getPurchaseOrder 对应明细；不是商品 ID 或预览 line_id，不得自造。"},
                    "remark": {"type": "string", "description": "该商品行备注，不是整单备注；保留原行时从 getPurchaseOrder 对应明细取值。"},
                },
                "required": ["product_id", "quantity", "unit_price"],
            },
        },
        "remark": {"type": "string", "description": "修改后的整单备注，最多 200 个字符；省略保留，空字符串清空。"},
        "discount_amount": {"type": "number", "description": "修改后的采购单优惠金额，非负，允许 0；省略保留原值。"},
        "discount_account_id": {"type": "string", "description": "优惠承担结算账户的内部 ID，取自 searchBillingReferences；直接传 ID，不解析名称，留空不修改。"},
        "payment_amount": {"type": "number", "description": "修改后的采购单付款金额，非负，允许 0；省略保留。仅追加一笔付款请用 previewPurchasePayment。"},
        "payment_account_id": {"type": "string", "description": "付款结算账户内部 ID，取自 searchBillingReferences；直接传 ID，不解析名称，留空不修改。"},
        "confirmed_by_user": {"type": "boolean", "default": False, "description": "仅在 getPurchaseOrder 核实原单且用户明确确认本次修改内容后传 true；未确认不调用，不得代用户自造确认。"},
    },
    "required": ["order_id"],
}

_PREVIEW_PURCHASE_RETURN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "原采购单内部 ID 或业务单号 orderNo，取自 listPurchaseOrders/getPurchaseOrder；不是采购退货单 ID，也不是销售单。"},
        "items": {
            "type": "array",
            "description": "退给供应商的商品及数量，每行含 product_id、quantity，可选 unit_price；省略按快捷退货预填的全部可退明细退货，传入时不能为空数组。",
            "items": _RETURN_ITEMS_INPUT["items"],
        },
        "refund_amount": {"type": "number", "minimum": 0, "description": "退货时向供应商收回的退款金额，非负；省略或 0 不随退货收款。与优惠同时提供时合计不得超过退货总额。"},
        "refund_account_id": {"type": "string", "description": "接收供应商退款的结算账户名称、编号或 ID，候选用 searchBillingReferences；退款大于 0 时必填并须唯一匹配。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次采购退货优惠/减免金额，非负；省略或 0 不附加优惠。与退款同时提供时合计不得超过退货总额。"},
        "discount_account_id": {"type": "string", "description": "优惠承担结算账户名称、编号或 ID，候选用 searchBillingReferences；优惠大于 0 时必填并须唯一匹配。"},
        "return_date": {"type": "string", "description": "采购退货日期 YYYY-MM-DD；省略用原单快捷退货返回的 returnDate，无该值时用当天。"},
        "remark": {"type": "string", "description": "可选的本次采购退货整单备注。"},
    },
    "required": ["order_id"],
}

_LIST_PURCHASE_RETURNS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "采购退货单列表页码，从 1 开始；has_more 为 true 时可继续翻页。"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页采购退货单数量，1 到 100，默认 20。"},
        "start_date": {"type": "string", "description": "采购退货单筛选开始日期 YYYY-MM-DD；留空不限制起始日期。"},
        "end_date": {"type": "string", "description": "采购退货单筛选结束日期 YYYY-MM-DD，不早于开始日期；留空不限制结束日期。"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3], "description": "退货单状态筛选，0=草稿、2=已生效、3=已作废；省略不筛选，其余状态含义以 ERP 为准。"},
        "return_no": {"type": "string", "description": "采购退货业务单号的模糊匹配关键词，不是原采购单号；留空不筛选。"},
        "supplier_id": {"type": "string", "description": "供应商内部 ID 或名称，候选通过 searchBillingReferences 查询；留空查询所有供应商。"},
    },
}

_PREVIEW_SALES_RETURN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "原销售单内部 ID 或业务单号 orderNo，取自 listSalesOrders/getSalesOrder；不是销售退货单 ID，也不是采购单。"},
        "items": {
            "type": "array",
            "description": "客户退回的商品及数量，每行含 product_id、quantity，可选 unit_price；省略按快捷退货预填的全部可退明细退货，传入时不能为空数组。",
            "items": _RETURN_ITEMS_INPUT["items"],
        },
        "refund_amount": {"type": "number", "minimum": 0, "description": "退货时支付给客户的退款金额，非负；省略或 0 不随退货退款。与折让同时提供时合计不得超过退货总额。"},
        "refund_account_id": {"type": "string", "description": "向客户退款的结算账户名称、编号或 ID，候选用 searchBillingReferences；退款大于 0 时必填并须唯一匹配。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次销售退货折让金额，非负；省略或 0 不附加折让。与退款同时提供时合计不得超过退货总额。"},
        "discount_account_id": {"type": "string", "description": "折让承担结算账户名称、编号或 ID，候选用 searchBillingReferences；折让大于 0 时必填并须唯一匹配。"},
        "return_date": {"type": "string", "description": "销售退货日期 YYYY-MM-DD；省略用原单快捷退货返回的 returnDate，无该值时用当天。"},
        "remark": {"type": "string", "description": "可选的本次销售退货整单备注。"},
    },
    "required": ["order_id"],
}

_LIST_SALES_RETURNS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "销售退货单列表页码，从 1 开始；has_more 为 true 时可继续翻页。"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页销售退货单数量，1 到 100，默认 20。"},
        "start_date": {"type": "string", "description": "销售退货单筛选开始日期 YYYY-MM-DD；留空不限制起始日期。"},
        "end_date": {"type": "string", "description": "销售退货单筛选结束日期 YYYY-MM-DD，不早于开始日期；留空不限制结束日期。"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3], "description": "退货单状态筛选，0=草稿、2=已生效、3=已作废；省略不筛选，其余状态含义以 ERP 为准。"},
        "refund_status": {"type": "integer", "enum": [0, 1, 2], "description": "向客户退款的状态：0=未退款、1=部分退款、2=已完成；省略不筛选。"},
        "return_no": {"type": "string", "description": "销售退货业务单号的模糊匹配关键词，不是原销售单号；留空不筛选。"},
        "customer_id": {"type": "string", "description": "客户内部 ID 或名称，候选通过 searchBillingReferences 查询；留空查询所有客户。"},
    },
}

_PREVIEW_SALES_RECEIPT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "本次继续收款的单笔销售单内部 ID 或 orderNo，取自 listSalesOrders/getSalesOrder；不是独立收款单或销售退货单 ID。"},
        "receipt_amount": {"type": "number", "minimum": 0, "description": "本次追加的收款金额，非负，不是累计已收金额；可为 0 但与免账不能同时为 0，合计不得超过该单未收金额。"},
        "receipt_account": {"type": "string", "description": "收款结算账户名称、编号或 searchBillingReferences 返回的 ID；即使收款为 0、仅免账也须提供并匹配。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次免账金额（优惠/折让），非负，省略按 0；与收款合计须大于 0 且不超过该单未收金额。"},
        "discount_account": {"type": "string", "description": "免账承担结算账户名称、编号或 searchBillingReferences 返回的 ID；免账大于 0 时必填并须匹配。"},
    },
    "required": ["order_id", "receipt_amount", "receipt_account"],
}

_PREVIEW_PURCHASE_PAYMENT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "本次继续付款的单笔采购单内部 ID 或 orderNo，取自 listPurchaseOrders/getPurchaseOrder；不是独立付款单或采购退货单 ID。"},
        "payment_amount": {"type": "number", "minimum": 0, "description": "本次追加的付款金额，非负，不是累计已付金额；可为 0 但与免账不能同时为 0，合计不得超过该单未付金额。"},
        "payment_account": {"type": "string", "description": "付款结算账户名称、编号或 searchBillingReferences 返回的 ID；即使付款为 0、仅免账也须提供并匹配。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次免账金额（优惠/折让），非负，省略按 0；与付款合计须大于 0 且不超过该单未付金额。"},
        "discount_account": {"type": "string", "description": "免账承担结算账户名称、编号或 searchBillingReferences 返回的 ID；免账大于 0 时必填并须匹配。"},
    },
    "required": ["order_id", "payment_amount", "payment_account"],
}

_PREVIEW_STOCK_TRANSFER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {"type": "string", "description": "仓库间调拨的完整商品文本，须含商品和数量；修改后传完整明细，商品候选用 searchProducts 查询。"},
        "from_warehouse": {"type": "string", "description": "必填，调出仓库名称、编号或 searchBillingReferences 返回的仓库 ID，须与调入仓库不同。"},
        "to_warehouse": {"type": "string", "description": "必填，调入仓库名称、编号或 searchBillingReferences 返回的仓库 ID，须与调出仓库不同。"},
        "handler": {"type": "string", "description": "必填，调拨经手人名称、编号或 searchBillingReferences 返回的经手人 ID。"},
        "transfer_date": {"type": "string", "description": "调拨日期 YYYY-MM-DD；省略默认当天。"},
        "remark": {"type": "string", "description": "可选的调拨整单备注，最多 200 个字符。"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "partial": {"type": "boolean", "default": False, "description": "默认要求全部商品就绪；仅在用户同意跳过未匹配行时传 true，生成部分调拨预览，不直接提交。"},
    },
}

_PREVIEW_OTHER_STOCK_DOC_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["inbound", "outbound"], "description": "单仓库存变动方向：inbound=其他入库（如报溢），outbound=其他出库（如报损）；不是仓库间调拨。"},
        "order_text": {"type": "string", "description": "其他出入库的完整商品文本，须含商品和数量；修改后传完整明细，商品候选用 searchProducts 查询。"},
        "warehouse": {"type": "string", "description": "必填，发生其他出入库的仓库名称、编号或 searchBillingReferences 返回的仓库 ID。"},
        "handler": {"type": "string", "description": "必填，单据经手人名称、编号或 searchBillingReferences 返回的经手人 ID。"},
        "doc_type": {"type": "string", "description": "必填，与 kind 同方向的其他入库/出库类型 ID 或名称；先用 listStockDocTypes 查询，如报损、报溢，不得自造类型。"},
        "doc_date": {"type": "string", "description": "其他出入库单据日期 YYYY-MM-DD；省略默认当天。"},
        "remark": {"type": "string", "description": "可选的其他出入库整单备注，最多 200 个字符。"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "partial": {"type": "boolean", "default": False, "description": "默认要求全部商品就绪；仅在用户同意跳过未匹配行时传 true，生成部分出入库预览，不直接提交。"},
    },
    "required": ["kind"],
}

_PREVIEW_RECEIPT_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "receipt_amount": {"type": "number", "minimum": 0.01, "description": "本次独立收款总额，至少 0.01；不能用 0 金额仅免账，所有核销行合计不得超过此金额。"},
        "account": {"type": "string", "description": "必填，收款结算账户名称、编号或 searchBillingReferences 返回的账户 ID。"},
        "handler": {"type": "string", "description": "必填，收款经手人名称、编号或 searchBillingReferences 返回的经手人 ID。"},
        "customer": {"type": "string", "description": "收款往来客户名称或 searchBillingReferences 返回的 ID；核销销售类单据时必填并须匹配，与 supplier 只能提供其一。"},
        "supplier": {"type": "string", "description": "收款往来供应商名称或 searchBillingReferences 返回的 ID；核销采购类单据（如采购退货）时必填并须匹配，与 customer 只能提供其一。"},
        "order_date": {"type": "string", "description": "独立收款单日期 YYYY-MM-DD；省略默认当天。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次收款优惠（抹零）金额，非负；省略或 0 不附加优惠，不增加可核销总额。"},
        "discount_account": {"type": "string", "description": "免账承担结算账户名称、编号或 searchBillingReferences 返回的 ID；优惠大于 0 时必填并须匹配。"},
        "fund_type": {
            "type": "string",
            "description": "收款款项类型名称、编号或 ID；省略时按销售单/采购退货核销推断系统类型，无法推断则从本预览 reference_resolutions.fund_type.candidates 选择后重试。无单独款项类型工具；无核销收款须选适用的自定义类型。",
        },
        "writeoff_details": {
            "type": "array",
            "description": "独立收款核销分配，可含多单；通常核销销售单或采购退货单，每行含 biz_type、对应单据内部 biz_id 和正数 writeoff_amount。核实往来单位，合计不超过收款总额；省略或空数组只收款不核销。",
            "items": _WRITEOFF_DETAILS_INPUT["items"],
        },
        "remark": {"type": "string", "description": "可选的独立收款单备注，最多 200 个字符。"},
    },
    "required": ["receipt_amount", "account", "handler"],
}

_PREVIEW_PAYMENT_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "payment_amount": {"type": "number", "minimum": 0.01, "description": "本次独立付款总额，至少 0.01；不能用 0 金额仅免账，所有核销行合计不得超过此金额。"},
        "account": {"type": "string", "description": "必填，付款结算账户名称、编号或 searchBillingReferences 返回的账户 ID。"},
        "handler": {"type": "string", "description": "必填，付款经手人名称、编号或 searchBillingReferences 返回的经手人 ID。"},
        "supplier": {"type": "string", "description": "付款往来供应商名称或 searchBillingReferences 返回的 ID；核销采购类单据时必填并须匹配，与 customer 只能提供其一。"},
        "customer": {"type": "string", "description": "付款往来客户名称或 searchBillingReferences 返回的 ID；核销销售类单据（如销售退货）时必填并须匹配，与 supplier 只能提供其一。"},
        "order_date": {"type": "string", "description": "独立付款单日期 YYYY-MM-DD；省略默认当天。"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "本次付款优惠（抹零）金额，非负；省略或 0 不附加优惠，不增加可核销总额。"},
        "discount_account": {"type": "string", "description": "免账承担结算账户名称、编号或 searchBillingReferences 返回的 ID；优惠大于 0 时必填并须匹配。"},
        "fund_type": {
            "type": "string",
            "description": "付款款项类型名称、编号或 ID；省略时按采购单/销售退货核销推断系统类型，无法推断则从本预览 reference_resolutions.fund_type.candidates 选择后重试，无单独款项类型工具。",
        },
        "writeoff_details": {
            "type": "array",
            "description": "独立付款核销分配，可含多单；通常核销采购单或销售退货单，每行含 biz_type、对应单据内部 biz_id 和正数 writeoff_amount。核实往来单位，合计不超过付款总额；省略或空数组只付款不核销。",
            "items": _WRITEOFF_DETAILS_INPUT["items"],
        },
        "remark": {"type": "string", "description": "可选的独立付款单备注，最多 200 个字符。"},
    },
    "required": ["payment_amount", "account", "handler"],
}

_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "独立收款/付款单列表页码，从 1 开始；has_more 为 true 时可继续翻页。"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页单据数量，1 到 100，默认 20；不是统计范围。"},
        "start_date": {"type": "string", "description": "收付款单筛选开始日期 YYYY-MM-DD；留空不限制起始日期。"},
        "end_date": {"type": "string", "description": "收付款单筛选结束日期 YYYY-MM-DD，不早于开始日期；留空不限制结束日期。"},
        "status": {"type": "integer", "enum": [0, 2, 3], "description": "财务单据状态：0=草稿、2=已生效、3=已作废；省略不按状态筛选。"},
        "order_no": {"type": "string", "description": "独立收款/付款业务单号的模糊匹配关键词，不是被核销的销售/采购单号；留空不筛选。"},
        "counterparty_id": {"type": "string", "description": "往来客户或供应商的内部 ID、名称，候选用 searchBillingReferences；先尝试匹配客户再供应商，留空不筛选。"},
        "account_id": {"type": "string", "description": "结算账户内部 ID 或名称，候选用 searchBillingReferences；留空不按账户筛选。"},
    },
}

# 款项类型方向与核销场景推断的系统类型编码：
# ERP 收付款单 typeId 必填；系统类型按核销业务唯一确定。
_FUND_TYPE_DIRECTION_CODES = {"receipt": 1, "payment": 2}
_FUND_TYPE_AUTO_CODES = {
    ("receipt", "sales_order"): "sales",
    ("receipt", "purchase_return"): "purchase_refund",
    ("payment", "purchase_order"): "purchase",
    ("payment", "sales_return"): "sales_refund",
}


class DocumentTools:
    """采购单、退货、库存写单据与资金单据的两段式工具。

    以混入类并入 BillingToolSet：session、_api、_contexts、基础资料解析
    （_resolve_reference 等）与通用提交（_submit_prepared_document）
    由宿主 ToolSet 提供。
    """

    session: ErpBillingSession
    _api: BillingApiPort
    _contexts: InvocationContextStore

    # ------------------------------------------------------------------
    # 采购单
    # ------------------------------------------------------------------

    async def preview_purchase_order(
        self,
        order_text: str = "",
        supplier: str = "",
        warehouse: str = "",
        handler: str = "",
        order_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        confirmed_prices: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """预览向供应商采购入库的采购单，不用于向供应商退货或客户销售退货。

        采购退货用 previewPurchaseReturn，销售退货用 previewSalesReturn。
        基础资料用 searchBillingReferences，商品用 searchProducts 查候选。
        采购价默认取目录最近采购价；缺价或 0 价会返回 price_warnings，
        必须取得用户确认的正数采购价，通过 confirmed_prices 重新预览。
        本工具仅预览，无远端写入；根据 required_actions 补齐并重新调用，
        ready_to_submit=true 且取得 preview_id 后，用户明确确认该预览
        才能调用 submitPurchaseOrder；就绪不等于用户已确认。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "supplier": supplier.strip(),
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_date": order_date.strip(),
                "order_text": order_text.strip(),
            }
            if values["order_date"]:
                self._validate_order_date(values["order_date"])
            if len(remark.strip()) > 200:
                raise DomainError("erp_purchase_order_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("supplier", "供应商", "请问采购供应商是哪一位？"),
                    ("warehouse", "入库仓库", "请问入到哪个仓库？"),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_date", "录单日期", "请问录单日期是哪一天？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            draft = await self._match_order_products(
                values["order_text"], "text", confirmed_products,
            )
            self._apply_confirmed_units(draft, confirmed_units)
            prices = self._normalize_confirmed_prices(draft, confirmed_prices)
            product_payload = (
                draft.billing_products_payload()
                if draft is not None
                else {
                    "confirmed_products": [],
                    "recommended_products": [],
                    "unmatched_products": [],
                }
            )
            price_warnings = self._purchase_price_warnings(draft, prices)

            reference_resolutions = {
                "supplier": await self._resolve_reference("supplier", values["supplier"]),
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
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
                and not price_warnings
                and draft is not None
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                payload, preview = self._build_purchase_order_preview(
                    draft=draft,
                    references=reference_resolutions,
                    order_date=values["order_date"],
                    remark=remark.strip(),
                    prices=prices,
                    partial=partial,
                )
            return self._finalize_document_preview(
                kind="purchase_order",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=price_warnings,
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将已确认的采购入库预览写入 ERP 并保存过账，不提交采购退货。

        先调用 previewPurchaseOrder，使用最近一次就绪预览；用户明确确认
        后才能调用本工具，未确认不得调用或自造 confirmed_by_user=true。
        成功后可用 getPurchaseOrder 核对；结果未知先查询，不盲目重提。

        Args:
            preview_id: 最近一次 previewPurchaseOrder 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得自造、跨类型或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复开单。
            confirmed_by_user: 默认 false；仅在用户明确确认上述采购预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_purchase_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="purchase_order",
            doc_label="采购单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_purchase_order(self, order_id: str) -> dict[str, Any]:
        """只读查询一张采购单的商品明细、付款记录和状态，不是采购统计报表。

        不确定单据先用 listPurchaseOrders 定位，采购汇总分析用 queryPurchaseReport。
        核实详情后可用 previewPurchaseReturn 退给供应商，或 previewPurchasePayment
        继续付款；updatePurchaseOrder/voidPurchaseOrder 前须先本工具核实并取得用户确认。

        Args:
            order_id: 用户提供或 listPurchaseOrders 返回的采购单内部 ID 或业务单号 orderNo；
                不是采购退货单 ID，也不是付款单 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            result = await self._api.get_purchase_order_detail(context, order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_purchase_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        supplier_id: str = "",
    ) -> dict[str, Any]:
        """只读分页查找采购单，按日期、状态、供应商或单号定位业务单据。

        采购退货记录用 listPurchaseReturns，采购统计分析用 queryPurchaseReport，
        不要用当前页合计代替报表。选定单据后用 getPurchaseOrder 查看详情，
        再衔接采购退货、继续付款或经确认的修改/作废。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            supplier = await self._optional_reference_id("supplier", supplier_id)
            result = await self._api.search_purchase_orders(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                payment_status=payment_status,
                return_status=return_status,
                order_no=order_no.strip(),
                supplier_id=supplier,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_purchase_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废已存在的采购单并真实写入 ERP，不是向供应商退货。

        先用 getPurchaseOrder 核实内容和状态，用户明确确认作废后才能调用；
        未确认不调用，不得自造 confirmed_by_user=true。作废不可恢复，
        完成后可再用 getPurchaseOrder 核对；采购退货改用 previewPurchaseReturn。

        Args:
            order_id: 经 getPurchaseOrder 核实的采购单内部 ID 或业务单号 orderNo，
                不是采购退货单 ID。
            confirmed_by_user: 仅在用户明确确认作废该采购单后传 true，不得代用户确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "purchase_order",
                "采购单",
                order_id,
                confirmed_by_user,
                lambda resolved: self._api.void_purchase_order(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    async def update_purchase_order(
        self,
        order_id: str,
        order_date: str | None = None,
        handler_id: str | None = None,
        items: list[dict[str, Any]] | None = None,
        supplier_id: str = "",
        warehouse_id: str = "",
        remark: str | None = None,
        discount_amount: float | None = None,
        discount_account_id: str = "",
        payment_amount: float | None = None,
        payment_account_id: str = "",
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """修改已存在的采购单并真实写入 ERP，不新建采购单或采购退货单。

        先调用 getPurchaseOrder 核实原单和待改内容，取得用户明确确认后再调用；
        未确认不调用，不得自造 confirmed_by_user=true。只传需要修改的字段，
        省略字段保留 ERP 当前值；items 是完整替换，不是增量。完成后用
        getPurchaseOrder 核对；仅追加一笔付款用 previewPurchasePayment。"""
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
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            payload: dict[str, Any] = {"id": target_id}
            if order_date is not None:
                if not order_date.strip():
                    raise DomainError("erp_purchase_order_date_invalid", "录单日期不能为空")
                self._validate_order_date(order_date.strip())
                payload["orderDate"] = order_date.strip()
            if handler_id is not None:
                if not handler_id.strip():
                    raise DomainError("erp_purchase_order_handler_required", "经办人不能为空")
                payload["handlerId"] = await self._resolve_update_reference(
                    "handler", handler_id.strip(), "经手人",
                )
            if remark is not None:
                if len(remark.strip()) > 200:
                    raise DomainError("erp_purchase_order_remark_too_long", "备注最多 200 个字符")
                payload["remark"] = remark.strip()
            if items is not None:
                payload["items"] = self._build_purchase_modify_items(items)
            clean_supplier = supplier_id.strip()
            if clean_supplier:
                payload["supplierId"] = await self._resolve_update_reference(
                    "supplier", clean_supplier, "供应商",
                )
            clean_warehouse = warehouse_id.strip()
            if clean_warehouse:
                payload["warehouseId"] = await self._resolve_update_reference(
                    "warehouse", clean_warehouse, "入库仓库",
                )
            if discount_amount is not None:
                payload["discountAmount"] = non_negative_amount(discount_amount, "优惠金额")
            if discount_account_id.strip():
                payload["discountAccountId"] = discount_account_id.strip()
            if payment_amount is not None:
                payload["paymentAmount"] = non_negative_amount(payment_amount, "付款金额")
            if payment_account_id.strip():
                payload["paymentAccountId"] = payment_account_id.strip()
            if len(payload) == 1:
                raise DomainError("erp_purchase_order_update_empty", "请提供需要修改的字段")
            result = await self._api.update_purchase_order(context, target_id, payload)
            document_no = await self._lookup_document_no("purchase_order", result.document_id)
            return self.ok_response(
                modified=True,
                document_no=document_no or result.document_id,
            )
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 采购退货单
    # ------------------------------------------------------------------

    async def preview_purchase_return(
        self,
        order_id: str,
        items: list[dict[str, Any]] | None = None,
        refund_amount: float | None = None,
        refund_account_id: str = "",
        discount_amount: float | None = None,
        discount_account_id: str = "",
        return_date: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        """预览把已采购商品退给供应商的采购退货单，可同时收回供应商退款。

        不是新采购入库，也不是客户退货（后者用 previewSalesReturn）。先用
        listPurchaseOrders/getPurchaseOrder 定位原采购单，不传已有退货单 ID。
        本工具回读快捷退货数据，自动带出供应商、仓库、经手人与可退商品数量。
        仅预览，无远端写入；检查 required_actions、ready_to_submit 和 preview_id，
        未就绪先补齐重预览；就绪且用户明确确认后才能调用 submitPurchaseReturn。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            prefill = (await self._api.get_purchase_order_quick_return(context, token)).document
            source_items = self._quick_return_items(prefill)
            if not source_items:
                raise DomainError("erp_purchase_return_items_empty", "采购单没有可退货的商品明细")
            selected = self._select_return_items(source_items, items, "采购单")
            if return_date.strip():
                self._validate_order_date(return_date.strip())
            effective_date = (
                return_date.strip()
                or str(prefill.get("returnDate") or "").strip()
                or date.today().isoformat()
            )
            total_amount = self._return_total_amount(selected)
            refund, discount, refund_account, discount_account, ready = (
                await self._resolve_return_money(
                    total_amount,
                    refund_amount,
                    refund_account_id,
                    discount_amount,
                    discount_account_id,
                )
            )
            payload: dict[str, Any] = {
                "id": 0,
                "returnDate": effective_date,
                "sourceOrderId": str(prefill.get("sourceOrderId") or ""),
                "sourceOrderNo": str(prefill.get("sourceOrderNo") or ""),
                "supplierId": str(prefill.get("supplierId") or ""),
                "supplierName": str(prefill.get("supplierName") or ""),
                "warehouseId": str(prefill.get("warehouseId") or ""),
                "warehouseName": str(prefill.get("warehouseName") or ""),
                "handlerId": str(prefill.get("handlerId") or ""),
                "handlerName": str(prefill.get("handlerName") or ""),
                "saveType": 2,
                "remark": remark.strip(),
                "items": [
                    {
                        "productId": item["product_id"],
                        "quantity": item["quantity"],
                        "unitPrice": item["unit_price"],
                        **({"unit": item["unit"]} if item["unit"] else {}),
                    }
                    for item in selected
                ],
            }
            if prefill.get("sourcePaidAmount") is not None:
                payload["sourcePaidAmount"] = prefill.get("sourcePaidAmount")
            if prefill.get("sourcePayableAmount") is not None:
                payload["sourcePayableAmount"] = prefill.get("sourcePayableAmount")
            if refund is not None and refund > 0 and refund_account is not None:
                payload["paymentAmount"] = refund
                payload["paymentAccountId"] = str(refund_account["id"])
                payload["paymentAccountName"] = str(refund_account.get("name") or "")
            if discount is not None and discount > 0 and discount_account is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_account["id"])
                payload["discountAccountName"] = str(discount_account.get("name") or "")
            preview = self._build_return_preview_summary(
                document_kind="purchase_return",
                source_order_no=str(prefill.get("sourceOrderNo") or ""),
                counterparty_label="供应商",
                counterparty_name=str(prefill.get("supplierName") or ""),
                warehouse_name=str(prefill.get("warehouseName") or ""),
                handler_name=str(prefill.get("handlerName") or ""),
                doc_date=effective_date,
                items=selected,
                total_amount=total_amount,
                refund_label="退款金额",
                refund_amount=refund,
                refund_account=refund_account,
                discount_amount=discount,
                discount_account=discount_account,
                remark=remark.strip(),
            )
            required_actions = ["confirm_submit"] if ready else []
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("purchase_return", payload, preview)
            return self.ok_response(
                source_order={
                    "order_id": str(prefill.get("sourceOrderId") or ""),
                    "order_no": str(prefill.get("sourceOrderNo") or ""),
                    "supplier": str(prefill.get("supplierName") or ""),
                    "warehouse": str(prefill.get("warehouseName") or ""),
                },
                items=[
                    {
                        "product_id": item["product_id"],
                        "product_name": item["product_name"],
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "unit_price": item["unit_price"],
                    }
                    for item in selected
                ],
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_return(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将退给供应商的采购退货预览写入 ERP 并保存过账，不提交销售退货。

        先调用 previewPurchaseReturn，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。成功后可用
        getPurchaseReturn 核对，结果未知先用 listPurchaseReturns 查询，勿盲目重提。

        Args:
            preview_id: 最近一次 previewPurchaseReturn 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得自造、跨类型或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复退货。
            confirmed_by_user: 默认 false；仅在用户明确确认上述采购退货预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_purchase_return(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="purchase_return",
            doc_label="采购退货单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_purchase_return(self, return_id: str) -> dict[str, Any]:
        """只读查询一张采购退货单详情，即退给供应商的记录，不是原采购单。

        先用 listPurchaseReturns 定位；voidPurchaseReturn 前须先核实此详情并取得
        用户明确确认。可取本退货单内部 ID 供 previewReceiptOrder 核销供应商退款；
        新发起采购退货则用 getPurchaseOrder 核实原采购单后调用 previewPurchaseReturn。

        Args:
            return_id: 用户提供或 listPurchaseReturns/submitPurchaseReturn 返回的采购退货单
                内部 ID 或业务单号 returnNo；不是原采购单 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not return_id.strip():
                raise DomainError("erp_purchase_return_id_invalid", "采购退货单 ID 不能为空")
            result = await self._api.get_purchase_return_detail(context, return_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_purchase_returns(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        return_no: str = "",
        supplier_id: str = "",
    ) -> dict[str, Any]:
        """只读分页查找退给供应商的采购退货单，不是采购入库单或销售退货单。

        返回单据列表而非采购统计；采购分析用 queryPurchaseReport，不以当前页
        合计代替报表。选定退货单后用 getPurchaseReturn 核实详情，再确认作废
        或用 previewReceiptOrder 预览退款核销；新退货来源请查 listPurchaseOrders。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            supplier = await self._optional_reference_id("supplier", supplier_id)
            result = await self._api.search_purchase_returns(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                return_no=return_no.strip(),
                supplier_id=supplier,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_purchase_return(
        self,
        return_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废已存在的采购退货单并真实写入 ERP，不是作废原采购单或新增退货。

        先调用 getPurchaseReturn 核实退货内容和状态，用户明确确认作废后才能调用；
        未确认不调用，不得自造 confirmed_by_user=true。完成后可再用 getPurchaseReturn 核对。

        Args:
            return_id: 经 getPurchaseReturn 核实的采购退货单内部 ID 或业务单号 returnNo，
                不是源采购单 ID。
            confirmed_by_user: 仅在用户明确确认作废该采购退货单后传 true，不得代用户确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "purchase_return",
                "采购退货单",
                return_id,
                confirmed_by_user,
                lambda resolved: self._api.void_purchase_return(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 销售退货单
    # ------------------------------------------------------------------

    async def preview_sales_return(
        self,
        order_id: str,
        items: list[dict[str, Any]] | None = None,
        refund_amount: float | None = None,
        refund_account_id: str = "",
        discount_amount: float | None = None,
        discount_account_id: str = "",
        return_date: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        """预览客户退回已售商品的销售退货单，可同时向客户退款。

        向供应商退货用 previewPurchaseReturn。先用 listSalesOrders/getSalesOrder
        定位原销售单，不传已有销售退货单 ID；快捷退货自动带出客户、仓库、
        经手人与可退商品数量。仅预览，无远端写入；检查 required_actions、
        ready_to_submit 和 preview_id，未就绪先补齐重预览；就绪且用户明确确认
        后才能调用 submitSalesReturn。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_sales_order_id_invalid", "销售单 ID 不能为空")
            prefill = (await self._api.get_sales_order_quick_return(context, token)).document
            source_items = self._quick_return_items(prefill)
            if not source_items:
                raise DomainError("erp_sales_return_items_empty", "销售单没有可退货的商品明细")
            selected = self._select_return_items(source_items, items, "销售单")
            if return_date.strip():
                self._validate_order_date(return_date.strip())
            effective_date = (
                return_date.strip()
                or str(prefill.get("returnDate") or "").strip()
                or date.today().isoformat()
            )
            total_amount = self._return_total_amount(selected)
            refund, discount, refund_account, discount_account, ready = (
                await self._resolve_return_money(
                    total_amount,
                    refund_amount,
                    refund_account_id,
                    discount_amount,
                    discount_account_id,
                )
            )
            payload: dict[str, Any] = {
                "id": 0,
                "returnDate": effective_date,
                "sourceOrderId": str(prefill.get("sourceOrderId") or ""),
                "sourceOrderNo": str(prefill.get("sourceOrderNo") or ""),
                "customerId": str(prefill.get("customerId") or ""),
                "customerName": str(prefill.get("customerName") or ""),
                "warehouseId": str(prefill.get("warehouseId") or ""),
                "warehouseName": str(prefill.get("warehouseName") or ""),
                "handlerId": str(prefill.get("handlerId") or ""),
                "handlerName": str(prefill.get("handlerName") or ""),
                "saveType": 2,
                "remark": remark.strip(),
                "items": [
                    {
                        "productId": item["product_id"],
                        "quantity": item["quantity"],
                        "unitPrice": item["unit_price"],
                        **({"unit": item["unit"]} if item["unit"] else {}),
                    }
                    for item in selected
                ],
            }
            for passthrough in ("refundedAmount", "unrefundedAmount", "refundStatus"):
                if prefill.get(passthrough) is not None:
                    payload[passthrough] = prefill.get(passthrough)
            if refund is not None and refund > 0 and refund_account is not None:
                payload["paymentAmount"] = refund
                payload["paymentAccountId"] = str(refund_account["id"])
                payload["paymentAccountName"] = str(refund_account.get("name") or "")
            if discount is not None and discount > 0 and discount_account is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_account["id"])
                payload["discountAccountName"] = str(discount_account.get("name") or "")
            preview = self._build_return_preview_summary(
                document_kind="sales_return",
                source_order_no=str(prefill.get("sourceOrderNo") or ""),
                counterparty_label="客户",
                counterparty_name=str(prefill.get("customerName") or ""),
                warehouse_name=str(prefill.get("warehouseName") or ""),
                handler_name=str(prefill.get("handlerName") or ""),
                doc_date=effective_date,
                items=selected,
                total_amount=total_amount,
                refund_label="退款金额",
                refund_amount=refund,
                refund_account=refund_account,
                discount_amount=discount,
                discount_account=discount_account,
                remark=remark.strip(),
            )
            required_actions = ["confirm_submit"] if ready else []
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("sales_return", payload, preview)
            return self.ok_response(
                source_order={
                    "order_id": str(prefill.get("sourceOrderId") or ""),
                    "order_no": str(prefill.get("sourceOrderNo") or ""),
                    "customer": str(prefill.get("customerName") or ""),
                    "warehouse": str(prefill.get("warehouseName") or ""),
                    "unrefunded_amount": prefill.get("unrefundedAmount"),
                },
                items=[
                    {
                        "product_id": item["product_id"],
                        "product_name": item["product_name"],
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "unit_price": item["unit_price"],
                    }
                    for item in selected
                ],
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_return(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将客户退货的销售退货预览写入 ERP 并保存过账，不提交采购退货。

        先调用 previewSalesReturn，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。成功后可用
        getSalesReturn 核对，结果未知先用 listSalesReturns 查询，勿盲目重提。

        Args:
            preview_id: 最近一次 previewSalesReturn 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得自造、跨类型或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复退货。
            confirmed_by_user: 默认 false；仅在用户明确确认上述销售退货预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_sales_return(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="sales_return",
            doc_label="销售退货单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_sales_return(self, return_id: str) -> dict[str, Any]:
        """只读查询一张销售退货单详情，即客户退货记录，不是原销售单。

        先用 listSalesReturns 定位；voidSalesReturn 前须先核实此详情并取得
        用户明确确认。可取本退货单内部 ID 供 previewPaymentOrder 核销客户退款；
        新发起客户退货则用 getSalesOrder 核实原销售单后调用 previewSalesReturn。

        Args:
            return_id: 用户提供或 listSalesReturns/submitSalesReturn 返回的销售退货单
                内部 ID 或业务单号 returnNo；不是原销售单 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not return_id.strip():
                raise DomainError("erp_sales_return_id_invalid", "销售退货单 ID 不能为空")
            result = await self._api.get_sales_return_detail(context, return_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_sales_returns(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        refund_status: int | None = None,
        return_no: str = "",
        customer_id: str = "",
    ) -> dict[str, Any]:
        """只读分页查找客户退货的销售退货单，不是销售原单或采购退货单。

        返回单据列表而非销售统计；销售分析用 querySalesReport，不以当前页
        合计代替报表。选定退货单后用 getSalesReturn 核实详情，再确认作废
        或用 previewPaymentOrder 预览退款核销；新退货来源请查 listSalesOrders。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            customer = await self._optional_reference_id("customer", customer_id)
            result = await self._api.search_sales_returns(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                refund_status=refund_status,
                return_no=return_no.strip(),
                customer_id=customer,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_sales_return(
        self,
        return_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废已存在的销售退货单并真实写入 ERP，不是作废原销售单或新增退货。

        先调用 getSalesReturn 核实退货内容和状态，用户明确确认作废后才能调用；
        未确认不调用，不得自造 confirmed_by_user=true。完成后可再用 getSalesReturn 核对。

        Args:
            return_id: 经 getSalesReturn 核实的销售退货单内部 ID 或业务单号 returnNo，
                不是源销售单 ID。
            confirmed_by_user: 仅在用户明确确认作废该销售退货单后传 true，不得代用户确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "sales_return",
                "销售退货单",
                return_id,
                confirmed_by_user,
                lambda resolved: self._api.void_sales_return(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 销售单继续收款 / 采购单继续付款
    # ------------------------------------------------------------------

    async def preview_sales_receipt(
        self,
        order_id: str,
        receipt_amount: float,
        receipt_account: str,
        discount_amount: float | None = None,
        discount_account: str = "",
    ) -> dict[str, Any]:
        """预览对单笔销售单继续收款或免账，不新建独立收款单。

        先用 listSalesOrders/getSalesOrder 核实目标销售单及未收金额；需要独立
        收款单或多单核销用 previewReceiptOrder，客户退货用 previewSalesReturn。
        本工具仅预览，无远端写入；返回 required_actions、ready_to_submit、preview_id，
        按候选补齐后重预览，用户明确确认就绪预览后才能调用 submitSalesReceipt。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_sales_order_id_invalid", "销售单 ID 不能为空")
            amount = non_negative_amount(receipt_amount, "收款金额")
            discount = (
                non_negative_amount(discount_amount, "免账金额")
                if discount_amount is not None
                else 0.0
            )
            if amount + discount <= 0:
                raise DomainError("erp_sales_receipt_amount_invalid", "收款金额与免账金额不能同时为 0")
            detail = (await self._api.get_sales_order_detail(context, token)).order
            resolved_id = str(detail.get("id") or token)
            order_no = str(detail.get("orderNo") or "")
            unreceived = detail.get("unreceivedAmount")
            if isinstance(unreceived, (int, float)) and amount + discount > unreceived + 0.005:
                raise DomainError(
                    "erp_sales_receipt_amount_invalid",
                    "收款金额加免账金额（%.2f）不能超过未收金额 %.2f"
                    % (amount + discount, unreceived),
                )
            account_resolution = await self._resolve_reference(
                "settlement_account", receipt_account.strip(),
            )
            discount_resolution = (
                await self._resolve_reference("settlement_account", discount_account.strip())
                if discount > 0
                else None
            )
            ready = bool(
                account_resolution["status"] == "matched"
                and (discount_resolution is None or discount_resolution["status"] == "matched")
            )
            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            required_actions: list[str] = []
            if ready:
                selected_account = account_resolution["selected"]
                payload = {
                    "orderId": resolved_id,
                    "receiptAmount": amount,
                    "receiptAccountId": str(selected_account["id"]),
                }
                preview = {
                    "document_kind": "sales_receipt",
                    "order_no": order_no or resolved_id,
                    "customer": str(detail.get("customerName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "received_amount": detail.get("receivedAmount"),
                    "unreceived_amount": unreceived,
                    "receipt_amount": amount,
                    "receipt_account": str(selected_account.get("name") or ""),
                    "discount_amount": discount or None,
                }
                if discount > 0 and discount_resolution is not None:
                    payload["discountAmount"] = discount
                    payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
                    preview["discount_account"] = str(
                        discount_resolution["selected"].get("name") or "",
                    )
                required_actions = ["confirm_submit"]
            else:
                required_actions = ["select_receipt_account"]
                if discount_resolution is not None and discount_resolution["status"] != "matched":
                    required_actions.append("select_discount_account")
            preview_id = None
            if ready and payload is not None and preview is not None:
                preview_id = self.session.store_prepared_document("sales_receipt", payload, preview)
            references: dict[str, dict[str, Any]] = {"receipt_account": account_resolution}
            if discount_resolution is not None:
                references["discount_account"] = discount_resolution
            return self.ok_response(
                order={
                    "order_id": resolved_id,
                    "order_no": order_no,
                    "customer": str(detail.get("customerName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "received_amount": detail.get("receivedAmount"),
                    "unreceived_amount": unreceived,
                },
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_receipt(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将已确认的单笔继续收款/免账写入销售单，不提交独立收款单。

        先调用 previewSalesReceipt，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。提交后用 getSalesOrder
        核对收款状态；结果未知也先核对，不盲目重收。

        Args:
            preview_id: 最近一次 previewSalesReceipt 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得使用 previewReceiptOrder 的 ID、自造或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复收款。
            confirmed_by_user: 默认 false；仅在用户明确确认上述单笔收款预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            order_id = str(payload["orderId"])
            body = {key: value for key, value in payload.items() if key != "orderId"}
            await self._api.receive_sales_order(context, order_id, body)
            return order_id

        return await self._submit_prepared_document(
            kind="sales_receipt",
            doc_label="销售单收款",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def preview_purchase_payment(
        self,
        order_id: str,
        payment_amount: float,
        payment_account: str,
        discount_amount: float | None = None,
        discount_account: str = "",
    ) -> dict[str, Any]:
        """预览对单笔采购单继续付款或免账，不新建独立付款单。

        先用 listPurchaseOrders/getPurchaseOrder 核实目标采购单及未付金额；需要
        独立付款单或多单核销用 previewPaymentOrder，采购退货用 previewPurchaseReturn。
        本工具仅预览，无远端写入；返回 required_actions、ready_to_submit、preview_id，
        按候选补齐后重预览，用户明确确认就绪预览后才能调用 submitPurchasePayment。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            amount = non_negative_amount(payment_amount, "付款金额")
            discount = (
                non_negative_amount(discount_amount, "免账金额")
                if discount_amount is not None
                else 0.0
            )
            if amount + discount <= 0:
                raise DomainError("erp_purchase_payment_amount_invalid", "付款金额与免账金额不能同时为 0")
            detail = (await self._api.get_purchase_order_detail(context, token)).document
            resolved_id = str(detail.get("id") or token)
            order_no = str(detail.get("orderNo") or "")
            unpaid = detail.get("unpaidAmount")
            if isinstance(unpaid, (int, float)) and amount + discount > unpaid + 0.005:
                raise DomainError(
                    "erp_purchase_payment_amount_invalid",
                    "付款金额加免账金额（%.2f）不能超过未付金额 %.2f"
                    % (amount + discount, unpaid),
                )
            account_resolution = await self._resolve_reference(
                "settlement_account", payment_account.strip(),
            )
            discount_resolution = (
                await self._resolve_reference("settlement_account", discount_account.strip())
                if discount > 0
                else None
            )
            ready = bool(
                account_resolution["status"] == "matched"
                and (discount_resolution is None or discount_resolution["status"] == "matched")
            )
            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            required_actions: list[str] = []
            if ready:
                selected_account = account_resolution["selected"]
                payload = {
                    "orderId": resolved_id,
                    "paymentAmount": amount,
                    "paymentAccountId": str(selected_account["id"]),
                }
                preview = {
                    "document_kind": "purchase_payment",
                    "order_no": order_no or resolved_id,
                    "supplier": str(detail.get("supplierName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "paid_amount": detail.get("paidAmount"),
                    "unpaid_amount": unpaid,
                    "payment_amount": amount,
                    "payment_account": str(selected_account.get("name") or ""),
                    "discount_amount": discount or None,
                }
                if discount > 0 and discount_resolution is not None:
                    payload["discountAmount"] = discount
                    payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
                    preview["discount_account"] = str(
                        discount_resolution["selected"].get("name") or "",
                    )
                required_actions = ["confirm_submit"]
            else:
                required_actions = ["select_payment_account"]
                if discount_resolution is not None and discount_resolution["status"] != "matched":
                    required_actions.append("select_discount_account")
            preview_id = None
            if ready and payload is not None and preview is not None:
                preview_id = self.session.store_prepared_document("purchase_payment", payload, preview)
            references: dict[str, dict[str, Any]] = {"payment_account": account_resolution}
            if discount_resolution is not None:
                references["discount_account"] = discount_resolution
            return self.ok_response(
                order={
                    "order_id": resolved_id,
                    "order_no": order_no,
                    "supplier": str(detail.get("supplierName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "paid_amount": detail.get("paidAmount"),
                    "unpaid_amount": unpaid,
                },
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_payment(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将已确认的单笔继续付款/免账写入采购单，不提交独立付款单。

        先调用 previewPurchasePayment，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。提交后用 getPurchaseOrder
        核对付款状态；结果未知也先核对，不盲目重付。

        Args:
            preview_id: 最近一次 previewPurchasePayment 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得使用 previewPaymentOrder 的 ID、自造或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复付款。
            confirmed_by_user: 默认 false；仅在用户明确确认上述单笔付款预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            order_id = str(payload["orderId"])
            body = {key: value for key, value in payload.items() if key != "orderId"}
            await self._api.pay_purchase_order(context, order_id, body)
            return order_id

        return await self._submit_prepared_document(
            kind="purchase_payment",
            doc_label="采购单付款",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    # ------------------------------------------------------------------
    # 库存调拨与其他出入库
    # ------------------------------------------------------------------

    async def preview_stock_transfer(
        self,
        order_text: str,
        from_warehouse: str,
        to_warehouse: str,
        handler: str,
        transfer_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """预览商品从一个仓库调到另一个仓库的库存调拨单，不是采购或销售。

        调出、调入仓库必须不同；单仓报损/报溢用 previewOtherStockDoc。
        仓库和经手人候选用 searchBillingReferences，商品候选用 searchProducts。
        本工具仅预览，无远端写入；根据 required_actions 补商品/单位等并重预览，
        ready_to_submit=true 且取得 preview_id 后，用户明确确认才能调用 submitStockTransfer。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "from_warehouse": from_warehouse.strip(),
                "to_warehouse": to_warehouse.strip(),
                "handler": handler.strip(),
                "order_text": order_text.strip(),
            }
            effective_date = transfer_date.strip() or date.today().isoformat()
            self._validate_order_date(effective_date)
            if len(remark.strip()) > 200:
                raise DomainError("erp_stock_transfer_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("from_warehouse", "调出仓库", "请问从哪个仓库调出？"),
                    ("to_warehouse", "调入仓库", "请问调到哪个仓库？"),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            draft = await self._match_order_products(values["order_text"], "text", confirmed_products)
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
                "from_warehouse": await self._resolve_reference("warehouse", values["from_warehouse"]),
                "to_warehouse": await self._resolve_reference("warehouse", values["to_warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
            }
            references_ready = all(
                resolution["status"] == "matched"
                for resolution in reference_resolutions.values()
            )
            if (
                references_ready
                and reference_resolutions["from_warehouse"]["selected"]["id"]
                == reference_resolutions["to_warehouse"]["selected"]["id"]
            ):
                raise DomainError(
                    "erp_stock_transfer_warehouse_invalid",
                    "调出仓库不能与调入仓库相同",
                )
            unit_warnings = self._unit_warnings(draft)
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
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                items, preview_items, total = self._build_stock_items(draft, partial)
                payload = {
                    "id": 0,
                    "transferDate": effective_date,
                    "fromWarehouseId": reference_resolutions["from_warehouse"]["selected"]["id"],
                    "toWarehouseId": reference_resolutions["to_warehouse"]["selected"]["id"],
                    "handlerId": reference_resolutions["handler"]["selected"]["id"],
                    "saveType": 2,
                    "remark": remark.strip(),
                    "items": items,
                }
                preview = {
                    "document_kind": "stock_transfer",
                    "transfer_date": effective_date,
                    "from_warehouse": str(reference_resolutions["from_warehouse"]["selected"].get("name") or ""),
                    "to_warehouse": str(reference_resolutions["to_warehouse"]["selected"].get("name") or ""),
                    "handler": str(reference_resolutions["handler"]["selected"].get("name") or ""),
                    "remark": remark.strip(),
                    "items": preview_items,
                    "total_quantity": total,
                }
            return self._finalize_document_preview(
                kind="stock_transfer",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=[],
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_stock_transfer(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将已确认的仓库间调拨预览写入 ERP 并保存过账，不提交报损/报溢单。

        先调用 previewStockTransfer，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。结果未知时先核对 ERP，
        不盲目重提；可用 queryStockLogs 查看库存变动。

        Args:
            preview_id: 最近一次 previewStockTransfer 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得自造、跨类型或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复调拨。
            confirmed_by_user: 默认 false；仅在用户明确确认上述调拨预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_stock_transfer(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="stock_transfer",
            doc_label="调拨单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def preview_other_stock_doc(
        self,
        kind: str,
        order_text: str,
        warehouse: str,
        handler: str,
        doc_type: str,
        doc_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """预览单个仓库的其他入库/出库单，用于报溢、报损等非购销库存变动。

        仓库间移动用 previewStockTransfer，不用报损/报溢替代销售或采购退货。
        先用 listStockDocTypes 查询方向对应的类型；基础资料用 searchBillingReferences，
        商品用 searchProducts 查候选。仅预览，无远端写入；按 required_actions 补齐
        并重预览，ready_to_submit=true 且取得 preview_id 后，用户明确确认才能调用
        submitOtherStockDoc。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized_kind = kind.strip()
            if normalized_kind not in {"inbound", "outbound"}:
                raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
            doc_label = "其他入库单" if normalized_kind == "inbound" else "其他出库单"
            values = {
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_text": order_text.strip(),
            }
            effective_date = doc_date.strip() or date.today().isoformat()
            self._validate_order_date(effective_date)
            if len(remark.strip()) > 200:
                raise DomainError("erp_stock_doc_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("warehouse", "仓库", "请问在哪个仓库%s？" % ("入库" if normalized_kind == "inbound" else "出库")),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            type_resolution = await self._resolve_stock_doc_type(
                context, normalized_kind, doc_type,
            )
            draft = await self._match_order_products(values["order_text"], "text", confirmed_products)
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
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
                "doc_type": type_resolution,
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
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                items, preview_items, total = self._build_stock_items(draft, partial)
                payload = {
                    "id": 0,
                    "%sDate" % ("inbound" if normalized_kind == "inbound" else "outbound"): effective_date,
                    "warehouseId": reference_resolutions["warehouse"]["selected"]["id"],
                    "%sType" % ("inbound" if normalized_kind == "inbound" else "outbound"): int(
                        reference_resolutions["doc_type"]["selected"]["id"],
                    ),
                    "handlerId": reference_resolutions["handler"]["selected"]["id"],
                    "saveType": 2,
                    "remark": remark.strip(),
                    "items": items,
                }
                preview = {
                    "document_kind": "other_stock_doc",
                    "kind": normalized_kind,
                    "doc_date": effective_date,
                    "doc_type": str(reference_resolutions["doc_type"]["selected"].get("name") or ""),
                    "warehouse": str(reference_resolutions["warehouse"]["selected"].get("name") or ""),
                    "handler": str(reference_resolutions["handler"]["selected"].get("name") or ""),
                    "remark": remark.strip(),
                    "items": preview_items,
                    "total_quantity": total,
                }
            result = self._finalize_document_preview(
                kind="other_stock_doc",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=[],
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
            result["doc_label"] = doc_label
            return result
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_other_stock_doc(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将已确认的其他入库/出库预览写入 ERP 并保存过账，不提交仓库调拨单。

        先调用 previewOtherStockDoc，方向和类型沿用最近预览；用户明确确认就绪
        预览后才能提交，未确认不调用，不得自造 confirmed_by_user=true。
        结果未知先核对 ERP，不盲目重提；可用 queryStockLogs 查看库存变动。

        Args:
            preview_id: 最近一次 previewOtherStockDoc 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得自造、跨类型或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复出入库。
            confirmed_by_user: 默认 false；仅在用户明确确认上述出入库预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            kind = "inbound" if "inboundType" in payload else "outbound"
            result = await self._api.create_other_stock_doc(context, kind, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="other_stock_doc",
            doc_label="其他出入库单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    # ------------------------------------------------------------------
    # 收款单与付款单
    # ------------------------------------------------------------------

    async def preview_receipt_order(
        self,
        receipt_amount: float,
        account: str,
        handler: str,
        customer: str = "",
        supplier: str = "",
        order_date: str = "",
        discount_amount: float | None = None,
        discount_account: str = "",
        fund_type: str = "",
        writeoff_details: list[dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """预览独立收款单，可不核销，也可将一笔收款分配到多张业务单据核销。

        单笔销售单继续收款用 previewSalesReceipt；本工具通常核销销售单应收或
        采购退货退款。核销前用 getSalesOrder/getPurchaseReturn 等对应详情工具
        核实单据、往来单位及内部 ID；基础资料用 searchBillingReferences 查候选。
        仅预览，无远端写入；按 required_actions 和候选补齐后重新预览，
        ready_to_submit=true 且取得 preview_id 后，用户明确确认才能调用 submitReceiptOrder。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            amount = non_negative_amount(receipt_amount, "收款金额")
            if amount <= 0:
                raise DomainError("erp_receipt_order_amount_invalid", "收款金额必须大于 0")
            payload, preview, references, ready, required_actions = (
                await self._build_financial_order_preview(
                    context=context,
                    direction="receipt",
                    amount=amount,
                    account=account,
                    handler=handler,
                    customer=customer,
                    supplier=supplier,
                    order_date=order_date,
                    discount_amount=discount_amount,
                    discount_account=discount_account,
                    fund_type=fund_type,
                    writeoff_details=writeoff_details,
                    remark=remark,
                )
            )
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("receipt_order", payload, preview)
            return self.ok_response(
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_receipt_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将独立收款单及预览中的核销分配写入 ERP 并保存生效，不是销售单继续收款。

        先调用 previewReceiptOrder，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。成功后可用 getReceiptOrder
        核对，结果未知先用 listReceiptOrders 查询，勿盲目重提。

        Args:
            preview_id: 最近一次 previewReceiptOrder 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得使用 previewSalesReceipt 的 ID、自造或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复收款。
            confirmed_by_user: 默认 false；仅在用户明确确认上述独立收款预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_receipt_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="receipt_order",
            doc_label="收款单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_receipt_order(self, order_id: str) -> dict[str, Any]:
        """只读查询一张独立收款单详情及核销明细，不是销售单的收款记录或统计报表。

        不确定单据先用 listReceiptOrders 定位；voidReceiptOrder 前须先核实此详情
        并取得用户明确确认。销售单继续收款状态查 getSalesOrder，结算统计用 querySettlementReport。

        Args:
            order_id: 用户提供或 listReceiptOrders/submitReceiptOrder 返回的独立收款单
                内部 ID 或业务单号 orderNo；不是被核销的销售单或采购退货单 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_financial_order_id_invalid", "收款单 ID 不能为空")
            result = await self._api.get_financial_order_detail(context, "receipt", order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_receipt_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        order_no: str = "",
        counterparty_id: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        """只读分页查找独立收款单，支持日期、状态、往来单位及结算账户筛选。

        不是销售订单列表或结算汇总，结算统计用 querySettlementReport，不以当前页
        合计代替报表。选定单据后用 getReceiptOrder 核实详情，明确确认后才可 voidReceiptOrder。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            params = await self._financial_order_params(
                context, start_date, end_date, status, order_no, counterparty_id, account_id,
                customer_field="customerId", supplier_field="supplierId",
            )
            result = await self._api.list_financial_orders(
                context, "receipt",
                self._with_page(params, page, page_size),
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_receipt_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废独立收款单并真实写入 ERP，不是作废销售单或发起客户退款。

        先调用 getReceiptOrder 核实收款内容、核销明细和状态，用户明确确认作废
        后才能调用；未确认不调用，不得自造 confirmed_by_user=true。
        完成后可再用 getReceiptOrder 核对。

        Args:
            order_id: 经 getReceiptOrder 核实的独立收款单内部 ID 或业务单号 orderNo，
                不是被核销的业务单据 ID。
            confirmed_by_user: 仅在用户明确确认作废该收款单后传 true，不得代用户确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_financial_flow(
                "receipt", "收款单", order_id, confirmed_by_user,
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    async def preview_payment_order(
        self,
        payment_amount: float,
        account: str,
        handler: str,
        supplier: str = "",
        customer: str = "",
        order_date: str = "",
        discount_amount: float | None = None,
        discount_account: str = "",
        fund_type: str = "",
        writeoff_details: list[dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """预览独立付款单，可不核销，也可将一笔付款分配到多张业务单据核销。

        单笔采购单继续付款用 previewPurchasePayment；本工具通常核销采购单应付
        或销售退货退款。核销前用 getPurchaseOrder/getSalesReturn 等对应详情工具
        核实单据、往来单位及内部 ID；基础资料用 searchBillingReferences 查候选。
        仅预览，无远端写入；按 required_actions 和候选补齐后重新预览，
        ready_to_submit=true 且取得 preview_id 后，用户明确确认才能调用 submitPaymentOrder。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            amount = non_negative_amount(payment_amount, "付款金额")
            if amount <= 0:
                raise DomainError("erp_payment_order_amount_invalid", "付款金额必须大于 0")
            payload, preview, references, ready, required_actions = (
                await self._build_financial_order_preview(
                    context=context,
                    direction="payment",
                    amount=amount,
                    account=account,
                    handler=handler,
                    customer=customer,
                    supplier=supplier,
                    order_date=order_date,
                    discount_amount=discount_amount,
                    discount_account=discount_account,
                    fund_type=fund_type,
                    writeoff_details=writeoff_details,
                    remark=remark,
                )
            )
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("payment_order", payload, preview)
            return self.ok_response(
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_payment_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """将独立付款单及预览中的核销分配写入 ERP 并保存生效，不是采购单继续付款。

        先调用 previewPaymentOrder，用户明确确认最近的就绪预览后才能提交；
        未确认不调用，不得自造 confirmed_by_user=true。成功后可用 getPaymentOrder
        核对，结果未知先用 listPaymentOrders 查询，勿盲目重提。

        Args:
            preview_id: 最近一次 previewPaymentOrder 在 ready_to_submit=true 时返回且
                已获用户确认的 preview_id；不得使用 previewPurchasePayment 的 ID、自造或用旧预览，成功后消费失效。
            idempotency_key: 可省略，默认 preview_id；显式值须非空且最多 128 字符，
                同一提交重试复用原键，不得换键重复付款。
            confirmed_by_user: 默认 false；仅在用户明确确认上述独立付款预览后传 true，不得代用户确认。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_payment_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="payment_order",
            doc_label="付款单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_payment_order(self, order_id: str) -> dict[str, Any]:
        """只读查询一张独立付款单详情及核销明细，不是采购单的付款记录或统计报表。

        不确定单据先用 listPaymentOrders 定位；voidPaymentOrder 前须先核实此详情
        并取得用户明确确认。采购单继续付款状态查 getPurchaseOrder，结算统计用 querySettlementReport。

        Args:
            order_id: 用户提供或 listPaymentOrders/submitPaymentOrder 返回的独立付款单
                内部 ID 或业务单号 orderNo；不是被核销的采购单或销售退货单 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_financial_order_id_invalid", "付款单 ID 不能为空")
            result = await self._api.get_financial_order_detail(context, "payment", order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_payment_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        order_no: str = "",
        counterparty_id: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        """只读分页查找独立付款单，支持日期、状态、往来单位及结算账户筛选。

        不是采购订单列表或结算汇总，结算统计用 querySettlementReport，不以当前页
        合计代替报表。选定单据后用 getPaymentOrder 核实详情，明确确认后才可 voidPaymentOrder。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            params = await self._financial_order_params(
                context, start_date, end_date, status, order_no, counterparty_id, account_id,
                customer_field="customerId", supplier_field="supplierId",
            )
            result = await self._api.list_financial_orders(
                context, "payment",
                self._with_page(params, page, page_size),
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_payment_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废独立付款单并真实写入 ERP，不是作废采购单或发起采购退货。

        先调用 getPaymentOrder 核实付款内容、核销明细和状态，用户明确确认作废
        后才能调用；未确认不调用，不得自造 confirmed_by_user=true。
        完成后可再用 getPaymentOrder 核对。

        Args:
            order_id: 经 getPaymentOrder 核实的独立付款单内部 ID 或业务单号 orderNo，
                不是被核销的业务单据 ID。
            confirmed_by_user: 仅在用户明确确认作废该付款单后传 true，不得代用户确认。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_financial_flow(
                "payment", "付款单", order_id, confirmed_by_user,
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 内部辅助：预览装配
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_fields(
        values: dict[str, str],
        required: tuple[tuple[str, str, str], ...],
    ) -> list[dict[str, str]]:
        return [
            {"field": field, "label": label, "prompt": prompt}
            for field, label, prompt in required
            if not values.get(field)
        ]

    def _finalize_document_preview(
        self,
        *,
        kind: str,
        missing: list[dict[str, str]],
        reference_resolutions: dict[str, dict[str, Any]],
        unit_warnings: list[dict[str, Any]],
        price_warnings: list[dict[str, Any]],
        product_payload: dict[str, Any],
        ready: bool,
        payload: dict[str, Any] | None,
        preview: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """收敛各类预览校验结果：存储不可变预览并输出待办。"""
        preview_id = None
        if ready and payload is not None and preview is not None:
            preview_id = self.session.store_prepared_document(kind, payload, preview)
        required_actions = ["confirm_submit"] if ready else []
        if not ready:
            required_actions.extend(
                "provide_%s" % item["field"] for item in missing
            )
            for field, resolution in reference_resolutions.items():
                if resolution["status"] == "ambiguous":
                    required_actions.append("select_%s" % field)
                elif resolution["status"] == "unmatched":
                    required_actions.append("replace_%s" % field)
            if product_payload.get("recommended_products"):
                required_actions.append("select_products")
            if product_payload.get("unmatched_products"):
                required_actions.append("resolve_unmatched_products")
            if unit_warnings:
                required_actions.append("confirm_units")
            if price_warnings:
                required_actions.append("confirm_prices")
        result = self.ok_response(
            missing_required_fields=missing,
            reference_resolutions=self._public_reference_resolutions(reference_resolutions),
            unit_warnings=unit_warnings,
            price_warnings=price_warnings,
            required_actions=required_actions,
            **product_payload,
            ready_to_submit=ready,
            preview_id=preview_id,
            preview=preview,
        )
        return result

    def _public_reference_resolutions(
        self,
        resolutions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """候选保留机器可用 ID；用户界面由 Agent 隐藏内部标识。"""
        return {
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
            for field, resolution in resolutions.items()
        }

    def _build_purchase_order_preview(
        self,
        *,
        draft: Any,
        references: dict[str, dict[str, Any]],
        order_date: str,
        remark: str,
        prices: dict[str, float],
        partial: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """构建采购单提交 payload 与用户预览摘要。"""
        supplier = references["supplier"]["selected"]
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
                    "erp_purchase_order_product_unconfirmed",
                    "采购单仍有未确认商品，不能生成提交预览",
                )
            unit_price = prices.get(line.order_line.line_id, line.product.purchase_price)
            if unit_price is None or unit_price <= 0:
                raise DomainError(
                    "erp_purchase_order_price_missing",
                    "商品 %s 缺少有效采购价，请通过 confirmed_prices 确认单价" % line.product.name,
                )
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
                "unitPrice": positive_amount(unit_price, "采购单价"),
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            amount = line_amount(line.order_line.quantity, float(unit_price))
            preview_item = {
                "name": line.product.name,
                "quantity": line.order_line.quantity,
                "unit": line.product.unit or line.order_line.unit,
                "unit_price": float(unit_price),
            }
            if amount is None:
                has_complete_amount = False
            else:
                preview_item["line_amount"] = float(amount)
                total_amount += amount
            preview_items.append(preview_item)
        payload = {
            "id": 0,
            "orderDate": order_date,
            "supplierId": supplier["id"],
            "warehouseId": warehouse["id"],
            "handlerId": handler["id"],
            "saveType": 2,
            "remark": remark,
            "items": items,
        }
        preview = {
            "document_kind": "purchase_order",
            "order_date": order_date,
            "supplier": str(supplier.get("name") or ""),
            "warehouse": str(warehouse.get("name") or ""),
            "handler": str(handler.get("name") or ""),
            "remark": remark,
            "items": preview_items,
        }
        if preview_items and has_complete_amount:
            preview["total_amount"] = float(total_amount)
        return payload, preview

    def _purchase_price_warnings(
        self,
        draft: Any,
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        """目录无有效采购价（缺价或 0 价）且未经确认的行，要求补充单价。

        ERP 拒绝 unitPrice 为 0 的采购行（上游错误 50303），测试目录中
        商品最近采购价常为 0，必须先经 confirmed_prices 确认才能提交。
        """
        warnings: list[dict[str, Any]] = []
        if draft is None:
            return warnings
        for line in draft.lines:
            if line.status != "matched" or line.product is None:
                continue
            if prices.get(line.order_line.line_id) is not None:
                continue
            catalog_price = line.product.purchase_price
            if catalog_price is None or catalog_price <= 0:
                warnings.append(
                    {
                        "line_id": line.order_line.line_id,
                        "product": line.product.name,
                        "product_id": line.product.product_id,
                        "prompt": "商品缺少有效采购价（目录价为空或 0），请通过 confirmed_prices 确认单价后重新预览。",
                    },
                )
        return warnings

    @staticmethod
    def _normalize_confirmed_prices(
        draft: Any,
        confirmed_prices: list[dict[str, Any]] | None,
    ) -> dict[str, float]:
        """按行确认采购价；行必须已匹配商品且价格大于 0。"""
        if confirmed_prices is None:
            return {}
        if not isinstance(confirmed_prices, list):
            raise DomainError("erp_price_confirmation_invalid", "confirmed_prices 必须是数组")
        lines = {line.order_line.line_id: line for line in draft.lines} if draft else {}
        result: dict[str, float] = {}
        seen: set[str] = set()
        for entry in confirmed_prices:
            if not isinstance(entry, dict):
                raise DomainError("erp_price_confirmation_invalid", "价格确认必须是对象")
            line_id = entry.get("line_id")
            if not isinstance(line_id, str) or line_id in seen or line_id not in lines:
                raise DomainError("erp_price_confirmation_invalid", "价格确认行不存在或重复")
            seen.add(line_id)
            line = lines[line_id]
            if (line.product is None or line.status != "matched"
                    or entry.get("product_id") != line.product.product_id):
                raise DomainError("erp_price_confirmation_invalid", "请对已匹配商品的行确认价格")
            result[line_id] = positive_amount(entry.get("unit_price"), "确认单价")
        return result

    def _build_stock_items(
        self,
        draft: Any,
        partial: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
        """构建调拨/其他出入库明细：无单价，数量合计供预览。"""
        items: list[dict[str, Any]] = []
        preview_items: list[dict[str, Any]] = []
        total_quantity = 0.0
        for line in draft.lines:
            if line.product is None:
                if partial:
                    continue
                raise DomainError(
                    "erp_stock_doc_product_unconfirmed",
                    "单据仍有未确认商品，不能生成提交预览",
                )
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            preview_items.append(
                {
                    "name": line.product.name,
                    "quantity": line.order_line.quantity,
                    "unit": line.product.unit or line.order_line.unit,
                },
            )
            total_quantity += float(line.order_line.quantity)
        return items, preview_items, total_quantity

    async def _resolve_stock_doc_type(
        self,
        context: Any,
        kind: str,
        value: str,
    ) -> dict[str, Any]:
        """解析入库/出库类型：数字视为类型 ID，其余按名称匹配。"""
        token = (value or "").strip()
        if not token:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": [],
            }
        if token.isdigit():
            return {
                "status": "matched",
                "query": token,
                "selected": {"id": token, "name": "类型ID %s" % token, "is_default": False},
                "candidates": [],
            }
        result = await self._api.list_stock_doc_types(context, kind)
        rows = result.data if isinstance(result.data, list) else []
        options = [row for row in rows if isinstance(row, dict)]
        normalized = normalize_name(token)
        exact = [
            option
            for option in options
            if normalized
            in {
                normalize_name(str(option.get("id") or "")),
                normalize_name(str(option.get("name") or "")),
            }
        ]
        if len(exact) == 1:
            return {"status": "matched", "query": token, "selected": exact[0], "candidates": []}
        contains = [
            option
            for option in options
            if normalized in normalize_name(str(option.get("name") or ""))
        ]
        candidates = (contains or options)[:5]
        return {
            "status": "ambiguous" if candidates else "unmatched",
            "query": token,
            "selected": None,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------
    # 内部辅助：退货预览
    # ------------------------------------------------------------------

    @staticmethod
    def _quick_return_items(prefill: dict[str, Any]) -> list[dict[str, Any]]:
        """归一化快捷退货预填明细，保留原数量与单价。"""
        items: list[dict[str, Any]] = []
        for row in prefill.get("items") or []:
            if not isinstance(row, dict):
                continue
            product_id = str(row.get("productId") or "").strip()
            if not product_id:
                continue
            try:
                quantity = float(row.get("quantity") or 0)
                unit_price = float(row.get("unitPrice") or 0)
            except (TypeError, ValueError):
                continue
            items.append(
                {
                    "product_id": product_id,
                    "product_name": str(row.get("productName") or ""),
                    "unit": str(row.get("unit") or ""),
                    "quantity": quantity,
                    "unit_price": unit_price,
                },
            )
        return items

    def _select_return_items(
        self,
        source_items: list[dict[str, Any]],
        items: list[dict[str, Any]] | None,
        doc_label: str,
    ) -> list[dict[str, Any]]:
        """应用用户退货明细覆盖；不传默认整单退货。

        退货数量不能超过源单数量（快捷退货预填已扣除历史退货）。
        """
        if items is None:
            return [dict(item) for item in source_items]
        if not isinstance(items, list) or not items:
            raise DomainError("erp_return_items_invalid", "退货明细不能为空")
        by_product = {item["product_id"]: item for item in source_items}
        selected: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_return_items_invalid", "第%d行退货明细不是 JSON 对象" % index)
            product_id = str(raw.get("product_id") or "").strip()
            if not product_id:
                raise DomainError("erp_return_items_invalid", "第%d行退货明细缺少 product_id" % index)
            source = by_product.get(product_id)
            if source is None:
                available = "、".join(
                    item["product_name"] or item["product_id"] for item in source_items[:5]
                )
                raise DomainError(
                    "erp_return_items_invalid",
                    "商品 %s 不在%s明细中；可退商品：%s" % (product_id, doc_label, available),
                )
            quantity = non_negative_amount(raw.get("quantity"), "退货数量")
            if quantity < 0.0001:
                raise DomainError("erp_return_items_invalid", "退货数量必须大于 0")
            if quantity > source["quantity"] + 0.0001:
                raise DomainError(
                    "erp_return_items_invalid",
                    "商品 %s 退货数量 %.4f 超过可退数量 %.4f"
                    % (source["product_name"] or product_id, quantity, source["quantity"]),
                )
            unit_price = source["unit_price"]
            if raw.get("unit_price") is not None:
                unit_price = non_negative_amount(raw.get("unit_price"), "退货单价")
            selected.append(
                {
                    "product_id": product_id,
                    "product_name": source["product_name"],
                    "unit": source["unit"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                },
            )
        return selected

    @staticmethod
    def _return_total_amount(items: list[dict[str, Any]]) -> Decimal:
        total = Decimal("0")
        for item in items:
            amount = line_amount(item["quantity"], item["unit_price"])
            if amount is not None:
                total += amount
        return total

    async def _resolve_return_money(
        self,
        total_amount: Decimal,
        refund_amount: float | None,
        refund_account_id: str,
        discount_amount: float | None,
        discount_account_id: str,
    ) -> tuple[
        float | None,
        float | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        bool,
    ]:
        """解析退货同时发生的退款与优惠，返回金额、账户与就绪状态。"""
        refund = (
            non_negative_amount(refund_amount, "退款金额")
            if refund_amount is not None
            else None
        )
        discount = (
            non_negative_amount(discount_amount, "优惠金额")
            if discount_amount is not None
            else None
        )
        if refund is not None and discount is not None and refund + discount > float(total_amount) + 0.005:
            raise DomainError(
                "erp_return_money_invalid",
                "退款金额加优惠金额不能超过退货单总额 %.2f" % float(total_amount),
            )
        refund_account: dict[str, Any] | None = None
        discount_account: dict[str, Any] | None = None
        ready = True
        if refund is not None and refund > 0:
            if not refund_account_id.strip():
                raise DomainError(
                    "erp_return_money_invalid",
                    "退款金额大于 0 时必须提供退款账户",
                )
            resolution = await self._resolve_reference(
                "settlement_account", refund_account_id.strip(),
            )
            if resolution["status"] != "matched":
                ready = False
            else:
                refund_account = resolution["selected"]
        if discount is not None and discount > 0:
            if not discount_account_id.strip():
                raise DomainError(
                    "erp_return_money_invalid",
                    "优惠金额大于 0 时必须提供优惠承担账户",
                )
            resolution = await self._resolve_reference(
                "settlement_account", discount_account_id.strip(),
            )
            if resolution["status"] != "matched":
                ready = False
            else:
                discount_account = resolution["selected"]
        return refund, discount, refund_account, discount_account, ready

    def _build_return_preview_summary(
        self,
        *,
        document_kind: str,
        source_order_no: str,
        counterparty_label: str,
        counterparty_name: str,
        warehouse_name: str,
        handler_name: str,
        doc_date: str,
        items: list[dict[str, Any]],
        total_amount: Decimal,
        refund_label: str,
        refund_amount: float | None,
        refund_account: dict[str, Any] | None,
        discount_amount: float | None,
        discount_account: dict[str, Any] | None,
        remark: str,
    ) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "document_kind": document_kind,
            "source_order_no": source_order_no,
            "counterparty_label": counterparty_label,
            counterparty_label: counterparty_name,
            "warehouse": warehouse_name,
            "handler": handler_name,
            "return_date": doc_date,
            "remark": remark,
            "items": [
                {
                    "name": item["product_name"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "unit_price": item["unit_price"],
                }
                for item in items
            ],
            "total_amount": float(total_amount),
        }
        if refund_amount is not None and refund_amount > 0:
            preview[refund_label] = refund_amount
            preview["refund_account"] = str((refund_account or {}).get("name") or "")
        if discount_amount is not None and discount_amount > 0:
            preview["discount_amount"] = discount_amount
            preview["discount_account"] = str((discount_account or {}).get("name") or "")
        return preview

    # ------------------------------------------------------------------
    # 内部辅助：资金单据预览
    # ------------------------------------------------------------------

    async def _resolve_fund_type(
        self,
        direction: str,
        value: str,
        writeoffs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析收付款单款项类型；ERP 要求 typeId 必填。

        未提供时按核销场景自动选择系统类型；既未提供也无法自动选择时
        返回该方向全部候选（系统销售收款类型强制核销，无核销收款必须
        选自定义类型），由模型引导用户补充 fund_type 后重新预览。
        """
        context = self._contexts.get()
        effective = value.strip() or self._auto_fund_type_code(direction, writeoffs)
        snapshot = await self._api.search_fund_types(
            context, _FUND_TYPE_DIRECTION_CODES[direction],
        )
        options = list(snapshot.options)
        if not effective:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": options[:5],
            }
        deduped = self._deduplicate_reference_options(options, effective)
        normalized = normalize_name(effective)
        exact = [
            option
            for option in deduped
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
                "query": effective,
                "selected": exact[0],
                "candidates": [],
            }
        candidates = deduped[:5]
        return {
            "status": "ambiguous" if candidates else "unmatched",
            "query": effective,
            "selected": None,
            "candidates": candidates,
        }

    @staticmethod
    def _auto_fund_type_code(
        direction: str,
        writeoffs: list[dict[str, Any]],
    ) -> str:
        """按核销场景推断系统款项类型编码；无核销时返回空。"""
        biz_types = {item["bizType"] for item in writeoffs}
        if direction == "receipt":
            if "sales_order" in biz_types:
                return "sales"
            if "purchase_return" in biz_types:
                return "purchase_refund"
            return ""
        if "purchase_order" in biz_types:
            return "purchase"
        if "sales_return" in biz_types:
            return "sales_refund"
        return ""

    async def _build_financial_order_preview(
        self,
        *,
        context: Any,
        direction: str,
        amount: float,
        account: str,
        handler: str,
        customer: str,
        supplier: str,
        order_date: str,
        discount_amount: float | None,
        discount_account: str,
        fund_type: str,
        writeoff_details: list[dict[str, Any]] | None,
        remark: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], bool, list[str]]:
        """构建收款/付款单预览：解析账户、往来单位与款项类型并校验核销。"""
        is_receipt = direction == "receipt"
        amount_label = "收款金额" if is_receipt else "付款金额"
        if customer.strip() and supplier.strip():
            raise DomainError(
                "erp_financial_order_counterparty_invalid",
                "客户与供应商只能提供其一",
            )
        effective_date = order_date.strip() or date.today().isoformat()
        self._validate_order_date(effective_date)
        if len(remark.strip()) > 200:
            raise DomainError("erp_financial_order_remark_too_long", "备注最多 200 个字符")
        account_resolution = await self._resolve_reference(
            "settlement_account", account.strip(),
        )
        handler_resolution = await self._resolve_reference("handler", handler.strip())
        references: dict[str, dict[str, Any]] = {
            "account": account_resolution,
            "handler": handler_resolution,
        }
        customer_resolution = None
        supplier_resolution = None
        if customer.strip():
            customer_resolution = await self._resolve_reference("customer", customer.strip())
            references["customer"] = customer_resolution
        if supplier.strip():
            supplier_resolution = await self._resolve_reference("supplier", supplier.strip())
            references["supplier"] = supplier_resolution
        discount = (
            non_negative_amount(discount_amount, "优惠金额")
            if discount_amount is not None
            else None
        )
        discount_resolution = None
        if discount is not None and discount > 0:
            discount_resolution = await self._resolve_reference(
                "settlement_account", discount_account.strip(),
            )
            references["discount_account"] = discount_resolution

        writeoffs = self._normalize_writeoff_details(writeoff_details)
        if writeoffs:
            writeoff_total = sum(item["writeoffAmount"] for item in writeoffs)
            if writeoff_total > amount + 0.005:
                raise DomainError(
                    "erp_writeoff_details_invalid",
                    "核销总额 %.2f 不能超过%s %.2f" % (writeoff_total, amount_label, amount),
                )
            self._validate_writeoff_counterparty(writeoffs, customer_resolution, supplier_resolution)

        fund_type_resolution = await self._resolve_fund_type(direction, fund_type, writeoffs)
        references["fund_type"] = fund_type_resolution

        ready = bool(
            account_resolution["status"] == "matched"
            and handler_resolution["status"] == "matched"
            and (discount_resolution is None or discount_resolution["status"] == "matched")
            and fund_type_resolution["status"] == "matched"
        )
        required_actions: list[str] = []
        if ready:
            required_actions = ["confirm_submit"]
        else:
            for field, resolution in references.items():
                if resolution["status"] == "ambiguous":
                    required_actions.append("select_%s" % field)
                elif resolution["status"] == "unmatched":
                    required_actions.append("replace_%s" % field)
            if fund_type_resolution["status"] == "missing":
                required_actions.append("provide_fund_type")

        counterparty_label = ""
        counterparty_name = ""
        payload: dict[str, Any] = {
            "id": 0,
            "orderDate": effective_date,
            "accountId": "",
            "handlerId": "",
            "%sAmount" % ("receipt" if is_receipt else "payment"): amount,
            "saveType": 2,
        }
        if ready:
            selected_account = account_resolution["selected"]
            payload["accountId"] = str(selected_account["id"])
            payload["handlerId"] = str(handler_resolution["selected"]["id"])
            payload["typeId"] = str(fund_type_resolution["selected"]["id"])
            if customer_resolution is not None and customer_resolution["status"] == "matched":
                payload["counterpartyType"] = "customer"
                payload["customerId"] = str(customer_resolution["selected"]["id"])
                counterparty_label = "客户"
                counterparty_name = str(customer_resolution["selected"].get("name") or "")
            elif supplier_resolution is not None and supplier_resolution["status"] == "matched":
                payload["counterpartyType"] = "supplier"
                payload["supplierId"] = str(supplier_resolution["selected"]["id"])
                counterparty_label = "供应商"
                counterparty_name = str(supplier_resolution["selected"].get("name") or "")
            else:
                payload[
                    "skip%sValidation" % ("Customer" if is_receipt else "Supplier")
                ] = True
            if discount is not None and discount > 0 and discount_resolution is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
            if writeoffs:
                payload["writeoffDetails"] = writeoffs
            if remark.strip():
                payload["remark"] = remark.strip()

        preview: dict[str, Any] = {
            "document_kind": "%s_order" % direction,
            "order_date": effective_date,
            "amount_label": amount_label,
            "amount": amount,
            "fund_type": (
                str(fund_type_resolution["selected"].get("name") or "")
                if fund_type_resolution["selected"] is not None
                else ""
            ),
            "account": (
                str(account_resolution["selected"].get("name") or "")
                if account_resolution["selected"] is not None
                else ""
            ),
            "handler": (
                str(handler_resolution["selected"].get("name") or "")
                if handler_resolution["selected"] is not None
                else ""
            ),
            "remark": remark.strip(),
        }
        if counterparty_label:
            preview["counterparty_label"] = counterparty_label
            preview[counterparty_label] = counterparty_name
        if discount is not None and discount > 0:
            preview["discount_amount"] = discount
            if discount_resolution is not None and discount_resolution["selected"] is not None:
                preview["discount_account"] = str(discount_resolution["selected"].get("name") or "")
        if writeoffs:
            preview["writeoff_details"] = [
                {
                    "biz_type": item["bizType"],
                    "biz_id": item["bizId"],
                    "writeoff_amount": item["writeoffAmount"],
                }
                for item in writeoffs
            ]
        return payload, preview, references, ready, required_actions

    @staticmethod
    def _normalize_writeoff_details(
        writeoff_details: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """清洗核销明细：逐行校验类型、标识与金额合法性。"""
        if writeoff_details is None:
            return []
        if not isinstance(writeoff_details, list):
            raise DomainError("erp_writeoff_details_invalid", "writeoff_details 必须是数组")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(writeoff_details, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_writeoff_details_invalid", "第%d行核销明细不是 JSON 对象" % index)
            biz_type = str(raw.get("biz_type") or "").strip()
            if biz_type not in {"sales_order", "purchase_order", "sales_return", "purchase_return"}:
                raise DomainError(
                    "erp_writeoff_details_invalid",
                    "第%d行核销明细 biz_type 必须是 sales_order、purchase_order、sales_return 或 purchase_return" % index,
                )
            biz_id = str(raw.get("biz_id") or "").strip()
            if not biz_id:
                raise DomainError("erp_writeoff_details_invalid", "第%d行核销明细缺少 biz_id" % index)
            amount = non_negative_amount(raw.get("writeoff_amount"), "核销金额")
            if amount < 0.0001:
                raise DomainError("erp_writeoff_details_invalid", "核销金额必须大于 0")
            result.append(
                {"bizType": biz_type, "bizId": biz_id, "writeoffAmount": amount},
            )
        return result

    @staticmethod
    def _validate_writeoff_counterparty(
        writeoffs: list[dict[str, Any]],
        customer_resolution: dict[str, Any] | None,
        supplier_resolution: dict[str, Any] | None,
    ) -> None:
        """核销销售类单据需客户、采购类单据需供应商已解析。"""
        for item in writeoffs:
            biz_type = item["bizType"]
            needs_customer = biz_type in {"sales_order", "sales_return"}
            needs_supplier = biz_type in {"purchase_order", "purchase_return"}
            resolution = customer_resolution if needs_customer else supplier_resolution
            label = "客户" if needs_customer else "供应商"
            if (needs_customer or needs_supplier) and (
                resolution is None or resolution.get("status") != "matched"
            ):
                raise DomainError(
                    "erp_financial_order_counterparty_required",
                    "核销%s需要先提供%s" % (
                        {"sales_order": "销售单", "sales_return": "销售退货单",
                         "purchase_order": "采购单", "purchase_return": "采购退货单"}[biz_type],
                        label,
                    ),
                )

    async def _financial_order_params(
        self,
        context: Any,
        start_date: str,
        end_date: str,
        status: int | None,
        order_no: str,
        counterparty_id: str,
        account_id: str,
        *,
        customer_field: str,
        supplier_field: str,
    ) -> dict[str, Any]:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, Any] = {}
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if order_no.strip():
            params["orderNo"] = order_no.strip()
        if counterparty_id.strip():
            customer = await self._resolve_reference("customer", counterparty_id.strip())
            if customer["status"] == "matched" and customer["selected"] is not None:
                params[customer_field] = str(customer["selected"]["id"])
            else:
                supplier = await self._resolve_reference("supplier", counterparty_id.strip())
                if supplier["status"] == "matched" and supplier["selected"] is not None:
                    params[supplier_field] = str(supplier["selected"]["id"])
                else:
                    raise DomainError(
                        "erp_reference_unmatched",
                        "未找到与“%s”匹配的客户或供应商" % counterparty_id.strip(),
                    )
        if account_id.strip():
            account = await self._resolve_reference("settlement_account", account_id.strip())
            if account["status"] == "matched" and account["selected"] is not None:
                params["accountId"] = str(account["selected"]["id"])
            else:
                raise DomainError(
                    "erp_reference_unmatched",
                    "未找到与“%s”匹配的结算账户" % account_id.strip(),
                )
        return params

    # ------------------------------------------------------------------
    # 内部辅助：作废与列表
    # ------------------------------------------------------------------

    async def _void_document_flow(
        self,
        kind: str,
        doc_label: str,
        document_id: str,
        confirmed_by_user: bool,
        void: Callable[[str], Any],
    ) -> str:
        """作废单据通用流程：确认、回查编号、执行、回显编号。"""
        if confirmed_by_user is not True:
            raise DomainError(
                "erp_document_confirmation_required",
                "必须先向用户展示%s详情并取得明确确认" % doc_label,
            )
        token = document_id.strip()
        if not token:
            raise DomainError("erp_%s_id_invalid" % kind, "%s ID 不能为空" % doc_label)
        document_no = await self._lookup_document_no(kind, token)
        await void(token)
        return document_no or token

    async def _void_financial_flow(
        self,
        kind: str,
        doc_label: str,
        order_id: str,
        confirmed_by_user: bool,
    ) -> str:
        context = self._contexts.get()
        if confirmed_by_user is not True:
            raise DomainError(
                "erp_document_confirmation_required",
                "必须先向用户展示%s详情并取得明确确认" % doc_label,
            )
        token = order_id.strip()
        if not token:
            raise DomainError("erp_financial_order_id_invalid", "%s ID 不能为空" % doc_label)
        document_no = ""
        try:
            detail = await self._api.get_financial_order_detail(context, kind, token)
            document_no = str(detail.document.get("orderNo") or "").strip()
        except DomainError:
            document_no = ""
        await self._api.void_financial_order(context, kind, token)
        return document_no or token

    def _document_page_response(self, result: Any) -> dict[str, Any]:
        return self.ok_response(
            page=result.page_num,
            page_size=result.page_size,
            total=result.total,
            has_more=(result.page_num * result.page_size) < result.total,
            documents=list(result.rows),
        )

    def _build_purchase_modify_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清洗采购单修改明细：必填 product_id、quantity、unit_price。"""
        if not items:
            raise DomainError("erp_purchase_order_items_empty", "商品明细不能为空")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细不是 JSON 对象" % index)
            product_id = str(raw.get("product_id") or raw.get("productId") or "").strip()
            if not product_id:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 product_id" % index)
            quantity = raw.get("quantity")
            if quantity is None:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 quantity" % index)
            item: dict[str, Any] = {
                "productId": product_id,
                "quantity": non_negative_amount(quantity, "采购数量"),
            }
            if item["quantity"] < 0.0001:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细数量必须大于 0" % index)
            if raw.get("unit_price") is None and raw.get("unitPrice") is None:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 unit_price" % index)
            item["unitPrice"] = non_negative_amount(
                raw.get("unit_price") if raw.get("unit_price") is not None else raw.get("unitPrice"),
                "采购单价",
            )
            for camel_key, snake_key in (
                ("unit", "unit"),
                ("remark", "remark"),
                ("orderItemId", "order_item_id"),
            ):
                value = raw.get(snake_key)
                if value not in (None, ""):
                    item[camel_key] = value
            result.append(item)
        return result


def build_document_tools(host: DocumentTools) -> list[SessionFunctionTool]:
    """把采购、退货、库存与资金单据工具注册为 MCP 工具。"""
    return [
        # 采购单
        SessionFunctionTool(
            host.preview_purchase_order,
            is_read_only=True,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_order,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_purchase_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_purchase_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_PURCHASE_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_purchase_order,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.update_purchase_order,
            output_schema=_DOCUMENT_UPDATE_OUTPUT_SCHEMA,
            input_schema_override=_UPDATE_PURCHASE_ORDER_INPUT_SCHEMA,
        ),
        # 采购退货单
        SessionFunctionTool(
            host.preview_purchase_return,
            is_read_only=True,
            output_schema=_RETURN_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_RETURN_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_return,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_purchase_return,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_purchase_returns,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_PURCHASE_RETURNS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_purchase_return,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 销售退货单
        SessionFunctionTool(
            host.preview_sales_return,
            is_read_only=True,
            output_schema=_RETURN_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_SALES_RETURN_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_sales_return,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_sales_return,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_sales_returns,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_SALES_RETURNS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_sales_return,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 销售单继续收款 / 采购单继续付款
        SessionFunctionTool(
            host.preview_sales_receipt,
            is_read_only=True,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_SALES_RECEIPT_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_sales_receipt,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.preview_purchase_payment,
            is_read_only=True,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_PAYMENT_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_payment,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        # 库存调拨与其他出入库
        SessionFunctionTool(
            host.preview_stock_transfer,
            is_read_only=True,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_STOCK_TRANSFER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_stock_transfer,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.preview_other_stock_doc,
            is_read_only=True,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_OTHER_STOCK_DOC_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_other_stock_doc,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        # 收款单
        SessionFunctionTool(
            host.preview_receipt_order,
            is_read_only=True,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_RECEIPT_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_receipt_order,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_receipt_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_receipt_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_receipt_order,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 付款单
        SessionFunctionTool(
            host.preview_payment_order,
            is_read_only=True,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PAYMENT_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_payment_order,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_payment_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_payment_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_payment_order,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
    ]
