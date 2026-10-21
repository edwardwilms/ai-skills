#!/usr/bin/env python3
"""Run one task on the Gemini fleet, over whichever transport fits its size.

    python gemini_task.py --ask "which of these 400 files mention retry logic?"
    python gemini_task.py --bulk "inventory every MCP tool and where it is used"
    python gemini_task.py --ask-file - --json

TWO TRANSPORTS, AND THE MEASUREMENT THAT SPLIT THEM

Measured 2026-10-21 with the prompt "Reply with exactly: OK":

    gemini CLI    12,206 prompt tokens
    direct API         5 prompt tokens

A ~2,400x floor. The CLI injects a large system prompt and environment context
on every invocation, which is exactly what you want when the model must read
files and run tools, and pure waste when you are asking one question. So:

  --ask   direct API. No floor. For single questions, classification, scoring,
          and the jury's third voice. Cannot read files - give it the text.
  --bulk  the CLI, agentic, --approval-mode plan (read-only). For digesting a
          large corpus where 12k tokens is noise against a 500k payload.

Choosing one transport for both would have meant either paying 12k tokens per
question or losing the ability to read a repo at all.

ALSO MEASURED: this key is FLASH-ONLY. gemini-pro-latest and
gemini-3.1-pro-preview answer 429; gemini-4-argon answers TerminalQuotaError.
So Gemini is the cheap/bulk fleet here and never the frontier one - and in the
jury it is the weakest of three voices, which jury.py says out loud rather than
presenting three peers.

COST: tokens are recorded, dollars are NOT invented. Gemini is metered, but I
have no verified price table for these models, and a made-up USD figure in the
ledger would be worse than an honest token count.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
STALL_S = 300.0
MAX_S = 1800.0
POLL_S = 5.0
NEWLINE = chr(10)

# 503 from generativelanguage was transient on the very first probe of this API,
# then arrived three times in a row - so a 503 is not reliably a blip. More
# tries, exponential backoff, and a message that says which failure it was.
RETRIES_503 = 5


class GeminiUnavailable(Exception):
    """Gemini did not answer. Distinct from "answered, and said X".

    This mirrors jev.JevUnavailable, for the reason written in its docstring: a
    library must not collapse "unavailable" into a bare exit. Every failure mode
    here used to `raise SystemExit` - no key, 429, non-503 HTTP, unreachable -
    and SystemExit derives from BaseException, so `except Exception` does not
    catch it.

    MEASURED: jury.py wraps each juror in `except Exception` precisely so that
    one juror falling over cannot take the panel down, and a 429 walked straight
    through it ("ESCAPED jury.py's handler -> SystemExit: gemini API said HTTP
    429"), killing the whole panel and discarding the codex answer that was
    already in hand. A 429 is not an exceptional event on this key either: the
    pro models answer it by default, which is why this one had to be an
    Exception. Only main() turns it into an exit status.
    """


def api_key() -> str:
    import env
    value, _where = env.key("GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not value:
        raise GeminiUnavailable(
            "set GEMINI_API_KEY. Read from the process environment and, on "
            "Windows, from HKCU/Environment too - neither had it. On Linux/macOS, "
            "export it in your shell profile and restart Claude Code from that shell.")
    return value


def tier_spec(tier: str) -> dict:
    import route as route_mod
    return route_mod.load_json(route_mod.ROUTES_FILE)["fleets"]["gemini"]["tiers"][tier]


def ask(prompt: str, model: str, *, timeout: float = 120.0) -> dict:
    """One question over the direct API. No 12k floor, no file access."""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    url = f"{API_BASE}/{model}:generateContent?key={api_key()}"
    last = ""
    for attempt in range(RETRIES_503):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            # 503 was observed as transient on the very first probe of this API;
            # 429 is the free-tier rate limit and retrying it fast makes it worse.
            if exc.code == 503 and attempt < RETRIES_503 - 1:
                time.sleep(1.5 * (2 ** attempt))
                continue
            if exc.code == 429:
                raise GeminiUnavailable(
                    f"gemini rate-limited ({model}). Measured on this key: the pro "
                    "models answer 429 and only flash is usable.") from exc
            raise GeminiUnavailable(f"gemini API said {last}") from exc
        except OSError as exc:
            raise GeminiUnavailable(f"gemini API unreachable: {exc}") from exc
    raise GeminiUnavailable(f"gemini API said {last} after {RETRIES_503} tries")


def text_of(payload: dict) -> str:
    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


def usage_of(payload: dict) -> dict:
    u = payload.get("usageMetadata", {}) or {}
    return {
        "input_tokens": u.get("promptTokenCount"),
        "total_tokens": u.get("totalTokenCount"),
    }


def bulk(prompt: str, model: str, cd: Path | None, *, stall: float, max_s: float) -> tuple[str, str]:
    """Agentic digest through the CLI. (status, text).

    --approval-mode plan is read-only by construction: the bulk tier reads a
    large corpus and reports, it never edits. The router advises; it does not
    write to anyone's repo on its own.
    """
    import cli
    work = Path(tempfile.mkdtemp(prefix="gemini-task-"))
    out_file = work / "out.json"
    argv = [
        *cli.resolve("gemini"),
        "-m", model,
        # -p is what selects headless mode; the CLI APPENDS stdin to it, so an
        # empty -p plus the prompt on stdin keeps a large prompt off the command
        # line entirely (Windows caps it at 32,767 chars). Passing "-p -" sends
        # the literal string "-" as the prompt, which is what the first version
        # of this did.
        "-p", "",
        "--approval-mode", "plan",
        # Measured 2026-10-21: in an untrusted folder the CLI prints "approval
        # mode overridden to default" and then REFUSES to run headless. It is
        # --approval-mode plan that turns the warning into a refusal, so the two
        # flags have to travel together. Skipping the trust gate is sound only
        # BECAUSE plan mode is read-only: what the gate guards is already closed
        # by the flag above it. Remove plan mode and this becomes unsafe.
        "--skip-trust",
        "-o", "json",
    ]
    env = dict(os.environ)
    env["GEMINI_API_KEY"] = api_key()
    with open(out_file, "w", encoding="utf-8") as fout, \
            open(work / "err.txt", "w", encoding="utf-8") as ferr:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=fout, stderr=ferr,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=str(cd) if cd else None, env=env)
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
                size = out_file.stat().st_size
            except OSError:
                size = last_size
            if size > last_size:
                last_size, last_change = size, time.time()
            now = time.time()
            if now - last_change >= stall or now - started >= max_s:
                print(f"[gemini] watchdog kill: stalled={now - last_change:.0f}s "
                      f"elapsed={now - started:.0f}s", file=sys.stderr)
                proc.kill()
                proc.wait()
                return "killed", ""
    raw = out_file.read_text(encoding="utf-8", errors="replace")
    # Surface stderr on failure. The first version discarded it, and a refusal
    # with a perfectly clear explanation ("not running in a trusted directory,
    # use --skip-trust") arrived as status=empty with no text - which looks
    # exactly like a model that had nothing to say.
    stderr_tail = ""
    try:
        stderr_tail = (work / "err.txt").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    # -o json wraps the answer, but the CLI also prints warnings above it
    # ("256-color support not detected", "Ripgrep is not available"), so the
    # payload has to be found rather than assumed to start at byte 0.
    at = raw.find("{")
    if at < 0:
        return "empty", (raw.strip() + (NEWLINE + stderr_tail if stderr_tail else "")).strip()
    try:
        payload = json.loads(raw[at:])
    except ValueError:
        return "partial", raw[at:].strip()
    return "ok", (payload.get("response") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ask", help="one question over the direct API (no token floor)")
    ap.add_argument("--ask-file", help="path, or '-' for stdin; same transport as --ask")
    ap.add_argument("--bulk", help="agentic digest through the CLI (read-only)")
    ap.add_argument("--bulk-file", help="path, or '-' for stdin; same transport as --bulk")
    ap.add_argument("--model", help="default: the pin in routes.v1.json")
    ap.add_argument("--cd", help="working root for --bulk")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stall", type=float, default=STALL_S)
    ap.add_argument("--max", type=float, default=MAX_S)
    ap.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args()

    def body(direct, from_file):
        if from_file:
            return sys.stdin.read() if from_file == "-" else Path(from_file).read_text(
                encoding="utf-8", errors="replace")
        return direct

    ask_text = body(a.ask, a.ask_file)
    bulk_text = body(a.bulk, a.bulk_file)
    if bool(ask_text) == bool(bulk_text):
        ap.error("pass exactly one of --ask/--ask-file or --bulk/--bulk-file")

    tier = "bulk" if bulk_text else "cheap"
    model = a.model or tier_spec(tier)["model"]
    started = time.time()

    # The ONLY place that turns "gemini is unavailable" into an exit status. The
    # exit code and the message are what SystemExit produced before, so the CLI
    # contract is unchanged - what changed is that a library caller (jury.py)
    # can now catch it with `except Exception` like any other juror failure.
    try:
        if ask_text:
            payload = ask(ask_text, model)
            status, text, usage = "ok", text_of(payload), usage_of(payload)
            if not text:
                status = "empty"
        else:
            cd = Path(a.cd) if a.cd else None
            status, text = bulk(bulk_text, model, cd, stall=a.stall, max_s=a.max)
            usage = {}
    except GeminiUnavailable as exc:
        raise SystemExit(str(exc)) from exc

    elapsed = time.time() - started
    if not a.no_ledger:
        try:
            import ledger
            ledger.append({
                "type": "dispatch", "fleet": "gemini", "model": model, "tier": tier,
                "transport": "api" if ask_text else "cli",
                "project": Path(a.cd or Path.cwd()).name,
                "status": status, "seconds": round(elapsed, 1), "via": "gemini_task",
                # Metered in dollars, but no verified price table for these models
                # exists here. Tokens are the measurement; a USD figure would be
                # a guess wearing the clothes of a measurement.
                "cost_unit": "tokens_metered_usd_unpriced",
                **{k: v for k, v in usage.items() if v is not None},
            })
        except Exception:
            pass

    if a.json:
        print(json.dumps({"status": status, "model": model, "text": text,
                          "seconds": round(elapsed, 1), **usage}, indent=2,
                         ensure_ascii=False))
    else:
        print(text)
        if usage.get("input_tokens") is not None:
            print(f"\n[{model} | {usage['input_tokens']} input tokens | "
                  f"{elapsed:.1f}s]", file=sys.stderr)
    return 0 if status == "ok" else 12


if __name__ == "__main__":
    sys.exit(main())
