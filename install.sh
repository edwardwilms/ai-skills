#!/usr/bin/env bash
# Symlink skills/<name>/ into each agent's skills directory.
#
#   ./install.sh                      # Claude Code only (default)
#   ./install.sh --codex              # Codex only
#   ./install.sh --claude --codex     # both
#   ./install.sh --upstream-codex     # also link lhermoso's gh-workflow-suite into ~/.codex/skills
#   ./install.sh --dry-run ...        # print what would change
#   ./install.sh --uninstall ...      # remove only symlinks that point into this repo
#
# Also links config/claude/agents/*.md into ~/.claude/agents (route-task needs
# three dispatch agents, and there was no target for agent FILES before).
#
# On Windows use install.ps1 instead: there is no bash on PATH there, and `ln`
# under MSYS without symlink privilege silently COPIES - which looks like a
# successful link and quietly breaks "edit here, every tool sees it". The check
# below detects that and says so rather than letting it pass.
#
# Adding a new agent: add one line to TARGETS below. Every agent that reads the
# open Agent Skills layout (<dir>/<name>/SKILL.md) works the same way.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
declare -A TARGETS=(
  [claude]="$HOME/.claude/skills"
  [codex]="$HOME/.codex/skills"
)

selected=() dry=0 uninstall=0 upstream_codex=0
for arg in "$@"; do
  case "$arg" in
    --claude) selected+=(claude) ;;
    --codex) selected+=(codex) ;;
    --upstream-codex) upstream_codex=1 ;;
    --dry-run) dry=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ ${#selected[@]} -eq 0 ] && selected=(claude)

run() { if [ "$dry" = 1 ]; then echo "+ $*"; else "$@"; fi; }

# `ln -s` is not guaranteed to produce a symlink: MSYS/Git-for-Windows falls
# back to copying when it lacks the privilege, with exit status 0. A copy that
# claims to be a link is the failure that is invisible, so prove it once.
check_symlinks_work() {
  local probe
  probe="$(mktemp -d)" || return 0
  : > "$probe/target"
  if ln -s "$probe/target" "$probe/link" 2>/dev/null && [ -L "$probe/link" ]; then
    rm -rf "$probe"
    return 0
  fi
  rm -rf "$probe"
  echo "WARN this system does not create real symlinks with 'ln -s'." >&2
  echo "     Anything 'linked' below is a COPY: edits in the repo will NOT be" >&2
  echo "     seen by the agents until you re-run install. On Windows prefer" >&2
  echo "     install.ps1, which makes junctions and tells you which it used." >&2
  return 1
}

link_one() {  # <source dir> <target root>
  local src="$1" root="$2" name dest
  name="$(basename "$src")"; dest="$root/$name"
  if [ "$uninstall" = 1 ]; then
    if [ -L "$dest" ] && [[ "$(readlink "$dest")" == "$REPO"/* ]]; then run rm "$dest"; echo "removed $dest"; fi
    return
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "SKIP $dest exists and is not a symlink (move it away first)" >&2; return
  fi
  run ln -sfn "$src" "$dest"; echo "linked $dest -> $src"
}

[ "$uninstall" = 0 ] && check_symlinks_work || true

for tool in "${selected[@]}"; do
  root="${TARGETS[$tool]}"
  run mkdir -p "$root"
  for src in "$REPO"/skills/*/; do link_one "${src%/}" "$root"; done
done

# Agents are FILES, so they get linked individually rather than by directory.
if [[ " ${selected[*]} " == *" claude "* ]] && [ -d "$REPO/config/claude/agents" ]; then
  agents_root="$HOME/.claude/agents"
  run mkdir -p "$agents_root"
  for src in "$REPO"/config/claude/agents/*.md; do
    [ -e "$src" ] || continue
    dest="$agents_root/$(basename "$src")"
    if [ "$uninstall" = 1 ]; then
      if [ -L "$dest" ] && [[ "$(readlink "$dest")" == "$REPO"/* ]]; then
        run rm "$dest"; echo "removed $dest"
      fi
      continue
    fi
    run ln -sfn "$src" "$dest"; echo "linked $dest -> $src"
  done
fi

if [ "$upstream_codex" = 1 ]; then
  [ -f "$REPO/upstream/lhermoso/skills/gh-workflow-suite/SKILL.md" ] || git -C "$REPO" submodule update --init
  run mkdir -p "${TARGETS[codex]}"
  link_one "$REPO/upstream/lhermoso/skills/gh-workflow-suite" "${TARGETS[codex]}"
fi

# --- the route-task hooks block, with the interpreter and path resolved -------
# The same block install.ps1 prints, for Linux and macOS. The interpreter is
# resolved, never assumed: MEASURED 2026-10-21 on a Linux box, `python` did not
# exist (only python3), so the template's `python <path>` exited 127 in the
# shell BEFORE route_hook.py ran - and the hook's fail-open guarantee lives in
# the script, so it never got the chance. Every turn would have opened with a
# hook error. python3 first, because on some systems `python` is still 2.x.
if [ "$uninstall" = 0 ] && [[ " ${selected[*]} " == *" claude "* ]] \
   && [ -d "$REPO/skills/route-task/scripts" ]; then
  py="$(command -v python3 || command -v python || true)"
  if [ -z "$py" ]; then
    echo "WARN no python3 or python on PATH: the route-task hooks need one; not printing them" >&2
  else
    hook_dir="${TARGETS[claude]}/route-task/scripts"
    echo
    echo "=== merge this into $HOME/.claude/settings.json under \"hooks\" ==="
    # JSON built by Python, not by echo: a path with a quote or a space in it is
    # exactly what hand-assembled JSON gets wrong.
    "$py" - "$py" "$hook_dir" <<'PY'
import json, shlex, sys
py, hook_dir = sys.argv[1], sys.argv[2]
cmd = lambda script: f"{shlex.quote(py)} {shlex.quote(hook_dir + '/' + script)}"
print(json.dumps({
    "UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": cmd("route_hook.py"), "timeout": 5}]}],
    "PreToolUse": [{"matcher": "Agent", "hooks": [
        {"type": "command", "command": cmd("agent_hook.py"), "timeout": 5}]}],
}, indent=2))
PY
    echo "=== end ==="
    echo "Not written for you on purpose: settings.json carries machine-specific choices."
  fi
  # Outside Windows there is no registry fallback (env.py), so a key that is not
  # in the environment Claude Code was started from is simply absent - and the
  # hook then advises from the keyword table with no sign jev was never asked.
  for pair in "JEV_API_KEY:route.py, judge.py and the hook" "GEMINI_API_KEY:gemini_task.py --ask"; do
    name="${pair%%:*}"
    if [ -z "${!name:-}" ]; then
      echo "WARN $name is not set in this shell; ${pair#*:} will run without it." >&2
      echo "     export it in your shell profile and restart Claude Code from that shell." >&2
    fi
  done
  if ! command -v rtk >/dev/null 2>&1 \
     && grep -qs 'rtk hook claude' "$HOME/.claude/settings.json" "$REPO/config/claude/settings.template.json"; then
    echo "WARN a PreToolUse hook runs 'rtk hook claude' and rtk is not on PATH: it would fail on every Bash call." >&2
  fi
fi

# Old-style commands with the same name would show up twice as /<name>.
if [ "$uninstall" = 0 ] && [[ " ${selected[*]} " == *" claude "* ]]; then
  for src in "$REPO"/skills/*/; do
    n="$(basename "$src")"
    [ -e "$HOME/.claude/commands/$n.md" ] && echo "WARN ~/.claude/commands/$n.md duplicates skill /$n; remove it" >&2
  done
fi
exit 0
