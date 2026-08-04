"""开单产品运行时装配。"""

from __future__ import annotations

from dataclasses import dataclass

from gjp_common.context import InvocationContext, InvocationContextStore
from .ports import BillingApiPort
from .session import ErpBillingSession
from .toolset import BillingToolSet


@dataclass(frozen=True)
class BillingRuntime:
    """单个已认证开单会话的运行时。"""

    session: ErpBillingSession
    contexts: InvocationContextStore
    toolset: BillingToolSet


def create_billing_runtime(
    session: ErpBillingSession,
    api: BillingApiPort,
    context: InvocationContext,
) -> BillingRuntime:
    """装配一个账号隔离的开单能力运行时。"""
    contexts = InvocationContextStore(default=context)
    return BillingRuntime(
        session=session,
        contexts=contexts,
        toolset=BillingToolSet(session, api, contexts),
    )
