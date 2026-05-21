"""Component 3 — Policy engine.

Policies are expressed in YAML so ops teams can change them without redeploy.
Each rule binds a subject (or scope) to an action on a resource, with optional
attribute-based conditions.

The engine is deny-by-default. The absence of an ``allow`` rule is a denial.

Example policy.yaml::

    rules:
      - id: analysts-can-read-postgres
        subjects: ["role:analyst"]
        actions: ["tool:postgres:read"]
        resources: ["tenant:acme/*"]

      - id: agents-cannot-drop-tables
        subjects: ["*"]
        actions: ["tool:postgres:write"]
        resources: ["*"]
        conditions:
          sql_contains_any: ["DROP", "TRUNCATE", "DELETE FROM"]
        effect: deny

The conditions syntax is deliberately narrow. A policy DSL that can express
anything tends to be one that nobody understands six months later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from atlas_mcp.errors.framework import PolicyError


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    subjects: tuple[str, ...]
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    effect: Literal["allow", "deny"] = "allow"
    conditions: dict[str, Any] = None  # type: ignore[assignment]


class PolicyEngine:
    """Evaluates rules in order; deny beats allow beats default-deny."""

    def __init__(self, rules: list[Rule], default_deny: bool = True):
        self.rules = rules
        self.default_deny = default_deny

    @classmethod
    def from_file(cls, path: str | Path, default_deny: bool = True) -> "PolicyEngine":
        data = yaml.safe_load(Path(path).read_text())
        rules = [
            Rule(
                id=r["id"],
                subjects=tuple(r.get("subjects", [])),
                actions=tuple(r.get("actions", [])),
                resources=tuple(r.get("resources", [])),
                effect=r.get("effect", "allow"),
                conditions=r.get("conditions"),
            )
            for r in data.get("rules", [])
        ]
        return cls(rules, default_deny=default_deny)

    def check(self, subject: str, tenant: str, action: str, resource: str, context: dict) -> None:
        """Raise :class:`PolicyError` if the call is not allowed.

        Matching rules are partitioned into allow and deny. Deny always wins.
        If no allow rule matches and default_deny is on, the call is rejected.
        """
        matched_allow: list[Rule] = []
        matched_deny: list[Rule] = []

        for rule in self.rules:
            if not self._matches(rule, subject, action, f"tenant:{tenant}/{resource}"):
                continue
            if rule.conditions and not self._conditions_match(rule.conditions, context):
                continue
            (matched_deny if rule.effect == "deny" else matched_allow).append(rule)

        if matched_deny:
            raise PolicyError(
                "denied_by_policy",
                retryable=False,
                hint=f"rule {matched_deny[0].id} denies this action",
            )
        if not matched_allow and self.default_deny:
            raise PolicyError(
                "not_authorized",
                retryable=False,
                hint=f"no policy rule allows {action} on {resource}",
            )

    # ── Pattern matching ──────────────────────────────────────────────────
    @staticmethod
    def _matches(rule: Rule, subject: str, action: str, resource: str) -> bool:
        return (
            _glob_any(rule.subjects, subject)
            and _glob_any(rule.actions, action)
            and _glob_any(rule.resources, resource)
        )

    @staticmethod
    def _conditions_match(conditions: dict, context: dict) -> bool:
        # Narrow DSL: only a handful of operators. Easy to audit.
        for key, value in conditions.items():
            if key == "sql_contains_any":
                sql = (context.get("sql") or "").upper()
                if any(needle.upper() in sql for needle in value):
                    return True
                return False
            if key == "pii_fields":
                requested = set(context.get("columns", []))
                if requested & set(value):
                    return True
                return False
            # Unknown conditions fail closed.
            return False
        return True


def _glob_any(patterns: tuple[str, ...], value: str) -> bool:
    """Supports trailing-star globs and bare `*` wildcard."""
    for pat in patterns:
        if pat == "*" or pat == value:
            return True
        if pat.endswith("*") and value.startswith(pat[:-1]):
            return True
    return False
