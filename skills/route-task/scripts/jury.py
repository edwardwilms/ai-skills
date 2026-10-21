#!/usr/bin/env python3
"""Ask the same question of independent fleets and print the answers side by side.

    python jury.py --prompt "should the ledger live in the repo or outside it?"
    python jury.py --prompt-file - --context-file diff.txt
    python jury.py --prompt "..." --with-claude      # spawn claude -p as a third voice

WHY THIS NEVER MERGES THE ANSWERS

A jury that synthesises hides the disagreement, and the disagreement is the
entire signal. If two fleets reach the same conclusion by different routes, that
is worth more than either answer; if they split, that is the most valuable
output this script can produce, and averaging it away would destroy it. So: one
block per juror, verbatim, no summary, no vote count.

WHO THE JURORS ARE, AND THAT THEY ARE NOT PEERS

  codex   gpt-6-astra at xhigh, read-only sandbox. Subscription: zero marginal
          dollars. Can read the repository.
  gemini  gemini-3.8-flash over the direct API. MEASURED 2026-10-21: this key is
          flash-only (the pro models answer 429, gemini-4-argon answers
          TerminalQuotaError), so this is the WEAKEST voice of the three and the
          output says so. It cannot read files; it sees only what it is given.
  claude  opus. Skipped by default when $CLAUDECODE is set, because the session
          running this script is already the Claude voice and spawning a nested
          one spends quota to duplicate an opinion you already have.
          --with-claude forces it, for standalone or non-Claude callers.

Presenting three unequal voices as peers would be the failure mode here, so each
block is labelled with its model and whether it could see the repository.

Cost: two of the three jurors are subscription fleets, which is why the policy
can afford a jury threshold as low as 0.70. Gemini's share is metered but tiny
over the direct API (measured: 10 input tokens for a short question).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ASK = """{question}

