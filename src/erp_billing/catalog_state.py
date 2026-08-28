"""租户级共享商品目录状态：TTL 缓存、并发去重与过期后台刷新。

商品目录是租户级数据而非会话级数据：会话 ToolSet 被 TTL 淘汰或新对话
建立时目录保持可用，避免每个新会话都串行翻页同步一遍拖垮首次开单。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from .catalog import ProductCatalog

logger = logging.getLogger(__name__)

# 后台刷新失败后的重试间隔：防止上游持续故障时每个请求都触发拉取
_RETRY_DELAY_SECONDS = 60.0

CatalogLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


class TenantCatalogState:
    """同一租户全部会话共享的商品目录缓存。

    首次加载必须等待完成；之后目录过期由后台任务刷新
    （stale-while-revalidate），读请求继续使用旧目录，不被同步耗时阻塞。
    loader 由调用方构建并捕获当前调用上下文，后台刷新复用同一份鉴权。
    """

    def __init__(
        self,
        ttl_seconds: float,
        aliases: dict[str, str],
        categories: dict[str, list[str]],
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("目录 TTL 必须大于 0")
        self.aliases = dict(aliases)
        self.categories = dict(categories)
        self._ttl_seconds = float(ttl_seconds)
        self._lock = asyncio.Lock()
        self._catalog: ProductCatalog | None = None
        self._version = ""
        self._expires_at = 0.0
        self._retry_not_before = 0.0
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def catalog(self) -> ProductCatalog | None:
        return self._catalog

    @property
    def version(self) -> str:
        return self._version

    async def refresh(self, loader: CatalogLoader) -> str:
        """显式全量同步并等待完成；并发调用在锁上串行执行。

        显式同步代表用户主动要求刷新，因此不做新鲜度短路。
        """
        async with self._lock:
            return self._install(await loader())

    async def ensure(self, loader: CatalogLoader) -> ProductCatalog | None:
        """保证目录可用：无目录时等待首次加载；过期时后台刷新并返回旧目录。"""
        if self._catalog is None:
            await self.refresh(loader)
            return self._catalog
        now = time.monotonic()
        if now < self._expires_at or now < self._retry_not_before:
            return self._catalog
        task = asyncio.create_task(self._refresh_in_background(loader))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return self._catalog

    async def _refresh_in_background(self, loader: CatalogLoader) -> None:
        try:
            async with self._lock:
                # 拿到锁时可能已有并发刷新完成，跳过重复拉取
                if time.monotonic() < self._expires_at:
                    return
                self._install(await loader())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._retry_not_before = time.monotonic() + _RETRY_DELAY_SECONDS
            logger.warning(
                "商品目录后台刷新失败，%.0f 秒后重试：%s",
                _RETRY_DELAY_SECONDS,
                exc,
            )

    def _install(self, rows: Iterable[dict[str, Any]]) -> str:
        self._catalog = ProductCatalog.from_product_rows(
            rows,
            self.aliases,
            self.categories,
        )
        self._version = datetime.now(timezone.utc).isoformat()
        self._expires_at = time.monotonic() + self._ttl_seconds
        return self._version
