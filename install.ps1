<#
.SYNOPSIS
  Install this repo's skills and agents for Claude Code and Codex, on Windows.

.DESCRIPTION
  install.sh exists and is the POSIX path. This file exists because on this
  machine there is no `bash` on PATH, and because install.sh links with
  `ln -sfn`: under MSYS without symlink privilege that silently COPIES, which
  quietly breaks the one property the symlink layout is for ("edit here, every
  tool sees it"). A copy that pretends to be a link is worse than an honest copy.

  So this script tries a real junction/symlink first and tells you, per item,
  which one you got. It also GENERATES the hooks block with absolute paths
  resolved, because a hook command containing "~" is not reliably expanded.

  It never overwrites settings.json. It prints the JSON for you to merge.

.PARAMETER DryRun
  Print what would change, touch nothing.

.PARAMETER Uninstall
  Remove only the links/copies that point into this repo.

.PARAMETER SkipCodex
  Do not install into ~/.codex/skills.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$Uninstall,
  [switch]$SkipCodex,
  # Installing all 15 skills costs 15 always-resident descriptions in every
  # system prompt - the exact thing this repo's own context-budget skill tells
  # you to count. -Only lets you take just what you want.
  [string[]]$Only
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClaudeSkills = Join-Path $HOME '.claude\skills'
$ClaudeAgents = Join-Path $HOME '.claude\agents'
$CodexSkills  = Join-Path $HOME '.codex\skills'

function Say([string]$m, [string]$colour = 'Gray') { Write-Host $m -ForegroundColor $colour }

function Link-One {
  param([string]$Source, [string]$Root)
  $name = Split-Path -Leaf $Source
  $dest = Join-Path $Root $name

  if ($Uninstall) {
    if (Test-Path $dest) {
      $item = Get-Item $dest -Force
      $target = $item.Target
      $pointsHere = ($target -and ($target -join '') -like "$Repo*") -or
                    (-not $target -and (Test-Path (Join-Path $dest '.from-ai-skills')))
      if ($pointsHere) {
        if (-not $DryRun) { Remove-Item $dest -Recurse -Force }
        Say "  removed $dest" 'Yellow'
      } else {
        Say "  SKIP $dest does not point into this repo" 'DarkYellow'
      }
    }
    return
  }

  if (Test-Path $dest) {
    $item = Get-Item $dest -Force
    $isOurs = ($item.Target -and ($item.Target -join '') -like "$Repo*") -or
              (Test-Path (Join-Path $dest '.from-ai-skills'))
    if (-not $isOurs) {
      Say "  SKIP $dest exists and is not ours (move it away first)" 'Red'
      return
    }
    if (-not $DryRun) { Remove-Item $dest -Recurse -Force }
  }

  if ($DryRun) { Say "  would link $dest -> $Source"; return }

  # A junction needs no privilege and behaves like a link for reads, which is
  # all a skill directory needs. Try it first; fall back to a copy and SAY SO.
  try {
    New-Item -ItemType Junction -Path $dest -Target $Source -ErrorAction Stop | Out-Null
    Say "  linked (junction) $dest" 'Green'
  } catch {
    try {
      New-Item -ItemType SymbolicLink -Path $dest -Target $Source -ErrorAction Stop | Out-Null
      Say "  linked (symlink)  $dest" 'Green'
    } catch {
      Copy-Item $Source $dest -Recurse -Force
      # The marker is how -Uninstall can tell our copy from someone else's dir,
      # since a copy carries no Target to inspect.
      Set-Content (Join-Path $dest '.from-ai-skills') $Repo -Encoding UTF8
      Say "  COPIED $dest  <- not a link: edits here will NOT be seen until you re-run" 'Yellow'
    }
  }
}

function Get-SkillDirs {
  $all = Get-ChildItem (Join-Path $Repo 'skills') -Directory
  if (-not $Only) { return $all }
  $picked = $all | Where-Object { $Only -contains $_.Name }
  $missing = $Only | Where-Object { $all.Name -notcontains $_ }
  foreach ($m in $missing) { Say "  SKIP unknown skill '$m'" 'Red' }
  return $picked
}

# --- skills -----------------------------------------------------------------
Say ""
Say "skills -> $ClaudeSkills" 'Cyan'
if (-not $DryRun -and -not $Uninstall) { New-Item -ItemType Directory -Force $ClaudeSkills | Out-Null }
Get-SkillDirs | ForEach-Object { Link-One $_.FullName $ClaudeSkills }

if (-not $SkipCodex) {
  Say ""
  Say "skills -> $CodexSkills" 'Cyan'
  if (-not $DryRun -and -not $Uninstall) { New-Item -ItemType Directory -Force $CodexSkills | Out-Null }
  Get-SkillDirs | ForEach-Object { Link-One $_.FullName $CodexSkills }
}

# --- agents -----------------------------------------------------------------
# Agents are FILES, not directories, and install.sh had no target for them at
# all before route-task needed one.
$agentDir = Join-Path $Repo 'config\claude\agents'
if (Test-Path $agentDir) {
  Say ""
  Say "agents -> $ClaudeAgents" 'Cyan'
  if (-not $DryRun -and -not $Uninstall) { New-Item -ItemType Directory -Force $ClaudeAgents | Out-Null }
  Get-ChildItem $agentDir -Filter *.md | ForEach-Object {
    $dest = Join-Path $ClaudeAgents $_.Name
    if ($Uninstall) {
      if (Test-Path $dest) {
        if (-not $DryRun) { Remove-Item $dest -Force }
        Say "  removed $dest" 'Yellow'
      }
    } elseif ($DryRun) {
      Say "  would copy $dest"
    } else {
      Copy-Item $_.FullName $dest -Force
      Say "  copied $dest" 'Green'
    }
  }
}

if ($Uninstall) { Say ""; Say "done (uninstall)" 'Cyan'; exit 0 }

# --- the hooks block, with absolute paths -----------------------------------
$hookDir = Join-Path $ClaudeSkills 'route-task\scripts'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }

