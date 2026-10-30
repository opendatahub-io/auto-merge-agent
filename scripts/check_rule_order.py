"""Require static alternatives and group members before LLM rules."""

from __future__ import annotations

from pathlib import Path

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.rule_order import rule_order_errors


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    errors = []
    for root in [repo / "examples", repo / "deploy/policy"]:
        store = ConfigStore(root)
        for slug in store.validate_all():
            host, owner, repo_name = slug.split("/")
            rules = store.load(host, owner, repo_name).policy.rules
            errors.extend(rule_order_errors(rules, slug))
    if errors:
        print("\n".join(errors))
        return 1
    print("Static rules precede LLM rules in all repository configs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
