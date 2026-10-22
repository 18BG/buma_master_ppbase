"""Realtime rules must use the subscriber, never the concurrent publisher."""

import asyncio
from types import SimpleNamespace

from ppbase.services import realtime_service, record_service

from app.auth import realtime
from app.auth.context import AUTH_RULE_CONTEXT


async def test_concurrent_subscribers_have_independent_live_permissions(monkeypatch):
    realtime.install_realtime_context_patch()

    async def live(engine, auth):
        await asyncio.sleep(0)
        return {
            "id": auth["id"],
            "created": "2026-01-01",
            "role.permission.customer.read": auth["id"] == "allowed",
        }

    async def check(engine, collection, rid, rule, context):
        await asyncio.sleep(0)
        return context["auth"]["role.permission.customer.read"]

    monkeypatch.setattr(realtime, "load_auth_record_context", live)
    monkeypatch.setattr(record_service, "check_record_rule", check)
    publisher = AUTH_RULE_CONTEXT.set({"role.permission.customer.read": True})
    try:

        async def allowed(identifier):
            subscription = SimpleNamespace(
                auth_payload={"type": "auth", "id": identifier},
                base_topic="buma_customer/record",
                options_query={},
                options_headers={},
            )
            return await realtime_service._is_subscription_allowed_for_event(
                engine=object(),
                collection=SimpleNamespace(
                    name="buma_customer",
                    view_rule="@request.auth.role.permission.customer.read = true",
                ),
                subscription=subscription,
                record_id="record",
                action="update",
            )

        assert await asyncio.gather(allowed("denied"), allowed("allowed")) == [False, True]
        assert realtime._SUBSCRIBER_CONTEXT.get() is None
    finally:
        AUTH_RULE_CONTEXT.reset(publisher)
