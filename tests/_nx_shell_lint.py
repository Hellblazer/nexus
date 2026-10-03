# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared shell-scanning primitives for the two `nx`-invocation lints.

Both ``test_retired_command_callers_lint.py`` and
``test_nx_required_option_callers_lint.py`` need the same answer to "is this
text an invocation of `nx` (or something that forwards straight into `nx`) in
COMMAND POSITION" -- a mention inside a comment or mid-string is not a call,
naming is not invoking. Kept here once rather than duplicated so the two
lints' idea of "command position" cannot drift apart.

Code review (nexus-egei6 fix round) found the required-option lint's shell
regex, copied unchanged from the retired-command lint, blind to two REAL
shapes in this codebase's own e2e gate scripts:

  * A shell wrapper function ending in `nx` (`_nx()`, `_client_nx()`,
    `_nx_poisoned()`, `_provisioner_nx()`) that injects env/config around a
    real `nx` call and forwards every argument via ``"$@"``. Calling
    ``_nx store put ...`` invokes `nx store put ...` exactly as much as
    calling `nx store put ...` directly does -- this is a first-class idiom
    in the release-gate harnesses under ``tests/e2e/``, not a rare edge
    case, and it covers 3 of the 4 files the required-option lint's own
    motivating incident (nexus-0fw11) fixed.
  * `uv run nx ...` -- `run`'s own argument is `nx`, not the command word
    bash actually executes (`uv` is), so neither lint's original lead-in
    class (which looks for what comes immediately before "nx") recognised
    it at all.

