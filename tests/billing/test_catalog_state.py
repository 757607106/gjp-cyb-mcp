"""租户共享商品目录状态：TTL、并发去重、后台刷新与会话目录视图。"""

from __future__ import annotations

import asyncio
import time

import pytest

from erp_billing import catalog_state as catalog_state_module
from erp_billing.catalog_state import TenantCatalogState
from erp_billing.config import ErpBillingSettings
from erp_billing.session import ErpBillingSession


def _row(product_id: str, name: str) -> dict:
    return {"productId": product_id, "name": name, "unit": "斤"}


def _settings() -> ErpBillingSettings:
    return ErpBillingSettings(
        product_catalog_path=None,
        alias_path=None,
        recommendation_score=0.60,
        use_default_fresh_aliases=False,
        category_path=None,
        use_default_categories=False,
    )


def _loader(rows, calls=None):
    async def loader():
        if calls is not None:
            calls.append(time.monotonic())
        return [dict(row) for row in rows]

    return loader


def _state(ttl=600.0, aliases=None):
    return TenantCatalogState(
        ttl_seconds=ttl,
        aliases=aliases or {},
        categories={},
    )


def _shared_session(state):
    return ErpBillingSession.from_settings(
        _settings(),
        allow_missing_catalog=True,
        catalog_state=state,
    )


def test_refresh_installs_catalog_with_version():
    state = _state()

    version = asyncio.run(state.refresh(_loader([_row("P001", "土豆")])))

    assert version == state.version
    assert [item.name for item in state.catalog.products] == ["土豆"]


def test_ensure_without_catalog_waits_for_first_load():
    state = _state()

    catalog = asyncio.run(state.ensure(_loader([_row("P001", "土豆")])))

    assert catalog is state.catalog
    assert [item.name for item in catalog.products] == ["土豆"]


def test_ensure_fresh_catalog_skips_reload():
    state = _state()
    asyncio.run(state.refresh(_loader([_row("P001", "土豆")])))

    calls = []
    catalog = asyncio.run(state.ensure(_loader([_row("P002", "苹果")], calls)))

    assert calls == []
    assert [item.name for item in catalog.products] == ["土豆"]


def test_stale_catalog_serves_old_and_refreshes_in_background():
    async def _scenario() -> TenantCatalogState:
        state = _state(ttl=0.05)
        await state.refresh(_loader([_row("P001", "土豆")]))
        await asyncio.sleep(0.06)
        returned = await state.ensure(_loader([_row("P002", "苹果")]))
        # 过期目录仍立即可用，读请求不被同步阻塞
        assert [item.name for item in returned.products] == ["土豆"]
        for _ in range(200):
            if state.catalog.products[0].name == "苹果":
                return state
            await asyncio.sleep(0.01)
        raise AssertionError("后台刷新未在预期时间内完成")

    state = asyncio.run(_scenario())
    assert [item.name for item in state.catalog.products] == ["苹果"]


def test_concurrent_ensure_triggers_single_background_fetch():
    async def _scenario() -> None:
        state = _state(ttl=0.05)
        await state.refresh(_loader([_row("P001", "土豆")]))
        await asyncio.sleep(0.06)
        calls = []
        await asyncio.gather(
            state.ensure(_loader([_row("P002", "苹果")], calls)),
            state.ensure(_loader([_row("P002", "苹果")], calls)),
        )
        while state._background_tasks:
            await asyncio.sleep(0.01)
        assert len(calls) == 1
        assert [item.name for item in state.catalog.products] == ["苹果"]

    asyncio.run(_scenario())


def test_background_refresh_failure_sets_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(catalog_state_module, "_RETRY_DELAY_SECONDS", 0.2)

    async def _scenario() -> None:
        state = _state(ttl=0.05)
        await state.refresh(_loader([_row("P001", "土豆")]))
        await asyncio.sleep(0.06)

        async def failing_loader():
            raise RuntimeError("upstream down")

        returned = await state.ensure(failing_loader)
        assert [item.name for item in returned.products] == ["土豆"]
        await asyncio.sleep(0.05)
        # 退避窗口内不触发新的后台拉取
        retry_calls = []
        await state.ensure(_loader([_row("P002", "苹果")], retry_calls))
        await asyncio.sleep(0.05)
        assert retry_calls == []
        assert [item.name for item in state.catalog.products] == ["土豆"]
        # 退避窗口过后恢复后台刷新
        await asyncio.sleep(0.2)
        await state.ensure(_loader([_row("P002", "苹果")]))
        for _ in range(200):
            if state.catalog.products[0].name == "苹果":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("退避结束后后台刷新未恢复")

    asyncio.run(_scenario())


