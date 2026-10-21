#!/usr/bin/env python3
"""Read the ledger and say what the router actually did - and where it was wrong.

    python report.py                  # last 30 days
    python report.py --days 7
    python report.py --json

WHAT THIS ANSWERS THAT NOTHING ELSE DOES

  1. "Did I esgotar Claude's window while Codex sat idle?" - dispatch share per
     fleet. This is the question two subscriptions create and neither vendor
     answers.
  2. "Where do I disagree with the router?" - recommendation against dispatch,
     per class. My disagreement is the only calibration signal that does not
     come from jev, which makes it the most trustworthy one here.
  3. "Which routes produce bad work?" - judgements joined to dispatches, with
     the mechanical check overriding the text opinion wherever one exists.

THE UNIT PROBLEM, WHICH IS NOT A BUG

There is no single cost number, and this report will not print one. Claude Max
and Codex are subscriptions: their scarce resource is QUOTA, and neither exposes
how much is left through any API, so what is measured is dispatch count - what I
SPENT, never what REMAINS. jev is billed in dollars and reports them exactly.
Gemini is billed in dollars but has no verified price table here, so it is
reported in TOKENS. Adding those four together would produce one confident,
false number; keeping them apart produces three true ones.

WHAT IT PROPOSES

Threshold changes, each with the count behind it. It proposes, it does not edit:
routes.v1.json is written by hand so that every value keeps a human-authored
_origin next to it.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger  # noqa: E402
import route as route_mod  # noqa: E402

# Below this many records a percentage is theatre. 20 is a judgement call: it is
# the point at which one outlier stops moving a rate by more than 5 points.
MIN_FOR_A_RATE = 20


def pct(part: int, whole: int) -> str:
    return f"{part / whole:5.1%}" if whole else "    -"


def gather(days: float) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recs = ledger.read(since=since)
    out = {"decision": [], "dispatch": [], "judgement": []}
    for r in recs:
        out.setdefault(r.get("type"), []).append(r)
    return out


def render(data: dict, days: float) -> list[str]:
    L = []
    dec, dis, jud = data["decision"], data["dispatch"], data["judgement"]
    L.append("=" * 78)
    L.append(f"ROUTE REPORT  last {days:g} days  |  {len(dec)} decisions, "
             f"{len(dis)} dispatches, {len(jud)} judgements")
    L.append("=" * 78)
    if not dec and not dis:
        L.append("")
        L.append("The ledger is empty for this window. Nothing here is a judgement about")
        L.append("the router - it is a statement that nothing was recorded. If the hook")
        L.append("is installed and the project is on the allowlist, that is a defect.")
        return L

    L.append("")
    L.append("FLEET SHARE OF DISPATCHES  (what I SPENT; neither subscription")
    L.append("exposes what is LEFT, so this is a proxy and not a gauge)")
    fleets = {}
    for r in dis:
        f = r.get("fleet") or "?"
        fleets[f] = fleets.get(f, 0) + 1
    total_dis = sum(fleets.values())
    for f, n in sorted(fleets.items(), key=lambda kv: -kv[1]):
        bar = "#" * round(40 * n / total_dis) if total_dis else ""
        L.append(f"  {f:<10} {n:4d} {pct(n, total_dis)} {bar}")
    subs = {f: n for f, n in fleets.items() if f in ("claude", "codex")}
    if len(subs) == 2 and total_dis >= MIN_FOR_A_RATE:
        hi, lo = max(subs, key=subs.get), min(subs, key=subs.get)
        gap = subs[hi] / sum(subs.values())
        if gap > 0.75:
            L.append(f"  -> {hi} took {gap:.0%} of subscription work while {lo} idled."
                     " That is the waste this router exists to fix; check")
            L.append("     thresholds.quota_pressure.switch_when_share_exceeds.")
    elif len(subs) < 2:
        L.append("  -> only one subscription fleet has been dispatched to. Quota pressure")
        L.append("     cannot balance what it never sees.")

    L.append("")
    L.append("WHERE I DISAGREED WITH THE ROUTER")
    router_dis = [r for r in dis if r.get("via") == "agent_hook"]
    off_router = [r for r in router_dis if not r.get("from_router")]
    if len(router_dis) < MIN_FOR_A_RATE:
        L.append(f"  {len(router_dis)} agent dispatches recorded - under {MIN_FOR_A_RATE},")
        L.append("  so no rate is reported. A percentage from a handful of records is")
        L.append("  theatre.")
    else:
        L.append(f"  agent dispatches: {len(router_dis)}, of which {len(off_router)} "
                 f"({pct(len(off_router), len(router_dis)).strip()}) went to an agent")
        L.append("  this router does not own.")
        if len(off_router) / len(router_dis) > 0.7:
            L.append("  -> the route-* agents are rarely chosen. They cost resident tokens")
            L.append("     in every system prompt; consider cutting them to two.")

    by_class, abstained = {}, 0
    for r in dec:
        by_class.setdefault(r.get("class", "?"), []).append(r)
        if r.get("abstained"):
            abstained += 1
    if dec:
        L.append("")
        L.append("WHAT THE CLASSIFIER SAW")
        for klass, rows in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
            # `.get(k, 0)` defends against a MISSING key, not against a null
            # VALUE - and a `"kind_confidence": null` is exactly what a record
            # written during a fallback can carry. MEASURED: one such line
            # killed the whole 30-day report with "unsupported format string
            # passed to NoneType.__format__", naming no line and printing no
            # section. ledger.read() already survives a half-written line and a
            # JSON list; the hole was one level up, in the semantics.
            confs = [c for c in (r.get("kind_confidence") for r in rows)
                     if isinstance(c, (int, float)) and not isinstance(c, bool)]
            shown = f"conf~{statistics.median(confs):.2f}" if confs else "conf~  n/a"
            L.append(f"  {klass:<22} {len(rows):4d} {pct(len(rows), len(dec))} "
                     f"{shown}")
        L.append(f"  abstained (too unsure to advise): {abstained} "
                 f"{pct(abstained, len(dec))}")
    return L + render_outcomes(data) + render_spend(data, subs) + render_proposals(data)


def _number(value: object) -> float | None:
    """The value if it is a real number, else None. A bool is not a score."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _flagged(r: dict) -> bool:
    """Does the judge's verdict deserve a human reading it?

    `(r.get("served") or 1.0) < 0.3` was the test, and `0.0 or 1.0` is `1.0`:
    the WORST possible verdict - the output served nothing at all - was read as
    "served perfectly" and dropped from the only list this report produces. The
    same `or` hid `needed_more_capability` of 0.0, which is harmless, and would
    have hidden any future score where 0 is the bad end.
    """
    served = _number(r.get("served"))
    needed = _number(r.get("needed_more_capability"))
    return ((served is not None and served < 0.3)
            or (needed is not None and needed > 0.7))


