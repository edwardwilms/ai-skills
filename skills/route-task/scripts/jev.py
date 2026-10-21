#!/usr/bin/env python3
"""Jev decision API client: typed, calibrated answers instead of prose.

    python jev.py --selftest                 # the vendor's own documented example
    python jev.py --selftest --no-network    # offline checks only (budget, redaction)

Jev (https://jevtypesafeai.com) answers *typed questions* about a state: `choice`
(pick a label), `score` (place on an ordered scale), `noul` (calibrated 0-1 yes/no).
It does not write code. Here it is the router's classifier, never an executor.

Billing, from the vendor docs (read 2026-10-21): USD 0.42 per 1M **input** tokens,
output free, prepaid credits. Their own example: 62 input tokens = USD 0.000026.
A routing call is ~500 tokens, so ~USD 0.0002. The decision is effectively free;
what it saves (not burning a frontier model on a scan) is not.

Everything here is stdlib. No requests, no SDK: this runs inside a 2-second hook
budget on a machine where `pip install` into the system Python is not a given.

API key comes from $JEV_API_KEY and is NEVER written to stdout, stderr, a log, or
an exception message. `_redact` is applied to every outbound error string.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://jevtypesafeai.com/api/v1/decide"

# Vendor-documented budgets (2026-10-21). state+questions combined, and per question.
# Raisable to ~262k with the TypeSafe-Danger-Zone-Long-Context header; we never do -
# a router that needs 262k tokens to decide where to send a task has already lost.
MAX_TOKENS_TOTAL = 64_000
MAX_TOKENS_PER_QUESTION = 32_000

# Reasons are a closed set so callers can branch on them instead of parsing prose.
REASONS = (
    "no_api_key",      # $JEV_API_KEY unset or empty
    "timeout",         # no answer inside the caller's budget
    "unauthorized",    # 401/403 - bad or revoked key
    "out_of_credit",   # 402 - prepaid credits exhausted
    "rate_limited",    # 429
    "server_error",    # 5xx
    "bad_response",    # 2xx that is not the documented shape
    "over_budget",     # refused locally, before spending anything
    "leak_guard",      # refused locally: the payload still carried a key/id shape
    "network",         # DNS, TLS, connection reset
)


class JevUnavailable(Exception):
    """Jev did not answer. Distinct from "answered, and said X".

    Callers MUST be able to tell these apart: the router falls back to a
    deterministic table when jev is unavailable, but trusts a low-confidence
    answer differently than no answer at all. A bare Exception would collapse
    the two and the fallback would fire on real answers.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REASONS:  # programmer error, not a runtime condition
            raise ValueError(f"unknown reason {reason!r}; add it to REASONS")
        self.reason = reason
        self.detail = _redact(detail)
        super().__init__(f"{reason}: {self.detail}" if self.detail else reason)


def _redact(text: str) -> str:
    """Strip anything that looks like a jev key from a string bound for a human.

    Keys are `jv_live_...`. A stack trace that echoes the request headers, or an
    API error that quotes the Authorization line, would otherwise publish the key
    into a log file or a hook transcript.
    """
    text = re.sub(r"jv_(live|test)_[A-Za-z0-9_\-]+", "jv_***", text)
    return re.sub(r"(?i)(authorization|bearer)\s*:?\s*\S+", r"\1 ***", text)


def est_tokens(obj: object) -> int:
    """Rough input-token estimate. An ESTIMATE, not a measurement.

    chars/4 is the usual English heuristic. We use it only to refuse obviously
    oversized payloads locally; the authoritative count comes back in
    `usage.input_tokens`, and the ledger records that one.
    """
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)


def check_budget(state: object, questions: dict) -> None:
    """Refuse locally what the API would reject, before spending a round trip."""
    for name, q in questions.items():
        if est_tokens(q) > MAX_TOKENS_PER_QUESTION:
            raise JevUnavailable(
                "over_budget",
                f"question {name!r} is ~{est_tokens(q)} tokens, limit {MAX_TOKENS_PER_QUESTION}",
            )
    total = est_tokens(state) + est_tokens(questions)
    if total > MAX_TOKENS_TOTAL:
        raise JevUnavailable(
            "over_budget",
            f"state+questions is ~{total} tokens, limit {MAX_TOKENS_TOTAL}; trim the state",
        )