def test_state_rejects_non_positive_ttl():
    with pytest.raises(ValueError):
        TenantCatalogState(ttl_seconds=0, aliases={}, categories={})


def test_shared_session_catalog_follows_state_and_matcher_rebuilds():
    async def _scenario() -> None:
        state = _state(aliases={"洋芋": "土豆"})
        session = _shared_session(state)
        assert session.catalog is None
        await session.sync_catalog(_loader([_row("P001", "土豆")]))
        assert session.catalog is state.catalog
        assert session.matcher.catalog is state.catalog
        draft = session.create_draft_from_text("洋芋2斤")
        assert draft.lines[0].status == "matched"
        # 共享目录刷新产生新实例后，匹配器自动跟随
        await state.refresh(_loader([_row("P002", "苹果")]))
        assert session.catalog is state.catalog
        assert session.matcher.catalog is state.catalog
        draft = session.create_draft_from_text("苹果3斤")
        assert draft.lines[0].status == "matched"

    asyncio.run(_scenario())


def test_new_session_sees_shared_catalog_without_syncing():
    async def _scenario() -> None:
        state = _state()
        first = _shared_session(state)
        await first.sync_catalog(
            _loader([_row("P001", "土豆"), _row("P002", "苹果")]),
        )
        # 新会话（模拟新对话或 ToolSet 被淘汰后重建）直接看到共享目录
        second = _shared_session(state)
        assert [item.name for item in second.catalog.products] == ["土豆", "苹果"]
        draft = second.create_draft_from_text("土豆2斤")
        assert draft.lines[0].status == "matched"

    asyncio.run(_scenario())


def test_partial_sync_scopes_to_session_and_full_sync_clears_override():
    async def _scenario() -> None:
        state = _state()
        first = _shared_session(state)
        second = _shared_session(state)
        await first.sync_catalog(
            _loader([_row("P001", "土豆"), _row("P002", "苹果")]),
        )
        # limit 截断同步只作用于当前会话
        await second.sync_catalog_partial(_loader([_row("P001", "土豆")]))
        assert [item.name for item in second.catalog.products] == ["土豆"]
        assert [item.name for item in first.catalog.products] == ["土豆", "苹果"]
        # 全量同步成功后清除覆盖，恢复共享视图
        await second.sync_catalog(
            _loader(
                [
                    _row("P001", "土豆"),
                    _row("P002", "苹果"),
                    _row("P003", "香蕉"),
                ],
            ),
        )
        assert [item.name for item in second.catalog.products] == [
            "土豆",
            "苹果",
            "香蕉",
        ]
        assert [item.name for item in first.catalog.products] == [
            "土豆",
            "苹果",
            "香蕉",
        ]

    asyncio.run(_scenario())


def test_shared_session_ensure_uses_state_swr():
    async def _scenario() -> None:
        state = _state(ttl=0.05)
        session = _shared_session(state)
        await session.sync_catalog(_loader([_row("P001", "土豆")]))
        await asyncio.sleep(0.06)
        # 过期后 ensure 不阻塞且旧目录可用，后台完成更新
        await session.ensure_catalog(_loader([_row("P002", "苹果")]))
        assert [item.name for item in session.catalog.products] == ["土豆"]
        for _ in range(200):
            if session.catalog.products[0].name == "苹果":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("共享目录后台刷新未生效")

    asyncio.run(_scenario())


def test_local_session_sync_and_ensure_keep_existing_behavior():
    async def _scenario() -> None:
        session = ErpBillingSession.from_settings(
            _settings(),
            allow_missing_catalog=True,
        )
        assert session.catalog_state is None
        # 空目录时 ensure 触发同步
        await session.ensure_catalog(_loader([_row("P001", "土豆")]))
        assert [item.name for item in session.catalog.products] == ["土豆"]
        # 已有目录时 ensure 不重复拉取
        calls = []
        await session.ensure_catalog(_loader([_row("P002", "苹果")], calls))
        assert calls == []
        # 截断同步替换本地目录
        await session.sync_catalog_partial(_loader([_row("P002", "苹果")]))
        assert [item.name for item in session.catalog.products] == ["苹果"]

    asyncio.run(_scenario())
