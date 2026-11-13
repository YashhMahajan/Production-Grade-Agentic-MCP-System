"""Critic agent — gatekeeper that blocks bad drafts."""

# 4th agent - critic
from __future__ import annotations

from dataclasses import dataclass

from scout_mcp.agents.base import Agent, AgentRun
from scout_mcp.agents.prompts import CRITIC_SYSTEM
from scout_mcp.agents.retriever import Finding
from scout_mcp.agents.synthesizer import Draft


@dataclass
class Verdict:
    approved: bool
    issues: list[str]
    revision_hints: str | None


class CriticAgent(Agent):
    name = "critic"
    system_prompt = CRITIC_SYSTEM

    async def act(
        self, run: AgentRun, *, question: str, findings: list[Finding], draft: Draft
    ) -> Verdict:  # type: ignore[override]
        findings_block = "\n".join(
            f"[S{i}] {f.source}: {f.summary}" for i, f in enumerate(findings, start=1)
        )
        prompt = (
            f"Customer question:\n{question}\n\n"
            f"Findings:\n{findings_block}\n\n"
            f"Draft:\n{draft.text}\n\n"
            "Evaluate the draft against the rules and emit the verdict JSON."
        )
        data = await self._complete_json(
            run, messages=[{"role": "user", "content": prompt}], max_tokens=400
        )
        return Verdict(
            approved=data.get("verdict") == "approve",
            issues=[str(i) for i in data.get("issues", [])],
            revision_hints=data.get("revision_hints"),
        )
