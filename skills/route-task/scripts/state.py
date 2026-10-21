#!/usr/bin/env python3
"""Build the `state` the router sends to jev - and scrub it first.

    python state.py --prompt "fix the flaky test in the migration module"
    python state.py --prompt-file - --cwd /path/to/repo --json

WHY THIS FILE EXISTS, AND WHY IT IS NOT OPTIONAL

A UserPromptSubmit hook sees the raw prompt I type. Those prompts routinely carry
client names, card links, pipe ids, e-mail addresses and pasted private code.
jev is a third party - its own docs say it is "not affiliated with or endorsed by
TypeSafe AI" - so sending the raw prompt would publish client-adjacent material
outside the trust boundary on every single turn, invisibly.

So the state is BUILT, never forwarded:

  * the prompt is scrubbed and capped (this is the irreducible disclosure: the
    router cannot classify a task it is not told about);
  * repo signals are SHAPES, not names - stack, test runner, size bucket, dirty
    file count, branch KIND. "integrations-assistant" and "feat/route-task" never
    leave the machine; "python+typescript, large, 3 dirty, feat" does, and it is
    strictly more useful to the policy anyway;
  * a paste is NOT exempt, and the first version of this file claimed it was.
    MEASURED: Claude Code INLINES a paste into the prompt before any hook sees
    it - the TUI expands the "[Pasted text #1 +42 lines]" marker into the
    submitted string - so what arrives here is the expanded text. A paste is
    therefore scrubbed and capped exactly like any other prompt text: not
    exempt, not special-cased. 5.2% of measured prompts exceed the 600-char
    cap, so the usual fate of a big paste is that most of it is CUT, which is
    not the same promise as "never sent". The cap and the rules below are the
    whole defense here.

HONEST LIMIT: this is a SCRUBBER, not a redactor. It removes what has a shape
(URLs, ids, paths, keys, addresses). It cannot remove a client's name typed as a
bare word - no regex can. The project allowlist in route.py is the real defense;
this file reduces exposure, it does not eliminate it. Verify with --json before
trusting it, and re-verify after editing any pattern here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Hard cap on the scrubbed task text. Two jobs at once: cost (you pay per input
# token) and exposure (what is not sent cannot leak). 600 chars ~ 150 tokens.
# ORIGIN OF THIS NUMBER: a declared guess, not a measurement. calibrate.py is
# what replaces it - run it at two caps and compare the confidence distribution.
# Until that is done, this comment is the only honest thing to say about 600.
MAX_TASK_CHARS = 600

# The mark `cap` leaves at the cut. A module constant so the selftest can assert
# the real slack instead of guessing a round number next to it.
CUT_MARKER = " ...[trimmed]"

# --- paths -----------------------------------------------------------------
# The three path flavours (drive letter, POSIX, UNC) differ only in their root,
# so the rules are BUILT from one grammar. Three hand-written copies of the same
# segment grammar drift, and the one that drifts is the one nobody re-reads.
_SEG = r"[^\s\\/\"'<>|]+"                       # one segment: no space, no sep
# A segment may carry INTERNAL spaces, because client folder names do.
# MEASURED 2026-10-21: of the 44 client folders under Documents/Clientes on this
# machine, 27 (61%) contain a space, and the old rule `[^\s"'<>|]+` stopped at
# the first one - "...\Clientes\Banco Alfa Seguros\contrato.pdf" came out as
# "<path> Alfa Seguros\<file.pdf>", publishing the client's name in the part
# that survived. 27 of 27 leaked. No other rule here touches a bare name.
# The two bounds are not decoration: two nested unbounded quantifiers over a
# pasted file is how a scrubber turns into a stall.
_SPACED_SEG = rf"{_SEG}(?: {_SEG}){{0,6}}"
# The TAIL is separator-agnostic (a real path may mix `\` and `/`) and never
# space-tolerant: it is the LAST segment, so no following separator proves where
# it ends, and a space-tolerant tail eats the rest of the sentence.
_TAIL = r"[^\s\"'<>|]*"


def _path_rule(root: str, sep: str) -> re.Pattern[str]:
    r"""An absolute path that tolerates spaces INSIDE it and stops at the prose.

    A space is allowed only where the SAME separator proves the path continues,
    which is why the glue separator is fixed per flavour instead of `[\\/]`.
    With `[\\/]` as glue, "veja C:\a\b.txt e o arquivo x/y.py" matched
    "b.txt e o arquivo x" as a spaced segment - because a `/` happened to follow
    it - and the prose went into <path>. The last segment, where the extension
    lives, is never space-tolerant for the same reason.
    """
    s = re.escape(sep)
    return re.compile(rf"{root}(?:{_SPACED_SEG}{s}){{0,12}}{_TAIL}(?<![.,;:!?])")


_BS = re.escape("\\")

# --- credentials written in pt-BR ------------------------------------------
# The rubric above this was English-only, and the prompts in this project are
# pt-BR. MEASURED 2026-10-21, all three verbatim through the scrubber: "a senha
# do cliente e Trocar@2026", "a credencial e wk-9f8e7d6c5b4a39281706" and a PEM
# header survived whole. The comment over that block says credentials "must
# never survive, whatever else matches" - in one language.
#
# This is NOT the English rule with more words bolted on, for two reasons:
#   * that rule eats the NEXT token ("senha do" -> <credential>), which deletes
#     the keyword and leaves the secret standing three words later;
#   * `chave` and `usuario` are ordinary pt-BR prose ("a chave do dicionario",
#     "qual a chave primaria"), so the keyword alone cannot be the trigger.
# So the VALUE has to look like a secret: 8+ chars, at least one letter AND at
# least one digit or symbol, and not one of our own <placeholders>. The gap
# between keyword and value is at most three plain words, which is what the
# measured phrasings need ("senha do cliente e <value>") and little enough that
# a secret-shaped token further down the sentence is not dragged in.
_PT_CRED_WORD = r"(?:senhas?|chaves?|credenciais|credencial|segredos?|usu[áa]rios?)"
_PT_GAP = r"(?:\s+[A-Za-zÀ-ÿ]{1,12}){0,3}?"
_SECRETY = r"(?=[^\s<]{8,})(?=\S*[A-Za-z])(?=\S*(?:\d|[^\w\s]))\S+"

# --- key SHAPES ------------------------------------------------------------
# A key needs no cue word. MEASURED 2026-10-21 on a second machine (Linux, a
# SaaS API repo): 17 prompts of real history carried a full product API key
# (`mw_` + 40-odd base62 chars) written bare - "use this key mw_..." - and every
# one survived, because the only rules that could see it wanted "api key:",
# "token" or "secret" in front. The <blob> rule could not either: an `_` inside
# the key breaks its character class. Paddle entity ids (`pri_`, `sub_`,
# `txn_`, `ctm_` + 26 chars) survived the same way.
#
# Named, because jev.py re-runs these over the FINAL payload as a send gate
# (find_leak below): a shape that slips past the scrubber's ordering, or a
# caller that forgets to scrub, is refused before anything leaves the machine.
#
# The last one is generic on purpose: a vendor prefix, then a run that mixes
# upper case, lower case and digits. snake_case identifiers have no upper case
# and slugs have no mixing, so prose and code names survive; a random token
# almost never lacks all three. A false positive costs one placeholder - or,
# at the send gate, one silent turn. A false negative costs a key.
KEY_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jev-key", re.compile(r"jv_(?:live|test)_[A-Za-z0-9_\-]+")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_\-]{12,}")),
    ("google-key", re.compile("AIza" + r"[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("google-oauth", re.compile(r"(?:ya29|1//)[A-Za-z0-9_\-\.]{20,}")),
    ("musclewiki-key", re.compile(r"\bmw_[A-Za-z0-9_\-]{4,}")),
    ("paddle-key", re.compile(r"\bpdl_(?:live|sdbx)_[A-Za-z0-9_\-]{8,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}|\bwhsec_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws-key", re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b")),
    ("tailscale-key", re.compile(r"\btskey-[A-Za-z0-9\-]{10,}")),
    ("prefixed-token", re.compile(
        r"\b[A-Za-z][A-Za-z0-9]{1,11}[_-]"
        r"(?=[A-Za-z0-9_\-]{16,})(?=[A-Za-z0-9_\-]*\d)"
        r"(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])[A-Za-z0-9_\-]+")),
)

# Not secrets, but they name one customer's subscription, price or payment.
# Same send gate, different placeholder: the classifier is told "an id".
ID_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("paddle-id", re.compile(
        r"\b(?:pri|sub|txn|ctm|pro|add|biz|ntf|ntfset|evt|dsc|adj|cbk|paymtd|seg)"
        r"_[0-9a-z]{20,}\b")),
)


def find_leak(text: str) -> str | None:
    """The NAME of the first key or id shape in `text`, or None.

    Returns the shape's name, never the match: the caller reports it, and a
    report that quoted the match would be the leak it exists to prevent.
    """
    for name, pattern in KEY_SHAPES + ID_SHAPES:
        if pattern.search(text):
            return name
    return None


# Ordered: earlier patterns win. URLs before paths, because a URL contains
# slashes that the path pattern would otherwise eat half of.
SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # credentials first - they must never survive, whatever else matches
    *((pattern, "<key>") for _name, pattern in KEY_SHAPES),
    *((pattern, "<id>") for _name, pattern in ID_SHAPES),
    (re.compile(r"(?i)\b(?:bearer|authorization|api[_-]?key|token|password|secret)\b"
                r"\s*[:=]?\s*\S+"), "<credential>"),
    # `token` is deliberately NOT repeated below: it is the same word in both
    # languages and the rule above already owns it.
    (re.compile(rf"(?i)\b{_PT_CRED_WORD}\b{_PT_GAP}\s*[:=]?\s*{_SECRETY}"), "<credential>"),
    # A PEM block, with or without its END marker and with or without newlines -
    # the measured leak was a header glued straight to the base64. Continuation
    # lines must be 20+ base64 chars precisely so this cannot walk into prose.
    (re.compile(r"-----BEGIN [A-Z ]{0,24}PRIVATE KEY-----"
                r"[A-Za-z0-9+/=]*(?:\s+[A-Za-z0-9+/=]{20,})*"
                r"(?:\s*-----END [A-Z ]{0,24}PRIVATE KEY-----)?"), "<key>"),
    # JWTs: three base64url segments. Caught before the generic blob rule.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "<jwt>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\bhttps?://\S+"), "<url>"),
    # UNC first: `\\host\share\...` is unreachable by the two rules under it.
    (_path_rule(rf"{_BS}{_BS}{_SEG}{_BS}", "\\"), "<path>"),
    (_path_rule(rf"\b[A-Za-z]:{_BS}", "\\"), "<path>"),            # C:\Users\...
    (_path_rule(r"\b[A-Za-z]:/", "/"), "<path>"),                  # C:/Users/...
    (_path_rule(r"(?<![\w.])/(?:home|Users|mnt|opt|var|etc|srv)/", "/"), "<path>"),
    # FILENAMES. Found by the --offline calibration pass, 2026-10-21, before a
    # single token was spent: the scrubber was shipping
    # "Fixture_Doc_V4_01012026 - Copy.html" and "fixture_page.html" - client
    # deliverable names - because they are RELATIVE paths and the absolute-path
    # rules above never saw them. Claude Code's own @-mention syntax made them
    # frequent. The extension is kept (it is real signal about the stack); the
    # name, which is the leak, is not.
    (re.compile(r"@?\"[^\"\n]{1,100}\.(\w{1,6})\""), r"<file.\1>"),
    (re.compile(r"@?'[^'\n]{1,100}\.(\w{1,6})'"), r"<file.\1>"),
    (re.compile(r"@[^\s\"'\n]{1,100}\.(\w{1,6})"), r"<file.\1>"),
    # The (?<!<) stops this rule from re-matching INSIDE a placeholder the two
    # rules above just wrote: without it, @"x.html" became <<file.html>>, because
    # "file.html" is itself a bare filename. Substitutions are sequential, so any
    # later pattern can eat an earlier replacement.
    (re.compile(r"(?<![<\w.-])[\w\-]{1,60}\.(html?|pdf|docx?|xlsx?|pptx?|csv|png|jpe?g"
                r"|gif|svg|zip|py|tsx?|jsx?|json|md|ya?ml|sql|tf|go|rs|java|rb|sh|ps1"
                r"|bat|toml|ini|cfg|txt|log|xml|env)\b", re.I), r"<file.\1>"),
    # QUOTED CONTENT. Same pass, same reason: 'uma linha longa de conteudo citado'
    # and 'campo de formulario' are lines out of a client's insurance document,
    # quoted verbatim into a prompt. Long quoted runs are content; short ones are
    # usually identifiers ('main', 'sonnet', 'pytest') and carry classification
    # signal, so they survive.
    # The lookarounds on the single-quote rule matter: an apostrophe in ordinary
    # prose ("it's", "doesn't") pairs with the next real quote and swallowed
    # unrelated text - measured output was "'copy' is one that i<quoted>s".
    # A quote that opens a span is preceded by a non-word character.
    # 15 is a judgement call, not a measurement: it trades a little
    # classification signal for not shipping client prose.
    (re.compile(r"\"[^\"\n]{15,}\""), "<quoted>"),
    (re.compile(r"(?<![\w])'[^'\n]{15,}'(?![\w])"), "<quoted>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<sha>"),
    # Pipefy card/pipe/org ids BEFORE short shas: an all-digit run is an id in
    # practice, and "1109887766" also matches the short-sha pattern. Measured
    # 2026-10-21 with the sha rule first: card and org ids came out labelled
    # <sha>. Both labels scrub equally, so privacy held - but the classifier was
    # being told "sha" about a card id, and --show-state read wrong to a human.
    (re.compile(r"\b\d{6,}\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{7,12}\b(?=\s|$|[.,;)])"), "<sha>"),   # short git shas
    # Long opaque runs: base64 blobs, minified payloads. After the specific rules
    # so a real key is labelled <key> rather than <blob>.
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "<blob>"),
)

# How deep to look for stack/test markers. 3 covers the layouts that matter
# (backend/, frontend/, apps/<x>/, packages/<x>/) without turning into a walk.
MARKER_DEPTH = 3

# Markers -> stack name. First match per family wins; several families can hold.
STACK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")),
    ("node", ("package.json",)),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("ruby", ("Gemfile",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("dotnet", ("global.json",)),
    ("terraform", ("main.tf",)),
    ("docker", ("Dockerfile", "docker-compose.yml")),
)

TEST_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pytest", ("pytest.ini", "conftest.py", "tox.ini")),
    ("vitest-or-jest", ("vitest.config.ts", "vitest.config.js", "jest.config.js")),
    ("go-test", ("go.mod",)),
    ("cargo-test", ("Cargo.toml",)),
)

# Branch kinds we report. Anything else becomes "other" - a branch named after a
# client or a ticket title must not be the thing we ship off the machine.
BRANCH_KINDS = ("main", "master", "staging", "production", "develop",
                "feat", "fix", "hotfix", "chore", "docs", "release", "revert")


def scrub(text: str) -> str:
    """Remove everything with a leakable shape. See the honest limit in __doc__."""
    for pattern, replacement in SCRUBBERS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def cap(text: str, limit: int = MAX_TASK_CHARS) -> str:
    """Trim to `limit`, marking the cut so the classifier knows it saw a fragment."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + CUT_MARKER


