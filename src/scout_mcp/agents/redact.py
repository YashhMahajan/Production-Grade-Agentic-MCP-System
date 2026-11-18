"""Keep contact details out of prompts.

Emails, phone numbers and card-like numbers are swapped for placeholders before a
request leaves for a model host, and swapped back in the reply. Names and free-text
details are not caught by patterns; that is a limit, not a guarantee.
"""

from __future__ import annotations

import copy
import re

from scout_mcp.agents.base import LLM

_PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}|\+\d{10,15}")),
]


class Redactor:
    def __init__(self) -> None:
        self._to_placeholder: dict[str, str] = {}
        self._to_value: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def _swap(self, kind: str, value: str) -> str:
        if value not in self._to_placeholder:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            placeholder = f"<{kind}_{self._counts[kind]}>"
            self._to_placeholder[value] = placeholder
            self._to_value[placeholder] = value
        return self._to_placeholder[value]

    def redact(self, text: str) -> str:
        for kind, pattern in _PATTERNS:
            text = pattern.sub(lambda m, k=kind: self._swap(k, m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        for placeholder, value in self._to_value.items():
            text = text.replace(placeholder, value)
        return text


class RedactingLLM(LLM):
    """Wraps a model client: what goes out is redacted, what comes back is restored."""

    def __init__(self, inner: LLM):
        self.inner = inner
        self.redactor = Redactor()

    @property
    def models(self) -> list[str]:  # type: ignore[override]
        return self.inner.models

    async def complete(self, system, messages, max_tokens=1024, tools=None):
        r = self.redactor
        safe = [{**m, "content": r.redact(m["content"]) if isinstance(m.get("content"), str) else m.get("content")}
                for m in messages]
        data = await self.inner.complete(r.redact(system), safe, max_tokens=max_tokens, tools=tools)
        data = copy.deepcopy(data)
        for choice in data.get("choices", []):
            message = choice.get("message", {})
            if isinstance(message.get("content"), str):
                message["content"] = r.restore(message["content"])
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                if isinstance(fn.get("arguments"), str):
                    fn["arguments"] = r.restore(fn["arguments"])
        return data
