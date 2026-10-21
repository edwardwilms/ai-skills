#!/usr/bin/env python3
"""Decide which fleet, model and effort a development task should go to.

    python route.py --prompt "the migration test is flaky on postgres"
    python route.py --prompt-file - --json          # machine-readable
    python route.py --prompt "..." --show-state     # print the exact jev payload
    python route.py --prompt "..." --dry-run        # policy only, no network, no charge
    python route.py --selftest                      # policy table, offline

THE SHAPE OF THIS FILE

jev classifies; THIS decides. The classifier's answer is data - six typed values
with probabilities. The policy is ours: versioned in routes.v1.json, with the
origin of every threshold written next to it, and applied here in code you can
read. A third-party classifier does not get to set policy.

`apply_policy` is deliberately pure: answers + signals + config -> Route. No
network, no clock, no ledger. That is what makes --selftest a real test of the
decision rather than a test of the API being up.

Exit codes: 0 routed (by jev or by fallback), 2 refused (bad config), 3 no state.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev  # noqa: E402
import state as state_mod  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = SKILL_DIR / "questions.v1.json"
ROUTES_FILE = SKILL_DIR / "routes.v1.json"
CODEX_MODELS = Path.home() / ".codex" / "models_cache.json"

# Ordered cheapest-first. "bulk" is outside the ladder on purpose: it is a
# capability (a 1M-token window), not a rung, so it never participates in an
# upgrade or a downgrade.
TIER_LADDER = ("cheap", "work", "frontier")
SUBSCRIPTION_FLEETS = ("claude", "codex")


@dataclass
class Verdict:
    """What the classifier said. Or, in degraded mode, what the keyword table guessed."""

    klass: str
    kind_confidence: float
    complexity_idx: int
    complexity_label: str
    verifiable: float
    reversible: float
    needs_repo_context: float
    second_opinion: float
    source: str  # "jev" | "fallback"
    unavailable_reason: str | None = None


@dataclass
class Route:
    fleet: str
    tier: str
    model: str | None
    effort: str | None
    agent: str | None
    jury: bool
    auto_dispatch_safe: bool
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    credits_remaining_usd: float | None = None
    # True only when the second_opinion signal raised the jury above the class
    # default. review and architecture always want a jury, so announcing it for
    # them is a constant - measured at 39.6% of real turns - and a line that
    # appears regardless of content is a line I learn to skip.
    jury_from_signal: bool = False

    def one_liner(self) -> str:
        where = f"{self.fleet}/{self.model}" + (f"/{self.effort}" if self.effort else "")
        return (
            f"{self.verdict.klass} | {self.verdict.complexity_label} | "
            f"verifiable {self.verdict.verifiable:.2f} | confidence "
            f"{self.verdict.kind_confidence:.2f} -> {where}"
        )

    def to_dict(self) -> dict:
        return {
            "fleet": self.fleet, "tier": self.tier, "model": self.model,
            "effort": self.effort, "agent": self.agent, "jury": self.jury,
            "auto_dispatch_safe": self.auto_dispatch_safe,
            "jury_from_signal": self.jury_from_signal,
            "class": self.verdict.klass,
            "kind_confidence": round(self.verdict.kind_confidence, 3),
            "complexity": self.verdict.complexity_label,
            "verifiable": round(self.verdict.verifiable, 3),
            "reversible": round(self.verdict.reversible, 3),
            "needs_repo_context": round(self.verdict.needs_repo_context, 3),
            "second_opinion": round(self.verdict.second_opinion, 3),
            "source": self.verdict.source,
            "unavailable_reason": self.verdict.unavailable_reason,
            "reasons": self.reasons,
            "cost_usd": self.cost_usd,
            "credits_remaining_usd": self.credits_remaining_usd,
        }


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path.name}: {exc}")


def complexity_labels(questions: dict) -> list[str]:
    """['trivial', 'routine', ...] from the score criteria. The scale is DEFINED
    in questions.v1.json, so it cannot drift away from what jev was asked."""
    criteria = questions["questions"]["complexity"]["criteria"]
    return [c.split(":", 1)[0].strip() for c in criteria]


def codex_model_exists(slug: str) -> bool | None:
    """True/False, or None when the cache is unreadable (= cannot judge).

    Read at runtime rather than hardcoded. A model slug baked into a config file
    ages in silence, and the failure surfaces as a confusing CLI error much later.
    """
    try:
        data = json.loads(CODEX_MODELS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return any(m.get("slug") == slug for m in data.get("models", []))


def fallback_verdict(prompt: str, config: dict, labels: list[str], reason: str) -> Verdict:
    """Degraded mode: a deliberately dumb keyword table.

    It never downgrades, because it has no confidence to reason about. Its only
    job is to always answer, so that jev being down costs nothing but precision.
    """
    fb = config["fallback"]
    lowered = " " + prompt.lower() + " "
    klass = fb["default"].get("class", "")
    for rule in fb["keyword_rules"]:
        if any(token in lowered for token in rule["any"]):
            klass = rule["class"]
            break
    if not klass:
        # The default route has no class; pick the one whose tier/fleet match it.
        want = (fb["default"]["tier"], fb["default"]["fleet"])
        klass = next(
            (k for k, v in config["classes"].items() if (v["tier"], v["fleet"]) == want),
            next(iter(config["classes"])),
        )
    # Mid scale, zero confidence: every threshold that could cheapen the route
    # reads as "not satisfied", so the asymmetric rule holds without special-casing.
    mid = len(labels) // 2
    return Verdict(
        klass=klass, kind_confidence=0.0, complexity_idx=mid, complexity_label=labels[mid],
        verifiable=0.0, reversible=0.0, needs_repo_context=1.0, second_opinion=0.0,
        source="fallback", unavailable_reason=reason,
    )


def verdict_from_answers(answers: dict, labels: list[str]) -> Verdict:
    klass, conf = jev.choice(answers, "kind")
    raw = jev.score(answers, "complexity")
    idx = max(0, min(len(labels) - 1, int(round(raw))))
    return Verdict(
        klass=klass, kind_confidence=conf, complexity_idx=idx, complexity_label=labels[idx],
        verifiable=jev.noul(answers, "verifiable"),
        reversible=jev.noul(answers, "reversible"),
        needs_repo_context=jev.noul(answers, "needs_repo_context"),
        second_opinion=jev.noul(answers, "second_opinion"),
        source="jev",
    )


def _step(tier: str, delta: int) -> str:
    if tier not in TIER_LADDER:
        return tier  # "bulk" is off the ladder
    i = TIER_LADDER.index(tier)
    return TIER_LADDER[max(0, min(len(TIER_LADDER) - 1, i + delta))]


def apply_policy(
    verdict: Verdict,
    signals: dict,
    config: dict,
    labels: list[str],
    *,
    shares: dict[str, float] | None = None,
) -> Route:
    """Pure: verdict + repo signals + config -> Route. No network, no clock, no I/O.

    Order matters. Upgrades are evaluated BEFORE downgrades so that a class which
    both permits a downgrade and triggers an upgrade ends up expensive. That is
    the asymmetry, and putting it in the control flow means it cannot be
    forgotten by a later edit to the thresholds.
    """
    reasons: list[str] = []
    th = config["thresholds"]
    klass_cfg = config["classes"].get(verdict.klass)
    if klass_cfg is None:
        # jev named a class we do not have a policy for. Fail expensive.
        klass_cfg = config["classes"][
            next(k for k, v in config["classes"].items() if v["tier"] == "frontier")
        ]
        reasons.append(
            f"no policy for class {verdict.klass!r}; took the frontier route (fail expensive)"
        )

    tier = klass_cfg["tier"]
    fleet = klass_cfg["fleet"]
    effort_override = klass_cfg.get("effort_override")
    jury = bool(klass_cfg.get("jury"))
    auto_safe = bool(klass_cfg.get("auto_dispatch_safe", False))

    # --- 1. upgrades ------------------------------------------------------
    up_label = th["upgrade_to_frontier_at_complexity"]["label"]
    up_idx = labels.index(up_label) if up_label in labels else len(labels)
    class_up = klass_cfg.get("upgrade_at_complexity")
    class_up_idx = labels.index(class_up) if class_up in labels else None

    if verdict.complexity_idx >= up_idx and tier in TIER_LADDER and tier != "frontier":
        reasons.append(
            f"complexity is {verdict.complexity_label!r} (>= {up_label!r}) -> frontier"
        )
        tier = "frontier"
        effort_override = None  # the tier's own effort wins once we are at frontier
    elif class_up_idx is not None and verdict.complexity_idx >= class_up_idx:
        reasons.append(
            f"{verdict.klass}: complexity {verdict.complexity_label!r} "
            f"(>= {class_up!r}) -> frontier"
        )
        tier = "frontier"
        effort_override = None

    # --- 2. downgrades: every condition must hold ------------------------
    dr = th["downgrade_requires"]
    max_idx = labels.index(dr["max_complexity_label"]) if dr["max_complexity_label"] in labels else -1
    runner = signals.get("test_runner", "none")
    blockers = []
    if not klass_cfg.get("allow_downgrade"):
        blockers.append(f"class {verdict.klass} never downgrades")
    if tier == "frontier" and verdict.complexity_idx >= up_idx:
        blockers.append("already upgraded for complexity")
    if verdict.verifiable < dr["verifiable_at_least"]:
        blockers.append(f"verifiable {verdict.verifiable:.2f} < {dr['verifiable_at_least']}")
    if verdict.reversible < dr["reversible_at_least"]:
        blockers.append(f"reversible {verdict.reversible:.2f} < {dr['reversible_at_least']}")
    if verdict.kind_confidence < dr["kind_confidence_at_least"]:
        blockers.append(
            f"confidence {verdict.kind_confidence:.2f} < {dr['kind_confidence_at_least']}"
        )
    if verdict.complexity_idx > max_idx:
        blockers.append(
            f"complexity {verdict.complexity_label!r} > {dr['max_complexity_label']!r}"
        )
    if th["require_test_runner_to_downgrade"]["value"] and runner == "none":
        # "verifiable" is a judgement about the task; having a runner is a fact
        # about the machine. Conflating them is how a guard fails open.
        blockers.append("no test runner here, so 'verifiable' cannot be cashed in")

    if not blockers and tier in TIER_LADDER and tier != "cheap":
        cheaper = _step(tier, -1)
        reasons.append(f"downgraded {tier} -> {cheaper}: verifiable, reversible and confident")
        tier = cheaper
    elif blockers:
        reasons.append("no downgrade: " + "; ".join(blockers))

    # --- 3. fleet ---------------------------------------------------------
    force_ctx = th["force_claude_when_needs_repo_context"]["value"]
    qp = th["quota_pressure"]
    # A class may declare its fleet a REQUIREMENT rather than a preference.
    # review does: the reviewer must not be the author, so neither context
    # pinning nor quota pressure may trade that away. Without this flag, moving
    # review down a tier quietly handed it to Claude - see _why_fleet_pinned.
    if klass_cfg.get("fleet_pinned"):
        reasons.append(f"fleet {fleet} is pinned by policy for {verdict.klass}")
    elif fleet in SUBSCRIPTION_FLEETS and tier in ("cheap", "work"):
        if verdict.needs_repo_context >= force_ctx:
            if fleet != "claude":
                reasons.append(
                    f"needs_repo_context {verdict.needs_repo_context:.2f} >= {force_ctx}"
                    " -> pinned to claude (the only fleet with this repo wired in)"
                )
            fleet = "claude"
        elif qp.get("enabled") and shares:
            mine = shares.get(fleet, 0.0)
            if mine > qp["switch_when_share_exceeds"]:
                other = next(f for f in SUBSCRIPTION_FLEETS if f != fleet)
                reasons.append(
                    f"quota pressure: {fleet} is {mine:.0%} of the last "
                    f"{qp['window_hours']}h of dispatches -> {other} "
                    "(share of SPEND, not of remaining quota)"
                )
                fleet = other

    # --- 4. resolve model and effort -------------------------------------
    fleet_cfg = config["fleets"][fleet]
    if tier not in fleet_cfg["tiers"]:
        # This overrides even fleet_pinned: a pin to a fleet that cannot serve
        # the tier is unsatisfiable, and refusing to route is worse than saying
        # out loud that the pin was broken.
        reasons.append(f"{fleet} has no {tier!r} tier -> claude (pin unsatisfiable)")
        fleet, fleet_cfg = "claude", config["fleets"]["claude"]
    spec = fleet_cfg["tiers"][tier]
    model, effort, agent = spec.get("model"), spec.get("effort"), spec.get("agent")

    if fleet == "codex" and model:
        exists = codex_model_exists(model)
        if exists is False:
            reasons.append(
                f"codex model {model!r} is not in models_cache.json -> claude"
                " (the pin went stale; re-check routes.v1.json)"
            )
            fleet = "claude"
            spec = config["fleets"]["claude"]["tiers"][tier]
            model, effort, agent = spec.get("model"), spec.get("effort"), spec.get("agent")
        elif exists is None:
            reasons.append("could not read ~/.codex/models_cache.json; trusting the pin")

    if effort_override:
        reasons.append(f"{verdict.klass}: effort pinned to {effort_override!r} by policy")
        effort = effort_override

    # --- 5. jury ----------------------------------------------------------
    jury_at = th["jury_at_second_opinion"]["value"]
    jury_from_signal = False
    if not jury and verdict.second_opinion >= jury_at:
        reasons.append(
            f"second_opinion {verdict.second_opinion:.2f} >= {jury_at} -> jury recommended"
        )
        jury = jury_from_signal = True

    if verdict.source == "fallback":
        reasons.insert(0, f"jev unavailable ({verdict.unavailable_reason}); keyword fallback")

    return Route(
        fleet=fleet, tier=tier, model=model, effort=effort, agent=agent,
        jury=jury, auto_dispatch_safe=auto_safe, verdict=verdict, reasons=reasons,
        jury_from_signal=jury_from_signal,
    )


def announce_only_when_added() -> bool:
    """Whether to print the jury hint only when the signal raised it. See the
    `_why_announce_only_when_added` note in routes.v1.json for the measurement."""
    return bool(
        load_json(ROUTES_FILE)["thresholds"]["jury_at_second_opinion"]
        .get("announce_only_when_signal_added", False)
    )


def abstain_below() -> float:
    """Confidence under which the hook says nothing at all. 0.0 disables it."""
    th = load_json(ROUTES_FILE)["thresholds"].get("hook_abstains_below_confidence")
    return float(th["value"]) if th else 0.0


def project_name(cwd: Path | None = None) -> str:
    """Basename of the git toplevel, or of cwd. Used ONLY for the allowlist check
    and the ledger - never sent to jev."""
    root = state_mod._git(["rev-parse", "--show-toplevel"], Path(cwd or os.getcwd()))
    return Path(root).name if root else Path(cwd or os.getcwd()).name


def allowed(config: dict, project: str) -> bool:
    """Empty allowlist = silent everywhere. Fail CLOSED: a fresh install must not
    start shipping prompts to a third party because nobody configured it yet."""
    return project in config["allowlist"]["projects"]


def decide(
    prompt: str,
    *,
    cwd: Path | None = None,
    recent_tools: list[str] | None = None,
    dry_run: bool = False,
    timeout: float = 2.0,
    shares: dict[str, float] | None = None,
) -> tuple[Route, dict]:
    """Returns (route, outbound_state). The state is returned so callers can show
    exactly what left the machine without rebuilding it and risking a mismatch."""
    questions = load_json(QUESTIONS_FILE)
    config = load_json(ROUTES_FILE)
    labels = complexity_labels(questions)
    st = state_mod.build(prompt, cwd=cwd, recent_tools=recent_tools)
    signals = st.get("repo", {})

    if dry_run:
        verdict = fallback_verdict(st["task"], config, labels, "dry-run: no call made")
        return apply_policy(verdict, signals, config, labels, shares=shares), st

    try:
        data = jev.decide(st, questions["questions"], timeout=timeout)
    except jev.JevUnavailable as exc:
        verdict = fallback_verdict(st["task"], config, labels, exc.reason)
        return apply_policy(verdict, signals, config, labels, shares=shares), st

    route = apply_policy(
        verdict_from_answers(data["answers"], labels), signals, config, labels, shares=shares
    )
    route.cost_usd = jev.cost_usd(data)
    route.credits_remaining_usd = jev.credits_remaining_usd(data)
    return route, st


# --- selftest --------------------------------------------------------------

def _selftest() -> int:
    """Table-driven, offline. Tests the DECISION, not the API being up."""
    questions = load_json(QUESTIONS_FILE)
    config = load_json(ROUTES_FILE)
    labels = complexity_labels(questions)
    failures = []

    def v(klass, cx, **kw) -> Verdict:
        base = dict(kind_confidence=0.9, verifiable=0.9, reversible=0.9,
                    needs_repo_context=0.2, second_opinion=0.1)
        base.update(kw)
        return Verdict(klass=klass, complexity_idx=labels.index(cx), complexity_label=cx,
                       source="jev", **base)

    HAS_TESTS = {"test_runner": "pytest"}
    NO_TESTS = {"test_runner": "none"}

    cases = [
        # label, verdict, signals, expected (fleet, tier) or None, must-appear reason fragment
        ("architecture never downgrades even when cheap-looking",
         v("architecture", "trivial"), HAS_TESTS, ("claude", "frontier"), "never downgrades"),
        ("bug-hunt climbs to frontier at 'hard'",
         v("bug-hunt", "hard"), HAS_TESTS, ("claude", "frontier"), "-> frontier"),
        ("mechanical-edit at 'deep' is upgraded, not cheapened",
         v("mechanical-edit", "deep"), HAS_TESTS, (None, "frontier"), "-> frontier"),
        ("no test runner blocks the downgrade",
         v("test-writing", "routine"), NO_TESTS, (None, "work"), "no test runner"),
        ("low confidence blocks the downgrade",
         v("test-writing", "routine", kind_confidence=0.3), HAS_TESTS, (None, "work"),
         "confidence"),
        ("unverifiable blocks the downgrade",
         v("test-writing", "routine", verifiable=0.2), HAS_TESTS, (None, "work"), "verifiable"),
        ("irreversible blocks the downgrade",
         v("refactor", "routine", reversible=0.1), HAS_TESTS, (None, "work"), "reversible"),
        ("everything green does downgrade",
         v("test-writing", "routine"), HAS_TESTS, (None, "cheap"), "downgraded"),
        ("repo-hungry work is pinned to claude",
         v("mechanical-edit", "trivial", needs_repo_context=0.9), HAS_TESTS, ("claude", None),
         "pinned to claude"),
        ("ops-infra is never auto-dispatchable",
         v("ops-infra", "routine"), HAS_TESTS, ("claude", "frontier"), None),
        ("high second_opinion asks for a jury",
         v("test-writing", "involved", second_opinion=0.9), HAS_TESTS, None, "jury recommended"),
        ("an unknown class fails expensive",
         v("something-we-never-defined", "trivial"), HAS_TESTS, (None, "frontier"),
         "fail expensive"),
    ]

    for label, verdict, signals, expected, fragment in cases:
        route = apply_policy(verdict, signals, config, labels)
        ok = True
        detail = []
        if expected:
            want_fleet, want_tier = expected
            if want_fleet and route.fleet != want_fleet:
                ok = False
                detail.append(f"fleet={route.fleet} want {want_fleet}")
            if want_tier and route.tier != want_tier:
                ok = False
                detail.append(f"tier={route.tier} want {want_tier}")
        if fragment and not any(fragment in r for r in route.reasons):
            ok = False
            detail.append(f"no reason contains {fragment!r}: {route.reasons}")
        if label.startswith("ops-infra") and route.auto_dispatch_safe:
            ok = False
            detail.append("auto_dispatch_safe is True")
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"\n       {'; '.join(detail)}" if detail else ""))
        if not ok:
            failures.append(label)

    # A pinned fleet survives BOTH optimisations. This is the regression sweep.py
    # caught on 2026-10-21: lowering review's tier moved it into the band where
    # context pinning applies, and it quietly became a Claude review of Claude's
    # own work. Asserted here because the failure is invisible in the output.
    pinned = apply_policy(
        v("review", "routine", needs_repo_context=0.95), HAS_TESTS, config, labels,
        shares={"codex": 0.95, "claude": 0.05},
    )
    ok = pinned.fleet == "codex"
    print(f"{'ok  ' if ok else 'FAIL'} a pinned fleet survives context pinning AND quota "
          f"pressure (review -> {pinned.fleet})")
    if not ok:
        failures.append("fleet_pinned")

    # Quota pressure: same verdict, different trailing shares, different fleet.
    vd = v("mechanical-edit", "involved", needs_repo_context=0.1)
    base = apply_policy(vd, HAS_TESTS, config, labels)
    pressed = apply_policy(vd, HAS_TESTS, config, labels, shares={"codex": 0.95, "claude": 0.05})
    ok = base.fleet == "codex" and pressed.fleet == "claude"
    print(f"{'ok  ' if ok else 'FAIL'} quota pressure moves the fleet "
          f"({base.fleet} -> {pressed.fleet})")
    if not ok:
        failures.append("quota pressure")

    # The fallback must always answer, and must never cheapen.
    fb = apply_policy(
        fallback_verdict("the deploy is broken", config, labels, "timeout"),
        HAS_TESTS, config, labels,
    )
    ok = fb.verdict.klass == "ops-infra" and not any("downgraded" in r for r in fb.reasons)
    print(f"{'ok  ' if ok else 'FAIL'} fallback answers and never downgrades "
          f"(class={fb.verdict.klass})")
    if not ok:
        failures.append("fallback")

    # Fail closed: a fresh config must not let the hook talk to a third party.
    ok = not allowed(config, "any-project-at-all")
    print(f"{'ok  ' if ok else 'FAIL'} empty allowlist blocks every project")
    if not ok:
        failures.append("allowlist")

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file", help="path, or '-' for stdin")
    ap.add_argument("--cwd")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-state", action="store_true",
                    help="print the exact payload that would leave this machine")
    ap.add_argument("--dry-run", action="store_true", help="no network, no charge")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-quota-pressure", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.prompt_file:
        text = sys.stdin.read() if a.prompt_file == "-" else Path(a.prompt_file).read_text(
            encoding="utf-8", errors="replace")
    elif a.prompt:
        text = a.prompt
    else:
        ap.error("need --prompt, --prompt-file or --selftest")

    shares = None
    if not a.no_quota_pressure:
        try:
            import ledger
            shares = ledger.dispatch_shares(
                load_json(ROUTES_FILE)["thresholds"]["quota_pressure"]["window_hours"]
            )
        except Exception:  # noqa: BLE001 - no ledger yet is a normal state
            shares = None

    cwd = Path(a.cwd) if a.cwd else None
    route, st = decide(text, cwd=cwd, dry_run=a.dry_run, timeout=a.timeout, shares=shares)

    if a.show_state:
        print("--- payload that leaves this machine (nothing else is sent) ---")
        print(json.dumps(st, indent=2, ensure_ascii=False))
        print("--- end payload ---\n")
    if a.json:
        print(json.dumps(route.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(route.one_liner())
        for reason in route.reasons:
            print(f"  . {reason}")
        if route.jury and (route.jury_from_signal or not announce_only_when_added()):
            print("  . jury: route-task jury")
        if not route.auto_dispatch_safe:
            print("  · NOT auto-dispatchable: decide this one yourself")
        if route.cost_usd:
            print(f"\n  jev: US$ {route.cost_usd:.6f}"
                  + (f" | credits left US$ {route.credits_remaining_usd:.4f}"
                     if route.credits_remaining_usd is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
