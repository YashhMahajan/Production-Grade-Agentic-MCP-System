"""Base classes for the agent layer.

Every agent in Atlas's copilot is a thin wrapper around an OpenRouter API
call. The base class handles:

* Token authentication and retries.
* JSON-mode parsing with schema validation.
* Token budget accounting (so the orchestrator can enforce a per-turn cap).
* Structured logging with a shared ``run_id`` so you can trace a whole
  conversation across all four agents in one log query.
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger("atlas.agents")


@dataclass
class AgentRun:
    """Per-turn accounting, shared across all agents in one user interaction."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0


class LLM:
    """Minimal async OpenRouter Messages client.

    Not the full SDK — just enough surface area for our agents. Isolating it
    here means swapping to a different provider (Anthropic, Bedrock, Vertex)
    means changing one file.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("ATLAS_LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
        self.model = model or os.environ.get("ATLAS_LLM_MODEL", "deepseek/deepseek-v4-flash:free")
        self.base_url = os.environ.get("ATLAS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        # Fallback sends the same prompt, customer text included, to other providers.
        # Off unless ATLAS_LLM_FALLBACK=true.
        self.fallback = os.environ.get("ATLAS_LLM_FALLBACK", "false").lower() == "true"

    async def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> dict:
        payload_messages = [{"role": "system", "content": system}] + messages
        
        openrouter_key = self.api_key or os.environ.get("OPENROUTER_API_KEY", "")
        
        # this is for switching the models when current one fails or throws error
        fallback_configs = [
            {
                "model": self.model,
                "api_key": self.api_key,
                "base_url": self.base_url,
            },
            {
                "model": "meta-llama/llama-3.3-70b-instruct:free",
                "api_key": openrouter_key,
                "base_url": "https://openrouter.ai/api/v1",
            },
            {
                "model": "nvidia/nemotron-3-super-120b-a12b:free",
                "api_key": openrouter_key,
                "base_url": "https://openrouter.ai/api/v1",
            },
            {
                "model": "llama-3.3-70b-versatile",
                "api_key": os.environ.get("ATLAS_LLM_GROQ_KEY", os.environ.get("GROQ_API_KEY", "")),
                "base_url": "https://api.groq.com/openai/v1",
            }
        ]

        if not self.fallback:
            fallback_configs = fallback_configs[:1]

        # Deduplicate configs while preserving order
        seen = set()
        configs_to_try = []
        for config in fallback_configs:
            key = (config["model"], config["base_url"])
            if key not in seen:
                seen.add(key)
                configs_to_try.append(config)

        last_exception = None
        for config in configs_to_try:
            if not config["api_key"]:
                log.debug("skipping_model_no_api_key", model=config["model"])
                continue
                
            body: dict[str, Any] = {
                "model": config["model"],
                "max_tokens": max_tokens,
                "messages": payload_messages,
            }
            if tools:
                body["tools"] = tools
            headers = {
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            }
            if "openrouter" in config["base_url"]:
                headers["HTTP-Referer"] = "https://github.com/atlas-mcp/atlas-mcp"
                headers["X-Title"] = "Atlas MCP"

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(f"{config['base_url']}/chat/completions", json=body, headers=headers)
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:
                log.warning("llm_fallback_triggered", failed_model=config["model"], error=str(e))
                last_exception = e
                
        if last_exception:
            raise last_exception
        raise RuntimeError("No models configured for fallback or all failed.")


class Agent(ABC):
    """Abstract base. Subclasses override ``act``."""

    name: str = "agent"
    system_prompt: str = ""

    def __init__(self, llm: LLM):
        self.llm = llm

    @abstractmethod
    async def act(self, run: AgentRun, **inputs) -> Any:
        ...

    # ── Convenience helpers ───────────────────────────────────────────────
    async def _complete_json(
        self, run: AgentRun, messages: list[dict], max_tokens: int = 512
    ) -> dict:
        resp = await self.llm.complete(self.system_prompt, messages, max_tokens=max_tokens)
        usage = resp.get("usage", {})
        run.tokens_in += usage.get("prompt_tokens", 0)
        run.tokens_out += usage.get("completion_tokens", 0)
        text = _first_text_block(resp)
        log.debug("agent_response", agent=self.name, run_id=run.run_id,
                  tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"))
        return _parse_json_lenient(text)

    async def _complete_text(
        self, run: AgentRun, messages: list[dict], max_tokens: int = 512
    ) -> str:
        resp = await self.llm.complete(self.system_prompt, messages, max_tokens=max_tokens)
        usage = resp.get("usage", {})
        run.tokens_in += usage.get("prompt_tokens", 0)
        run.tokens_out += usage.get("completion_tokens", 0)
        return _first_text_block(resp)


def _first_text_block(response: dict) -> str:
    choices = response.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        return message.get("content", "")
    return ""


def _parse_json_lenient(text: str) -> dict:
    """Extract a JSON object from an LLM response even with stray prose."""
    text = text.strip()
    # Strip markdown fences if present.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.removeprefix("json\n")
    # Find the first { and the matching last }.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM output: {text[:200]!r}")
    return json.loads(text[start:end + 1])
