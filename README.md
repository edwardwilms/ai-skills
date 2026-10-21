# ai-skills

Personal agent skills, kept in one place so they survive machine changes and switching or adding LLM tools.

## Layout

```
skills/<name>/SKILL.md      canonical skills, open Agent Skills format (one folder per skill;
                            scripts and prompts live next to their SKILL.md)
upstream/lhermoso/          git submodule: lhermoso/claude-workflows, pinned, reference only
config/claude/              global CLAUDE.md, RTK.md, settings.template.json (org specifics stripped)
config/claude/agents/       subagent definitions (model + effort frontmatter), copied to ~/.claude/agents
docs/ROUTER.md              the routing policy, every measurement behind it, and what leaves the machine
docs/UPSTREAM-DIFF.md       which skills fork which upstream file, and every local delta
install.sh                  symlinks skills into each tool's skills directory (POSIX), plus the hooks block
install.ps1                 the Windows path: junctions, plus the hooks block with absolute paths
```

Why this shape:
- **One format, many tools.** `<dir>/<name>/SKILL.md` is read by Claude Code (`~/.claude/skills`) and Codex (`~/.codex/skills`). A new tool that reads the same layout needs one line in `install.sh`'s `TARGETS`.
- **Symlinks, not copies.** Edit a skill here, and every tool sees it at once. `git log` is the history.
- **Upstream stays pristine.** The submodule is never edited; `docs/UPSTREAM-DIFF.md` says how ours differ.
- **Tool-specific bits are labelled.** Skill bodies mention Claude Code tools (Agent, Bash, `$ARGUMENTS`) and the Codex CLI where they need them. Other agents read these as instructions; adjust a skill when a tool lacks a feature.

## Install

```bash
git clone --recurse-submodules git@github.com:edwardwilms/ai-skills.git ~/Documentos/GitHub/ai-skills
cd ~/Documentos/GitHub/ai-skills
./install.sh                         # Claude Code: ~/.claude/skills/<name> -> skills/<name>
./install.sh --codex                 # Codex
./install.sh --codex --upstream-codex  # plus lhermoso's gh-workflow-suite for Codex
./install.sh --dry-run --claude      # preview
./install.sh --uninstall --claude    # remove only links that point here
```

**On Windows use `install.ps1`.** There is no `bash` on PATH there, and `ln -s`
under Git-for-Windows MSYS **copies instead of linking, with exit status 0** -
measured on this machine. A copy that claims to be a link breaks the "edit here,
every tool sees it" property in silence. `install.sh` now probes for this and
warns; `install.ps1` makes junctions and reports, per item, which it used.

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Only route-task
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
```

`-Only` exists because installing all the skills costs one always-resident
description each, in every system prompt - the thing `context-budget` tells you
to count.

Claude Code invokes a skill as `/<name>` exactly like the old `~/.claude/commands/<name>.md`. Do not keep both:
`install.sh` warns about a leftover `~/.claude/commands/<name>.md`.

Global config is not linked automatically (settings carry machine-specific choices). Copy by hand:
`config/claude/CLAUDE.md` and `RTK.md` → `~/.claude/`; merge `settings.template.json` into `~/.claude/settings.json`.

## Skills

| Skill | What it does |
|---|---|
| `full-review` | Claude ↔ Codex review loop: 4 gates (correctness, requirements, architecture, security) + defect checklist, mutation check of new tests, exact-head CI gate, posts an audit record. Scripts in `skills/full-review/scripts/` |
| `issue-pipeline` | Issue → plan → fix → PR → review |
| `drain-issues` | Dependency-aware batch processing of the issue backlog |
| `batch-issues` | Parallel issue processing without dependency analysis |
| `fix-issue` | Issue → worktree → TDD fix → PR, with a pre-edit fact gate |
| `quick-fix` | Fast autonomous fix |
| `create-issue` | Well-structured GitHub issue with acceptance criteria |
| `commit` / `pr` | Conventional commit; detailed PR with changelog |
| `review-pr` / `review-changes` | PR merge-safety review; diff regression review |
| `scan-debt` | Tech-debt and code-smell scan |
| `audit-commands` | Audit skills for overlap, staleness, drift |
| `context-budget` | Rank what is consuming the context window |
| `route-task` | Route a dev task across Claude / Codex / Gemini - fleet, model, effort - with jev as the classifier and a measured policy as the decision. Cross-fleet jury, quota-aware dispatch, judge, ledger report. Scripts in `skills/route-task/scripts/`, policy in `routes.v1.json`, reference in [`docs/ROUTER.md`](docs/ROUTER.md) |

## Adding a skill

1. `mkdir skills/<name>` and write `skills/<name>/SKILL.md` with `name:` and `description:` frontmatter (`argument-hint`, `allowed-tools` optional).
2. `./install.sh` (re-run is safe).
3. Commit.

## Never commit

Credentials (`~/.claude/.credentials.json`), config backups, session history,
project memory, `.env` files. Also the **route ledger** and the full
calibration: both hold scrubbed prompts, which still carry business context.
The ledger lives in `~/.claude/` precisely so git never sees it; only
`calibration-*.summary.json` (aggregates, no prompt text) is committed, so the
"measured" claims in `routes.v1.json` stay checkable.
