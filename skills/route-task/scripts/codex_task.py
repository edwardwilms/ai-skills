#!/usr/bin/env python3
"""Run one task on the Codex fleet at a chosen model and effort.

    python codex_task.py --prompt "rename X to Y across the repo" --tier cheap
    python codex_task.py --prompt-file - --model gpt-6.1-sol --effort high
    python codex_task.py --review --base origin/staging --tier frontier
    python codex_task.py --list-models

WHY CODEX IS A FIRST-CLASS FLEET HERE

It is logged in by ChatGPT subscription (verified 2026-10-21: ~/.codex/auth.json
carries tokens and a null OPENAI_API_KEY), so its marginal cost is zero dollars
and its scarce resource is quota - quota that is INDEPENDENT of Claude's. Idle
Codex capacity while the Claude window drains is the single biggest waste this
router exists to fix.

WHAT WAS INHERITED FROM skills/full-review/scripts/run_codex.py

That script already paid for these lessons; re-deriving them would be throwing
the receipts away:

  * prompt on stdin via `codex exec -`, with --sandbox read-only --ephemeral
  * NEVER `codex exec review`: it rejects --sandbox, hangs silently, and has
    returned clean reviews that dropped real findings. --review below builds a
    review PROMPT for plain `exec` instead.
  * a stall watchdog, because Codex can go >150s between events while reasoning
  * one retry, then give up with a status that says "inconclusive", not "clean"

WHAT WAS MEASURED HERE, 2026-10-21

  * `codex exec` has NO --effort flag. Effort goes through the config override
    `-c model_reasoning_effort=<level>`. The key is real: it appears in
    codex.exe and not in the code-mode host. Passing an invalid VALUE to it does
    NOT error, and neither does a wholly invented key - so -c cannot tell you
    whether it worked. That is why the level is validated locally, against
    models_cache.json, before the process starts.
  * token floor ~2,742 for a 5-token prompt. Worth knowing before sending many
    small tasks, though it is 4x cheaper than the Gemini CLI's ~12,200.
  * --output-last-message writes the final answer to a file, which is far more
    robust than digging it out of the JSONL stream. The JSONL is still written,
    because the watchdog needs something that grows.

Exit: 0 ok, 10 the model answered but flagged a problem, 12 inconclusive
(empty, partial, killed), 2 refused locally (bad model/effort).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"
STALL_S = 420.0   # inherited: Codex goes >150s between events while reasoning
MAX_S = 1800.0
POLL_S = 5.0


def load_models() -> list[dict]:
    try:
        return json.loads(MODELS_CACHE.read_text(encoding="utf-8")).get("models", [])
    except (OSError, ValueError):
        return []


def validate(model: str, effort: str | None) -> str | None:
    """Return an error string, or None. Checked HERE because -c cannot tell us.

    A silently-ignored effort override is the worst outcome: the run looks
    successful, the ledger records an effort that was never applied, and the
    measurements built on it are quietly wrong.
    """
    models = load_models()
    if not models:
        return None  # cache unreadable: cannot judge, so do not block
    entry = next((m for m in models if m.get("slug") == model), None)
    if entry is None:
        slugs = ", ".join(sorted(m.get("slug", "") for m in models if m.get("visibility") != "hide"))
        return f"model {model!r} is not in models_cache.json. Available: {slugs}"
    if effort:
        levels = [lv.get("effort") for lv in entry.get("supported_reasoning_levels", [])]
        if effort not in levels:
            return f"model {model!r} does not support effort {effort!r}. Supports: {', '.join(levels)}"
    return None


def tier_spec(tier: str) -> tuple[str, str]:
    """Model and effort for a tier, read from routes.v1.json - the same file the
    policy uses, so the executor cannot drift from the decision."""
    import route as route_mod
    spec = route_mod.load_json(route_mod.ROUTES_FILE)["fleets"]["codex"]["tiers"][tier]
    return spec["model"], spec.get("effort")


def build_args(model, effort, sandbox, cd, jsonl, last_msg, schema=None) -> list[str]:
    # NOT the bare name: `codex` on PATH is a .ps1 here and Popen cannot exec it.
    # cli.resolve finds the vendored native binary. See cli.py for the measurement.
    import cli
    args = [
        *cli.resolve("codex"), "exec", "-",
        "--sandbox", sandbox,
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--color", "never",
        "-m", model,
        "-o", str(last_msg),
    ]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    if cd:
        args += ["--cd", str(cd)]
    if schema:
        args += ["--output-schema", str(schema)]
    return args


def attempt(prompt, args, jsonl, last_msg, *, stall, max_s, label):
    """Run once with a stall watchdog. Returns "exited" or "killed".

    The watchdog measures growth of the JSONL stream, not wall time alone: a
    model that is reasoning hard looks identical to one that has hung, and only
    the event stream tells them apart.
    """
    with open(jsonl, "w", encoding="utf-8") as fout, \
            open(str(jsonl) + ".err", "w", encoding="utf-8") as ferr:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=fout, stderr=ferr, text=True,
            encoding="utf-8", errors="replace",
        )
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        started = last_change = time.time()
        last_size = 0
        while proc.poll() is None:
            time.sleep(POLL_S)
            try:
                size = jsonl.stat().st_size
            except OSError:
                size = last_size
            if size > last_size:
                last_size, last_change = size, time.time()
            now = time.time()
            if now - last_change >= stall or now - started >= max_s:
                print(f"[{label}] watchdog kill: stalled={now - last_change:.0f}s "
                      f"elapsed={now - started:.0f}s", file=sys.stderr)
                proc.kill()
                proc.wait()
                return "killed"
    return "exited"


def final_message(last_msg, jsonl):
    """(status, text). Prefers -o; falls back to digging through the JSONL."""
    try:
        text = last_msg.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return "ok", text
    except OSError:
        pass
    msgs = []
    try:
        for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item", {}) if ev.get("type") == "item.completed" else {}
            if item.get("type") == "agent_message" and item.get("text"):
                msgs.append(item["text"])
    except OSError:
        pass
    if msgs:
        return "partial", "\n\n---\n\n".join(msgs)
    return "empty", ""


def git_diff(base, cd):
    out = subprocess.run(["git", "diff", f"{base}...HEAD"], cwd=str(cd or Path.cwd()),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout


REVIEW_PROMPT = """Review this diff for defects. You are the independent reviewer:
the code was written by a different model, and your value is entirely in what it
missed.