Answer in at most 180 words. Lead with your position in one sentence, then the
single strongest reason for it, then the strongest argument AGAINST it that you
can see. If you do not have enough information to take a position, say exactly
what is missing instead of hedging - a confident answer built on a gap is the
failure this panel exists to catch.
"""


def wrap(question: str, context: str | None) -> str:
    prompt = ASK.format(question=question)
    if context:
        prompt += "\n--- context provided to every juror ---\n" + context + "\n"
    return prompt


def juror_codex(prompt: str, cd: Path | None, timeout: float) -> dict:
    import cli
    import codex_task
    model, effort = codex_task.tier_spec("frontier")
    argv = [
        *cli.resolve("codex"), "exec", "-", "--sandbox", "read-only", "--ephemeral",
        "--skip-git-repo-check", "--color", "never", "-m", model,
        "-c", f"model_reasoning_effort={effort}",
    ]
    if cd:
        argv += ["--cd", str(cd)]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    # Plain exec prints a banner and a transcript; the final answer is the tail
    # after the last "codex" speaker marker.
    out = proc.stdout
    marker = "\ncodex\n"
    text = out.rsplit(marker, 1)[-1].strip() if marker in out else out.strip()
    for noise in ("tokens used",):
        if noise in text:
            text = text.split(noise)[0].strip()
    return {"juror": "codex", "model": f"{model}/{effort}", "reads_repo": True,
            "text": text, "rc": proc.returncode}


def juror_gemini(prompt: str, timeout: float) -> dict:
    import gemini_task
    model = gemini_task.tier_spec("cheap")["model"]
    try:
        payload = gemini_task.ask(prompt, model, timeout=timeout)
    except gemini_task.GeminiUnavailable as exc:
        # Caught HERE, and not only by the net in main(), so the block keeps the
        # model name and says what happened. Until gemini_task grew this
        # exception the same condition was a SystemExit: it rode through the
        # future, out of `except Exception`, and took the panel with it. 429 is
        # the NORMAL answer from the pro models on this key, so the most likely
        # juror failure was also the one that discarded the other two answers.
        return {"juror": "gemini", "model": model, "reads_repo": False,
                "text": "", "weakest": True, "rc": -1,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"juror": "gemini", "model": model, "reads_repo": False,
            "text": gemini_task.text_of(payload).strip(),
            "weakest": True,
            "usage": gemini_task.usage_of(payload), "rc": 0}


def juror_claude(prompt: str, cd: Path | None, timeout: float) -> dict:
    import cli
    import route as route_mod
    spec = route_mod.load_json(route_mod.ROUTES_FILE)["fleets"]["claude"]["tiers"]["frontier"]
    argv = [
        *cli.resolve("claude"), "-p",
        "--model", spec["model"], "--effort", spec["effort"],
        # A juror opines on what it was given; it does not go editing things.
        "--disallowedTools", "Bash,Edit,Write,NotebookEdit",
    ]
    env = dict(os.environ)
    # Without this the nested CLI can inherit a session identity that makes it
    # try to resume rather than answer.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_CHILD_SESSION", None)
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace",
                          cwd=str(cd) if cd else None, env=env)
    return {"juror": "claude", "model": f"{spec['model']}/{spec['effort']}",
            "reads_repo": False, "text": (proc.stdout or proc.stderr).strip(),
            "rc": proc.returncode}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file", help="path, or '-' for stdin")
    ap.add_argument("--context-file", help="text handed to EVERY juror")
    ap.add_argument("--cd", help="working root for the jurors that can read files")
    ap.add_argument("--with-claude", action="store_true",
                    help="spawn claude -p even when running inside a Claude session")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args()

    if a.prompt_file:
        question = sys.stdin.read() if a.prompt_file == "-" else Path(a.prompt_file).read_text(
            encoding="utf-8", errors="replace")
    elif a.prompt:
        question = a.prompt
    else:
        ap.error("need --prompt or --prompt-file")

    context = None
    if a.context_file:
        context = Path(a.context_file).read_text(encoding="utf-8", errors="replace")
    prompt = wrap(question, context)
    cd = Path(a.cd) if a.cd else None

    inside_claude = bool(os.environ.get("CLAUDECODE"))
    want_claude = a.with_claude or not inside_claude

    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        if not a.no_codex:
            jobs["codex"] = pool.submit(juror_codex, prompt, cd, a.timeout)
        if not a.no_gemini:
            jobs["gemini"] = pool.submit(juror_gemini, prompt, a.timeout)
        if want_claude:
            jobs["claude"] = pool.submit(juror_claude, prompt, cd, a.timeout)
        started = time.time()
        verdicts = []
        for name, fut in jobs.items():
            try:
                verdicts.append(fut.result())
            # The net, and it stays `Exception` on purpose: every juror failure
            # that is a RUNTIME CONDITION is now an Exception (gemini's was a
            # SystemExit and escaped this), while BaseException is deliberately
            # not caught - a Ctrl-C must still stop the panel instead of being
            # rendered as a juror's opinion.
            except Exception as exc:
                verdicts.append({"juror": name, "model": "?", "reads_repo": None,
                                 "text": "", "error": f"{type(exc).__name__}: {exc}",
                                 "rc": -1})
    elapsed = time.time() - started

    if not a.no_ledger:
        try:
            import ledger
            for v in verdicts:
                # The token counts travel into the record. They were captured
                # from the response and dropped here, while report.py sums
                # `input_tokens` over fleet == "gemini" and counts the call - so
                # every jury showed up in the report as a Gemini call of ZERO
                # tokens, in the one path that has no size ceiling (a jury
                # forwards the whole --context-file to the juror).
                usage = v.get("usage") or {}
                ledger.append({
                    "type": "dispatch", "fleet": v["juror"], "model": v["model"],
                    "mode": "jury", "project": Path(cd or Path.cwd()).name,
                    "status": "ok" if v.get("text") else "empty",
                    "seconds": round(elapsed, 1), "via": "jury",
                    "cost_unit": "quota" if v["juror"] in ("claude", "codex")
                    else "tokens_metered_usd_unpriced",
                    **{k: val for k, val in usage.items() if val is not None},
                })
        except Exception:
            pass

    if a.json:
        print(json.dumps({"question": question, "seconds": round(elapsed, 1),
                          "verdicts": verdicts}, indent=2, ensure_ascii=False))
        return 0

    for v in verdicts:
        reads = {True: "can read the repo", False: "text-only",
                 None: "did not run"}[v["reads_repo"]]
        flags = " | WEAKEST VOICE (flash-only on this key)" if v.get("weakest") else ""
        print("=" * 78)
        print(f"{v['juror'].upper()}  [{v['model']} | {reads}{flags}]")
        print("=" * 78)
        print(v.get("error") or v["text"] or "(no answer)")
        print("")

    if inside_claude and not a.with_claude:
        print("-" * 78)
        print("CLAUDE was not spawned: this session is already the Claude voice, and a")
        print("nested one would spend quota to duplicate an opinion you have for free.")
        print("Add your own position as the third juror. --with-claude overrides this.")
    print("")
    print("No synthesis on purpose. Where they disagree is the finding; a merged")
    print("answer would have hidden exactly that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