def render_outcomes(data: dict) -> list[str]:
    jud = data["judgement"]
    L = ["", "OUTCOMES"]
    if not jud:
        L.append("  no judgements recorded. Dispatch is logged automatically; judge.py")
        L.append("  has to be called, so an empty section here means the loop is OPEN,")
        L.append("  not that the work was good.")
        return L
    mech = [r for r in jud if r.get("mechanical")]
    L.append(f"  mechanical checks: {len(mech)} of {len(jud)} judgements"
             "  <- the only signal here that did not come from jev")
    if mech:
        failed = [r for r in mech if r["mechanical"] == "fail"]
        L.append(f"    failed: {len(failed)} {pct(len(failed), len(mech))}")
    suspects = [r for r in jud if _flagged(r)]
    L.append(f"  flagged by the judge: {len(suspects)} {pct(len(suspects), len(jud))}")
    L.append("    measured 2026-10-21: this judge detects bad work cleanly (served 0.06,")
    L.append("    out-of-depth 0.87 on a hedging answer) and under-credits good work, so")
    L.append("    a FLAG is worth reading and a clean verdict is not worth trusting.")
    for r in suspects[:5]:
        L.append(f"      {(r.get('fleet') or '?'):<8} served={r.get('served')} "
                 f"out-of-depth={r.get('needed_more_capability')} "
                 f"quality={r.get('quality')}")
    return L


def render_spend(data: dict, subs: dict) -> list[str]:
    dec, dis, jud = data["decision"], data["dispatch"], data["judgement"]
    L = ["", "SPEND, IN THREE UNITS THAT DO NOT ADD UP"]
    jev_usd = sum(r.get("cost_usd") or 0.0 for r in dec)
    jev_usd += sum(r.get("judge_cost_usd") or 0.0 for r in jud)
    L.append(f"  jev            US$ {jev_usd:.4f}   (exact: the API reports it)")
    g_tokens = sum(r.get("input_tokens") or 0 for r in dis if r.get("fleet") == "gemini")
    g_calls = sum(1 for r in dis if r.get("fleet") == "gemini")
    L.append(f"  gemini         {g_tokens} input tokens over {g_calls} calls   "
             "(metered, but no verified price table here)")
    L.append(f"  claude+codex   {sum(subs.values()) if subs else 0} dispatches   "
             "(subscription: quota, not dollars)")
    credits = [r.get("credits_remaining_usd") for r in dec
               if r.get("credits_remaining_usd") is not None]
    if credits:
        L.append(f"  jev credit left: US$ {credits[-1]:.4f}")
        if credits[-1] < 1.0:
            L.append("  -> under a dollar. When it runs out the hook falls back to the")
            L.append("     keyword table: it keeps working and gets dumber, silently.")
    return L


