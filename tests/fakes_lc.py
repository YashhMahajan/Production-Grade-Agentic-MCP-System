"""A scripted chat model for the graph tests. Needs langchain-core; nothing here calls a network."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from scout_mcp.agents.prompts import PLAN_SYSTEM
from tests.fakes import directives_in


class ScriptedChat(BaseChatModel):
    """Answers from a list, one item per call. An Exception in the list is raised instead."""

    replies: list[Any]
    seen: list[list[str]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append([str(m.content) for m in messages])
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=item))])


class ObedientChat(BaseChatModel):
    """A model with no defences: it does whatever an instruction in its input tells it to.

    Asked to plan, it proposes every ``CALL tool {json}`` found in the question. Asked to write, it
    writes a plain claim, and ``seen_in_data`` records each instruction that reached it inside the
    evidence, so a test can show the attack arrived and still caused nothing. It has no way to
    call a tool from that step, which is the property under test.
    """

    seen_in_data: list[tuple[str, dict]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "obedient"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system, human = str(messages[0].content), str(messages[-1].content)
        if system == PLAN_SYSTEM:
            reply = json.dumps({"calls": [{"tool": t, "arguments": a} for t, a in directives_in(human)]})
        else:
            self.seen_in_data.extend(directives_in(human))
            reply = json.dumps({"claims": [{"text": "The refund is pending.", "cites": [1]}],
                                "needs_approval": True, "insufficient": False})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])
