"""图片订单识别模块的纯逻辑测试：媒体类型、JSON 解析与订单文本转换。"""

import asyncio
from pathlib import Path

import pytest

from gjp_cli.image_order import (
    image_media_type,
    maybe_order_text_from_image_input,
    order_text_from_ocr,
    parse_ocr_result,
)
from gjp_common.errors import DomainError


def test_image_media_type_by_suffix():
    assert image_media_type(Path("order.jpg")) == "image/jpeg"
    assert image_media_type(Path("order.JPEG")) == "image/jpeg"
    assert image_media_type(Path("order.png")) == "image/png"


def test_image_media_type_rejects_unknown_suffix():
    with pytest.raises(DomainError, match="不支持的图片格式"):
        image_media_type(Path("order.gif"))


def test_parse_ocr_result_accepts_plain_json():
    result = parse_ocr_result('{"items": [{"name": "黄瓜", "quantity": "3", "unit": "斤"}]}')

    assert result == {"items": [{"name": "黄瓜", "quantity": "3", "unit": "斤"}]}


def test_parse_ocr_result_strips_markdown_fence():
    raw = '```json\n{"items": [{"name": "牛肉", "quantity": "20", "unit": "斤"}]}\n```'

    result = parse_ocr_result(raw)

    assert result["items"][0]["name"] == "牛肉"


def test_parse_ocr_result_rejects_non_json_output():
    with pytest.raises(DomainError, match="不是合法 JSON"):
        parse_ocr_result("识别到黄瓜三斤")


def test_parse_ocr_result_rejects_missing_items():
    with pytest.raises(DomainError, match="items"):
        parse_ocr_result('{"products": []}')


def test_order_text_from_ocr_builds_line_per_product():
    text = order_text_from_ocr(
        {
            "items": [
                {"name": "黄瓜", "quantity": "3", "unit": "斤"},
                {"name": "牛肉", "quantity": "20", "unit": "斤"},
                {"name": "可辨识内容", "quantity": "", "unit": "", "status": "待确认"},
            ],
        },
    )

    assert text == "黄瓜 3斤\n牛肉 20斤\n可辨识内容 待确认"


def test_order_text_from_ocr_rejects_empty_items():
    with pytest.raises(DomainError, match="未识别到有效下单内容"):
        order_text_from_ocr({"items": []})


def test_maybe_order_text_passes_through_plain_order_text():
    assert asyncio.run(maybe_order_text_from_image_input("黄瓜 3斤，牛肉 20斤")) is None
    assert asyncio.run(maybe_order_text_from_image_input("黄瓜 3斤\n牛肉 20斤")) is None


def test_maybe_order_text_detects_image_path_and_reports_missing_file(tmp_path):
    with pytest.raises(DomainError, match="图片不存在"):
        asyncio.run(maybe_order_text_from_image_input(str(tmp_path / "missing-order.jpg")))
