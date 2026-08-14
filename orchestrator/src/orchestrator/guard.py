"""PreToolUse workspace-containment guard.

implements: spec/SPEC.md §1 binding row "sandbox / safety invariants ->
            PreToolUse hooks vetoing tool calls outside the per-issue
            workspace"; complements core §9.5 invariants at the tool layer.

Standalone script (stdlib only) injected into the agent session by runner.py
via `--settings`. Claude Code invokes it before each matched tool call with a
JSON payload on stdin; exit 2 + stderr = deny (fed back to the model), exit 0 =
allow.

Scope (documented), two independent rules:

1. Containment: file-mutation tools (Write/Edit/MultiEdit/NotebookEdit) are
   denied when their target path resolves outside the workspace.
2. Merge guard (issue #133, AgDR-036): Bash calls whose command matches one of
   an ENUMERATED set of Gate-C-violating verb shapes (`gh pr merge`,
   `gh pr review … --approve`, `gh pr close`, force-pushes) are denied. This is
   a pinned enumeration, NOT general Bash static analysis and NOT a security
   boundary: denial semantics are soft (AgDR-004's addendum — denials are fed
   back to the agent, which may route around them), and `gh api -X PUT
   repos/{o}/{r}/pulls/{n}/merge` reaches the same endpoint with the same
   inherited token. It raises the cost of a Gate C violation and makes every
   attempt observable in the transcript; branch protection is the mechanical
   backstop for the residual. Everything else about Bash is still unanalyzed —
   the workspace cwd, the fresh clone, and the allowlisted git/gh commands
   bound the blast radius there.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import sys
from pathlib import Path

FILE_PATH_KEYS = ("file_path", "notebook_path", "path")

HANDOFF_HINT = "(Gate C is Colin's — hand off, don't self-merge)"

# `gh pr <verb>` shapes denied outright, verb -> human name of the shape.
GH_PR_DENIED_VERBS = {
    "merge": "gh pr merge",
    "close": "gh pr close",
}


def _decode_ansi_c(body: str) -> str:
    """Decode a bash ANSI-C `$'...'` body into a plain single-quoted word, so
    tokens match what bash hands the CLI (codex review r16, PR #136 —
    `gh $'pr' merge` and `$'\\x70ush'` execute denied verbs while shlex left
    `$pr` in the token). unicode_escape covers every letter-producing bash
    escape (\\xHH, \\nnn octal, \\uXXXX, \\UXXXXXXXX); \\e/\\E are mapped
    first; \\cX yields only control chars, which cannot spell a verb."""
    body = body.replace("\\e", "\\x1b").replace("\\E", "\\x1b")
    text = codecs.decode(body, "unicode_escape")
    return "'" + text.replace("'", "'\\''") + "'"


# redirection operators, longest-first; the fd prefix — numeric (`2>`) or
# bash's brace allocation (`{fd}>` — codex review r21, PR #136) — is only an
# operator at a word boundary (`a2>` is word `a2` + `>`)
_REDIR_OPS = r"(?P<op><<-|<<<|<<|&>>|&>|>>|>\||<>|>&|<&|>|<)"
_REDIR_RE = re.compile(r"(?P<fd>)" + _REDIR_OPS)
_REDIR_WORD_RE = re.compile(
    r"(?P<fd>\d*|\{[A-Za-z_][A-Za-z0-9_]*\})" + _REDIR_OPS)


def _balanced_paren_end(command: str, k: int) -> int:
    """Index just past the `)` matching the `(` at k (quote/escape-aware);
    len(command) when unbalanced."""
    depth = 0
    qstate = ""
    i = k
    n = len(command)
    while i < n:
        c = command[i]
        if qstate:
            if c == "\\" and qstate == '"':
                i += 2
                continue
            if c == qstate:
                qstate = ""
        elif c == "\\":
            i += 2
            continue
        elif c in "'\"":
            qstate = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _normalize_quoting(command: str) -> str:
    """Rewrite the bash quoting forms shlex does not know, QUOTE-AWARE: a
    `$'` or `$"` is shell syntax only OUTSIDE ordinary quotes — inside double
    quotes it is literal text (codex review r17, PR #136: a PR body
    mentioning `$'` must not trip the undecodable-quoting denial). An
    unterminated unquoted `$'` raises so the caller denies in-band.

    Redirections are removed from the stream entirely — bash removes them
    from the arg list, so a redirection left in place both hides verbs
    (`gh </dev/null pr merge`, `git 2>/dev/null push -f`, `git <<X push -f`
    — codex reviews r18/r19, PR #136) and, for heredocs, misreads body DATA
    as syntax (the PR-handoff pattern `--body-file - <<'EOF'` may discuss
    `$'` literally). After an unquoted heredoc operator, everything past the
    first unquoted newline is dropped; a command hidden after the heredoc
    body is the already-named compound-command residual (tokens[0]
    anchoring)."""
    out: list[str] = []
    state = ""  # "" outside, "'" in single, '"' in double
    heredoc_pending = False
    at_word_start = True
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if state == "":
            if heredoc_pending and c == "\n":
                break
            redir = _REDIR_WORD_RE.match(command, i) if at_word_start \
                else _REDIR_RE.match(command, i)
            if redir:
                op = redir.group("op")
                j = redir.end()
                if (op in ("<", ">") and not redir.group("fd")
                        and command[j:j + 1] == "("):
                    # process SUBSTITUTION, not a redirection (codex review
                    # r20, PR #136 — swallowing `<(echo)` shifted a later
                    # `--approve` into value position): bash passes it as an
                    # argument (/dev/fd/N), so emit an opaque placeholder
                    # token that is neither flag, verb, nor force refspec.
                    j = _balanced_paren_end(command, j)
                    out.append(" procsubst ")
                    i = j
                    at_word_start = True
                    continue
                if op in (">&", "<&") and command[j:j + 1].isdigit():
                    # fd duplication (2>&1): the target is attached digits
                    while j < n and command[j].isdigit():
                        j += 1
                else:
                    # the target/delimiter is the next word; swallow it —
                    # including a process-substitution target (`< <(cmd)` /
                    # `2>(cmd)`), which must go as one balanced group or its
                    # tail would leak into the token stream
                    while j < n and command[j] in " \t":
                        j += 1
                    if command[j:j + 2] in ("<(", ">("):
                        j = _balanced_paren_end(command, j + 1)
                    else:
                        wstate = ""
                        while j < n:
                            wc = command[j]
                            if wstate:
                                if wc == wstate:
                                    wstate = ""
                            elif wc in "'\"":
                                wstate = wc
                            elif wc == "\\":
                                j += 1
                            elif wc in " \t\n":
                                break
                            j += 1
                if op in ("<<", "<<-"):
                    heredoc_pending = True
                i = j
                at_word_start = True
                continue
            if c == "\\":
                out.append(command[i:i + 2])
                at_word_start = False
                i += 2
                continue
            if c == "$" and command[i + 1:i + 2] == "'":
                j = i + 2
                while j < n and command[j] != "'":
                    j += 2 if command[j] == "\\" else 1
                if j >= n:
                    raise ValueError("unterminated ANSI-C quoting")
                out.append(_decode_ansi_c(command[i + 2:j]))
                at_word_start = False
                i = j + 1
                continue
            if c == "$" and command[i + 1:i + 2] == '"':
                # locale quoting is plain double-quoting
                out.append('"')
                state = '"'
                at_word_start = False
                i += 2
                continue
            if c in ("'", '"'):
                state = c
        elif state == "'":
            if c == "'":
                state = ""
        else:  # in double quotes
            if c == "\\":
                out.append(command[i:i + 2])
                i += 2
                continue
            if c == '"':
                state = ""
        out.append(c)
        at_word_start = state == "" and c in " \t\n"
        i += 1
    return "".join(out)


def _tokenize(command: str) -> list[str]:
    """shlex so a quoted `--body "gh pr merge here"` collapses to ONE token (no
    bare `pr` inside prose) and `+refs/heads/x` stays one token. An unbalanced
    quote falls back to a naive split — fail toward evaluation, never toward a
    silent allow."""
    # bash removes backslash-newline before parsing, but shlex leaves the
    # newline glued to the next token (`gh \<NL>pr merge` -> "\npr" — codex
    # review r9, PR #136); match bash before tokenizing
    command = command.replace("\\\n", "")
    command = _normalize_quoting(command)
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _bundled_bool(token: str, target: str, value_takers: str) -> bool:
    """Whether a short-flag bundle carries boolean flag `target`.

    pflag/git parse bundles left-to-right: booleans may stack until the first
    value-taking flag, whose remaining chars (or `=…` suffix) are its VALUE,
    not flags (codex review, PR #136 — `-b=thanks` and `-ofoo` are values and
    must not be scanned for `a`/`f`). Scan stops at `=` or the first
    value-taker.
    """
    if len(token) < 2 or token[0] != "-" or token[1] == "-":
        return False
    for ch in token[1:]:
        if ch == "=" or ch in value_takers:
            return False if ch != target else True
        if ch == target:
            return True
    return False


def _skip_flags(tokens: list[str], value_takers: tuple[str, ...]) -> list[str]:
    """Drop leading flags AND the values of flags that take one.

    `-R o/r` / `--repo o/r` consume the following token; `--repo=o/r` and
    `-Ro/r` are self-contained (codex review, PR #136 — leaving a flag's
    VALUE in subcommand position let `gh -R o/r pr merge` bypass the deny).
    """
    rest = list(tokens)
    while rest and rest[0].startswith("-"):
        flag = rest[0]
        rest = rest[1:]
        bare = flag.split("=", 1)[0]
        if "=" not in flag and bare in value_takers and rest:
            # attached short values (`-Ro/r`) are already self-contained:
            # only a BARE value-taking flag consumes the next token.
            if bare.startswith("--") or len(flag) == 2:
                rest = rest[1:]
    return rest


_GH_VALUE_FLAGS = ("-R", "--repo")
_PUSH_VALUE_OPTIONS = ("--push-option", "--receive-pack", "--exec", "--repo",
                       "--recurse-submodules")


def _bundle_consumes_next(token: str, value_takers: str) -> bool:
    """Whether a short-flag bundle ends in a bare value-taking flag, making
    the NEXT token its value (codex review r15, PR #136 — `gh pr review -cb
    --approve` parses `--approve` as the BODY, and `git push -vo opt` parses
    `opt` as the push-option value). A value-taker mid-bundle takes the rest
    of the token (or its `=` suffix) as the value instead."""
    if len(token) < 2 or token[0] != "-" or token[1] == "-":
        return False
    for i, ch in enumerate(token[1:], start=1):
        if ch == "=":
            return False
        if ch in value_takers:
            return i == len(token) - 1
    return False


def _push_value_option(tok: str) -> bool:
    """True when tok is a bare value-taking push option whose VALUE is the
    next token — exact, or a git-style unambiguous long-option abbreviation
    (codex review r13, PR #136: `--recei` abbreviates `--receive-pack`). A
    prefix ambiguous within this list is ambiguous for git too (no other
    push option shares these stems), so git rejects the command itself and
    not consuming is safe. `=` forms are self-contained."""
    if tok == "-o":
        return True
    if not tok.startswith("--") or "=" in tok or len(tok) < 4:
        return False
    return sum(1 for o in _PUSH_VALUE_OPTIONS if o.startswith(tok)) == 1


_GIT_GLOBAL_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                           "--exec-path", "--config-env", "--attr-source")


def _denied_shape(command: str) -> str | None:
    """Name the denied shape this command matches, or None.

    Matching is by VERB POSITION, not token presence: the rules are anchored at
    `tokens[0]`, so free-text arguments (a PR body that says "a human will
    merge") never match. Compound commands are deliberately not split — a NAMED
    residual, consistent with the raises-the-cost-not-a-boundary posture.
    """
    try:
        tokens = _tokenize(command)
    except ValueError:
        # quoting we could not decode but bash may still parse (r16):
        # an in-band denial, never an uncaught exception — hook exit codes
        # other than 2 ALLOW the tool call
        return "undecodable shell quoting (fail toward deny)"
    if not tokens:
        return None

    if tokens[0] == "gh":
        # Flags (with their values) may sit between `gh` and `pr` AND between
        # `pr` and the verb (`gh pr -R o/r merge` — codex review, PR #136);
        # the verb is the first non-flag token after the `pr` subcommand.
        rest = _skip_flags(tokens[1:], _GH_VALUE_FLAGS)
        if rest[:1] == ["pr"]:
            rest = _skip_flags(rest[1:], _GH_VALUE_FLAGS)
            if rest:
                verb, args = rest[0], rest[1:]
                if verb in GH_PR_DENIED_VERBS:
                    return GH_PR_DENIED_VERBS[verb]
                if verb == "review":
                    i = 0
                    while i < len(args):
                        a = args[i]
                        # bare body/body-file consume their SEPARATED value
                        # (codex review r4, PR #136: `--body --approve` makes
                        # `--approve` the literal body text, not an approval)
                        if a in ("-b", "--body", "-F", "--body-file",
                                 "-R", "--repo"):
                            i += 2
                            continue
                        # a bundle whose LAST letter is a bare value-taker
                        # consumes the next token too (`-cb --approve` makes
                        # `--approve` the body — codex r15). Deny-letter scan
                        # below still runs first on the bundle itself via
                        # _bundled_bool ordering: check denial before skipping
                        if _bundled_bool(a, "a", "bFR"):
                            return "gh pr review --approve"
                        if _bundle_consumes_next(a, "bFR"):
                            i += 2
                            continue
                        if (
                            a == "--approve" or a == "-a"
                            # explicit boolean form: --approve=true; =false is
                            # over-denied deliberately (fail toward deny — a
                            # worker has no reason to write it; denial is soft)
                            or a.startswith("--approve=")
                            # bundled booleans, value-aware: `-am` denies,
                            # `-b=x`/`-ba` do not (b/F take values)
                            # R takes a value too: -Ro/a's `a` is part of
                            # the repo selector, not an approve (r6)
                            or _bundled_bool(a, "a", "bFR")
                        ):
                            return "gh pr review --approve"
                        i += 1
        return None

    if tokens[0] == "git":
        # Git accepts global options before the subcommand (`git -C . push`,
        # `git -c k=v push` — codex review, PR #136); locate `push` as the
        # first non-flag token with global values consumed. While consuming,
        # inspect -c / --config-env values: a `remote.<name>.push=+refspec`
        # config IS a force push smuggled through a global flag (codex
        # review r6 demonstrated it rewinding a remote), and a
        # --config-env indirection hides the value in an env var we cannot
        # read. Push-config denials only apply when the located subcommand
        # is actually `push` (codex review r9 — `git -c ... status` is
        # read-only); alias configs deny unconditionally, since an alias
        # renames a verb out from under the subcommand match entirely.
        push_cfg_denial = None
        gi = 1
        while gi < len(tokens) and tokens[gi].startswith("-"):
            flag = tokens[gi]
            source = None
            value = None
            if flag.startswith("-c") and not flag.startswith("--") and len(flag) > 2:
                # attached short form: -cremote.origin.push=+x
                source, value = "-c", flag[2:]
            elif flag == "-c" or flag.split("=", 1)[0] == "--config-env":
                source = flag.split("=", 1)[0]
                if "=" in flag and source == "--config-env":
                    value = flag.split("=", 1)[1]
                elif gi + 1 < len(tokens):
                    # bare form: the value is the next token (`-c k=v`)
                    value = tokens[gi + 1]
                    gi += 1
            elif flag in _GIT_GLOBAL_VALUE_FLAGS:
                # a preceding bare value-taking global (`git -C . -c ...`)
                # must not end the scan at its separated value (codex
                # review r12, PR #136)
                gi += 1
            if value is not None:
                key = value.split("=", 1)[0]
                val = value.split("=", 1)[1] if "=" in value else ""
                # config variable names are case-insensitive
                # (`remote.origin.Push` works — codex review r7, PR #136)
                key_cf = key.lower()
                if key_cf.startswith("alias."):
                    return f"git -c {key}=... (alias defined via config)"
                if key_cf.endswith(".push") and (
                    val.startswith("+") or source == "--config-env"
                ):
                    push_cfg_denial = f"git -c {key}=+... (force via push config)"
                # remote.<name>.mirror=true is `push --mirror` by config
                # (codex review r7 demonstrated it deleting remote refs);
                # deny for any value — no legitimate worker sets it, and a
                # --config-env value is unreadable here anyway
                if key_cf.startswith("remote.") and key_cf.endswith(".mirror"):
                    push_cfg_denial = f"git -c {key}=... (mirror via remote config)"
            gi += 1
        rest = _skip_flags(tokens[1:], _GIT_GLOBAL_VALUE_FLAGS)
        if push_cfg_denial and rest[:1] == ["push"]:
            return push_cfg_denial
        if rest[:1] == ["push"]:
            args = rest[1:]
            i = 0
            positionals = 0
            while i < len(args):
                tok = args[i]
                # a bare value-taking push option consumes the NEXT token as
                # its value (codex review, PR #136 — `git push -o +foo` is a
                # push option, not a force refspec; r11 — a separated
                # `--receive-pack git-receive-pack` value counted as the
                # repository operand shifts a `+remote` repo into refspec
                # position)
                if _push_value_option(tok):
                    i += 2
                    continue
                # any --force* prefix: covers --force, --force-with-lease
                # (incl. its `=<ref>:<expect>` form), --force-if-includes,
                # and git's UNAMBIGUOUS ABBREVIATIONS (`--force-w`,
                # `--force-with-l` — codex review r4 demonstrated git 2.43
                # accepts them and forces); shorter prefixes like `--forc`
                # are ambiguous across the three --force* options and git
                # rejects them, so no bypass exists below this prefix
                if tok.startswith("--force"):
                    return f"git push {tok}"
                # --mirror force-updates (and deletes) every ref; git accepts
                # unambiguous abbreviations down to `--m` (codex review r8,
                # PR #136 — no other push flag starts with `m`)
                if tok != "--" and len(tok) >= 3 and "--mirror".startswith(tok):
                    return f"git push {tok} (mirror)"
                # value-aware bundle scan: `-fu`/`-uf` deny, `-ofoo` is the
                # push-option VALUE and does not (o takes a value)
                if tok == "-f" or _bundled_bool(tok, "f", "o"):
                    return f"git push {tok} (force)"
                # a bundle ending in bare `o` consumes the next token as the
                # push-option value (`-vo opt` — codex r15)
                if _bundle_consumes_next(tok, "o"):
                    i += 2
                    continue
                if not tok.startswith("-"):
                    positionals += 1
                    # the FIRST positional is the <repository> operand
                    # (codex review r5, PR #136: `git push [<options>]
                    # [<repository> [<refspec>...]]` — a remote named
                    # `+remote` is not a refspec); only subsequent
                    # positionals are refspecs. A force refspec with no
                    # repository operand is not a thing git accepts, so
                    # this does not weaken the deny.
                    if positionals >= 2 and len(tok) > 1 and tok.startswith("+"):
                        return f"git push {tok} (force refspec)"
                i += 1
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed hook input: do not brick the session

    tool_input = payload.get("tool_input") or {}

    # Merge guard FIRST: it is workspace-INDEPENDENT and must not ride behind
    # the early return below, or a payload without CLAUDE_PROJECT_DIR/cwd would
    # silently allow `gh pr merge`.
    if payload.get("tool_name") == "Bash":
        shape = _denied_shape(tool_input.get("command") or "")
        if shape:
            sys.stderr.write(
                f"switchboard-guard: denied: {shape} {HANDOFF_HINT}"
            )
            return 2
        # else: fall through — the containment loop below is a no-op on Bash
        # (no file-path keys in its tool_input).

    workspace = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    if not workspace:
        return 0
    root = Path(workspace).resolve()

    for key in FILE_PATH_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        if target != root and root not in target.parents:
            sys.stderr.write(
                f"denied: {payload.get('tool_name')} target {target} is outside "
                f"the per-issue workspace {root}. Work only inside the workspace."
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
