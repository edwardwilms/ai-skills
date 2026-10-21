---
name: route-task
allowed-tools: Bash, Read, Agent
argument-hint: decide <task> | jury <question> | exec <task> | bulk <task> | judge | report | calibrate
description: Decide which fleet, model and effort a dev task wants (Claude, Codex, Gemini), run a cross-fleet jury, move work off Claude's quota, judge an output, or report its misses.
---

# Route Task

Four credentials with opposite cost profiles. **Claude Max** and **Codex** are
subscriptions: zero marginal dollars, and the scarce thing is *quota*. **Gemini**
is metered but has a 1M-token window. **jev** is a decision API at US$ 0.42/Mtok
of input that returns typed, calibrated answers and writes no code.

So jev **classifies**, `routes.v1.json` **decides**, and you **dispatch**. The
classifier's answer is data; the policy is ours, versioned, with the origin of
every threshold written next to it.

`$ARGUMENTS` selects a verb. Run everything with the venv-free system Python;
these scripts are stdlib-only by design. `python` below means Python 3: on
Linux and macOS that is usually `python3`, and `python` may not exist at all.

## decide

```bash
python scripts/route.py --prompt "<the task>"
python scripts/route.py --prompt "<the task>" --show-state   # what leaves the machine
python scripts/route.py --prompt "<the task>" --dry-run      # policy only, no charge
```

Prints `class | complexity | verifiable | confidence -> fleet/model/effort` and
the one reason that decided it. Costs ~US$ 0.0005 and ~900 ms (measured p50 over
192 real prompts; p95 1326 ms).

The CLI is explicit consent, so it works in any project. The **hook** is gated by
`allowlist.projects` in `routes.v1.json` and stays silent outside it.

## jury

```bash
python scripts/jury.py --prompt "<question>" --context-file <diff or notes>
```

Runs the question on **independent fleets** and prints the answers side by side,
never merged - where they disagree is the finding. Two of the jurors cost zero
marginal dollars, which is why the policy can afford a low jury threshold.

Inside a Claude session it does not spawn a nested Claude: **you are the third
juror**, so add your own position. `--with-claude` overrides that.

## exec / bulk - get work off Claude's quota

```bash
python scripts/codex_task.py  --tier cheap --prompt "<mechanical change>"
python scripts/codex_task.py  --review --base origin/staging --tier frontier
python scripts/gemini_task.py --ask  "<one question, no file access>"
python scripts/gemini_task.py --bulk "<digest a large corpus>" --cd <repo>
```

Model and effort come from `routes.v1.json`, never from memory, and
`codex_task.py` validates them against `~/.codex/models_cache.json` before
spawning - `codex -c` accepts an invented key without complaining, so a bad
effort would otherwise be recorded as applied.

`--ask` is the direct API and `--bulk` is the CLI, because the floor differs by
~2400x: 12,206 CLI tokens against 5 API tokens for the same prompt.

## judge / report - close the loop

```bash
python scripts/judge.py  --task "<the task>" --output-file out.txt --mechanical pass
python scripts/report.py --days 30
```

The judge is a **detector, not a grader**: measured, it catches bad work cleanly
and under-credits good work. Pass `--mechanical pass|fail` whenever a real check
exists - tests, typecheck, lint, an exit code - and that signal wins, because it
is the only one in the ledger that did not come from the same model that routed.

The report prints spend in three units that deliberately do not add up, and
refuses to compute a rate from fewer than 20 records.

## calibrate - before trusting any of it

```bash
python scripts/calibrate.py --project <substring> --offline      # free, see the payload
python scripts/calibrate.py --project <substring> --limit 50
python scripts/sweep.py                                          # replay thresholds, free
```

`calibrate` replays real prompt history through the classifier; `sweep` replays a
saved calibration through candidate thresholds at no cost. A threshold with no
count behind it is a guess, and `routes.v1.json` labels which is which.

## What this will not do

- **It advises; it does not reroute.** No dispatch happens without you.
- **`ops-infra` is never auto-dispatchable.** It touches CI, deploys, secrets and
  migrations, where "reversible" is a lie.
- **The downgrade gate is near-inert on this workload** - measured acting on 1
  prompt in 192. The lever is the class-to-tier map. Do not sell it otherwise.
- **It does not print one cost number.** Two subscriptions, one metered API with
  no verified prices, and one exactly-billed API are four different units.

## Before you touch it

These are the tests - this repo has no pytest. **7 of the 16 scripts carry
`--selftest`**, which is what the claim "every script has one" used to say and
was never true (it was 4 of 16 when it was written):

```bash
python scripts/jev.py --selftest          # add --no-network for offline
python scripts/state.py --selftest        # the privacy gate, as assertions
python scripts/route.py --selftest        # 16 policy cases, offline
python scripts/cli.py --selftest          # the CLIs resolve to something runnable
python scripts/ledger.py --selftest       # append/read, and never the real ledger
python scripts/report.py --selftest       # a null score must not kill the report
python scripts/codex_task.py --selftest   # a partial answer must not read as clean
python scripts/selftest_hooks.py          # the hooks fail OPEN; --network for live
```

Still without one, and named rather than implied: `agent_hook.py` and
`route_hook.py` (covered from outside by `selftest_hooks.py`, which runs them as
subprocesses - that is where the hook guarantee actually lives), `calibrate.py`
and `sweep.py` (both spend money by design), and `env.py`, `judge.py`,
`jury.py` and `gemini_task.py`, which have none and are the next candidates.

`docs/ROUTER.md` holds the policy, what was measured to set it, and exactly what
leaves this machine.
