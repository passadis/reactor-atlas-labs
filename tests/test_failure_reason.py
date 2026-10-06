"""A model failure is shown by its reason: a throttle gets a countdown, not 'unreachable'."""

from __future__ import annotations

from agent_framework.exceptions import ChatClientException
from atlas_agent.factory import model_failure_reason


def test_a_wrapped_rate_limit_is_a_throttle() -> None:
    exc = ChatClientException(
        "service failed to complete the prompt: Model deployment rate limit exceeded. Your "
        "requests to gpt-5.4 for gpt-main in eastus2 have exceeded token rate limit.")
    assert model_failure_reason(exc) == "throttled"


def test_any_other_client_failure_is_unavailable() -> None:
    assert model_failure_reason(ChatClientException("connection reset")) == "model_unavailable"


def test_a_rate_limit_deep_in_the_cause_chain_is_found() -> None:
    try:
        try:
            raise ChatClientException("exceeded token rate limit")
        except ChatClientException as inner:
            raise ValueError("wrapper") from inner
    except ValueError as outer:
        assert model_failure_reason(outer) == "throttled"
