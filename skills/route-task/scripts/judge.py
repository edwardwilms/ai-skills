#!/usr/bin/env python3
"""Ask jev whether an output actually served the task. Closes the loop.

    python judge.py --task "fix the flaky migration test" --output-file out.txt
    python judge.py --task "..." --output-file - --mechanical pass
    python judge.py --task "..." --output-file o.txt --session-id abc --json

WHAT THIS IS WORTH, AND WHAT IT IS NOT

The ledger records what was recommended and what I dispatched. Neither says
whether the result was any good. This asks jev - three noul questions and one
score - and writes the answer as a `judgement` record. For a tenth of a cent it
turns the route table from a set of preferences into something with an outcome
attached.

Three limits, all declared, because a judge that oversells itself is worse than
no judge:

1. THE JUDGE READS TEXT. IT DOES NOT RUN CODE. It cannot know whether the test
   passed, whether the build is green, or whether the edit even applied. Pass
   --mechanical pass|fail when you have a real signal, and the mechanical signal
   WINS: the record keeps both, separately, and the report trusts the mechanical
   one. A judge presented as a measurement is the worst outcome here.

2. THE JUDGE AND THE ROUTER ARE THE SAME MODEL. A systematic classification
   error can be confirmed by its own author. That is why the two signals that do
   NOT come from jev - the mechanical check, and my own disagreement recorded by
   agent_hook - are the ones the report leans on.

3. MEASURED 2026-10-21: USE IT TO FLAG, NOT TO APPROVE. Given a hedging,
   evidence-free output it answered served 0.08, quality "useless",
   out-of-depth 0.88 - clean detection. Given a good output that named the
   cause, the fix and "ran it 50 times: 50 passes" it answered served 0.37.
   The first wording of the second question asked whether the output claimed
   anything "it did not establish", which a text-only judge reads as "I cannot
   see your test run" - so it penalised precisely the outputs that report
   evidence. Rewording it to ask about INTERNAL consistency is the fix below.
   After the fix, served separates the two cases cleanly: 0.80 against 0.06,
   and out-of-depth 0.10 against 0.88. Those two questions discriminate and are
   kept. `quality` is directionally right but under-credits ("acceptable" for a
   clean fix). The asymmetry still holds and the report relies on it: a bad
   verdict from this judge is worth acting on, a good one is not worth trusting.

4. IT SEES A SUMMARY, NOT THE WORK. The output is scrubbed and capped like
   everything else sent to a third party, so a long diff reaches it truncated.
   Treat a verdict on a trimmed input as weak evidence; the record says when it
   was trimmed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import jev  # noqa: E402
import state as state_mod  # noqa: E402

# The output gets a bigger budget than a prompt: judging needs to see the shape
# of what was produced. Still capped - you pay per input token, and the whole
# point of the scrubber is that what is not sent cannot leak.
MAX_OUTPUT_CHARS = 2400

QUESTIONS = {
    "served": {
        "type": "noul",
        "instructions": "Did this output accomplish the task it was given? Answer no "
                        "if it describes a plan instead of a result, stops partway, or "
                        "solves a different problem. An output that REPORTS a completed "
                        "result counts as yes even though you cannot verify the report "
                        "yourself - you are judging the work, not re-running it.",
    },
    # REMOVED, and the removal is the finding. A third question asked whether
    # the output's reasoning contained an unsupported leap. Measured twice,
    # 2026-10-21: the first wording penalised any output that reported evidence
    # (0.79 on a good answer, because a text-only judge cannot see a test run).
    # Rewording it to ask only about INTERNAL consistency did not fix the
    # direction: it then scored the good output 0.74 and the hedging one 0.58 -
    # inverted. A question that is measured to point the wrong way is worse than
    # no question: it costs tokens and someone will read 0.74 and act on it.
    # Do not add it back without a measurement showing it separates the cases.
    "needed_more_capability": {
        "type": "noul",
        "instructions": "Does this read like the work of a model that was out of its "
                        "depth - hedging, going in circles, or asking for the task to "
                        "be restated? Answer no for an output that is simply short "
                        "because the task was simple.",
    },
    "quality": {
        "type": "score",
        "instructions": "How good is this output for the task as given?",
        "criteria": [
            "useless: wrong, or did not address the task",
            "weak: partially right, needs redoing",
            "acceptable: does the job, rough edges",
            "good: does the job cleanly",
            "excellent: does the job and surfaces something I had not asked about",
        ],
    },
}


def judge(task: str, output: str, *, timeout: float = 20.0) -> tuple[dict, bool]:
    """(response, was_trimmed). Both the task and the output are scrubbed."""
    scrubbed = state_mod.scrub(output)
    trimmed = len(scrubbed) > MAX_OUTPUT_CHARS
    payload = {
        "task": state_mod.cap(state_mod.scrub(task)),
        "output": state_mod.cap(scrubbed, MAX_OUTPUT_CHARS),
    }
    return jev.decide(payload, QUESTIONS, timeout=timeout), trimmed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", required=True)
    ap.add_argument("--output-file", required=True, help="path, or '-' for stdin")
    ap.add_argument("--mechanical", choices=("pass", "fail", "none"), default="none",
                    help="a real check's result - tests, typecheck, lint, exit code")
    ap.add_argument("--session-id", default="", help="joins this to the decision record")
    ap.add_argument("--fleet", help="which fleet produced the output")
    ap.add_argument("--model")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args()

    output = sys.stdin.read() if a.output_file == "-" else Path(a.output_file).read_text(
        encoding="utf-8", errors="replace")
    if not output.strip():
        print("refused: the output is empty, and there is nothing to judge", file=sys.stderr)
        return 2

    try:
        data, trimmed = judge(a.task, output, timeout=a.timeout)
    except jev.JevUnavailable as exc:
        print(f"jev unavailable ({exc.reason}): no judgement written", file=sys.stderr)
        return 3

    answers = data["answers"]
    levels = [c.split(":", 1)[0] for c in QUESTIONS["quality"]["criteria"]]
    raw = jev.score(answers, "quality")
    record = {
        "type": "judgement",
        "session_id": a.session_id,
        "fleet": a.fleet,
        "model": a.model,
        "served": round(jev.noul(answers, "served"), 3),
        "needed_more_capability": round(jev.noul(answers, "needed_more_capability"), 3),
        "quality": levels[max(0, min(len(levels) - 1, int(round(raw))))],
        "quality_score": round(raw, 2),
        # Kept SEPARATE from the jev verdict, never folded into it. The report
        # trusts this one when it is present, because it is the only signal here
        # that did not come from the same model that did the routing.
        "mechanical": None if a.mechanical == "none" else a.mechanical,
        "output_trimmed": trimmed,
        "judge_cost_usd": jev.cost_usd(data),
        "via": "judge",
    }

    if not a.no_ledger:
        try:
            import ledger
            ledger.append(record)
        except Exception:
            pass

    if a.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    print(f"served {record['served']:.2f} | quality {record['quality']} "
          f"| out-of-depth {record['needed_more_capability']:.2f}")
    if record["mechanical"]:
        print(f"mechanical check: {record['mechanical']} <- this is what the report "
              "believes; the line above is a text opinion")
    else:
        print("no mechanical check given: this is a TEXT OPINION only. The judge did "
              "not run anything.")
    if trimmed:
        print(f"the output was trimmed to {MAX_OUTPUT_CHARS} chars before judging: "
              "weak evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
