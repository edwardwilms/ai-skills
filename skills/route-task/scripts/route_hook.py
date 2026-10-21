#!/usr/bin/env python3
"""UserPromptSubmit hook: advise which fleet/model/effort this turn wants.

Wired by install.ps1 / install.sh as:
    python <abs path>/route_hook.py

THE ONE RULE: THIS FAILS OPEN, ALWAYS.

No key, no network, bad JSON, jev down, config unreadable, project outside the
allowlist, unknown exception - every one of them exits 0 with no output and the
turn proceeds untouched. A router that can cost me a turn is not worth having,
and the cost asymmetry is brutal: the advice saves some quota, a crash costs the
whole turn. Everything below is wrapped accordingly.

WHAT IT PRINTS (at most three lines, as additionalContext)

    bug-hunt | involved | verifiable 0.31 | confidence 0.91 -> claude/opus/high
      . no downgrade: class bug-hunt never downgrades; verifiable 0.31 < 0.55
      . NOT auto-dispatchable: decide this one yourself

WHAT IT STAYS SILENT ABOUT, and why each gate exists

  * project not in routes.v1.json allowlist -> silent. This is the real privacy
    control: outside the list nothing is classified, charged or logged.
  * prompt under 40 chars, or starting with / or ! -> silent. Slash commands and
    one-word replies are not tasks.
  * a prompt already seen inside the TTL -> silent, and not charged. Retyping or
    retrying must not pay twice.
  * confidence below thresholds.hook_abstains_below_confidence -> silent, but a
    ledger line IS written recording the abstention. Measured on 192 real
    prompts: 31.8% fall under the confidence gate, and reading them shows why -
    they are conversational follow-ups ("pode continuar com a outra frente")
    that refer to a thread the classifier cannot see. Advising on a coin flip
    spends credit, costs context, and teaches me to distrust the good advice.

INPUT CONTRACT. Claude Code passes a JSON object on stdin. The exact field names
are not extractable from the shipped binary, so this PROBES several spellings
(prompt/user_prompt/message, cwd/project_dir/workspace) and falls back to
treating all of stdin as the prompt. Guessing one spelling would make the hook
silently inert - which looks identical to "nothing to advise".
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Overridable for the same reason the ledger is: the selftest must not share
# state with the live hook. Found by selftest_hooks.py, 2026-10-21 - the three
# fallback cases passed offline and failed on the next run, because the real
# cache still held their prompt hashes inside the TTL. A fixed path also means
# two checkouts would silence each other's hooks.
CACHE = Path(os.environ.get("ROUTE_HOOK_CACHE")
             or Path.home() / ".claude" / ".route-hook-cache.json")
CACHE_TTL_S = 300
MIN_PROMPT_CHARS = 40
SKIP_PREFIXES = ("/", "!")
MAX_STDIN_BYTES = 256 * 1024

PROMPT_KEYS = ("prompt", "user_prompt", "message", "text", "input")
CWD_KEYS = ("cwd", "project_dir", "projectDir", "workspace", "working_directory")
SESSION_KEYS = ("session_id", "sessionId")


def read_payload() -> dict:
    raw = sys.stdin.read(MAX_STDIN_BYTES) if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        # Not JSON: treat the whole thing as the prompt rather than giving up.
        return {"prompt": raw}
    return data if isinstance(data, dict) else {"prompt": raw}


def first(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def seen_recently(digest: str) -> bool:
    """True if this prompt was classified inside the TTL. Best effort: a broken
    cache means we might pay twice, never that we fail."""
    now = time.time()
    entries = {}
    try:
        if CACHE.exists():
            entries = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entries = {}
    if not isinstance(entries, dict):
        entries = {}
    entries = {k: v for k, v in entries.items()
               if isinstance(v, (int, float)) and now - v < CACHE_TTL_S}
    hit = digest in entries
    entries[digest] = now
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        pass
    return hit


def emit(context: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }, ensure_ascii=True))


def advice(route) -> str:
    lines = [route.one_liner()]
    # One reason only. The hook competes for the top of my context window with
    # the actual task; three bullets of policy trace is a worse trade than one.
    if route.reasons:
        lines.append("  . " + route.reasons[0])
    if route.jury and route.jury_from_signal:
        lines.append("  . a second opinion looks worth it: route-task jury")
    if not route.auto_dispatch_safe:
        lines.append("  . NOT auto-dispatchable: decide this one yourself")
    return "\n".join(lines[:3])


def main() -> int:
    payload = read_payload()
    prompt = first(payload, PROMPT_KEYS)
    if len(prompt.strip()) < MIN_PROMPT_CHARS or prompt.lstrip().startswith(SKIP_PREFIXES):
        return 0

    import route as route_mod

    cwd = Path(first(payload, CWD_KEYS) or os.getcwd())
    config = route_mod.load_json(route_mod.ROUTES_FILE)
    project = route_mod.project_name(cwd)
    if not route_mod.allowed(config, project):
        return 0

    digest = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:16]
    if seen_recently(digest):
        return 0

    timeout = float(os.environ.get("ROUTE_HOOK_TIMEOUT", "2.0"))
    shares = None
    try:
        import ledger
        shares = ledger.dispatch_shares(
            config["thresholds"]["quota_pressure"]["window_hours"])
    except Exception:
        shares = None

    route, _state = route_mod.decide(prompt, cwd=cwd, timeout=timeout, shares=shares)

    abstain = route_mod.abstain_below()
    abstained = (route.verdict.source == "jev"
                 and route.verdict.kind_confidence < abstain)

    try:
        import ledger
        ledger.append({
            "type": "decision",
            "session_id": first(payload, SESSION_KEYS),
            "project": project,
            "state_hash": digest,
            "abstained": abstained,
            **{k: v for k, v in route.to_dict().items() if k != "reasons"},
        })
    except Exception:
        pass

    if not abstained:
        emit(advice(route))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        # Deliberately BaseException, and deliberately silent. A traceback on
        # stderr would be shown to me every turn, and a non-zero exit from a
        # UserPromptSubmit hook can block the prompt. Neither is an acceptable
        # price for an advisory line. Debug with --selftest, not in production.
        sys.exit(0)
