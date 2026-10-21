#!/usr/bin/env python3
"""Prove the hooks fail OPEN. Runs them as real subprocesses.

    python selftest_hooks.py            # offline cases only
    python selftest_hooks.py --network  # plus the live-jev cases

Why subprocesses and not imports: the guarantee being tested is "exit 0, no
stderr, no blocking output, whatever happens", and that is enforced by the
BaseException guard around __main__. Importing main() would skip exactly the
code under test.

Every case asserts three things: exit code 0, nothing on stderr, and either
silence or well-formed hookSpecificOutput JSON. A hook that printed a traceback
would pass an exit-code-only test and still put a stack trace at the top of
every turn.

The ledger is redirected to a temp file via $ROUTE_LEDGER, so running this never
touches the real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
IN_ALLOWLIST = str(Path.home() / "ai-skills")  # ai-skills is on the allowlist


def run(script: str, payload, env_extra=None, timeout=30.0):
    env = dict(os.environ)
    env.update(env_extra or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        [PY, str(HERE / script)], input=data, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    return proc


def main() -> int:
    network = "--network" in sys.argv
    failures = []
    tmp = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    # Both the ledger AND the duplicate cache are redirected: sharing either
    # with the live hook made three cases pass offline and fail on rerun.
    base_env = {"ROUTE_LEDGER": str(tmp),
                "ROUTE_HOOK_CACHE": str(tmp.parent / "hook-cache.json")}

    def check(label, proc, *, expect_silent=None):
        problems = []
        if proc.returncode != 0:
            problems.append(f"exit {proc.returncode}")
        if proc.stderr.strip():
            problems.append(f"stderr: {proc.stderr.strip()[:120]}")
        out = proc.stdout.strip()
        if out:
            try:
                parsed = json.loads(out)
                hso = parsed.get("hookSpecificOutput", {})
                if hso.get("hookEventName") != "UserPromptSubmit":
                    problems.append("missing hookEventName")
                if not isinstance(hso.get("additionalContext"), str):
                    problems.append("additionalContext is not a string")
                elif len(hso["additionalContext"].splitlines()) > 3:
                    problems.append("more than 3 lines of advice")
            except ValueError:
                problems.append(f"stdout is not JSON: {out[:120]}")
        if expect_silent is True and out:
            problems.append("expected silence, got advice")
        if expect_silent is False and not out:
            problems.append("expected advice, got silence")
        print(("ok   " if not problems else "FAIL ") + label
              + ("" if not problems else "\n       " + "; ".join(problems)))
        if problems:
            failures.append(label)

    long_prompt = ("the postgres migration test fails intermittently on CI but "
                   "passes locally, and I need to find out why")

    print("--- degraded inputs: every one must exit 0 and say nothing ---")
    check("empty stdin", run("route_hook.py", "", base_env), expect_silent=True)
    check("malformed JSON", run("route_hook.py", "{not json at all", base_env),
          expect_silent=True)
    check("JSON that is a list, not an object", run("route_hook.py", "[1,2,3]", base_env),
          expect_silent=True)
    check("null prompt field", run("route_hook.py", {"prompt": None}, base_env),
          expect_silent=True)

    print("\n--- gates that must stay silent ---")
    check("prompt under 40 chars",
          run("route_hook.py", {"prompt": "fix it", "cwd": IN_ALLOWLIST}, base_env),
          expect_silent=True)
    check("slash command",
          run("route_hook.py", {"prompt": "/commit and then push all of the things",
                                "cwd": IN_ALLOWLIST}, base_env), expect_silent=True)
    check("project OUTSIDE the allowlist",
          run("route_hook.py", {"prompt": long_prompt, "cwd": str(Path.home())}, base_env),
          expect_silent=True)

    print("\n--- jev unavailable: must still advise, via the fallback ---")
    no_key = dict(base_env, JEV_API_KEY="")
    check("no API key -> keyword fallback",
          run("route_hook.py", {"prompt": long_prompt, "cwd": IN_ALLOWLIST}, no_key),
          expect_silent=False)
    bad_key = dict(base_env, JEV_API_KEY="jv_live_this_key_is_not_real_at_all")
    check("invalid API key -> keyword fallback",
          run("route_hook.py", {"prompt": long_prompt + " x", "cwd": IN_ALLOWLIST}, bad_key),
          expect_silent=False)
    tight = dict(base_env, ROUTE_HOOK_TIMEOUT="0.001")
    check("timeout of 1ms -> keyword fallback",
          run("route_hook.py", {"prompt": long_prompt + " y", "cwd": IN_ALLOWLIST}, tight),
          expect_silent=False)

    print("\n--- the Agent hook observes and never blocks ---")
    check("dispatch of a router agent", run("agent_hook.py", {
        "tool_name": "Agent", "cwd": IN_ALLOWLIST, "session_id": "s1",
        "tool_input": {"subagent_type": "route-deep"}}, base_env), expect_silent=True)
    check("dispatch of a built-in agent", run("agent_hook.py", {
        "tool_name": "Agent", "cwd": IN_ALLOWLIST,
        "tool_input": {"subagent_type": "Explore"}}, base_env), expect_silent=True)
    check("a non-Agent tool is ignored", run("agent_hook.py", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}}, base_env), expect_silent=True)
    check("garbage into the agent hook", run("agent_hook.py", "}{", base_env),
          expect_silent=True)

    # The assertion whose ABSENCE let the allowlist leak through this hook: the
    # "project OUTSIDE the allowlist" case above only ever ran against
    # route_hook. Silence cannot be the test here - this hook is silent even
    # when it writes - so the ledger is counted before and after.
    def ledger_lines():
        if not tmp.exists():
            return 0
        return len([ln for ln in tmp.read_text(encoding="utf-8").splitlines()
                    if ln.strip()])

    before = ledger_lines()
    proc = run("agent_hook.py", {
        "tool_name": "Agent", "cwd": str(Path.home()), "session_id": "s-out",
        "tool_input": {"subagent_type": "route-deep"}}, base_env)
    check("agent dispatch OUTSIDE the allowlist", proc, expect_silent=True)
    wrote = ledger_lines() - before
    print(("ok   " if wrote == 0 else "FAIL ")
          + f"...and it writes NO ledger line (wrote {wrote})")
    if wrote:
        failures.append("agent_hook logs outside the allowlist")
    # The control: inside the allowlist it must still write, or the gate above
    # would be green because the hook is broken rather than because it is right.
    before = ledger_lines()
    run("agent_hook.py", {"tool_name": "Agent", "cwd": IN_ALLOWLIST,
                          "session_id": "s-in",
                          "tool_input": {"subagent_type": "route-scout"}}, base_env)
    wrote = ledger_lines() - before
    print(("ok   " if wrote == 1 else "FAIL ")
          + f"inside the allowlist it still writes exactly one line (wrote {wrote})")
    if wrote != 1:
        failures.append("agent_hook stopped logging inside the allowlist")

    if network:
        print("\n--- live jev ---")
        proc = run("route_hook.py", {"prompt": long_prompt + " z", "cwd": IN_ALLOWLIST},
                   dict(base_env, ROUTE_HOOK_TIMEOUT="10"))
        check("real classification", proc)
        if proc.stdout.strip():
            ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            print("\n       advice as I would see it:")
            for line in ctx.splitlines():
                print("         " + line)

        print("\n--- the duplicate gate must not charge twice ---")
        same = {"prompt": "please go and refactor the whole of the usage reporting module now",
                "cwd": IN_ALLOWLIST}
        env = dict(base_env, ROUTE_HOOK_TIMEOUT="10")
        first = run("route_hook.py", same, env)
        second = run("route_hook.py", same, env)
        ok = bool(first.stdout.strip()) and not second.stdout.strip()
        print(("ok   " if ok else "FAIL ")
              + f"second identical prompt is silent (first={bool(first.stdout.strip())},"
                f" second={bool(second.stdout.strip())})")
        if not ok:
            failures.append("duplicate gate")

    recs = []
    if tmp.exists():
        recs = [json.loads(line) for line in tmp.read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = {}
    for r in recs:
        kinds[r.get("type")] = kinds.get(r.get("type"), 0) + 1
    print(f"\nledger written to the temp file: {kinds or 'nothing'}")
    print("(the real ledger at ~/.claude/route-ledger.jsonl was not touched)")

    print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
