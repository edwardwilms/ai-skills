# The router: jev classifies, the policy decides, you dispatch

Four credentials with opposite cost profiles, and until now no decision used
that fact.

| Credential | What is scarce | Consequence |
|---|---|---|
| **Claude Max** | quota (5 h window + weekly). Marginal dollars: 0 | spending Opus on a file sweep burns quota the hard task will want |
| **Codex** (ChatGPT) | quota, **independent of Claude's**. Marginal dollars: 0 | capacity that sits idle while Claude's window drains |
| **Gemini** | dollars, and free-tier rate limits | a 1,048,576-token window nothing else has |
| **jev** | US$ 0.42 / Mtok input, output free | returns a typed decision in ~900 ms and writes no code |

With two zero-marginal-dollar subscriptions, the question stops being *"which
model is cheapest"* and becomes **"which quota do I spend"**. That is what the
router is for.

`jev`'s answer is **data**. `skills/route-task/routes.v1.json` is the
**decision** - versioned, with the origin of every threshold written beside it.
A third-party classifier does not get to set policy.

---

## What leaves this machine

The hook sees the raw prompt I type, and my prompts carry client names, card
links, pipe ids, addresses and pasted private code. jev is a third party - its
own docs say it is *"not affiliated with or endorsed by TypeSafe AI"*. So the
payload is **built**, never forwarded:

```jsonc
{
  "task": "o webhook do card <url> falha para <email> na org <id>, veja <path>",
  "repo": { "stack": "python+node+docker", "test_runner": "pytest",
            "size": "medium", "dirty_files": 0, "branch_kind": "staging",
            "in_git_repo": true }
}
```

- The prompt is **scrubbed and capped** at 600 chars. This is the irreducible
  disclosure: the router cannot classify a task it is not told about.
- Repo signals are **shapes, not names**. `integrations-assistant` and
  `feat/route-task` never leave; `python+node+docker, medium, staging` does, and
  it is strictly more useful to the policy.
