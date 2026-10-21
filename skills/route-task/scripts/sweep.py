#!/usr/bin/env python3
"""Replay a saved calibration through candidate thresholds. Offline, free.

    python sweep.py                       # the shipped grid
    python sweep.py --verifiable 0.5 --confidence 0.6 --jury 0.7 --max-complexity routine

WHY THIS EXISTS

calibration-v1.json holds, per prompt, every number jev returned. The policy is
a pure function of those numbers plus the repo signals, so the effect of a
different threshold is COMPUTABLE from data already paid for - no second API
call, no guessing. This is what turns "DECLARED GUESS" in routes.v1.json into a
number with a count behind it.

It reports the three things a threshold change actually moves:

  cheap%   share of work routed to a cheap/bulk tier. THIS is the saving, and it
           is not the same as "downgrades": a class whose policy already starts
           at the cheap tier cannot be downgraded, so counting downgrades
           undercounts the saving to zero. (Measured 2026-10-21: 0 downgrades
           while 27% of work was already going cheap. The first report I wrote
           printed the downgrade count as the headline and would have made me
           conclude the router was useless.)
  frontier% share sent to a frontier model - the quota that gets burned.
  jury%     how often a second opinion is suggested. Above ~25% this stops being
           advice and becomes noise.

HONEST LIMIT: a threshold that routes more work cheap is not automatically
better. This says what WOULD have been routed, never whether the cheap route
would have SUCCEEDED. Only the ledger's dispatch/judgement records answer that.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route as route_mod  # noqa: E402

DEFAULT_IN = route_mod.SKILL_DIR / "calibration-v1.json"


def replay(results, config, labels, *, test_runner):
    cheap = frontier = jury = downgraded = 0
    fleets = {}
    for row in results:
        verdict = route_mod.Verdict(
            klass=row["class"],
            kind_confidence=row["confidence"],
            complexity_idx=labels.index(row["complexity"]),
            complexity_label=row["complexity"],
            verifiable=row["verifiable"],
            reversible=row["reversible"],
            needs_repo_context=row["needs_repo_context"],
            second_opinion=row["second_opinion"],
            source="jev",
        )
        signals = {"test_runner": row.get("test_runner") or test_runner}
        r = route_mod.apply_policy(verdict, signals, config, labels)
        if r.tier in ("cheap", "bulk"):
            cheap += 1
        if r.tier == "frontier":
            frontier += 1
        if r.jury:
            jury += 1
        if any("downgraded" in x for x in r.reasons):
            downgraded += 1
        fleets[r.fleet] = fleets.get(r.fleet, 0) + 1
    n = len(results)
    return {
        "cheap_pct": cheap / n, "frontier_pct": frontier / n,
        "jury_pct": jury / n, "downgrades": downgraded, "fleets": fleets,
    }


def patched(config, *, verifiable, confidence, max_complexity, jury, reversible):
    cfg = copy.deepcopy(config)
    dr = cfg["thresholds"]["downgrade_requires"]
    dr["verifiable_at_least"] = verifiable
    dr["kind_confidence_at_least"] = confidence
    dr["max_complexity_label"] = max_complexity
    dr["reversible_at_least"] = reversible
    cfg["thresholds"]["jury_at_second_opinion"]["value"] = jury
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="infile", default=str(DEFAULT_IN))
    ap.add_argument("--assume-test-runner", default="pytest",
                    help="used only for rows saved before test_runner was recorded")
    ap.add_argument("--verifiable", type=float)
    ap.add_argument("--confidence", type=float)
    ap.add_argument("--reversible", type=float)
    ap.add_argument("--jury", type=float)
    ap.add_argument("--max-complexity")
    a = ap.parse_args()

    data = json.loads(Path(a.infile).read_text(encoding="utf-8"))
    results = data["results"]
    questions = route_mod.load_json(route_mod.QUESTIONS_FILE)
    config = route_mod.load_json(route_mod.ROUTES_FILE)
    labels = route_mod.complexity_labels(questions)
    live = config["thresholds"]
    dr = live["downgrade_requires"]

    if any(v is not None for v in (a.verifiable, a.confidence, a.jury, a.max_complexity,
                                   a.reversible)):
        grid = [(
            a.verifiable if a.verifiable is not None else dr["verifiable_at_least"],
            a.confidence if a.confidence is not None else dr["kind_confidence_at_least"],
            a.max_complexity or dr["max_complexity_label"],
            a.jury if a.jury is not None else live["jury_at_second_opinion"]["value"],
            a.reversible if a.reversible is not None else dr["reversible_at_least"],
        )]
    else:
        grid = [
            # verifiable, confidence, max_complexity, jury, reversible
            (0.70, 0.60, "routine", 0.55, 0.60),   # what v1 shipped with
            (0.50, 0.60, "routine", 0.70, 0.60),
            (0.50, 0.60, "involved", 0.70, 0.60),
            (0.40, 0.60, "routine", 0.70, 0.60),
            (0.40, 0.60, "involved", 0.70, 0.60),
            (0.50, 0.50, "involved", 0.70, 0.50),
            (0.30, 0.50, "involved", 0.70, 0.40),  # deliberately loose, for contrast
        ]

    print(f"{len(results)} prompts from {Path(a.infile).name} "
          f"(questions={data['questions_version']}, measured {data['measured_at']})\n")
    print(f"{'verif':>6} {'conf':>5} {'max-cx':>9} {'jury':>5} {'revers':>6} | "
          f"{'cheap':>6} {'frontier':>8} {'jury%':>6} {'down':>5} | fleets")
    print("-" * 96)
    for verifiable, confidence, max_cx, jury, reversible in grid:
        cfg = patched(config, verifiable=verifiable, confidence=confidence,
                      max_complexity=max_cx, jury=jury, reversible=reversible)
        m = replay(results, cfg, labels, test_runner=a.assume_test_runner)
        shipped = (verifiable, confidence, max_cx, jury, reversible) == (
            dr["verifiable_at_least"], dr["kind_confidence_at_least"],
            dr["max_complexity_label"], live["jury_at_second_opinion"]["value"],
            dr["reversible_at_least"])
        fleets = " ".join(f"{k}:{v}" for k, v in sorted(m["fleets"].items(), key=lambda kv: -kv[1]))
        print(f"{verifiable:6.2f} {confidence:5.2f} {max_cx:>9} {jury:5.2f} {reversible:6.2f} | "
              f"{m['cheap_pct']:5.1%} {m['frontier_pct']:7.1%} {m['jury_pct']:5.1%} "
              f"{m['downgrades']:5d} | {fleets}" + ("   <- live" if shipped else ""))
    print("\ncheap% is the saving; downgrades alone undercounts it to zero.")
    print("A threshold that routes more cheaply is not thereby better: this says what")
    print("WOULD be routed, never whether it would have WORKED. Only the ledger says that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
