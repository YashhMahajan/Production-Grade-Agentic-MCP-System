"""No test can reach a model host or LangSmith, whatever it forgot to fake."""

from __future__ import annotations

import socket

import pytest


@pytest.mark.parametrize("host", ["openrouter.ai", "api.smith.langchain.com", "eu.api.smith.langchain.com",
                                  "OpenRouter.AI.", "smith.langchain.com"])
def test_a_model_or_tracing_host_does_not_resolve_in_a_test(host):
    with pytest.raises(AssertionError, match="never call a model or LangSmith"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", ["localhost", "notopenrouter.ai.example"])
def test_every_other_host_resolves_as_before(host):
    assert socket.getaddrinfo(host if host == "localhost" else "localhost", 80)
