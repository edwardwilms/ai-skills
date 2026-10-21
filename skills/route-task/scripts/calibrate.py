#!/usr/bin/env python3
"""Replay real prompt history through jev and measure whether the classifier is
good enough to wire into a hook. Phase 0: nothing is installed by this script.

    python calibrate.py --project integrations-assistant --limit 50
    python calibrate.py --project integrations-assistant --all    # every match
    python calibrate.py --project x --offline                     # gates only, no spend

WHY THIS RUNS BEFORE ANYTHING IS INSTALLED

A router whose classifier is wrong is worse than no router: it would advise
confidently, I would start trusting it, and the cost would surface as rework
weeks later. ~/.claude/history.jsonl holds hundreds of prompts I actually typed,
so "does this classify my real work correctly?" is answerable for cents, up
front, instead of being discovered in production.

WHAT IT MEASURES

  * class distribution - does it use the whole taxonomy, or collapse everything
    into two buckets? A classifier that answers "bug-hunt" to everything has a
    high average confidence and zero value.
  * confidence per class - and how much of it falls under the downgrade gate,
    the only threshold that can make the router cheapen a route.
  * LATENCY p50/p95 - the hook has a hard 2s budget. If p95 is above it, the
    hook times out on one call in twenty and silently falls back. This number
    decides the timeout; guessing it means guessing how often the router is
    actually in play.
  * what the policy WOULD have decided, per prompt, including every downgrade.

WHAT IT CANNOT MEASURE

There is no ground truth here: nothing in the history says what the right class
was. So this reports distributions and hands YOU the low-confidence cases to
read. The judgement is yours; this only makes it cheap to make. Do not read
"all checks passed" into a calibration run - it has no pass/fail.

PRIVACY. Every prompt goes through the same scrubber as the hook, and the sample
printed here is the SCRUBBED text: what jev saw, not what I typed. Reading
history across every project at once widens that disclosure, so --project is
required unless you pass --all-projects on purpose.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev  # noqa: E402
import route as route_mod  # noqa: E402
import state as state_mod  # noqa: E402

HISTORY = Path.home() / ".claude" / "history.jsonl"
OUT = route_mod.SKILL_DIR / "calibration-v1.json"

# The same gates the hook applies, so the calibration measures the population
# the hook would actually see - not a different one that happens to be on disk.
MIN_PROMPT_CHARS = 40
SKIP_PREFIXES = ("/", "!")

# Measured 2026-10-21: ~450 input tokens per decision at USD 0.42/Mtok.
USD_PER_DECISION = 0.00045


def load_history(project_filter, *, all_projects):
    if not HISTORY.exists():
        raise SystemExit(f"no history at {HISTORY}")
    rows, seen, projects = [], set(), {}
    for line in HISTORY.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        display = (rec.get("display") or "").strip()
        project = rec.get("project") or ""
        projects[project] = projects.get(project, 0) + 1
        if len(display) < MIN_PROMPT_CHARS or display.startswith(SKIP_PREFIXES):
            continue
        if not all_projects and project_filter and project_filter not in project:
            continue
        key = display[:200]
        if key in seen:  # a retyped prompt is one population member, not two
            continue
        seen.add(key)
        rows.append({"display": display, "project": project})
    if all_projects:
        print(f"projects in history: {len(projects)}")
        for p, n in sorted(projects.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {n:4d}  {Path(p).name or p}")
    return rows


def label_of(text, width=86):
    return text if len(text) <= width else text[: width - 1] + "..."


def run(rows, *, limit, max_usd, timeout):
    questions = route_mod.load_json(route_mod.QUESTIONS_FILE)
    config = route_mod.load_json(route_mod.ROUTES_FILE)
    labels = route_mod.complexity_labels(questions)

    results, latencies, spent = [], [], 0.0
    for i, row in enumerate(rows[:limit], 1):
        cwd = Path(row["project"]) if row["project"] and Path(row["project"]).is_dir() else None
        st = state_mod.build(row["display"], cwd=cwd)
        started = time.perf_counter()
        try:
            data = jev.decide(st, questions["questions"], timeout=timeout)
        except jev.JevUnavailable as exc:
            print(f"  [{i}] unavailable: {exc.reason}", file=sys.stderr)
            if exc.reason in ("out_of_credit", "unauthorized", "no_api_key"):
                print("  stopping: that reason will not fix itself", file=sys.stderr)
                break
            continue
        latencies.append((time.perf_counter() - started) * 1000.0)
        verdict = route_mod.verdict_from_answers(data["answers"], labels)
        route = route_mod.apply_policy(verdict, st.get("repo", {}), config, labels)
        spent += jev.cost_usd(data)
        results.append({
            "task": st["task"],
            "project": Path(row["project"]).name,
            "class": verdict.klass,
            "confidence": round(verdict.kind_confidence, 3),
            "complexity": verdict.complexity_label,
            "verifiable": round(verdict.verifiable, 3),
            "reversible": round(verdict.reversible, 3),
            "needs_repo_context": round(verdict.needs_repo_context, 3),
            "second_opinion": round(verdict.second_opinion, 3),
            "fleet": route.fleet,
            "tier": route.tier,
            "model": route.model,
            "effort": route.effort,
            "jury": route.jury,
            "downgraded": any("downgraded" in r for r in route.reasons),
            "latency_ms": round(latencies[-1]),
            # sweep.py replays these rows through apply_policy, and the test
            # runner is an input to it. Saving it means a later sweep measures
            # the real decision instead of an assumed one.
            "test_runner": st.get("repo", {}).get("test_runner", "none"),
        })
        if i % 25 == 0:
            print(f"  [{i}] US$ {spent:.4f} spent", file=sys.stderr)
        if spent >= max_usd:
            print(f"  stopping at the --max-usd ceiling (US$ {max_usd})", file=sys.stderr)
            break

    p95 = None
    if len(latencies) >= 20:
        idx = min(len(latencies) - 1, int(len(latencies) * 0.95))
        p95 = round(sorted(latencies)[idx])
    return {
        "questions_version": questions["version"],
        "routes_version": config["version"],
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scrubber_max_task_chars": state_mod.MAX_TASK_CHARS,
        "n": len(results),
        "cost_usd": round(spent, 6),
        "latency_ms": {
            "p50": round(statistics.median(latencies)) if latencies else None,
            "p95": p95,
            "max": round(max(latencies)) if latencies else None,
        },
        "results": results,
    }


def report(data, *, show):
    res = data["results"]
    if not res:
        print("no results")
        return
    n = len(res)
    print("\n" + "=" * 78)
    print(f"CALIBRATION  questions={data['questions_version']} routes={data['routes_version']}")
    print(f"{n} prompts | US$ {data['cost_usd']:.4f} | US$ {data['cost_usd']/n:.6f} per decision")
    lat = data["latency_ms"]
    print(f"latency p50 {lat['p50']} ms | p95 {lat['p95']} ms | max {lat['max']} ms")
    if lat["p95"] and lat["p95"] > 2000:
        print("  !! p95 is over the hook's 2s budget: raise the timeout, or accept"
              " that one call in twenty falls back")
    print("=" * 78)

    counts = {}
    for r in res:
        counts.setdefault(r["class"], []).append(r["confidence"])
    print(f"\nCLASS DISTRIBUTION  ({len(counts)} of 10 classes used)")
    for klass, confs in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        bar = "#" * round(40 * len(confs) / n)
        print(f"  {klass:<22} {len(confs):4d} {len(confs)/n:5.1%} "
              f"conf~{statistics.median(confs):.2f} {bar}")
    if len(counts) <= 2:
        print("  !! two classes or fewer: the taxonomy is collapsing. A high average"
              " confidence here means nothing.")

    routes = {}
    for r in res:
        key = f"{r['fleet']}/{r['tier']}"
        routes[key] = routes.get(key, 0) + 1
    print("\nWHERE THE POLICY WOULD SEND THEM")
    for key, cnt in sorted(routes.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<22} {cnt:4d} {cnt/n:5.1%}")
    down = [r for r in res if r["downgraded"]]
    juries = [r for r in res if r["jury"]]
    cheap = [r for r in res if r["tier"] in ("cheap", "bulk")]
    front = [r for r in res if r["tier"] == "frontier"]
    # THE headline. The first version of this report led with the downgrade
    # count, measured 0, while 27% of the work was already routed cheap - a
    # class whose policy starts at the cheap tier has nothing to drop to, so
    # counting downgrades undercounts the saving all the way to zero. Leading
    # with the wrong number would have retired a working router.
    print("")
    print(f"  CHEAP/BULK TIER: {len(cheap)} ({len(cheap)/n:.1%})  <- this is the saving")
    print(f"  frontier tier:   {len(front)} ({len(front)/n:.1%})  <- the quota that burns")
    print(f"  jury suggested:  {len(juries)} ({len(juries)/n:.1%})")
    print(f"  downgrades:      {len(down)} ({len(down)/n:.1%}) - near-inert by"
          " construction; see _the_uncomfortable_finding in routes.v1.json")

    gate = route_mod.load_json(route_mod.ROUTES_FILE)["thresholds"][
        "downgrade_requires"]["kind_confidence_at_least"]
    under = [r for r in res if r["confidence"] < gate]
    print(f"  under the {gate} confidence gate: {len(under)} ({len(under)/n:.1%})"
          " - these can never be cheapened")

    print(f"\nLOWEST CONFIDENCE ({show}) - read these; this is the judgement call")
    for r in sorted(res, key=lambda r: r["confidence"])[:show]:
        print(f"  {r['confidence']:.2f} {r['class']:<20} {r['fleet']}/{r['tier']:<9} "
              f"{label_of(r['task'])}")
    print(f"\nHIGHEST CONFIDENCE ({show}) - check these are obviously right")
    for r in sorted(res, key=lambda r: -r["confidence"])[:show]:
        print(f"  {r['confidence']:.2f} {r['class']:<20} {r['fleet']}/{r['tier']:<9} "
              f"{label_of(r['task'])}")
    if down:
        print(f"\nEVERY DOWNGRADE ({len(down)}) - a wrong one here is the expensive mistake")
        for r in down:
            print(f"  {r['confidence']:.2f} {r['class']:<20} -> {r['fleet']}/{r['tier']:<9} "
                  f"{label_of(r['task'])}")
    print("\nThis run has no pass/fail: there is no ground truth in the history.")


def summarise(data):
    """Aggregates only - no prompt text, not even scrubbed.

    The full calibration file is gitignored because ai-skills is a PUBLIC repo
    and scrubbed prompts still carry business context ("the system finally works
    end to end on the staging server" names no client and still says plenty).
    But the numbers in routes.v1.json claim to be MEASURED, and a claim of
    measurement with no artifact behind it is just a claim - so the aggregates,
    which leak nothing, are committed next to it.
    """
    res = data["results"]
    n = len(res) or 1

    # No rows at all is a REACHABLE state, not a theoretical one: run() does
    # `continue` on timeout and on server_error, the two most common failures,
    # so a few minutes of jev being down produces results == []. main() then
    # went on to write the file and call this, and `vs[len(vs) // 10]` raised
    # IndexError - AFTER the money had been spent. The empty summary is honest
    # about having measured nothing; it does not pretend to aggregates.
    if not res:
        return {
            "questions_version": data["questions_version"],
            "routes_version": data["routes_version"],
            "measured_at": data["measured_at"],
            "n": 0,
            "cost_usd": data["cost_usd"],
            "usd_per_decision": None,
            "latency_ms": data["latency_ms"],
            "_empty": "no decision came back (every call timed out or errored)."
                      " Nothing was measured, so there is nothing to aggregate -"
                      " do NOT read this as a result.",
            "by_class": {}, "by_tier": {}, "by_fleet": {}, "by_complexity": {},
            "cheap_or_bulk_share": None, "frontier_share": None,
            "jury_share": None, "downgrades": 0, "distributions": {},
        }

    def bucket(key):
        out = {}
        for r in res:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def pct(values, cuts):
        vs = sorted(values)
        return {
            "p10": round(vs[len(vs) // 10], 3),
            "median": round(vs[len(vs) // 2], 3),
            "p75": round(vs[3 * len(vs) // 4], 3),
            "p90": round(vs[9 * len(vs) // 10], 3),
            "fires_at": {str(c): round(sum(1 for v in vs if v >= c) / len(vs), 3) for c in cuts},
        }

    return {
        "questions_version": data["questions_version"],
        "routes_version": data["routes_version"],
        "measured_at": data["measured_at"],
        "n": data["n"],
        "cost_usd": data["cost_usd"],
        "usd_per_decision": round(data["cost_usd"] / n, 6),
        "latency_ms": data["latency_ms"],
        # The jev answers (by_class, by_complexity, distributions) are properties
        # of the CLASSIFIER and do not move when the policy is edited. Everything
        # about routes does. Keeping that distinction visible stopped this file
        # from publishing a frontier_share that looked current and was measured
        # under thresholds that had since been replaced.
        "_route_fields_measured_under": "the policy as of measured_at; sweep.py"
                                        " recomputes them against current routes.v1.json",
        "by_class": bucket("class"),
        "by_tier": bucket("tier"),
        "by_fleet": bucket("fleet"),
        "by_complexity": bucket("complexity"),
        "cheap_or_bulk_share": round(sum(1 for r in res if r["tier"] in ("cheap", "bulk")) / n, 3),
        "frontier_share": round(sum(1 for r in res if r["tier"] == "frontier") / n, 3),
        "jury_share": round(sum(1 for r in res if r["jury"]) / n, 3),
        "downgrades": sum(1 for r in res if r["downgraded"]),
        "distributions": {
            "confidence": pct([r["confidence"] for r in res], [0.4, 0.5, 0.6, 0.7, 0.8]),
            "verifiable": pct([r["verifiable"] for r in res], [0.5, 0.55, 0.7]),
            "second_opinion": pct([r["second_opinion"] for r in res], [0.55, 0.7, 0.8]),
            "reversible": pct([r["reversible"] for r in res], [0.5, 0.6]),
        },
    }


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="substring of the project path to keep")
    ap.add_argument("--all-projects", action="store_true",
                    help="read history from every project (widens what is sent)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--all", action="store_true", help="no limit beyond --max-usd")
    ap.add_argument("--max-usd", type=float, default=0.50, help="hard spend ceiling")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--show", type=int, default=10)
    ap.add_argument("--offline", action="store_true",
                    help="load and scrub history, print the population, spend nothing")
    ap.add_argument("--out", help="where to write the record")
    a = ap.parse_args()

    if not (a.project or a.all_projects):
        ap.error("pass --project <substring>, or --all-projects on purpose "
                 "(it widens what leaves the machine)")

    rows = load_history(a.project, all_projects=a.all_projects)
    print(f"{len(rows)} prompts match (>= {MIN_PROMPT_CHARS} chars, deduped, no slash commands)")

    if a.offline:
        print("\nwhat the scrubber would send (first 10):")
        for row in rows[:10]:
            print(f"  {label_of(state_mod.scrub(row['display']))}")
        print("\n--offline: nothing was sent, nothing was charged")
        return 0

    limit = len(rows) if a.all else min(a.limit, len(rows))
    print(f"about to send {limit} decisions, ~US$ {limit * USD_PER_DECISION:.3f}"
          f" (ceiling US$ {a.max_usd})\n")

    data = run(rows, limit=limit, max_usd=a.max_usd, timeout=a.timeout)
    report(data, show=a.show)
    # Nothing came back. Do not touch either file: overwriting a real previous
    # calibration with an empty one would retire measurements that are still the
    # only evidence behind the numbers in routes.v1.json, and the next reader
    # would find an artifact that says "n: 0" where a measurement used to be.
    if not data["results"]:
        print("")
        print("no decision came back (timeouts/errors). NOTHING was written - the"
              " previous calibration, if any, is still the measurement of record.")
        return 12
    out = Path(a.out) if a.out else OUT
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = out.with_suffix(".summary.json")
    summary.write_text(json.dumps(summarise(data), indent=2, ensure_ascii=False),
                       encoding="utf-8")
    print("")
    print(f"wrote {out.name}          (gitignored: holds scrubbed prompts)")
    print(f"wrote {summary.name}  (committed: aggregates only, no prompt text)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
