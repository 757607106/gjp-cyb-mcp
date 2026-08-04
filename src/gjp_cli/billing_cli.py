"""ERP 开单命令入口：会话模式、图片识别或一次性输出草稿 JSON。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from erp_billing.adapters import create_match_logger_from_env
from erp_billing.config import ErpBillingSettings
from erp_billing.session import ErpBillingSession
from gjp_common.errors import DomainError
from gjp_common.paths import resolve_input_path


def run_billing_cli(args: argparse.Namespace) -> int:
    image = str(getattr(args, "image", "") or "").strip()
    has_direct_input = bool(args.text or args.input_file or image)
    if not has_direct_input:
        from .interactive import build_agent_console

        return build_agent_console(agent_key="erp-billing").run()

    settings = ErpBillingSettings.from_env()
    session = ErpBillingSession.from_settings(
        settings,
        allow_missing_catalog=True,
        match_logger=create_match_logger_from_env(),
    )
    if image:
        text = _order_text_from_image(image)
    else:
        text = args.text or _read_order_file(args.input_file)
    draft = session.create_draft_from_text(
        text,
        customer=args.customer,
        warehouse=args.warehouse,
    )
    print(
        json.dumps(
            draft.billing_products_payload(),
            ensure_ascii=False,
            indent=2,
        ),
    )
    return 0


def _order_text_from_image(image: str) -> str:
    """调用多模态模型识别下单图片；识别 JSON 输出到 stderr，stdout 只保留草稿。"""
    from .image_order import order_text_from_ocr, recognize_order_image

    result = asyncio.run(recognize_order_image(image))
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
    return order_text_from_ocr(result)


def _read_order_file(value: str) -> str:
    path = resolve_input_path(value)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DomainError(
            "ERP_INPUT_FILE_NOT_FOUND",
            "文件不存在：%s" % path,
        ) from exc
    except UnicodeDecodeError as exc:
        raise DomainError(
            "ERP_INPUT_FILE_INVALID",
            "文件不是 UTF-8 文本：%s" % path,
        ) from exc
