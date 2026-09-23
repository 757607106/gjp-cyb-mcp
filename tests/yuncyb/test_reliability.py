"""验证异常提交、目录生命周期与升级后的工具行为。"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from yuncyb.adapters import BusinessAuthenticatedJsonClient
from yuncyb.app import ApiKeyIdentityResolver, SessionCredentialStore
from yuncyb.catalog_state import TenantCatalogState
from yuncyb.toolset import YunCybToolSet
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError
from tests.yuncyb.test_yuncyb import CompleteSalesOrderApi, _yuncyb_toolset, _session


def _prepared(tmp_path, api):
    session = _session(tmp_path, [{"id": "1", "name": "土豆", "unit": "斤", "salesPrice": 3.5}])
    toolset = _yuncyb_toolset(session, api)
    preview_id = session.store_prepared_document(
        "sales_order", {"items": [{"productId": "1", "quantity": 2}]}, {"save_type": "final"},
    )
    return toolset, preview_id


@pytest.mark.parametrize("code", ["business_write_result_unknown", "erp_live_response_invalid"])
def test_uncertain_submission_cannot_be_replayed_with_either_key(tmp_path, code):
    class Api(CompleteSalesOrderApi):
        async def create_sales_order(self, context, payload):
            self.created_payloads.append(payload)
            raise DomainError(code, "响应丢失")

    async def scenario():
        api = Api()
        tools, preview = _prepared(tmp_path, api)
        for key in ("same", "same", "different"):
            result = await tools.submit_sales_order(preview, key, True)
            assert result["error"]["code"] == "erp_document_result_unknown"
        assert len(api.created_payloads) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_during_detail", [False, True])
def test_cancel_does_not_allow_duplicate_create(tmp_path, cancel_during_detail):
    async def scenario():
        entered = asyncio.Event()

        class Api(CompleteSalesOrderApi):
            async def create_sales_order(self, context, payload):
                result = await super().create_sales_order(context, payload)
                if not cancel_during_detail:
                    entered.set()
                    await asyncio.Event().wait()
                return result

            async def get_sales_order_detail(self, context, order_id):
                entered.set()
                await asyncio.Event().wait()

        api = Api()
        tools, preview = _prepared(tmp_path, api)
        task = asyncio.create_task(tools.submit_sales_order(preview, "key", True))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replay = await tools.submit_sales_order(preview, "key", True)
        assert len(api.created_payloads) == 1
        if cancel_during_detail:
            assert replay["submitted"] and replay["idempotent_replay"]
        else:
            assert replay["error"]["code"] == "erp_document_result_unknown"

    asyncio.run(scenario())


def test_definite_rejection_can_retry_same_preview(tmp_path):
    class Api(CompleteSalesOrderApi):
        async def create_sales_order(self, context, payload):
            if not self.created_payloads:
                self.created_payloads.append(payload)
                raise DomainError("erp_live_request_failed", "库存不足")
            return await super().create_sales_order(context, payload)

    async def scenario():
        tools, preview = _prepared(tmp_path, Api())
        assert not (await tools.submit_sales_order(preview, "key", True))["ok"]
        assert (await tools.submit_sales_order(preview, "key", True))["submitted"]

    asyncio.run(scenario())


def test_concurrent_cold_load_fetches_once():
    async def scenario():
        state = TenantCatalogState(60, {}, {})
        count = 0

        async def load():
            nonlocal count
            count += 1
            await asyncio.sleep(0)
            return [{"id": "1", "name": "土豆"}]

        await asyncio.gather(*(state.ensure(load) for _ in range(5)))
        assert count == 1

    asyncio.run(scenario())


def test_close_cancels_refresh_and_keeps_old_catalog():
    async def scenario():
        state = TenantCatalogState(60, {}, {})

        async def initial():
            return [{"id": "1", "name": "土豆"}]

        await state.refresh(initial)
        state._expires_at = 0
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await state.ensure(blocked)
        await entered.wait()
        await state.close()
        assert cancelled.is_set()
        assert not state._background_tasks
        assert state.catalog.products[0].name == "土豆"

    asyncio.run(scenario())


def test_api_key_identity_is_stable_isolated_and_credential_free():
    store = SessionCredentialStore()
    resolver = ApiKeyIdentityResolver(store)

    def resolve(key):
        return resolver.resolve(SimpleNamespace(request=SimpleNamespace(headers={"x-api-key": key})))

    first = resolve("synthetic-secret-one")
    assert first == resolve("synthetic-secret-one")
    assert first.tenant_id != resolve("synthetic-secret-two").tenant_id
    assert "synthetic-secret-one" not in repr(first)
    assert store.resolve(first).value == "synthetic-secret-one"


@pytest.mark.parametrize("quantity", [0, -1, 0.00001, float("inf"), float("nan")])
def test_modify_rejects_invalid_quantity(quantity):
    with pytest.raises(DomainError):
        YunCybToolSet._build_modify_items([{"product_id": "1", "quantity": quantity}])


@pytest.mark.parametrize("price", [-1, float("inf"), float("nan")])
def test_modify_rejects_invalid_price(price):
    with pytest.raises(DomainError):
        YunCybToolSet._build_modify_items([{"product_id": "1", "quantity": 1, "unit_price": price}])


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_http_write_failure_is_unknown(method, failure):
    async def scenario():
        credentials = SimpleNamespace(resolve=lambda ctx: SimpleNamespace(kind="api_key", value="synthetic"))
        client = BusinessAuthenticatedJsonClient("https://example.com", credentials)

        async def respond(request):
            if failure == "timeout":
                raise httpx.ReadTimeout("响应丢失", request=request)
            return httpx.Response(500, request=request)

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(DomainError) as error:
                await client._request_json(InvocationContext("t", "u", "a"), method, "/sales/orders")
            if method != "GET":
                assert error.value.code == "business_write_result_unknown"
            else:
                assert error.value.code in {"business_upstream_unavailable", "erp_live_request_failed"}
        finally:
            await client.close()

    asyncio.run(scenario())