For each finding give: severity (BLOCKER/HIGH/MEDIUM/LOW), the file:line, and a
concrete failure scenario - inputs or state that produce a wrong result. A
finding with no failure scenario is a style opinion; say so or drop it.

End with exactly one line: VERDICT: LGTM or VERDICT: CHANGES_REQUESTED.
If you could not read enough of the diff to judge, end with VERDICT: INCONCLUSIVE
rather than LGTM. Silence about what you could not see reads as approval, and
that is the failure mode that matters here.

--- diff ---
{diff}
"""


# --- selftest --------------------------------------------------------------
# This file had no test, and the defect it was missing is exactly the one the
# retry loop exists to prevent: a PARTIAL answer leaving as if it were clean.
# No codex is run - a fake binary stands in, writes a final message on its first
# invocation and then hangs so the watchdog kills it, then exits non-zero
# WITHOUT writing on its second. That is the measured behaviour of the real
# thing (it writes -o only at the end of a turn), and it is what made a stale
# file read back as a fresh success.

FAKE_CODEX = '''import pathlib, sys, time
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
calls = pathlib.Path(__file__).with_name("calls.txt")
n = len(calls.read_text(encoding="utf-8").split()) if calls.exists() else 0
calls.write_text("x " * (n + 1), encoding="utf-8")
if n == 0:
    out.write_text("ACHEI 1 de 9 arquivos antes de travar.\\nVERDICT: LGTM\\n",
                   encoding="utf-8")
    # Hang, so the watchdog kills this attempt. BOUNDED: on Windows the launcher
    # is a .bat, so killing the process kills cmd.exe and can orphan this one.
    time.sleep(6)
sys.exit(1)   # second invocation: dies without writing anything
'''


def _fake_bin(work: Path) -> Path:
    """A stand-in for the codex binary that cli.resolve will accept as CODEX_BIN.

    A launcher is needed because resolve() returns ONE argv element, and neither
    Windows nor POSIX will exec a .py by path.
    """
    (work / "fake_codex.py").write_text(FAKE_CODEX, encoding="utf-8")
    if os.name == "nt":
        launcher = work / "fake_codex.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{work / "fake_codex.py"}" %*\r\n',
            encoding="utf-8")
    else:
        launcher = work / "fake_codex.sh"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{work / "fake_codex.py"}" "$@"\n',
            encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def _selftest() -> int:
    import contextlib
    import io

    failures = []

    def check(label: str, ok: bool, got: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  <- {got!r}" if not ok else ""))
        if not ok:
            failures.append(label)

    work = Path(tempfile.mkdtemp(prefix="codex-selftest-"))
    launcher = _fake_bin(work)

    # The thing under test is the retry loop, not local validation (which has
    # its own refusal path), and models_cache.json has no entry for a fake.
    real_validate, real_poll = validate, POLL_S
    globals()["validate"] = lambda model, effort: None
    globals()["POLL_S"] = 0.2
    env_before = os.environ.get("CODEX_BIN")
    os.environ["CODEX_BIN"] = str(launcher)
    argv_before = sys.argv
    sys.argv = ["codex_task.py", "--prompt", "say something", "--model", "fake-model",
                "--no-ledger", "--stall", "1", "--max", "25"]
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = main()
    finally:
        globals()["validate"], globals()["POLL_S"] = real_validate, real_poll
        sys.argv = argv_before
        if env_before is None:
            os.environ.pop("CODEX_BIN", None)
        else:
            os.environ["CODEX_BIN"] = env_before

    out = buf.getvalue()
    calls = len((work / "calls.txt").read_text(encoding="utf-8").split())
    check("the fake binary was invoked twice (killed, then retried)", calls == 2, calls)
    # THE assertion. Before the fix this was rc 0 with the first attempt's text
    # printed as a clean answer, verdict and all.
    check("a partial first attempt plus an empty retry is NOT rc 0", rc != 0, rc)
    check("inconclusive is reported as 12", rc == 12, rc)
    # Whatever text is printed must carry the warning. The failing run printed
    # the fragment, verdict line included, with nothing around it.
    check("whatever is printed is labelled partial",
          "[partial output" in err.getvalue(), err.getvalue()[-200:])
    check("the fragment is still shown (it is the only information there is)",
          "antes de travar" in out, out[:200])

    # And the control: a run that answers cleanly must still be rc 0, or the fix
    # above would have turned every run into a failure.
    good = work / "fake_ok.py"
    good.write_text('import pathlib, sys\n'
                    'a = sys.argv[1:]\n'
                    'pathlib.Path(a[a.index("-o") + 1]).write_text("tudo certo",'
                    ' encoding="utf-8")\n', encoding="utf-8")
    if os.name == "nt":
        ok_launcher = work / "fake_ok.bat"
        ok_launcher.write_text(f'@echo off\r\n"{sys.executable}" "{good}" %*\r\n',
                               encoding="utf-8")
    else:
        ok_launcher = work / "fake_ok.sh"
        ok_launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{good}" "$@"\n',
                               encoding="utf-8")
        ok_launcher.chmod(0o755)
    globals()["validate"] = lambda model, effort: None
    os.environ["CODEX_BIN"] = str(ok_launcher)
    sys.argv = ["codex_task.py", "--prompt", "say something", "--model", "fake-model",
                "--no-ledger"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc_ok = main()
    finally:
        globals()["validate"] = real_validate
        sys.argv = argv_before
        if env_before is None:
            os.environ.pop("CODEX_BIN", None)
        else:
            os.environ["CODEX_BIN"] = env_before
    check("a clean answer is still rc 0", rc_ok == 0, rc_ok)
    check("and its text is printed", "tudo certo" in buf.getvalue(), buf.getvalue()[:120])

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file", help="path, or '-' for stdin")
    ap.add_argument("--tier", choices=("cheap", "work", "frontier"),
                    help="read model+effort from routes.v1.json")
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    ap.add_argument("--sandbox", default="read-only",
                    choices=("read-only", "workspace-write", "danger-full-access"),
                    help="default read-only: the router advises, it does not write")
    ap.add_argument("--cd", help="working root for the agent")
    ap.add_argument("--review", action="store_true", help="review a diff instead")
    ap.add_argument("--base", default="origin/staging", help="diff base for --review")
    ap.add_argument("--stall", type=float, default=STALL_S)
    ap.add_argument("--max", type=float, default=MAX_S)
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the retry loop cannot pass a partial as clean;"
                         " runs a fake binary, never codex")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.list_models:
        for m in load_models():
            if m.get("visibility") == "hide":
                continue
            levels = ",".join(lv.get("effort", "") for lv in m.get("supported_reasoning_levels", []))
            print(f"{m['slug']:<20} default={m.get('default_reasoning_level', '?'):<7} [{levels}]")
            print(f"{'':<20} {m.get('description', '')}")
        return 0

    model, effort = a.model, a.effort
    if a.tier:
        t_model, t_effort = tier_spec(a.tier)
        model = model or t_model
        effort = effort or t_effort
    if not model:
        ap.error("need --tier or --model")

    problem = validate(model, effort)
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2

    cd = Path(a.cd) if a.cd else None
    if a.review:
        diff = git_diff(a.base, cd)
        if not diff.strip():
            print(f"refused: git diff {a.base}...HEAD is empty", file=sys.stderr)
            return 2
        prompt = REVIEW_PROMPT.format(diff=diff)
    elif a.prompt_file:
        prompt = sys.stdin.read() if a.prompt_file == "-" else Path(a.prompt_file).read_text(
            encoding="utf-8", errors="replace")
    elif a.prompt:
        prompt = a.prompt
    else:
        ap.error("need --prompt, --prompt-file or --review")

    work = Path(tempfile.mkdtemp(prefix="codex-task-"))
    jsonl, last_msg = work / "events.jsonl", work / "final.txt"
    args = build_args(model, effort, a.sandbox, cd, jsonl, last_msg)

    started = time.time()
    status, text = "empty", ""
    for i, label in enumerate(("codex", "codex-retry")):
        # TRUNCATE THE -o FILE TOO. attempt() opens the JSONL with "w", so that
        # one is always fresh; this one was not, and MEASURED on the real binary:
        # `codex -o <file>` neither creates nor truncates it at startup - it
        # writes only at the end of a turn. So attempt 1 killed AFTER writing,
        # plus attempt 2 dying WITHOUT writing, made final_message() read
        # attempt 1's text back while run_state was "exited": the demotion to
        # "partial" never fired and a truncated answer went out as rc 0 with no
        # "[partial output]" warning. The ancestor this file inherits from
        # (skills/full-review/scripts/run_codex.py) dug the text out of the
        # JSONL, which IS truncated, so it was immune by construction - the
        # regression arrived with the -o file. See _selftest.
        last_msg.unlink(missing_ok=True)
        run_state = attempt(prompt, args, jsonl, last_msg, stall=a.stall, max_s=a.max,
                            label=label)
        got, fresh = final_message(last_msg, jsonl)
        if run_state == "killed" and got == "ok":
            # Inherited lesson: a killed run that still left a final message is
            # PARTIAL, never ok. Trusting it is how a truncated review passes.
            got = "partial"
        if got == "ok":
            status, text = got, fresh
            break
        # Worst status across attempts wins; the best TEXT survives. A fragment
        # from attempt 1 is worth printing under the INCONCLUSIVE warning, where
        # the "nothing at all" of attempt 2 would print nothing and explain
        # nothing - but neither is ever rc 0, which is why the status is kept at
        # the worse of the two rather than at the last one seen.
        if fresh and not text:
            status, text = "partial", fresh
        elif not text:
            status = got
        print(f"[{label}] {got}; {'retrying once' if i == 0 else 'giving up'}",
              file=sys.stderr)

    elapsed = time.time() - started
    if not a.no_ledger:
        try:
            import ledger
            ledger.append({
                "type": "dispatch", "fleet": "codex", "model": model, "effort": effort,
                "tier": a.tier, "project": Path(cd or Path.cwd()).name,
                "mode": "review" if a.review else "exec",
                "status": status, "seconds": round(elapsed, 1), "via": "codex_task",
                # Dollars are the wrong unit and writing 0.0 would be a lie of a
                # different kind: this is subscription QUOTA being spent.
                "cost_unit": "quota",
            })
        except Exception:
            pass

    if status == "empty":
        print(f"inconclusive: no final message after 2 attempts ({elapsed:.0f}s)",
              file=sys.stderr)
        print(f"events: {jsonl}", file=sys.stderr)
        return 12
    print(text)
    if status == "partial":
        print(f"[partial output after {elapsed:.0f}s - INCONCLUSIVE, not clean]",
              file=sys.stderr)
        return 12
    if a.review and "VERDICT: CHANGES_REQUESTED" in text:
        return 10
    if a.review and "VERDICT: LGTM" not in text:
        return 12
    return 0


if __name__ == "__main__":
    sys.exit(main())
