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

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml

from scout_mcp.errors.framework import PolicyError


@dataclass(frozen=True, slots=True)
# Rule is an immutable dataclass. The engine loads rules from the YAML file, parses them into Rule instances, and holds them in a list.
class Rule:
    id: str
    subjects: tuple[str, ...]
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    effect: Literal["allow", "deny"] = "allow"
    conditions: dict[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Decision:
    """What the engine decided, and which rule decided it.

    ``rule`` is the deny rule that blocked the call, or the first allow rule that matched.
    It is None when nothing matched (a default denial, or an allow because default-deny is off).
    """

    allowed: bool
    rule: str | None = None
    code: str | None = None
    hint: str | None = None


class PolicyEngine:
    """Evaluates rules in order; deny beats allow beats default-deny."""

    def __init__(self, rules: list[Rule], default_deny: bool = True):
        self.rules = rules
        self.default_deny = default_deny #default_deny=True is our safety net, no explicit allow means denial.

    @classmethod
    def from_file(cls, path: str | Path, default_deny: bool = True) -> PolicyEngine:
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

    def decide(
        self, subject: str | Sequence[str], tenant: str, action: str, resource: str, context: dict
    ) -> Decision:
        """Evaluate the rules and say what happened, without raising.

        Matching rules are partitioned into allow and deny. Deny always wins.
        If no allow rule matches and default_deny is on, the call is not allowed.
        """
        # A caller is its own subject plus every role it holds; any of them may match a rule.
        subjects = (subject,) if isinstance(subject, str) else tuple(subject)
        matched_allow: list[Rule] = []
        matched_deny: list[Rule] = []

        for rule in self.rules:
            if not self._matches(rule, subjects, action, tenant, resource):
                continue
            if rule.conditions and not self._conditions_match(rule.conditions, context):
                continue
            (matched_deny if rule.effect == "deny" else matched_allow).append(rule)

        if matched_deny:
            rule = matched_deny[0]
            return Decision(False, rule.id, "denied_by_policy", f"rule {rule.id} denies this action")
        if not matched_allow and self.default_deny:
            return Decision(False, None, "not_authorized",
                            f"no policy rule allows {action} on {resource}")
        return Decision(True, matched_allow[0].id if matched_allow else None)

    def check(
        self, subject: str | Sequence[str], tenant: str, action: str, resource: str, context: dict
    ) -> None:
        """Raise :class:`PolicyError` if the call is not allowed."""
        decision = self.decide(subject, tenant, action, resource, context)
        if not decision.allowed:
            raise PolicyError(decision.code, retryable=False, hint=decision.hint)

    # ── Pattern matching ──────────────────────────────────────────────────
    # the glob is deliberately limited (exact match, trailing star, or bare *). No full regex, no complex expressions.
    
    @staticmethod
    def _matches(rule: Rule, subjects: tuple[str, ...], action: str, tenant: str, resource: str) -> bool:
        return (
            any(_glob_any(rule.subjects, s) for s in subjects)
            and _glob_any(rule.actions, action)
            and _resource_matches(rule.resources, tenant, resource)
        )

    # _conditions_match is a switch over known condition names with explicit handlers. Adding a new condition type is a code change, not a config change, which is what you want for security-sensitive logic.
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

# The default for unknown conditions is False, we fail closed.
def _glob_any(patterns: tuple[str, ...], value: str) -> bool:
    """fnmatch-style globs: ``*`` matches any run of characters, including ``/``.

    The old trailing-star-only matcher treated the ``*`` in ``tenant:*/docs/*``
    as a literal, so no per-tenant rule in policy.yaml could ever match.
    """
    return any(fnmatchcase(value, pat) for pat in patterns)


def _resource_matches(patterns: tuple[str, ...], tenant: str, resource: str) -> bool:
    """Match a resource against rule patterns, keeping the tenant and the path apart.

    ``tenant:*/docs/*`` means any tenant, then a top-level ``docs/`` folder. Matching it
    against the joined string ``tenant:acme/x/docs/y`` let the first ``*`` swallow ``acme/x``,
    so any key with ``docs`` somewhere inside matched. The tenant is matched on its own, and
    the rest of the pattern is matched against the resource alone. A resource with a ``..``
    segment never matches a tenant pattern. A bare pattern such as ``*`` still matches anything.
    """
    for pattern in patterns:
        if pattern.startswith("tenant:") and "/" in pattern:
            tenant_pattern, _, path_pattern = pattern.partition("/")
            if ".." in resource.split("/"):
                continue
            if fnmatchcase(f"tenant:{tenant}", tenant_pattern) and fnmatchcase(resource, path_pattern):
                return True
        elif fnmatchcase(f"tenant:{tenant}/{resource}", pattern):
            return True
    return False
