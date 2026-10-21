#!/usr/bin/env python3
"""Resolve an external agent CLI to an argv prefix that subprocess can actually run.

    python cli.py --selftest

THE PROBLEM, MEASURED 2026-10-21

On this machine `codex`, `gemini` and `claude` on PATH are PowerShell shims
(<nodejs dir>/codex.ps1). subprocess.Popen(["codex", ...]) dies with
[WinError 2] "cannot find the file", because CreateProcess does not execute
.ps1 - and the error names a missing FILE, which sends you looking for the wrong
thing entirely. This is the same shape as the known claude.exe lesson: the thing
on PATH is not the thing you can exec.

Resolution order, most direct first:

  1. $CODEX_BIN / $GEMINI_BIN / $CLAUDE_BIN - explicit beats clever
  2. a native .exe on PATH, or the vendored one inside the npm tree (codex ships
     one at .../codex-win32-x64/vendor/.../bin/codex.exe)
  3. node + the bundle entry point, read out of the generated .cmd shim (gemini
     has no native binary: its shim runs `node .../bundle/gemini.js`)
  4. the .cmd shim through %COMSPEC% - works, costs one extra process
  5. the bare name, which is correct everywhere that is not Windows

Prompts always travel on STDIN, never argv, so even the %COMSPEC% path has no
quoting hazard - and that is also what keeps a long prompt from hitting the
32,767-char command-line limit.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BACKSLASH = chr(92)
SEPARATORS = chr(92) + "/"

ENV_OVERRIDE = {"codex": "CODEX_BIN", "gemini": "GEMINI_BIN", "claude": "CLAUDE_BIN"}


def _from_cmd_shim(name: str) -> list[str] | None:
    """Read the npm-generated .cmd and pull out the `node <entry>.js` it calls.

    Deliberately regex-free. The first version used a pattern with a literal
    backslash in it, which a heredoc collapsed into an escaped parenthesis -
    "unbalanced parenthesis at position 15", which names nothing useful.
    """
    shim = shutil.which(name + ".cmd")
    if not shim:
        return None
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Anchor on the .js and scan BACK to its own %dp0%. Anchoring forward found
    # the shim's FIRST "%dp0% - which is the `IF EXIST "%dp0%node.exe"` probe -
    # and produced a path spanning both, which failed is_file() and silently
    # dropped us to the cmd.exe fallback. It worked, by accident, one extra
    # process at a time.
    marker = '"%dp0%'
    end = text.rfind('.js"')
    if end < 0:
        return None
    at = text.rfind(marker, 0, end)
    if at < 0:
        return None
    relative = text[at + len(marker):end + 3].lstrip(SEPARATORS)
    entry = Path(shim).parent / relative.replace(BACKSLASH, os.sep)
    node = shutil.which("node")
    if node and entry.is_file():
        return [node, str(entry)]
    return None


def _vendored_exe(name: str) -> str | None:
    """Hunt for a native .exe inside the npm tree next to the shim."""
    shim = shutil.which(name + ".cmd") or shutil.which(name + ".ps1") or shutil.which(name)
    if not shim:
        return None
    root = Path(shim).parent / "node_modules"
    if not root.is_dir():
        return None
    for candidate in root.glob(f"**/bin/{name}.exe"):
        return str(candidate)
    return None


def resolve(name: str) -> list[str]:
    override = os.environ.get(ENV_OVERRIDE.get(name, ""), "")
    if override:
        return [override]

    direct = shutil.which(name + ".exe")
    if direct:
        return [direct]

    if os.name == "nt":
        vendored = _vendored_exe(name)
        if vendored:
            return [vendored]
        via_node = _from_cmd_shim(name)
        if via_node:
            return via_node
        shim = shutil.which(name + ".cmd")
        if shim:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", shim]

    found = shutil.which(name)
    return [found] if found else [name]


def _selftest() -> int:
    failures = []
    for name in ("codex", "gemini"):
        argv = resolve(name)
        shown = argv[0] if len(argv) == 1 else " ".join(
            [Path(argv[0]).name] + [Path(a).name for a in argv[1:]])
        try:
            proc = subprocess.run(argv + ["--version"], capture_output=True, text=True,
                                  timeout=90, encoding="utf-8", errors="replace")
            version = (proc.stdout or proc.stderr).strip().splitlines()
            ok = proc.returncode == 0 and bool(version)
            detail = version[-1][:40] if version else f"rc={proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, str(exc)[:70]
        print(f"{'ok  ' if ok else 'FAIL'} {name:<8} -> {shown}\n       {detail}")
        if not ok:
            failures.append(name)

    # The regression that produced this file: the bare name must not be what we
    # hand to Popen on Windows, because PATH resolves it to a .ps1.
    if os.name == "nt":
        for name in ("codex", "gemini"):
            argv = resolve(name)
            bad = argv == [name] or argv[0].lower().endswith(".ps1")
            print(f"{'FAIL' if bad else 'ok  '} {name} does not resolve to a bare name or .ps1")
            if bad:
                failures.append(f"{name}-unrunnable")

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