:data:`NX_LEAD_IN` and :func:`discover_nx_wrapper_names` close both,
structurally: wrapper names are DISCOVERED by reading each shell function's
own body (does it invoke a real `nx`, or a previously-discovered wrapper,
forwarding all arguments?), never hand-listed by name -- a hand-kept list is
the same rot this whole lint family exists to catch one level up.
"""
from __future__ import annotations

import re
import shlex

#: What puts an `nx`-shaped word in "command position": start of a shell
#: statement, or immediately after one of the usual lead-ins (``;``, ``&``,
#: ``|``, ``(``, ``&&``, ``||``, ``$(``, or a keyword that introduces a new
#: command -- ``if``/``then``/``else``/``do``/``sudo``/``exec``/``time``).
#: The final alternative, ``uv run`` (optionally followed by ``--flag
#: [value]`` pairs, e.g. ``uv run --project x nx ...``), is the fix: `uv` is
#: the word bash actually executes, and `run`'s own argument list is where
#: `nx` arrives, so no punctuation or keyword ever precedes it directly.
#:
#: After any of those, zero or more PREFIX WORDS may sit between the lead-in
#: and `nx` without moving it out of command position: ``env``, an inline
#: assignment (``NX_LOCAL=1``, ``TOKEN="$t"``), or an expanded shell array
#: (``"${NXTOK[@]}"``). Substantive review (nexus-egei6) found the array form
#: live 20 times in a since-deleted rehearsal script
#: (``NXTOK=(env "NX_SERVICE_TOKEN=$tok")`` then ``"${NXTOK[@]}" nx tuple
#: ack ...``), invisible to both lints for exactly the ``--claimant`` /
#: ``--lease-s`` commands they guard; a bare ``NAME=value nx ...`` was
#: invisible the same way.
_PREFIX_WORD = (
    r"(?:env"
    r"|[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|[^\s;&|()]*)"
    r"|\"?\$\{[A-Za-z_]\w*\[[@*]\]\}\"?)"
)
NX_LEAD_IN = (
    r"(?:^|[;&|(]|&&|\|\||\$\(|\b(?:if|then|else|do|sudo|exec|time)\s+"
    r"|\buv\s+run\b(?:\s+--[\w=-]+(?:\s+\S+)?)*\s+)"
    r"(?:\s*" + _PREFIX_WORD + r"\s+)*"
)


def strip_shell_comment(line: str) -> str:
    """Blank out a trailing ``# ...`` comment (first-``#``-after-whitespace).

    Same narrow, best-effort rule both lints already used independently:
    does not understand quoting, so a literal ``#`` inside a quoted string
    can be wrongly treated as a comment start. Not observed to cause a false
    negative in this corpus; documented, not silently perfect.
    """
    return re.sub(r"(^|\s)#.*$", "", line)


def join_continuations(text: str) -> list[tuple[int, str]]:
    """``[(starting_lineno, logical_line), ...]``, joining backslash continuations.

    A line ending in a single, unescaped backslash is joined with the next
    physical line (its own leading whitespace stripped, a single space
    inserted). A line ending in an escaped backslash (``\\\\``, a literal
    backslash character, not a continuation) is left alone. Needed here
    because every real wrapper-function body in this repo spreads one
    logical `env VAR=... VAR=... "$BIN/nx" "$@"` command across many
    backslash-continued physical lines for readability -- without joining,
    "nx" and "$@" never appear on the same scanned line and wrapper
    detection sees nothing.
    """
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        start_lineno = i + 1
        buf = lines[i]
        while buf.endswith("\\") and not buf.endswith("\\\\") and i + 1 < n:
            i += 1
            buf = buf[:-1].rstrip() + " " + lines[i].lstrip()
        out.append((start_lineno, buf))
        i += 1
    return out


# Both bash spellings: ``name() {`` and ``function name [()] {``.
_FUNC_HEAD = r"^\s*(?:function\s+([A-Za-z_]\w*)\s*(?:\(\))?|([A-Za-z_]\w*)\s*\(\))\s*\{"
_FUNC_ONELINE_RE = re.compile(_FUNC_HEAD + r"(.*)\}\s*;?\s*$")
_FUNC_OPEN_RE = re.compile(_FUNC_HEAD + r"\s*$")
_FUNC_CLOSE_RE = re.compile(r"^\s*\}\s*$")


def _function_bodies(text: str) -> dict[str, str]:
    """``{function_name: body_text}`` for simple, non-nested `name() { ... }`
    shell functions -- either the one-liner form (``name() { body; }``) or
    the multi-line form (``name() {`` / body line(s) / a lone ``}``).

    Deliberately does not handle a body containing its own nested
    ``{ ... }`` block (an `if`/`case`/subshell inside the function): every
    real wrapper this module exists for is a single `env ...` or `uv run
    ...` statement, and a function whose body this scanner cannot bound
    correctly simply never matches :func:`discover_nx_wrapper_names`'s
    invoke-and-forward check, so it is silently not treated as a wrapper --
    the same fail-closed posture as an unresolved invocation elsewhere in
    these lints.
    """
    joined = join_continuations(text)
    bodies: dict[str, str] = {}
    i = 0
    n = len(joined)
    while i < n:
        line = strip_shell_comment(joined[i][1])
        m1 = _FUNC_ONELINE_RE.match(line)
        if m1:
            bodies[m1.group(1) or m1.group(2)] = m1.group(3)
            i += 1
            continue
        m2 = _FUNC_OPEN_RE.match(line)
        if m2:
            name = m2.group(1) or m2.group(2)
            j = i + 1
            parts: list[str] = []
            while j < n and not _FUNC_CLOSE_RE.match(strip_shell_comment(joined[j][1])):
                parts.append(joined[j][1])
                j += 1
            bodies[name] = "\n".join(parts)
            i = j + 1
            continue
        i += 1
    return bodies


def _forwards_all_args(tokens: list[str]) -> bool:
    return "$@" in tokens or "$*" in tokens


def _invokes_known_name(tokens: list[str], known: set[str]) -> bool:
    """True if *tokens* (a shlex-split, quote-stripped function body) calls
    a name in *known* -- directly, via a path ending in ``/<name>``
    (``"$BIN_DIR/nx"``, ``"$VENV/bin/nx"``), or via ``uv run [flags]
    <name>``.
    """
    for t in tokens:
        if any(c.isspace() for c in t):
            # shlex keeps a whole quoted string as one token, so
            # ``echo "binary is $DIR/nx"`` yields a token ending in ``/nx``.
            # A command word or path has no whitespace; prose does.
            continue
        for name in known:
            if t == name or t.endswith("/" + name):
                return True
    for idx, t in enumerate(tokens):
        if t != "uv" or idx + 1 >= len(tokens) or tokens[idx + 1] != "run":
            continue
        j = idx + 2
        while j < len(tokens):
            t2 = tokens[j]
            if t2.startswith("-"):
                j += 1 if "=" in t2 else 2  # bare flag consumes a separate value token
                continue
            if t2 in known:
                return True
            break  # first non-flag token after `run` is the real command; stop
    return False


def discover_nx_wrapper_names(text: str) -> set[str]:
    """Shell function names in *text* that act as `nx`.

    A name qualifies when its own body invokes a real `nx` (bare word, a
    path ending in ``/nx``, or via ``uv run nx``) -- or a PREVIOUSLY
    discovered wrapper, so one wrapper calling another is still caught
    (fixed point) -- while forwarding every argument (``"$@"``/``$@``/
    ``$*``). A function that merely mentions "nx" without forwarding args,
    or forwards args to something unrelated to nx, is not a wrapper.

    Returns the empty set for a file with no such function -- most files.
    """
    bodies = _function_bodies(text)
    known: set[str] = {"nx"}
    changed = True
    while changed:
        changed = False
        for name, body in bodies.items():
            if name in known:
                continue
            # A one-liner body (``name() { uv run nx "$@"; }``) carries a
            # trailing ``;`` glued directly onto the closing quote with no
            # separating space; left alone, shlex folds it into the token
            # (``$@;``), which then fails the exact ``$@``/``$*`` check
            # below. Statement structure inside the body is not otherwise
            # meaningful here, so ``;`` is simply treated as whitespace.
            flat = body.replace("\n", " ").replace(";", " ")
            try:
                tokens = shlex.split(flat, posix=True)
            except ValueError:
                continue  # unparseable body -- not treated as a wrapper (fail closed)
            if _invokes_known_name(tokens, known) and _forwards_all_args(tokens):
                known.add(name)
                changed = True
    return known - {"nx"}


def alias_invocation(invocation: str, alias: str) -> str:
    """Replace the leading ``nx`` word of *invocation* with *alias*.

    ``invocation`` is always ``"nx <rest>"`` (both lints' own convention);
    this just swaps the verb so the SAME "<rest>" is searched for after a
    wrapper name instead of the literal binary name.
    """
    return alias + invocation[len("nx"):]