def render_proposals(data: dict) -> list[str]:
    dec = data["decision"]
    L = ["", "PROPOSALS  (hand-edit routes.v1.json; every value keeps an _origin)"]
    proposals = []
    if len(dec) >= MIN_FOR_A_RATE:
        rate = sum(1 for r in dec if r.get("abstained")) / len(dec)
        if rate > 0.35:
            proposals.append(
                f"abstention is {rate:.0%} of {len(dec)} turns - the hook is mostly "
                "silent. Either lower hook_abstains_below_confidence, or accept that "
                "this much of my input is unclassifiable follow-ups.")
        jrate = sum(1 for r in dec if r.get("jury") and r.get("jury_from_signal")) / len(dec)
        if jrate > 0.3:
            proposals.append(
                f"the signal alone asked for a jury on {jrate:.0%} of {len(dec)} turns; "
                "raise jury_at_second_opinion until it is advice again.")
        cheap = sum(1 for r in dec if r.get("tier") in ("cheap", "bulk"))
        if cheap / len(dec) < 0.1:
            proposals.append(
                f"only {pct(cheap, len(dec)).strip()} of decisions reached a cheap tier. "
                "The lever is the class->tier map, not the downgrade gate: the gate was "
                "measured acting on 0.5% of real work.")
    if not proposals:
        proposals.append(
            f"none. Either the thresholds are holding or there is not enough data "
            f"({len(dec)} decisions; a rate needs {MIN_FOR_A_RATE}). An empty proposal "
            "list is not praise.")
    return L + [f"  * {p}" for p in proposals]


# --- selftest --------------------------------------------------------------
# $ROUTE_LEDGER is redirected to a temp file: this report READS the ledger, and
# a test that read the real one would pass or fail depending on the day.
# The two cases that matter here both arrived as live defects: a null score in a
# record killed the whole 30-day report, and the worst verdict there is
# (served = 0.0) was silently dropped from the only list the report produces.


def _selftest() -> int:
    import os
    import tempfile

    failures = []

    def check(label: str, ok: bool, got: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  <- {got!r}" if not ok else ""))
        if not ok:
            failures.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="report-selftest-")) / "ledger.jsonl"
    before = os.environ.get("ROUTE_LEDGER")
    os.environ["ROUTE_LEDGER"] = str(tmp)
    try:
        check("the ledger under test is the temp one", ledger.ledger_path() == tmp,
              ledger.ledger_path())
        empty = render(gather(30), 30)
        check("an empty window is reported as 'nothing recorded', not as health",
              any("ledger is empty" in line for line in empty), empty)

        ledger.append({"type": "decision", "class": "bug-hunt",
                       "kind_confidence": 0.91, "abstained": False})
        # THE record that killed the report: a null confidence. `.get(k, 0)`
        # defends against a missing key, never against a null value.
        ledger.append({"type": "decision", "class": "bug-hunt",
                       "kind_confidence": None, "abstained": False})
        ledger.append({"type": "decision", "class": "ops-infra",
                       "kind_confidence": "muito", "abstained": True})
        ledger.append({"type": "dispatch", "fleet": "gemini", "input_tokens": 120})
        ledger.append({"type": "dispatch", "fleet": "claude", "via": "agent_hook"})
        ledger.append({"type": "judgement", "fleet": "codex", "served": 0.0,
                       "needed_more_capability": None, "quality": "ruim"})
        ledger.append({"type": "judgement", "fleet": "codex", "served": 0.95,
                       "needed_more_capability": 0.1, "quality": "bom"})
        with tmp.open("a", encoding="utf-8") as fh:
            fh.write('{"type": "decision", "class": "killed mid-ap')

        data = gather(30)
        check("the half-written line was skipped, not fatal",
              len(data["decision"]) == 3, len(data["decision"]))
        lines = render(data, 30)
        text = "\n".join(lines)
        check("a null kind_confidence does not kill the report", bool(text))
        check("a class with no usable confidence prints n/a, not 0.00",
              "n/a" in text, text)
        check("the one real confidence is still reported", "conf~0.91" in text, text)

        check("served = 0.0 is flagged", _flagged({"served": 0.0}))
        check("served = 0.95 is not flagged", not _flagged({"served": 0.95}))
        check("a null score is not a flag", not _flagged({"served": None}))
        check("a string score is not a flag", not _flagged({"served": "zero"}))
        check("out-of-depth 0.87 is flagged",
              _flagged({"needed_more_capability": 0.87}))
        check("the 0.0 judgement reaches the flagged list",
              any("served=0.0" in line for line in lines), lines)
        check("flagged counts 1 of 2 judgements",
              any("flagged by the judge: 1" in line for line in lines), lines)
        check("the gemini tokens are summed",
              any("120 input tokens" in line for line in lines), lines)
    finally:
        if before is None:
            os.environ.pop("ROUTE_LEDGER", None)
        else:
            os.environ["ROUTE_LEDGER"] = before

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    data = gather(a.days)
    if a.json:
        payload = {k: len(v) for k, v in data.items()}
        payload["ledger"] = str(ledger.ledger_path())
        print(json.dumps(payload, indent=2))
        return 0
    print("\n".join(render(data, a.days)))
    print("")
    print(f"ledger: {ledger.ledger_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
