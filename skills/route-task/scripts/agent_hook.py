#!/usr/bin/env python3
"""PreToolUse(Agent) hook: record which subagent I ACTUALLY dispatched.

Wired by install.ps1 / install.sh as:
    python <abs path>/agent_hook.py     with matcher "Agent"

WHY THIS IS THE MOST VALUABLE LINE IN THE LEDGER

route_hook.py writes what the router RECOMMENDED. This writes what I DID. The
gap between them is the calibration signal the whole design rests on, and it
costs nothing to collect: when I ignore the advice and dispatch something else,
that disagreement is recorded without me saying a word. Asking me to rate the
router would have produced a signal I stopped supplying after a week.

It also feeds quota pressure: dispatch counts per fleet in the trailing window
are how the policy decides which subscription to spend next. Measuring what I
SPENT is not measuring what is LEFT - neither Claude Max nor Codex exposes
remaining quota - and every consumer of that number says so.

Like route_hook.py, this fails open and silent. A PreToolUse hook that errors
can block the tool call; blocking a dispatch to log a dispatch would be absurd.
It never emits a permission decision - it only observes.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MAX_STDIN_BYTES = 256 * 1024

# Which fleet a subagent name spends. The route-* agents are this router's own
# dispatch targets; everything else is a built-in, which also runs on Claude.
# Codex and Gemini dispatches are NOT visible here - they go through Bash, so
# codex_task.py and gemini_task.py write their own dispatch records. If this
# were the only writer, quota pressure would conclude Claude is the only fleet
# in use and would never switch away from it.
TIER_BY_AGENT = {
    "route-scout": "cheap",
    "route-builder": "work",
    "route-deep": "frontier",
}


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN_BYTES) if not sys.stdin.isatty() else ""
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool = payload.get("tool_name") or payload.get("toolName") or ""
    if tool and tool != "Agent":
        return 0

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    agent = tool_input.get("subagent_type") or tool_input.get("subagentType") or ""

    # THE ALLOWLIST APPLIES HERE TOO. routes.v1.json and docs/ROUTER.md both say
    # that outside allowlist.projects nothing is "classified, charged or logged"
    # - which was true of two verbs out of three. MEASURED in a project OUTSIDE
    # the list: route_hook wrote 0 lines and this hook wrote 1, naming
    # projeto_integracoes, the project deliberately excluded because the
    # scrubber was never calibrated against it.
    #
    # The question is asked through route.allowed - the same function route_hook
    # calls - rather than re-implemented here: two implementations of one
    # question drift, and the one that drifts is the one nobody re-reads.
    #
    # WHICH SIDE IS SAFE WHEN THE CONFIG CANNOT BE READ: not writing. A hook's
    # fail-open promise is about never BLOCKING the tool call, and that is kept
    # either way - this returns 0 and emits no decision. What is at stake is one
    # ledger line, i.e. some calibration signal, against writing down a project
    # name the written contract says is not logged. A lost line is recoverable;
    # a line that should not exist is not. route_hook already behaves this way,
    # because load_json raises and its BaseException guard exits 0 in silence.
    import route as route_mod

    cwd = Path(payload.get("cwd") or payload.get("projectDir") or os.getcwd())
    try:
        # load_json raises SystemExit (a BaseException) on an unreadable file,
        # which is why the tuple below names it explicitly.
        config = route_mod.load_json(route_mod.ROUTES_FILE)
        in_scope = route_mod.allowed(config, route_mod.project_name(cwd))
    except (SystemExit, Exception):
        in_scope = False
    if not in_scope:
        return 0

    import ledger
    ledger.append({
        "type": "dispatch",
        "session_id": payload.get("session_id") or payload.get("sessionId") or "",
        "project": Path(payload.get("cwd") or os.getcwd()).name,
        "fleet": "claude",
        "agent": agent,
        "tier": TIER_BY_AGENT.get(agent),
        # Routed by this router, or chosen freely? The route-* prefix is the only
        # honest way to tell, and the share of non-route-* dispatches is what
        # says whether these three agents are earning their place in the
        # always-resident agent list.
        "from_router": agent in TIER_BY_AGENT,
        "via": "agent_hook",
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
