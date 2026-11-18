"""Run the draft flow's replay cases and print the result as JSON. Needs the `copilot` extra.

    python scripts/eval_copilot.py                 # offline: no model, no network
    python scripts/eval_copilot.py --langsmith     # also upload the cases as a LangSmith dataset

Exits 1 when any case disagrees with its label. The upload needs LANGSMITH_API_KEY; it sends only the
hand-written cases, never customer data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from scout_mcp.agents import evals


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--langsmith", action="store_true", help="also upload the cases as a dataset")
    args = parser.parse_args()
    cases = evals.load_cases()
    results = await evals.run_all(cases)
    report = evals.summary(results)
    if args.langsmith:
        if not os.environ.get("LANGSMITH_API_KEY"):
            print("error: --langsmith needs LANGSMITH_API_KEY", file=sys.stderr)
            return 2
        from langsmith import Client
        report["langsmith"] = evals.upload_dataset(Client(api_key=os.environ["LANGSMITH_API_KEY"]), cases)
    print(json.dumps(report, indent=2))
    return 0 if report["agreement"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