- **A paste is not exempt** - and this line used to claim it was ("pasted
  content is never sent, not a sample, not a hash"). **Measured:** Claude Code
  *inlines* a paste into the prompt before any hook sees it - the TUI expands
  the `[Pasted text #1 +42 lines]` marker into the submitted string - so a paste
  arrives here as prompt text and is scrubbed and capped like any other prompt
  text. 5.2% of measured prompts exceed the 600-char cap, so the usual fate of a
  large paste is that most of it is **cut**: a weaker claim than "never sent",
  and the true one.
- Outside `allowlist.projects` the hook is **silent** - nothing classified,
  charged or logged. This, not the scrubber, is the real control. It ships empty
  (fail closed); it currently lists `integrations-assistant` and `ai-skills`.
  `projeto_integracoes` was deliberately left out despite being the largest
  population: it is where the client deliverable filenames came from, and the
  scrubber has not been calibrated against it.

See it before trusting it:

```bash
python scripts/route.py --prompt "<a real one>" --show-state --dry-run
```

**This is a scrubber, not a redactor.** It removes what has a shape - URLs, ids,
paths, keys, filenames, long quoted runs. It cannot remove a client's name typed
as a bare word; no regex can. Two of its patterns exist only because the
`--offline` calibration pass caught them on real history, before a token was
spent: an `@`-mentioned client deliverable filename (invisible to the
absolute-path rules, because `@`-mentions are relative paths) and a verbatim
quoted line out of a client's insurance document. The fixtures in `state.py`
that lock those two cases are SYNTHETIC - the real strings are not reproduced
here, in a public repo, which is the whole point of the rule.

Two more rules were added the same way, by measuring instead of reasoning:

- **An absolute path tolerates spaces inside it.** The old rule ended at the
  first space, so `...\Clientes\Banco Alfa Seguros\contrato.pdf` came out as
  `<path> Alfa Seguros\<file.pdf>` - the client's name in the surviving half. Of
  the 44 client folders on this machine 27 (61%) have a space in the name, and
  27 of 27 leaked. The space is allowed only where the *same* separator proves
  the path continues; the last segment is never space-tolerant, because nothing
  marks where it ends and a greedy rule there eats the sentence.
- **Credentials are matched in pt-BR too** (`senha`, `chave`, `credencial`,
  `segredo`, `usuario`, plus PEM headers). The rubric was English-only while the
  prompts here are not. `chave` and `usuario` are ordinary words in this
  language, so the keyword alone does not trigger: the value has to look like a
  secret (8+ chars, a letter plus a digit or symbol). `a chave do dicionario`
  survives; `a senha do cliente e <value>` does not.
- **A key needs no cue word.** Measured on a second machine (Linux, a SaaS API
  repo): 17 prompts of real history carried a full product API key written
  bare ("use this key `mw_...`"), and all 17 survived, because every rule that could
  see a key wanted `api key:`, `token` or `secret` in front of it, and an `_`
  inside the key broke the `<blob>` rule. Payment-provider ids (`pri_`, `sub_`,
  `txn_`, `ctm_`) survived the same way. Key and id **shapes** are now matched on
  their own (`KEY_SHAPES` / `ID_SHAPES` in `state.py`), including one generic
  rule: a short prefix, then a run that mixes upper case, lower case and digits.
  snake_case and slugs never mix all three, so code names stay as signal.
- **A send gate, not just a scrubber.** Scrubbing is ordered substitution: one
  reordered rule, or one caller that forgets to scrub, ships a key with no error
  anywhere. So `jev.decide` checks the final bytes against the same shapes, and
  if any match it sends **nothing** (`leak_guard`) and names the shape, never
  the match. Callers already treat that as "jev unavailable", so the hook stays
  fail-open: a false positive costs one turn of advice, not a key.

---

## The policy

Routes are `{fleet, tier, effort}`. Tiers ladder `cheap -> work -> frontier`;
`bulk` sits outside the ladder because it is a capability (a 1M window), not a
rung.

| class | tier | fleet | why |
|---|---|---|---|
| `bulk-scan`, `huge-context-digest` | bulk | gemini | the only window that fits; spends no subscription quota at all |
| `mechanical-edit`, `doc-explain` | cheap | codex | preserves Claude quota for work that needs the repo wired in |
| `test-writing`, `refactor` | work | claude | needs this repo's conventions and its suite |
| `bug-hunt` | work, high | claude | climbs to frontier at `hard`: diagnosis is where a cheap model burns two budgets |
| `review` | work | **codex, pinned** | the reviewer must not be the author |
| `architecture` | frontier, xhigh | claude | wrong answers get built upon before anyone notices |
| `ops-infra` | frontier, high | claude | CI, deploys, secrets, migrations - never auto-dispatchable |

**The rule is asymmetric.** A downgrade needs `verifiable >= 0.55`,
`reversible >= 0.6`, `confidence >= 0.6`, complexity at most `routine`, **and a
test runner to actually exist here**. Any doubt upgrades. A cheap misroute that
gets redone on a frontier model costs twice and destroys trust in the advice; an
expensive one costs quota. It fails **expensive**, by design.

**Fleet choice has a second input: quota pressure.** Tied on fitness, work goes
to the subscription fleet with the smaller share of dispatches in the trailing
5 h. That is what turns two subscriptions into one budget. Two exceptions, both
deliberate: `needs_repo_context >= 0.5` pins to Claude (the only fleet with this
repo's MCP servers and skills), and a class may declare `fleet_pinned`, which
`review` does - for it the fleet is a requirement, not a preference.

---

## What was measured, and what it changed

Phase 0 ran **before** anything was installed: 192 real prompts from
`~/.claude/history.jsonl`, US$ 0.089, no change to the workflow. It refuted
three of my own guesses. `calibration-v1.summary.json` holds the aggregates
(committed; the full file is gitignored because scrubbed prompts still carry
business context). `sweep.py` replays that file through candidate thresholds for
free, which is how a threshold stops being a guess.

| Guess | Measured | Now |
|---|---|---|
| `verifiable >= 0.70` gates downgrades | reachable by **5.2%** of prompts (p90 = 0.66) - the gate was off | `0.55`, just under p75 |
| jury at `second_opinion >= 0.55` | fired on **80.2%** of turns. That is noise, not advice | `0.70`, where the signal adds 7.8% rather than 40.6% |
| the asymmetric downgrade gate is the mechanism | it acts on **0.5%** of my work - one prompt in 192 | kept as a rail, and labelled as near-inert |

**The uncomfortable finding.** Four carefully reasoned thresholds operate on a
single prompt out of 192, because the classes that permit a downgrade already
*start* at the cheap tier (nothing below to drop to) and the classes that
dominate my work - review, ops-infra, architecture, bug-hunt, 72% of it - forbid
it. `sweep.py` reproduces this: every threshold row, including a deliberately
loose one, returns the same 27.1% cheap share and zero downgrades. **The lever
is the class-to-tier map.** The gate stays because it costs nothing and guards
workloads that are not mine, but it must not be sold as what saves anything here.

Other numbers worth keeping:

- **Latency** p50 913 ms, p95 1326 ms, max 1505 ms. That is what makes the
  hook's 2 s timeout a measurement rather than a hope.
- **Cost** US$ 0.000466 per decision. At ~100 prompts a day that is ~US$ 1.40 a
  month, and the ledger tracks `credits_remaining_usd` so the wallet running dry
  is visible before the hook quietly gets dumber.
- **31.8%** of real prompts fall under the confidence gate, and reading them
  explains why: they are conversational follow-ups - *"pode continuar com a
  outra frente"* - referring to a thread the classifier cannot see. The hook now
  **abstains** below 0.5 confidence (20.8% of turns) and still writes a ledger
  line. Advising on a coin flip spends credit and teaches me to distrust the
  advice that is good.
- **One saving declined.** Dropping `ops-infra` to the work tier would have
  moved 9.9% of work - more than twice what the `review` change moved - and
  taken frontier from 69.8% to 55.7%. Refused: a wrong ops-infra answer writes
  to a live system, and the failure is neither bounded nor detectable in time.
  The bigger number was the worse decision.

## Measurements about the tools, not the policy

Each of these cost one probe and would have cost much more as an assumption.

- **The CLIs on PATH are `.ps1` shims.** `Popen(["codex", ...])` dies with
  `[WinError 2] cannot find the file` - an error that names a missing FILE and
  sends you hunting for the wrong thing. `cli.py` resolves codex to its vendored
  `.exe` and gemini to `node + bundle`.
- **`codex exec` has no `--effort`.** Effort goes through
  `-c model_reasoning_effort=<level>`. The key is real (it appears in
  `codex.exe`), but `-c` accepts an invalid value *and* a wholly invented key
  without complaining - so a silently-ignored effort would have been written to
  the ledger as applied. `codex_task.py` validates against
  `~/.codex/models_cache.json` before spawning.
- **The Gemini CLI refuses to run headless in an untrusted folder**, and it is
  `--approval-mode plan` that turns the warning into a refusal - so plan and
  `--skip-trust` must travel together. Skipping the trust gate is sound *only
  because* plan mode is read-only.
- **Gemini's two transports differ by ~2400x at the floor**: 12,206 CLI tokens
  against 5 API tokens for the same 5-token prompt. CLI for one big agentic
  digest; API for many small questions.
- **This Gemini key is flash-only.** `gemini-pro-latest` and
  `gemini-3.1-pro-preview` answer 429; `gemini-4-argon` answers
  `TerminalQuotaError`. So Gemini is never the frontier fleet here, and in the
  jury it is the weakest of three voices - which `jury.py` prints rather than
  presenting three peers.
- **`ln -s` does not create symlinks on this machine.** Git-for-Windows MSYS
  copies instead, with exit status 0. `install.sh` now probes for it and warns;
  `install.ps1` makes junctions and reports which it used per item. A copy that
  claims to be a link breaks "edit here, every tool sees it" in silence.
  Measured outcome of the real install here, 2026-10-21: **junction** for both
  `~/.claude/skills/route-task` and `~/.codex/skills/route-task`, so edits in
  the repo are live. Agents are files and are copied - re-run the installer
  after editing one.
- **On Linux, `python` may not exist.** Measured on the second machine: only
  `python3`. The hook command `python <path>/route_hook.py` then exits 127 in
  the shell *before* the script runs - and the fail-open guarantee lives in the
  script, so a missing interpreter is the one failure it cannot catch. Every
  turn would open with a hook error. `install.sh` now prints the hooks block
  with the interpreter resolved (`python3` first), as `install.ps1` does.
- **Outside Windows there is no second place to find a key.** `env.py` falls
  back to HKCU only on Windows; on Linux and macOS a key that is not exported in
  the shell Claude Code was started from is simply absent, and the hook advises
  from the keyword table with no sign jev was never asked. `install.sh` warns
  when `JEV_API_KEY` or `GEMINI_API_KEY` is missing from its own shell.

## The leak I made building this

Worth recording because it is the same failure the router exists to prevent, and
I walked straight into it.

The two scrubber rules above were found by replaying real prompt history. I then
used the real strings as the test fixtures that lock those rules - a client
deliverable filename and two verbatim lines from a client's insurance document -
and quoted one of them in this document as the example. Four commits later, that
material was sitting in a **public** repository, in the file whose entire purpose
is to stop exactly that.

Nothing had been pushed, so it was removed from every tree in the branch with a
`git filter-branch` tree filter rather than fixed forward: a blob reachable from
any commit on a branch is published the moment that branch is. The fixtures are
now synthetic and exercise the same patterns, and the selftests still pass -
which is the proof that the real strings were never what made the test work.

Two rules came out of it, and they generalise past this repo:

- **A finding from real data is not a fixture.** Reproduce the SHAPE (spaces, an
  extension, an `@`-mention, a quoted run over 15 chars), never the content.
- **Check the check.** My first scan for leaked client strings used
  `git grep -i <needle> origin/main..HEAD`, which is not valid syntax for
  `git grep` - it searched nothing and reported zero hits for all six needles. A
  guard that reports "clean" because it ran on nothing is worse than no guard.
  The scan that found the leak was `git grep <needle> HEAD -- .`, plus a loop
  over `git rev-list` to prove it was gone from every commit and not just the
  tip.

## The judge, and why it only gets to flag

`judge.py` asks jev whether an output served its task. Measured twice against a
good answer and a hedging one:

- `served` separates them cleanly: **0.76 against 0.06**. `needed_more_capability`
  likewise: **0.10 against 0.87**.
- `quality` is directionally right but under-credits - "acceptable" for a clean,
  evidenced fix.
- A third question was **removed, and the removal is the finding.** It asked
  whether the reasoning contained an unsupported leap. First wording scored the
  good answer 0.79, because a text-only judge reads "ran it 50 times" as a claim
  it cannot see - it penalised precisely the outputs that report evidence.
  Rewording it for internal consistency did not fix the direction: 0.74 on the
  good answer against 0.58 on the hedging one, **inverted**. A question measured
  to point the wrong way is worse than no question.

So the judge is a **detector, not a grader**: act on a flag, do not trust a
clean verdict. And where a real check exists, pass `--mechanical pass|fail` - it
is stored separately, it wins, and it is the only signal in the ledger that did
not come from the same model that did the routing.

## What this will not do

- **It advises; it does not reroute.** Nothing is dispatched without you.
- **It prints no single cost number.** Two subscriptions measured in quota, one
  metered API with no verified price table, one exactly-billed API. Four units.
  Adding them would produce one confident, false number.
- **It cannot see remaining quota.** Neither Claude Max nor Codex exposes it.
  Quota pressure measures what I *spent*, and every consumer of that number says
  so rather than drawing a gauge.
- **It does not write your `settings.json`.** `install.ps1` prints the hooks
  block with absolute paths and leaves the merge to you.

## The ledger

`~/.claude/route-ledger.jsonl` - **outside this repo, always.** This repo is
public and the ledger holds (scrubbed) prompts and project names.
`.gitignore` is the second line of defense; living where git never looks is the
first. Asked independently, both Codex and Gemini reached the same conclusion
and named the same reason: `.gitignore` can be bypassed with `git add -f`.

Three record types, joined by `session_id`: `decision` (what was recommended),
`dispatch` (what I actually ran), `judgement` (whether it served). **The gap
between the first two is the calibration signal the whole design rests on**, and
it costs nothing to collect: when I ignore the advice, that disagreement is
recorded without me saying a word.
