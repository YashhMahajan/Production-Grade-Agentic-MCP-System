"""The chat models behind the copilot, and how a failed call is handled.

Both models are free OpenRouter models behind one key. ``ChatOpenAI`` is the interface;
the loop around it is ours because what happens after a failure decides how much of a
small daily allowance is spent:

* a 400, 401, 402 or 403 is the request or the key. Another model would fail the same way.
* a 429 that names a per-day cap is shared by every free model. Another model would fail too.
* anything else (a 5xx, a timeout, a per-provider 429, an empty reply) tries the next model.

``max_retries=0`` on each model, so the library never retries behind our back, and every
attempt is counted.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from scout_mcp.agents.base import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL


class FinalLLMError(Exception):
    """Trying another model would not help (bad request, bad key, or the day's cap)."""


class LLMUnavailable(Exception):
    """No model produced a usable reply."""


class CallBudget(LLMUnavailable):
    """The run's cap on model calls is reached, so no further attempt is made."""


def classify(exc: Exception) -> str:
    """``"final"`` when another model would fail the same way, else ``"transient"``."""
    status = getattr(exc, "status_code", None)
    if status in (400, 401, 402, 403):
        return "final"
    if status == 429 and "per-day" in f"{exc} {getattr(exc, 'body', '')}".lower():
        return "final"
    return "transient"


def reply_text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


async def invoke_models(models: Sequence[Any], messages: list, counter: list[int],
                        limit: int | None = None, config: dict | None = None) -> str:
    """One logical call: the first model that answers wins. ``counter[0]`` counts every attempt.

    With ``limit`` set, no attempt starts once the counter has reached it, fallbacks included.
    """
    last: Exception | None = None
    for model in models:
        if limit is not None and counter[0] >= limit:
            raise CallBudget("call_budget")
        counter[0] += 1
        try:
            text = reply_text(await model.ainvoke(messages, config=config))
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            if classify(exc) == "final":
                raise FinalLLMError(str(exc)) from exc
            last = exc
            continue
        if text.strip():
            return text
        last = ValueError("empty completion")
    raise LLMUnavailable(str(last) if last else "no models configured")


def model_names() -> list[str]:
    primary = os.environ.get("SCOUT_LLM_MODEL", DEFAULT_MODEL)
    fallback = os.environ.get("SCOUT_LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)
    return [primary] + ([fallback] if fallback and fallback != primary else [])


def build_models(max_tokens: int = 700) -> list[Any]:
    """The primary and the fallback as ``ChatOpenAI`` clients. Needs the ``copilot`` extra."""
    key = os.environ.get("SCOUT_LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
    if not key:
        raise FinalLLMError("SCOUT_LLM_API_KEY is not set")
    from langchain_openai import ChatOpenAI

    base_url = os.environ.get("SCOUT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    return [ChatOpenAI(model=name, api_key=key, base_url=base_url, max_retries=0,
                       timeout=60, max_tokens=max_tokens) for name in model_names()]
