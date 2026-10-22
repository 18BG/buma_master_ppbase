"""Apply BUMA live role relations to each realtime subscriber's rule context."""

from contextvars import ContextVar
from functools import wraps

from app.auth.context import is_record_auth, load_auth_record_context

_SUBSCRIBER_CONTEXT = ContextVar("buma_realtime_subscriber_context", default=None)
_INSTALLED = False


def install_realtime_context_patch():
    global _INSTALLED
    if _INSTALLED:
        return
    from ppbase.services import realtime_service

    original_context = realtime_service._build_rule_context

    def context(auth):
        rule, request = original_context(auth)
        live = _SUBSCRIBER_CONTEXT.get()
        if live is not None:
            request["auth"].update(live)
        return rule, request

    def with_subscriber_context(operation, missing):
        @wraps(operation)
        async def wrapped(*args, **kwargs):
            auth = kwargs["subscription"].auth_payload
            live = await load_auth_record_context(kwargs["engine"], auth)
            # Never borrow the writer's context or freeze a subscription's role.
            if is_record_auth(auth) and not live.get("created"):
                return missing
            token = _SUBSCRIBER_CONTEXT.set(live)
            try:
                return await operation(*args, **kwargs)
            finally:
                _SUBSCRIBER_CONTEXT.reset(token)

        return wrapped

    realtime_service._build_rule_context = context
    realtime_service._is_subscription_allowed_for_event = with_subscriber_context(
        realtime_service._is_subscription_allowed_for_event, False
    )
    realtime_service._build_subscription_event_data = with_subscriber_context(
        realtime_service._build_subscription_event_data, None
    )
    _INSTALLED = True