def _git(args: list[str], cwd: Path) -> str:
    """Run a read-only git command. Empty string on any failure - not a git repo,
    git missing, or a timeout are all 'no signal', never an error that propagates."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def branch_kind(branch: str) -> str:
    """'feat/route-task' -> 'feat'. The slug after the slash is a feature NAME and
    is deliberately dropped: it is the part that leaks."""
    if not branch:
        return "unknown"
    head = branch.split("/", 1)[0].lower()
    return head if head in BRANCH_KINDS else "other"


def size_bucket(n_files: int) -> str:
    if n_files == 0:
        return "unknown"
    if n_files < 50:
        return "tiny"
    if n_files < 300:
        return "small"
    if n_files < 1500:
        return "medium"
    return "large"


def repo_signals(cwd: Path | None = None) -> dict:
    """Cheap, name-free signals about where the work is happening.

    These carry real information for the policy ("is this verifiable?" depends on
    whether a test runner exists) while naming nothing: no repo name, no branch
    slug, no paths.
    """
    cwd = Path(cwd or os.getcwd())
    root_out = _git(["rev-parse", "--show-toplevel"], cwd)
    root = Path(root_out) if root_out else cwd

    tracked = _git(["ls-files"], root)
    files = tracked.splitlines() if tracked else []
    n_files = len(files)
    dirty = _git(["status", "--porcelain"], root)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)

    # Markers are found at ANY depth up to MARKER_DEPTH, not just at the root.
    # MEASURED 2026-10-21: scanning only the root reported
    # {"stack": "docker", "test_runner": "none"} for a monorepo with 3,274 tests,
    # because its pyproject.toml, package.json and conftest.py live under
    # backend/ and frontend/. "no test runner" blocks every downgrade, so a
    # root-only scan would have permanently pinned that repo to expensive
    # routes - a wrong signal that fails costly and silently.
    # git ls-files is already fetched, respects .gitignore, and costs nothing.
    present = {
        part
        for path in files
        for part in [path.rsplit("/", 1)[-1]]
        if path.count("/") < MARKER_DEPTH
    }
    if root.is_dir():  # untracked-but-present markers still count
        present |= {p.name for p in root.iterdir()}
    stacks = [name for name, markers in STACK_MARKERS if present & set(markers)]
    runners = [name for name, markers in TEST_MARKERS if present & set(markers)]

    return {
        "stack": "+".join(stacks) or "unknown",
        "test_runner": runners[0] if runners else "none",
        "size": size_bucket(n_files),
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
        "branch_kind": branch_kind(branch),
        "in_git_repo": bool(root_out),
    }


def build(
    prompt: str,
    *,
    cwd: Path | None = None,
    recent_tools: list[str] | None = None,
    include_repo: bool = True,
) -> dict:
    """The full state object sent to jev. Nothing else is ever sent."""
    state: dict = {"task": cap(scrub(prompt))}
    if include_repo:
        state["repo"] = repo_signals(cwd)
    if recent_tools:
        # Tool NAMES only - never their arguments, which carry paths and content.
        state["recent_tools"] = [t for t in recent_tools if re.fullmatch(r"\w{1,32}", t)][:8]
    return state


# --- selftest --------------------------------------------------------------
# This is the privacy gate expressed as a test instead of as prose in a doc.
# A prose promise fails open; this fails loud.

NASTY = (
    "fix the webhook on https://app.pipefy.com/pipes/304512783 for "
    "maria.silva@acme-cliente.com.br — card 1109887766 keeps failing. "
    r"log at C:\Users\edward.wilms\Documents\Clientes\Acme\dump.txt and "
    "/home/deploy/app/.env has JEV_API_KEY=jv_live_Zq91xKpL77aabbccddee plus "
    "api_key: " + "AIza" + "Sy" + "D-" + "9" * 31 + " and token "
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abc123signature — "
    "org 302372204, sha 724b595f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f, "
    "uuid acd84a65-95be-4713-92b1-2ac314d41ecf. "
    # The two cases the --offline calibration pass caught on real history.
    'check @"Fixture_Doc_V4_01012026 - Copy.html" and @fixture_page.html, '
    "and add a line below 'uma linha longa de conteudo citado' in fixture_report.docx. "
    # The space-in-the-folder-name leak, in all three path flavours, plus a
    # credential phrased in pt-BR. Synthetic, like every fixture here: the rule
    # is to reproduce the SHAPE (a composite client name, a separator, an
    # extension), never real client content. See docs/ROUTER.md.
    r"abre C:\Users\edward.wilms\Documents\Clientes\Banco Alfa Seguros\contrato.pdf, "
    "o log em /var/log/Banco Alfa/erro.log "
    "e o anexo em " + "\\\\fileserver\\Contratos Alfa\\anexo.docx. "
    "a senha do cliente e Trocar@2026 e a credencial e wk-9f8e7d6c5b4a39281706. "
    "-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEAbcdefghijklmnopqrstuvwx"
)

MUST_NOT_SURVIVE = (
    "app.pipefy.com", "304512783", "maria.silva", "acme-cliente",
    "1109887766", "edward.wilms", "Clientes", "Acme", "dump.txt",
    "deploy", "jv_live_Zq91xKpL77", "AIza" + "SyD-99", "eyJhbGciOiJIUzI1NiJ9",
    "302372204", "724b595f1c2d", "acd84a65-95be",
    # filenames and quoted client content
    "Fixture_Doc", "Copy", "fixture_page", "fixture_report",
    "uma linha longa de conteudo",
    # the composite client name, in each path flavour. "Alfa Seguros" is the
    # exact fragment the old rule left behind, so it is a needle of its own.
    "Banco Alfa Seguros", "Alfa Seguros", "Banco Alfa", "Contratos Alfa",
    "fileserver", "contrato.pdf", "anexo.docx",
    # the pt-BR credentials and the PEM body
    "Trocar@2026", "wk-9f8e7d6c5b4a", "MIIEowIBAAKCAQEA",
)

# A PASTE, which is not exempt from anything - see the module docstring. The
# assertions over this fixture are deliberately only the two that are TRUE: the
# cap holds, and the shapes this scrubber knows how to remove are removed. There
# is no assertion that pasted content fails to reach the state, because it does.
# What stood here before was `"pasted" not in json.dumps(state)` over the
# fixture "do the thing": the word cannot appear in a built state, so it
# exercised nothing - and it would have FAILED on the marker form
# ("[Pasted text #1 +42 lines]") while PASSING on the expanded form, which is
# the one that carries the content.
PASTED_DIFF = (
    "aplique este diff que eu colei no prompt:\n"
    "--- a/app/cobranca/regras.py\n"
    "+++ b/app/cobranca/regras.py\n"
    "@@ -17,9 +17,9 @@ def taxa(valor, contrato):\n"
    r"-    tabela = carregar('C:\Users\ed\Documents\Clientes\Banco Beta Previdencia\taxas.json')"
    "\n"
    "+    tabela = carregar(TABELA_DE_TAXAS)\n"
    "-    avisar('joana.ramos@beta-cliente.com.br', assunto)\n"
    "+    avisar(responsavel.email, assunto)\n"
    '-    motivo = "o cliente pediu revisao da taxa de juros do contrato"\n'
    "+    motivo = motivo_do_card\n"
    "     if valor > limite:\n"
    "-        return valor * 1.19\n"
    "+        return valor * ALIQUOTA\n"
    "     registrar(contrato, valor, motivo)\n"
    "     if not tabela:\n"
    "-        raise RuntimeError('tabela de taxas vazia')\n"
    "+        raise TabelaVazia(contrato)\n"
    "     for faixa in tabela:\n"
    "         if faixa.cobre(valor):\n"
    "             return faixa.aplicar(valor, arredondar=True)\n"
    "     return valor * ALIQUOTA_PADRAO\n"
    # Padding with ordinary code, on purpose: the three leaky shapes sit in the
    # first ~300 chars, so they are inside the surviving window, while the
    # fixture as a whole clears the cap with room to spare. Without the margin
    # the "cap fired" check would flip to red on an unrelated scrubber edit.
    "@@ -48,6 +48,8 @@ def registrar(contrato, valor, motivo):\n"
    "-    linha = f'{contrato}:{valor}'\n"
    "+    linha = formatar(contrato, valor, motivo)\n"
    "     with aberto(DESTINO, 'a') as fh:\n"
    "         fh.write(linha + chr(10))\n"
    "     if contador % 100 == 0:\n"
    "         liberar(fh)\n"
)

PASTE_MUST_NOT_SURVIVE = (
    "Banco Beta Previdencia", "Beta Previdencia", "taxas.json",
    "joana.ramos", "beta-cliente", "revisao da taxa de juros",
)


def _selftest() -> int:
    failures = []

    def check(label: str, ok: bool, got: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  <- {got!r}" if not ok else ""))
        if not ok:
            failures.append(label)

    cleaned = scrub(NASTY)
    print(f"\nscrubbed:\n  {cleaned}\n")
    # A needle that is not in the fixture is a check that cannot fail. One was:
    # MUST_NOT_SURVIVE said "Copia" while NASTY says "- Copy", so the assertion
    # guarding the filename rules passed over a string nobody had ever scrubbed.
    # This loop makes that class of dead check impossible in the one file whose
    # whole job is to be the privacy gate - so it runs BEFORE the removals.
    for needle in MUST_NOT_SURVIVE:
        check(f"the fixture really contains {needle!r} before the scrub",
              needle in NASTY, "dead needle: proves nothing")
    for needle in MUST_NOT_SURVIVE:
        check(f"removed {needle!r}", needle not in cleaned)

    # The scrub must still leave something classifiable, or the router is blind.
    for kept in ("fix", "webhook", "failing"):
        check(f"kept the task verb/noun {kept!r}", kept in cleaned.lower())

    # Regressions produced by the scrubber's own substitutions, both measured on
    # real history 2026-10-21. They are cheap to reintroduce by editing a pattern
    # and invisible without an assertion, so they live here permanently.
    check("no placeholder is nested inside another", "<<" not in cleaned, cleaned)
    apostrophes = scrub("the 'copy' is one that i think it's what i wanted")
    check("an apostrophe in prose does not open a quoted span",
          "<quoted>" not in apostrophes, apostrophes)
    check("a short quoted identifier survives (it is signal, not content)",
          "'copy'" in apostrophes, apostrophes)

    # A path with a SPACE inside it: the leak, and the two ways the fix can go
    # wrong. A greedy space-tolerant pattern eats the rest of the sentence, so
    # the prose after the path is asserted as explicitly as the removal is.
    spaced = scrub(r"abre C:\Users\ed\Documents\Clientes\Banco Alfa Seguros\contrato.pdf"
                   " agora e me diga o que achou")
    check("a spaced client folder does not survive the path rule",
          "Alfa" not in spaced, spaced)
    check("the prose AFTER a spaced path survives",
          spaced.endswith("agora e me diga o que achou"), spaced)
    cases = {
        # text -> (must be gone, must still be there)
        r"veja C:\Users\ed\Banco Alfa Seguros\nota.pdf": ("Alfa", "veja"),
        r"veja C:\Users\ed\Banco Alfa Seguros\nota.pdf, depois volte":
            ("Alfa", "depois volte"),
        r"compare C:\a\Banco Alfa\x.txt com D:\b\Banco Gama\y.txt e me diga":
            ("Banco", "e me diga"),
        r"o arquivo C:\Users\ed\Banco Alfa Seguros\sub\relatorio sem extensao":
            ("Alfa", "o arquivo"),
        r"o log em /var/log/Banco Alfa/erro.log cresceu": ("Alfa", "cresceu"),
        "o anexo em " + "\\\\fileserver\\Contratos Alfa\\anexo.docx" + " chegou":
            ("Alfa", "chegou"),
        r"veja C:/Users/ed/Banco Alfa Seguros/nota.pdf por favor": ("Alfa", "por favor"),
        r"veja C:\a\b.txt e o arquivo x/y.py": ("b.txt", "e o arquivo"),
    }
    for text, (gone, kept_) in cases.items():
        got = scrub(text)
        check(f"path case: {gone!r} gone from {text[:38]!r}...", gone not in got, got)
        check(f"path case: {kept_!r} survives", kept_ in got, got)

    # pt-BR credentials, and the prose that must NOT be mistaken for one. The
    # second group is why the keyword alone cannot be the trigger: `chave` and
    # `usuario` are ordinary words in this language.
    for text, secret in (("a senha do cliente e Trocar@2026", "Trocar@2026"),
                         ("a credencial e wk-9f8e7d6c5b4a39281706", "wk-9f8e"),
                         ("usuario: admin e senha: p4ssw0rd!2026", "p4ssw0rd"),
                         ("-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEAbcdefgh",
                          "MIIEow")):
        got = scrub(text)
        check(f"pt-BR credential removed from {text[:34]!r}", secret not in got, got)
    for prose in ("a chave do dicionario", "qual a chave primaria da tabela",
                  "o usuario reclamou do relatorio", "preciso da chave estrangeira"):
        got = scrub(prose)
        check(f"pt-BR prose is not a credential: {prose!r}",
              "<credential>" not in got and got == prose, got)

    # Bare keys and ids, no cue word. Every value is BUILT from a repeated
    # pattern so it is obviously not a real key - the shape is what is under
    # test, never the content (see docs/ROUTER.md on the fixture leak).
    fake = "Ab3" * 14                                   # mixed case + digits, 42 chars
    for text, secret, shape in (
        (f"use this key mw_{fake} please", "Ab3Ab3", "musclewiki-key"),
        (f"curl ?api_key=mw_{fake[:20]}", "Ab3Ab3", "musclewiki-key"),
        ("it was mw_Ab3C...", "Ab3C", "musclewiki-key"),       # a truncated one too
        (f"sandbox pdl_sdbx_apikey_01{'h' * 24}x9", "hhhhhhhh", "paddle-key"),
        (f"stripe sk_live_{fake[:24]}", "Ab3Ab3", "stripe-key"),
        (f"slack xoxb-{'1234-' * 3}abcdEFGH", "1234-1234", "slack-token"),
        ("aws AKIA" + "ABCD2345" * 2 + " here", "ABCD2345", "aws-key"),
        (f"tailscale tskey-auth-{fake[:20]}", "Ab3Ab3", "tailscale-key"),
        (f"some vendor xyz_{fake[:24]} token", "Ab3Ab3", "prefixed-token"),
        (f"customer ctm_01{'k' * 24} on sub_01{'q' * 24}", "kkkkkkkk", "paddle-id"),
        (f"price pri_01{'m' * 24}", "mmmmmmmm", "paddle-id"),
    ):
        got = scrub(text)
        check(f"bare {shape} removed from {text[:30]!r}", secret not in got, got)
        check(f"the send gate names it {shape!r}", find_leak(text) == shape, find_leak(text))
        check(f"the send gate is quiet after the scrub ({shape})", find_leak(got) is None, got)
        check(f"the send gate never quotes the match ({shape})",
              secret not in str(find_leak(text)), find_leak(text))
    # ...and the code names this kind of prompt is full of are NOT keys. Each of
    # these is classification signal, and a send gate that fired on them would
    # silence the router on every turn that mentions code.
    for prose in ("get_muscle_roles_allowed in dependencies", "claude-opus-5-5 and gpt-6.1-sol",
                  "test_bodymap_stream_returns_403_for_growth_tier", "JEV_API_KEY is unset",
                  "ExerciseStandard and TierRestrictionError", "the issue-486 worktree",
                  "feat/route-task on main", "submit_button_v2 and react-dom-18"):
        check(f"code name is not a key: {prose!r}",
              find_leak(prose) is None and "<key>" not in scrub(prose), scrub(prose))

    check("cap marks the cut", cap("y" * (MAX_TASK_CHARS + 50)).endswith(CUT_MARKER))
    check("cap respects the limit", len(cap("y" * 5000)) <= MAX_TASK_CHARS + len(CUT_MARKER))

    sig = repo_signals(Path(__file__).resolve().parent)
    print(f"\nrepo signals here: {json.dumps(sig)}")
    check("signals name nothing", not any(
        isinstance(v, str) and ("/" in v or "\\" in v or "ai-skills" in v)
        for v in sig.values()
    ), sig)

    st = build("do the thing", recent_tools=["Bash", "Edit", "rm -rf /"])
    check("tool args are rejected, only names pass", st["recent_tools"] == ["Bash", "Edit"],
          st["recent_tools"])
    # The paste. Both halves matter: the needles must be IN the fixture (or the
    # removals prove nothing), and the cap must actually fire (or the removals
    # pass because the text was truncated before reaching them).
    task = build(PASTED_DIFF, include_repo=False)["task"]
    check("a pasted diff is capped like any other prompt text",
          len(task) <= MAX_TASK_CHARS + len(CUT_MARKER), len(task))
    check("the paste is long enough that the cap fired", task.endswith(CUT_MARKER),
          task[-40:])
    for needle in PASTE_MUST_NOT_SURVIVE:
        check(f"paste: the fixture really contains {needle!r}", needle in PASTED_DIFF,
              "dead needle: proves nothing")
        check(f"paste: removed {needle!r}", needle not in task, task)
    # ...and the replacements are INSIDE the surviving window, which is what
    # separates "the rule removed it" from "the cap happened to cut it off".
    for mark in ("<path>", "<email>", "<quoted>"):
        check(f"paste: {mark} was substituted before the cut", mark in task, task)

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file", help="path, or '-' for stdin")
    ap.add_argument("--cwd")
    ap.add_argument("--no-repo", action="store_true", help="omit repo signals")
    ap.add_argument("--json", action="store_true", help="print the exact outbound payload")
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

    state = build(text, cwd=Path(a.cwd) if a.cwd else None, include_repo=not a.no_repo)
    print(json.dumps(state, indent=2, ensure_ascii=False) if a.json
          else json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