def api_key(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    import env
    value, _where = env.key("JEV_API_KEY")
    if not value:
        raise JevUnavailable(
            "no_api_key",
            "set JEV_API_KEY. It is read from the process environment and, on "
            "Windows, from HKCU/Environment - so a shell or a Claude Code "
            "session started before it was set is still covered. Neither had it. "
            "On Linux/macOS, export it in your shell profile and restart Claude "
            "Code from that shell.",
        )
    return value


def decide(
    state: object,
    questions: dict,
    *,
    model: str | None = None,
    timeout: float = 2.0,
    key: str | None = None,
    long_context: bool = False,
) -> dict:
    """POST one batch of typed questions. Returns the parsed response dict.

    Raises JevUnavailable (never a bare urllib error) for every failure mode.
    Batch related questions into ONE call: you pay per input token, and the
    state is the expensive part - asking six questions about one state costs
    barely more than asking one.
    """
    check_budget(state, questions)
    payload: dict = {"state": state, "questions": questions}
    if model:
        payload["model"] = model
    body = json.dumps(payload, ensure_ascii=False)
    # THE SEND GATE. Callers scrub before they get here, and this is not a
    # second scrubber: it REFUSES. Scrubbing is ordered regex substitution, and
    # one reordered rule or one caller that forgets to scrub ships a key with no
    # error anywhere. So the final bytes are checked against the same shapes
    # once more, and anything key- or id-shaped means nothing is sent. Callers
    # already treat JevUnavailable as "fall back", so a hook stays fail-open:
    # the cost of a false positive is one turn without advice.
    import state as state_mod
    shape = state_mod.find_leak(body)
    if shape:
        raise JevUnavailable("leak_guard", f"payload carried a {shape} shape; nothing was sent")

    headers = {
        "Authorization": f"Bearer {api_key(key)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if long_context:
        headers["TypeSafe-Danger-Zone-Long-Context"] = "true"

    req = urllib.request.Request(
        ENDPOINT,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001 - the body is a nicety, never a requirement
            pass
        reason = {401: "unauthorized", 403: "unauthorized", 402: "out_of_credit",
                  429: "rate_limited"}.get(exc.code)
        if reason is None:
            reason = "server_error" if exc.code >= 500 else "bad_response"
        raise JevUnavailable(reason, f"HTTP {exc.code} {body}") from exc
    except TimeoutError as exc:
        raise JevUnavailable("timeout", f"no answer in {timeout}s") from exc
    except urllib.error.URLError as exc:
        # URLError wraps socket.timeout on some Python builds; keep them distinct.
        if isinstance(exc.reason, TimeoutError):
            raise JevUnavailable("timeout", f"no answer in {timeout}s") from exc
        raise JevUnavailable("network", str(exc.reason)) from exc
    except OSError as exc:
        raise JevUnavailable("network", str(exc)) from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise JevUnavailable("bad_response", f"not JSON: {raw[:200]}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevUnavailable("bad_response", f"no answers map: {raw[:200]}")
    missing = sorted(set(questions) - set(data["answers"]))
    if missing:
        # Partial answers are worse than none: the policy would read an absent
        # question as a default and route on it silently.
        raise JevUnavailable("bad_response", f"unanswered: {', '.join(missing)}")
    return data


# --- accessors -------------------------------------------------------------
# The response shape is the vendor's, not ours. Reading it through these three
# functions means a shape change breaks in one place instead of nine.

def choice(answers: dict, name: str) -> tuple[str, float]:
    """(winning label, confidence). Confidence is the vendor's own field."""
    a = answers[name]
    return a["choice"], float(a.get("confidence", 0.0))


def score(answers: dict, name: str) -> float:
    """Fractional position on the question's ordered scale."""
    return float(answers[name]["score"])


def noul(answers: dict, name: str) -> float:
    """Calibrated 0-1 probability of yes."""
    return float(answers[name]["noul"])


def cost_usd(data: dict) -> float:
    return float(data.get("usage", {}).get("cost_usd", 0.0) or 0.0)


def credits_remaining_usd(data: dict) -> float | None:
    v = data.get("usage", {}).get("credits_remaining_usd")
    return None if v is None else float(v)


# --- selftest --------------------------------------------------------------

# Verbatim from the vendor's SKILL.md (read 2026-10-21). Using their example means
# a failure here is either our client or their API - not our question design.
DOC_EXAMPLE_STATE = (
    "Customer: I've been charged twice and nobody has replied for 3 days."
)
DOC_EXAMPLE_QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Where should this go?",
        "criteria": {
            "billing": "billing, payments or refunds",
            "bug": "the product is broken",
            "account": "login or access",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this?",
        "criteria": ["routine", "today", "urgent", "critical, about to churn"],
    },
    "escalate": {"type": "noul", "instructions": "Escalate to a human now?"},
}
# What the docs show for that input. We assert the SHAPE and the documented
# winner, not the exact probabilities - those are the model's to change.
DOC_EXAMPLE_ROUTE = "billing"


def _selftest(network: bool) -> int:
    failures = []

    def check(label: str, ok: bool, got: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  <- {got}" if not ok else ""))
        if not ok:
            failures.append(label)

    # 1. The key never leaks, even when the input is the key itself.
    leaked = _redact("boom: Authorization: Bearer jv_live_abc123DEF456")
    check("redaction strips jv_live_ keys", "jv_live_abc123" not in leaked, leaked)
    check("redaction strips the bearer value", "abc123DEF456" not in leaked, leaked)

    # 2. A missing key is an actionable refusal, not a KeyError.
    # Clearing the process environment is no longer enough to create that state:
    # api_key() also reads HKCU/Environment, where the real key lives. So the
    # registry fallback is opted out of for the duration of this one check.
    saved = os.environ.pop("JEV_API_KEY", None)
    os.environ["ROUTE_NO_HKCU"] = "1"
    try:
        api_key()
        check("missing key raises", False, "no exception")
    except JevUnavailable as exc:
        check("missing key raises no_api_key", exc.reason == "no_api_key", exc.reason)
        check("missing-key message is actionable", "JEV_API_KEY" in str(exc), str(exc))
    finally:
        os.environ.pop("ROUTE_NO_HKCU", None)
        if saved is not None:
            os.environ["JEV_API_KEY"] = saved

    # 3. Oversized payloads are refused locally - before spending anything.
    try:
        check_budget("x" * (MAX_TOKENS_TOTAL * 4 + 8), {"q": {"type": "noul"}})
        check("oversized state refused locally", False, "no exception")
    except JevUnavailable as exc:
        check("oversized state refused locally", exc.reason == "over_budget", exc.reason)

    # 4. The send gate refuses a key that reached it unscrubbed - BEFORE the
    # key lookup. A fake key built from a repeated pattern; the shape is under
    # test, never content. urlopen is replaced for the duration: found by
    # mutation-testing this very check, a gate that regresses would otherwise
    # make this --no-network test POST its fixture to the real endpoint.
    sent = []

    def no_network(*_args, **_kwargs):
        sent.append(1)
        raise urllib.error.URLError("selftest: the network is off")

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = no_network
    try:
        decide({"task": "deploy with mw_" + "Ab3" * 14}, {"q": {"type": "noul"}},
               key="unused")
        check("send gate refuses an unscrubbed key", False, "no exception")
    except JevUnavailable as exc:
        check("send gate refuses an unscrubbed key", exc.reason == "leak_guard", exc.reason)
        check("the refusal names the shape, not the key",
              "Ab3Ab3" not in str(exc) and "musclewiki-key" in str(exc), str(exc))
    finally:
        urllib.request.urlopen = real_urlopen
    check("nothing reached the network", not sent, f"{len(sent)} attempt(s)")

    # 5. An unknown reason is a programmer error, caught at construction.
    try:
        JevUnavailable("made_up_reason")
        check("unknown reason rejected", False, "no exception")
    except ValueError:
        check("unknown reason rejected", True)

    if not network:
        print("\n(skipped the live call: --no-network)")
    else:
        try:
            data = decide(DOC_EXAMPLE_STATE, DOC_EXAMPLE_QUESTIONS, timeout=10.0)
        except JevUnavailable as exc:
            check(f"live call to the documented example ({exc.reason})", False, exc)
        else:
            got, conf = choice(data["answers"], "route")
            check(f"documented winner is {DOC_EXAMPLE_ROUTE!r}", got == DOC_EXAMPLE_ROUTE, got)
            check("confidence is a 0-1 float", 0.0 <= conf <= 1.0, conf)
            check("score question answered", isinstance(score(data["answers"], "urgency"), float))
            check("noul question answered", 0.0 <= noul(data["answers"], "escalate") <= 1.0)
            print(
                f"\n     usage: {data.get('usage', {}).get('input_tokens')} input tokens"
                f" · US$ {cost_usd(data):.6f}"
                f" · credits left US$ {credits_remaining_usd(data)}"
            )

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks")
    ap.add_argument("--no-network", action="store_true", help="offline checks only")
    ap.add_argument("--state", help="ad-hoc: the state to evaluate")
    ap.add_argument("--questions", help="ad-hoc: path to a questions JSON file, or '-'")
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args()

    if a.selftest:
        return _selftest(network=not a.no_network)
    if not (a.state and a.questions):
        ap.error("need --selftest, or both --state and --questions")

    text = sys.stdin.read() if a.questions == "-" else open(a.questions, encoding="utf-8").read()
    try:
        data = decide(a.state, json.loads(text), timeout=a.timeout)
    except JevUnavailable as exc:
        print(json.dumps({"error": exc.reason, "detail": exc.detail}), file=sys.stderr)
        return 3
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
