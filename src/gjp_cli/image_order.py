"""图片订单识别：调用 LLM_VISION_* 多模态模型把下单图片转为结构化订单。

仅供 gjp_cli 本地验证链路使用；OCR 提示词属于业务资产，统一维护在
erp_billing.prompt.ERP_BILLING_OCR_PROMPT，最终输出订单 JSON。
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Optional

from agentscope.message import Base64Source, DataBlock, SystemMsg, UserMsg

from erp_billing.prompt import ERP_BILLING_OCR_PROMPT
from gjp_common.errors import DomainError
from gjp_common.paths import resolve_input_path
from .model_runtime import LLMSettings, build_chat_model


logger = logging.getLogger(__name__)

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def image_media_type(path: Path) -> str:
    """根据扩展名判定图片媒体类型；不支持的格式直接报错。"""
    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.casefold())
    if media_type is None:
        raise DomainError(
            "ERP_IMAGE_UNSUPPORTED",
            "不支持的图片格式：%s；可选：%s"
            % (path.suffix or path.name, "、".join(sorted(_IMAGE_MEDIA_TYPES))),
        )
    return media_type


def load_image_block(value: str) -> DataBlock:
    """读取本地图片文件并封装为 AgentScope 多模态 DataBlock。"""
    path = resolve_input_path(value)
    media_type = image_media_type(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise DomainError("ERP_IMAGE_NOT_FOUND", "图片不存在：%s" % path) from exc
    except OSError as exc:
        raise DomainError("ERP_IMAGE_INVALID", "图片读取失败：%s" % path) from exc
    return DataBlock(
        source=Base64Source(
            data=base64.b64encode(raw).decode("ascii"),
            media_type=media_type,
        ),
    )


def _strip_code_fence(raw_text: str) -> str:
    """去除模型可能附加的 Markdown 代码块围栏，只保留 JSON 正文。"""
    text = (raw_text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_ocr_result(raw_text: str) -> dict[str, Any]:
    """解析视觉模型输出的订单 JSON，并校验 items 结构。"""
    text = _strip_code_fence(raw_text)
    if not text:
        raise DomainError("ERP_IMAGE_OCR_INVALID", "视觉模型没有返回识别内容")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainError(
            "ERP_IMAGE_OCR_INVALID",
            "视觉模型输出不是合法 JSON：%s" % text[:200],
        ) from exc
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise DomainError(
            "ERP_IMAGE_OCR_INVALID",
            "视觉模型输出缺少 items 数组：%s" % text[:200],
        )
    return {"items": items}


def order_text_from_ocr(result: dict[str, Any]) -> str:
    """把识别出的订单 JSON 规整为分行订单文本（每行：名称 数量单位）。"""
    lines: list[str] = []
    for item in result.get("items", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        quantity = str(item.get("quantity", "")).strip()
        unit = str(item.get("unit", "")).strip()
        parts = [name]
        if quantity or unit:
            parts.append(quantity + unit)
        if str(item.get("status", "")).strip() == "待确认":
            parts.append("待确认")
        lines.append(" ".join(parts))
    if not lines:
        raise DomainError("ERP_IMAGE_ORDER_EMPTY", "图片中未识别到有效下单内容")
    return "\n".join(lines)


async def recognize_order_image(value: str) -> dict[str, Any]:
    """识别下单图片，返回 {"items": [...]} 结构的订单 JSON。

    在调用方的事件循环内直接执行，避免临时事件循环导致模型 HTTP
    连接在循环关闭后无法清理（Event loop is closed）。
    """
    image_block = load_image_block(value)
    settings = LLMSettings.vision_from_env()
    try:
        raw_text = await _call_vision_model(image_block, settings)
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("ERP_IMAGE_OCR_FAILED", "视觉模型调用失败：%s" % exc) from exc
    logger.info(
        "图片订单识别完成 provider=%s model=%s output_chars=%d",
        settings.provider,
        settings.model_name,
        len(raw_text),
    )
    return parse_ocr_result(raw_text)


async def maybe_order_text_from_image_input(text: str) -> Optional[str]:
    """整行输入是图片路径时识别为订单文本；否则返回 None 按普通文本处理。"""
    candidate = text.strip().strip("'\"")
    if not candidate or "\n" in candidate:
        return None
    if Path(candidate).suffix.casefold() not in _IMAGE_MEDIA_TYPES:
        return None
    return order_text_from_ocr(await recognize_order_image(candidate))


async def _call_vision_model(image_block: DataBlock, settings: LLMSettings) -> str:
    """携带 OCR 提示词与图片调用多模态模型，返回文本输出。"""
    model = build_chat_model(settings)
    response = await model(
        [
            SystemMsg("system", ERP_BILLING_OCR_PROMPT),
            UserMsg("user", [image_block]),
        ],
    )
    if inspect.isasyncgen(response):
        last = None
        async for chunk in response:
            last = chunk
        response = last
    if response is None:
        raise DomainError("ERP_IMAGE_OCR_INVALID", "视觉模型没有返回识别内容")
    texts = [
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    ]
    return "\n".join(texts).strip()
