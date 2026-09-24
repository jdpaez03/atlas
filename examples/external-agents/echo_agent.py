"""ECHO: a minimal external ATLAS agent for the `cli` adapter (standard library only).

Contract atlas.external/1 (docs/CONTRACTS.md, "External agents"): ATLAS writes one request JSON on stdin and
reads one report JSON from stdout. Anything the agent wants to log goes to stderr.

Try it by hand:
    echo '{"contract": "atlas.external/1", "task": {"title": "t", "description": "count these words"},
           "inputs": ["one two three"]}' | python examples/external-agents/echo_agent.py
"""

from __future__ import annotations

import json
import sys


def words(text: str) -> int:
    return len(str(text).split())


def handle(request: dict) -> dict:
    task = request.get("task") or {}
    inputs = [str(x) for x in request.get("inputs") or []]
    in_desc = words(task.get("description", ""))
    in_inputs = sum(words(x) for x in inputs)
    return {
        "asked_to": task.get("title") or "(untitled task)",
        "actions_taken": ["Counted the words of the task description and of the dependency inputs"],
        "inputs_used": ["task.description"] + [f"inputs[{i}]" for i in range(len(inputs))],
        "findings": [
            {
                "kind": "FACT",
                "statement": f"The task description has {in_desc} words and the {len(inputs)} dependency "
                             f"input(s) have {in_inputs} words in total.",
                "sources": ["task.description", "inputs"],
                "confidence": "HIGH",
            },
            {
                "kind": "ASSUMPTION",
                "statement": "Word counts are a rough proxy for how much context this task received.",
                "confidence": "MEDIUM",
            },
        ],
        "unresolved": [],
        "confidence": "HIGH",
        "limitations": ["ECHO only counts words; it does not read files or the web"],
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(f"echo_agent: invalid request JSON: {exc}", file=sys.stderr)
        return 2
    if request.get("contract", "atlas.external/1") != "atlas.external/1":
        print(f"echo_agent: unsupported contract {request.get('contract')!r}", file=sys.stderr)
        return 2
    json.dump(handle(request), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
