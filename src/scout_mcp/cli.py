"""The ``scout-mcp`` command.

With no arguments it starts the server, as it always has. ``scout-mcp policy ...`` asks
questions of ``policy.yaml`` without a running stack, so a rule change can be checked in
seconds and in CI.

    scout-mcp policy test                      # run config/policy_test.yaml against config/policy.yaml
    scout-mcp policy check --subject role:support_copilot --tenant acme \\
        --action tool:s3:read --resource docs/a.txt
    scout-mcp new-tool weather.lookup          # a plugin package that already meets the tool contract
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from scout_mcp.auth.policy import Decision, PolicyEngine
from scout_mcp.scaffold import ScaffoldError, scaffold_tool

DEFAULT_POLICY = "config/policy.yaml"
DEFAULT_CASES = "config/policy_test.yaml"


def _describe(d: Decision) -> str:
    if d.allowed:
        return f"allow by rule {d.rule}" if d.rule else "allow (no rule matched and default-deny is off)"
    return f"deny: {d.hint}"


def _policy_test(args: argparse.Namespace) -> int:
    engine = PolicyEngine.from_file(args.policy)
    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    exercised: set[str] = set()
    failed = 0
    for case in cases:
        d = engine.decide(tuple(case["subjects"]), case["tenant"], case["action"],
                          case["resource"], case.get("context") or {})
        if d.rule:
            exercised.add(d.rule)
        problems = []
        if d.allowed != (case["expect"] == "allow"):
            problems.append(f"expected {case['expect']}, got {_describe(d)}")
        elif "rule" in case and d.rule != case["rule"]:
            problems.append(f"expected rule {case['rule']}, decided by {d.rule}")
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'PASS'}  {case['name']}  ->  {'; '.join(problems) or _describe(d)}")
    rules = [r.id for r in engine.rules]
    missed = [r for r in rules if r not in exercised]
    print(f"\n{len(cases) - failed} of {len(cases)} cases passed; "
          f"{len(rules) - len(missed)} of {len(rules)} rules exercised")
    if missed:
        print(f"rules no case is decided by: {', '.join(missed)}")
    return 1 if failed or (args.strict and missed) else 0


def _policy_check(args: argparse.Namespace) -> int:
    engine = PolicyEngine.from_file(args.policy)
    d = engine.decide(tuple(args.subject), args.tenant, args.action, args.resource,
                      json.loads(args.context) if args.context else {})
    print(f"{'ALLOW' if d.allowed else 'DENY'}  {_describe(d)}")
    return 0 if d.allowed else 1


def _new_tool(args: argparse.Namespace) -> int:
    try:
        root, policy = scaffold_tool(args.name, Path(args.dir))
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created {root}/\n")
    print("Install it next to the server with `pip install -e` on that directory; the tool is")
    print("found through the scout_mcp.tools entry point. Nothing in config/ was changed. Add")
    print("this to config/policy.yaml and config/policy_test.yaml yourself:\n")
    print(policy)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout-mcp", description="Scout MCP server and tools.")
    sub = parser.add_subparsers(dest="command")
    policy = sub.add_parser("policy", help="test or query policy.yaml").add_subparsers(
        dest="policy_command", required=True)

    test = policy.add_parser("test", help="run a case file against the policy")
    test.add_argument("--policy", default=DEFAULT_POLICY)
    test.add_argument("--cases", default=DEFAULT_CASES)
    test.add_argument("--strict", action="store_true", help="also fail if a rule decides no case")
    test.set_defaults(run=_policy_test)

    check = policy.add_parser("check", help="ask whether one call would be allowed, and by which rule")
    check.add_argument("--policy", default=DEFAULT_POLICY)
    check.add_argument("--subject", action="append", required=True,
                       help="the caller or one of its roles; repeat for several")
    check.add_argument("--tenant", required=True)
    check.add_argument("--action", required=True, help="a scope such as tool:s3:read")
    check.add_argument("--resource", required=True, help="the table or key the tool touches")
    check.add_argument("--context", help='JSON, for example \'{"sql": "SELECT 1"}\'')
    check.set_defaults(run=_policy_check)

    new_tool = sub.add_parser("new-tool", help="write a plugin package for a new tool")
    new_tool.add_argument("name", help="lower case, such as weather or weather.lookup")
    new_tool.add_argument("--dir", default=".", help="where to create the package directory")
    new_tool.set_defaults(run=_new_tool)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        from scout_mcp import server

        server.main()
        return 0
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