$hooks = [ordered]@{
  UserPromptSubmit = @(
    [ordered]@{ hooks = @( [ordered]@{
      type = 'command'
      command = "`"$py`" `"$(Join-Path $hookDir 'route_hook.py')`""
      timeout = 5
    }) }
  )
  PreToolUse = @(
    [ordered]@{
      matcher = 'Agent'
      hooks = @( [ordered]@{
        type = 'command'
        command = "`"$py`" `"$(Join-Path $hookDir 'agent_hook.py')`""
        timeout = 5
      })
    }
  )
}

Say ""
Say "=== merge this into $HOME\.claude\settings.json under `"hooks`" ===" 'Cyan'
($hooks | ConvertTo-Json -Depth 6)
Say "=== end ===" 'Cyan'
Say ""
Say "Not written for you on purpose: settings.json carries machine-specific" 'DarkGray'
Say "choices, and this script silently overwriting them would be the kind of" 'DarkGray'
Say "helpful that loses work." 'DarkGray'

# --- warnings that are about the EXISTING config, not about route-task ------
# Checked in BOTH places: the live settings, and the repo template that the
# README tells you to merge by hand. Warning only about the live file would
# stay silent for exactly the person who has not merged it yet - which is the
# person the warning is for.
$settings = Join-Path $HOME '.claude\settings.json'
$template = Join-Path $Repo 'config\claude\settings.template.json'
$raw = ''
foreach ($f in @($settings, $template)) {
  if (Test-Path $f) { $raw += (Get-Content $f -Raw) }
}
if ($raw) {
  if ($raw -match 'rtk hook claude' -and -not (Get-Command rtk -ErrorAction SilentlyContinue)) {
    Say ""
    Say "WARNING: settings.json or config/claude/settings.template.json wires a" 'Yellow'
    Say "PreToolUse hook to 'rtk hook claude', and 'rtk' is not on PATH here." 'Yellow'
    Say "That hook would fail on every Bash call." 'Yellow'
    Say "It predates route-task and this script does not touch it - but a hook" 'Yellow'
    Say "pointing at a missing binary is better known than discovered." 'Yellow'
  }
}
foreach ($pair in @(@('JEV_API_KEY', 'route.py and judge.py'), @('GEMINI_API_KEY', 'gemini_task.py'))) {
  if (-not [Environment]::GetEnvironmentVariable($pair[0], 'User') -and
      -not [Environment]::GetEnvironmentVariable($pair[0], 'Machine')) {
    Say ""
    Say "WARNING: $($pair[0]) is not set; $($pair[1]) will refuse with an actionable error." 'Yellow'
  }
}

$old = Join-Path $HOME '.claude\commands'
if (Test-Path $old) {
  Get-SkillDirs | ForEach-Object {
    $clash = Join-Path $old "$($_.Name).md"
    if (Test-Path $clash) { Say "WARN $clash duplicates skill /$($_.Name); remove it" 'Yellow' }
  }
}

Say ""
Say "next: verify before trusting" 'Cyan'
Say "  python `"$(Join-Path $hookDir 'cli.py')`" --selftest"
Say "  python `"$(Join-Path $hookDir 'selftest_hooks.py')`""
Say "  python `"$(Join-Path $hookDir 'route.py')`" --selftest"
Say "  python `"$(Join-Path $hookDir 'route.py')`" --prompt `"<a real task>`" --show-state"
