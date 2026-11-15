"""The ``scout-mcp`` command.

With no arguments it starts the server, as it always has. ``scout-mcp policy ...`` asks
questions of ``policy.yaml`` without a running stack, so a rule change can be checked in
seconds and in CI.

    scout-mcp policy test                      # run config/policy_test.yaml against config/policy.yaml
    scout-mcp policy check --subject role:support_copilot --tenant acme \\
        --action tool:s3:read --resource docs/a.txt
    scout-mcp new-tool weather.lookup          # a plugin package that already meets the tool contract
    scout-mcp ingest --tenant acme docs/*.md   # chunk, embed and write documents for one tenant
    scout-mcp tenant add initech               # the Keycloak clients a new tenant needs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

from scout_mcp.auth.policy import Decision, PolicyEngine
from scout_mcp.ingest import (
    DEFAULT_COLLECTION, DEFAULT_INDEX, IngestError, documents_from_files, ingest_documents,
)
from scout_mcp.scaffold import ScaffoldError, scaffold_tool
from scout_mcp.tenants import TenantError, add_tenant

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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _ingest(args: argparse.Namespace) -> int:
    try:
        documents = documents_from_files(args.tenant, [Path(f) for f in args.files])
        report = asyncio.run(ingest_documents(
            documents, collection=args.collection, index=args.index, force=args.force))
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{_plural(report.documents, 'document')} written, {_plural(report.chunks, 'chunk')} embedded; "
          f"{report.skipped} unchanged and skipped, {report.stale_deleted} stale chunks removed")
    if not os.environ.get("SCOUT_INGEST_POSTGRES_DSN"):
        print("note: Postgres was not written. Set SCOUT_INGEST_POSTGRES_DSN to a role that can write "
              "documents; without the row, search results come back without a title and body.")
    return 0


def _tenant_add(args: argparse.Namespace) -> int:
    password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not password:
        print("error: set KEYCLOAK_ADMIN_PASSWORD in the environment (not as an argument, so it stays out "
              "of shell history)", file=sys.stderr)
        return 1
    url = args.keycloak_url or os.environ.get("SCOUT_KEYCLOAK_ADMIN_URL", "http://keycloak:8080")
    try:
        created = asyncio.run(add_tenant(args.name, url=url, realm=args.realm,
                                         user=os.environ.get("KEYCLOAK_ADMIN", "admin"), password=password))
    except TenantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created {len(created)} clients in realm {args.realm} for tenant {args.name}. "
          "Each secret is shown once; store them now.\n")
    for c in created:
        print(f"  {c.client_id:<22}{c.secret}")
    print(f"""
Next:
  1. Load the tenant's data: rows in customers and orders with tenant_id = '{args.name}', then
       scout-mcp ingest --tenant {args.name} docs/*.md
  2. Outbound HTTP is off for this tenant until it has an allowlist. Nothing was written to
     config/; add this to config/http_allowlist.yaml and have someone review it:
""")
    print(f"{args.name}:")
    for host in args.allow_host or ["api.example.com"]:
        print(f"  - {host}")
    print(f"""
  3. Check the policy before you rely on it:
       scout-mcp policy check --subject role:support_copilot --tenant {args.name} \\
           --action tool:postgres:read --resource orders""")
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

    ingest = sub.add_parser("ingest", help="chunk, embed and write documents for one tenant")
    ingest.add_argument("--tenant", required=True)
    ingest.add_argument("files", nargs="+", help=".md or .txt files")
    ingest.add_argument("--collection", default=DEFAULT_COLLECTION, help="Qdrant collection")
    ingest.add_argument("--index", default=DEFAULT_INDEX, help="Elasticsearch index")
    ingest.add_argument("--force", action="store_true", help="re-embed documents that have not changed")
    ingest.set_defaults(run=_ingest)

    tenant = sub.add_parser("tenant", help="onboard a tenant").add_subparsers(dest="tenant_command", required=True)
    add = tenant.add_parser("add", help="create a tenant's Keycloak clients and print what to do next")
    add.add_argument("name", help="lower case letters, digits, - and _")
    add.add_argument("--keycloak-url", help="default: SCOUT_KEYCLOAK_ADMIN_URL, then http://keycloak:8080")
    add.add_argument("--realm", default="scout")
    add.add_argument("--allow-host", action="append", help="an outbound host to put in the allowlist snippet")
    add.set_defaults(run=_tenant_add)
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
