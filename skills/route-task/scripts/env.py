#!/usr/bin/env python3
"""Read an API key, falling back to the Windows User environment.

    python env.py --check JEV_API_KEY GEMINI_API_KEY

WHY THIS EXISTS

On Windows, `setx` / the Environment GUI writes to HKCU/Environment, and only
processes started AFTERWARDS inherit it. Measured 2026-10-21: JEV_API_KEY and
GEMINI_API_KEY were set as User variables, and the running Claude Code process
did not have them - so the hook it spawns would not either.

That failure is silent and expensive in a specific way: the router does not
break, it DEGRADES. jev comes back "no_api_key", the keyword fallback answers,
and the advice keeps appearing - just dumber, with no sign that the classifier
was never consulted. Of all the ways this system can fail, a working-but-blind
one is the worst, because it is the only one that does not announce itself.

So the process environment is checked first, and HKCU/Environment second. Read
only, no writes, no PowerShell subprocess. Non-Windows returns the process
environment and nothing else - so on Linux and macOS the key has to be exported
in the shell Claude Code is started from, and install.sh warns when it is not.
"""
from __future__ import annotations

import os
import sys

HKCU_ENVIRONMENT = "Environment"


def from_windows_user_env(name: str) -> str:
    """The HKCU value, or "". Never raises."""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HKCU_ENVIRONMENT) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value or "")
    except OSError:
        return ""


# Set this to make key() skip the registry. It exists because the registry
# fallback made the "no key configured" state UNREACHABLE for a test: jev.py's
# selftest created it by clearing the process environment, and the fallback then
# found the real key in HKCU anyway. A fallback that cannot be turned off cannot
# be tested, and the no-key path is the one that has to produce an actionable
# message rather than a KeyError.
OPT_OUT = "ROUTE_NO_HKCU"


def key(*names: str) -> tuple[str, str]:
    """(value, where). `where` is "env", "hkcu" or "" - the caller can say which."""
    for name in names:
        value = os.environ.get(name, "")
        if value.strip():
            return value.strip(), "env"
    if os.environ.get(OPT_OUT, "").strip():
        return "", ""
    for name in names:
        value = from_windows_user_env(name)
        if value.strip():
            return value.strip(), "hkcu"
    return "", ""


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", nargs="+", required=True)
    a = ap.parse_args()
    missing = 0
    for name in a.check:
        value, where = key(name)
        if value:
            # Length only. Printing a prefix of a key is still printing a key.
            print(f"ok   {name:<18} found in {where} ({len(value)} chars)")
        else:
            print(f"MISS {name:<18} not in the process env nor HKCU/Environment")
            missing += 1
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
