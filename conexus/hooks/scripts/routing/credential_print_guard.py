#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 Phase 3 Step 1 (nexus-wauo1.22): deny a Bash command that would
print a Claude Code credential.

RDR-219 ("Harness Credentials Never Leave the Keychain") replaced the old
practice of copying the operator's interactive-login OAuth credential into
harness-owned files with a dedicated automation identity
(``nexus-automation-oauth-token``) that travels only in a child process's
environment, via ``tests/e2e/lib/claude_credentials.py``'s ``run --``/
``status`` modes. That helper is Gap 1 (copies on disk). This guard is
Gap 2 (nothing stops an agent printing the credential): a mistaken
redirect on 2026-09-25 put a live token into a subagent's own transcript,
which is exactly what a Bash-tool PreToolUse hook can see and refuse
before it happens.

DENY, with a message naming ``claude_credentials.py status`` and
``claude_credentials.py run --`` (RDR-219 Technical Design, "The plugin
guard"):

1. ``security find-generic-password`` or ``security dump-keychain -d``
   naming either credential item (:data:`CREDENTIAL_KEYCHAIN_ITEMS`).
2. Reading a ``.credentials.json`` file to output: ``cat``, ``less``,
   ``head``, ``tail``, ``jq``, or a ``python -c`` open-and-print shape.
3. Printing a protected credential environment variable
   (:data:`CREDENTIAL_ENV_VARS`): a shell expansion of it (``echo
   $CLAUDE_CODE_OAUTH_TOKEN``, ``printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN"``,
   redirected to a file or not -- the expansion itself is what is denied,
   regardless of where it goes), or a bare dump command that would print
   EVERY variable including it (``env``, ``printenv``, ``set`` invoked
   with nothing after them, or piped onward).

ALLOW: ``claude_credentials.py status`` and ``claude_credentials.py run
-- <command>`` -- these carry neither a keychain read, a
``.credentials.json`` reference, nor an expansion/bare-dump of a
protected variable, so they pass by construction, with no special-cased
carve-out. Also allowed, by the same construction: a bare variable NAME
with no ``$`` (``docker run -e CLAUDE_CODE_OAUTH_TOKEN`` -- docker reads
the value from ITS OWN environment, this guard sees only the name), and
the token-shaped grep RDR-219 itself uses to hunt leaked copies
(``grep -rlE 'sk-ant-o(a|r)t' <dir>``).

NO ESCAPE (RDR-219, unlike the git-write precedent this guard's shape
otherwise follows): this hook never calls
:func:`_lib.should_skip_for_reason`, so a ``# routing-allow: <reason>``
comment on an otherwise-denied command changes nothing. RDR-219's only
answer to a false positive is the message naming ``status`` and the
helper -- there is no escape token for a reviewer to reach for instead.

FAILURE BEHAVIOUR, stated and pinned (RDR-219, plan-audit residual 1):
``run_hook(fail_closed=...)`` in ``_lib.py`` is all-or-nothing -- one
flag decides every exception the SAME way, with no room for "deny only
when the raw text looks dangerous." So the marker-scoped split lives
INSIDE this hook's own ``body()``, not at the ``run_hook`` boundary:
``registry.yaml``'s ``fail_closed`` and this module's own ``run_hook``
call both say ``False`` (see :func:`test_routing_fail_closed_boundary
.TestTheDeliberatelyFailOpenRulesStayFailOpen`-style pinning). If the
matching logic in :func:`_matched_reason` itself raises, :func:`body`
denies when the RAW command text contains one of
:data:`_FAILSAFE_MARKERS` -- a plain substring test, done BEFORE any of
the regex matching above, never the regexes themselves -- and allows
otherwise. A bug in the matching logic can therefore never brick every
Bash call for every conexus user: at worst it degrades to the same
plain-substring test the FAILURE MODES section of RDR-219 names
explicitly.
"""
from __future__ import annotations

import os
import re
import sys

# RDR-215 nexus-q02nx.21: hooks.json now launches this script with a bare
# `python3`, so PATH decides the interpreter. Put back the resolution
# `_run_python_hook.sh` used to perform, before anything that needs 3.12
# or `nexus` is imported. See _interpreter.py for what is at stake. This
# script itself imports no `nexus` (RDR-219 Technical Design requires it
# stdlib-only), but `_lib`'s own dependency chain does not, so the same
# preamble the sibling routing hooks use keeps this one consistent with
# them rather than exempt from their reasoning.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _interpreter  # noqa: E402 -- must follow the sys.path insert

_interpreter.reexec_if_needed()

sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "credential_print_guard"

#: RDR-219: the credential environment-variable names this guard protects.
#: ONE constant so a pending amendment (nexus-wauo1.35, probably adding
#: ``NX_HARNESS_CLAUDE_OAUTH_TOKEN``) is a one-line change -- every
#: pattern below is built from this tuple, never a hardcoded name.
CREDENTIAL_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN",)

#: The two keychain items RDR-219 names: the retired interactive-login
#: item (nothing in this repo should read it again) and the harness's own
#: automation identity.
CREDENTIAL_KEYCHAIN_ITEMS: tuple[str, ...] = (
    "Claude Code-credentials",
    "nexus-automation-oauth-token",
)

CREDENTIAL_FILE_NAME = ".credentials.json"

#: FAILURE BEHAVIOUR markers (see module docstring): a plain substring
#: test over the raw command text, evaluated only if the matching logic
#: below raises. Deliberately the literal markers RDR-219's own Failure
#: Modes section names, not a re-derivation of the regexes above.
_FAILSAFE_MARKERS: tuple[str, ...] = (
    *CREDENTIAL_KEYCHAIN_ITEMS,
    CREDENTIAL_FILE_NAME,
    *CREDENTIAL_ENV_VARS,
)

_REDIRECT = (
    "This command would print a Claude Code credential. Harnesses never read "
    "the keychain or a credential file directly, and never print the "
    "automation token -- use `claude_credentials.py status` to check it, or "
    "`claude_credentials.py run -- <command>` to run a child process with it "
    "set in the environment (RDR-219)."
)

# ---------------------------------------------------------------------------
# Rule 1: a keychain read naming one of the two credential items.
# ---------------------------------------------------------------------------

_SECURITY_RE = re.compile(r"\bsecurity\b")
_FIND_GENERIC_RE = re.compile(r"\bfind-generic-password\b")
_DUMP_KEYCHAIN_RE = re.compile(r"\bdump-keychain\b")
#: A `-d` flag token (start-of-string or preceded by whitespace), not a
#: `-d` glued inside a longer flag or filename.
_DASH_D_FLAG_RE = re.compile(r"(?:^|\s)-d\b")


def _keychain_item_named(command: str) -> str | None:
    for item in CREDENTIAL_KEYCHAIN_ITEMS:
        if item in command:
            return item
    return None


def _matches_keychain_read(command: str) -> str | None:
    """A reason string when *command* reads a named credential item
    straight out of the keychain, or ``None``."""
    if not _SECURITY_RE.search(command):
        return None
    item = _keychain_item_named(command)
    if item is None:
        return None
    if _FIND_GENERIC_RE.search(command):
        return f"`security find-generic-password` naming {item!r}"
    if _DUMP_KEYCHAIN_RE.search(command) and _DASH_D_FLAG_RE.search(command):
        return f"`security dump-keychain -d` naming {item!r}"
    return None


# ---------------------------------------------------------------------------
# Rule 2: reading a `.credentials.json` file to output.
# ---------------------------------------------------------------------------

_FILE_READ_CMD_RE = re.compile(r"\b(cat|less|head|tail|jq)\b")
_PYTHON_DASH_C_RE = re.compile(r"\bpython3?\b[^\n]*\s-c\b")


def _matches_credential_file_read(command: str) -> str | None:
    """A reason string when *command* reads a ``.credentials.json`` file
    to output, or ``None``. Structure-agnostic, like the git-write
    precedent this guard's shape follows: both the read command and the
    file name need only appear somewhere in the text, not adjacent --
    the accepted cost is a false positive on an unrelated file merely
    ending in ``.credentials.json``, which is cheap next to missing a
    real one."""
    if CREDENTIAL_FILE_NAME not in command:
        return None
    read_match = _FILE_READ_CMD_RE.search(command)
    if read_match:
        return f"`{read_match.group(1)}` on a `{CREDENTIAL_FILE_NAME}` file"
    if _PYTHON_DASH_C_RE.search(command):
        return f"a `python -c` open-and-print of a `{CREDENTIAL_FILE_NAME}` file"
    return None


# ---------------------------------------------------------------------------
# Rule 3: printing a protected credential environment variable.
# ---------------------------------------------------------------------------


def _expansion_re(var: str) -> re.Pattern[str]:
    """``$VAR``/``${VAR}``/``${VAR:-default}`` -- any shell EXPANSION of
    *var*'s value. Requires the ``$`` sigil, which is exactly what
    distinguishes an expansion (denied -- this prints the value) from a
    bare variable NAME with no sigil (allowed -- e.g. `docker run -e
    CLAUDE_CODE_OAUTH_TOKEN`, which names the variable for docker to copy
    from ITS OWN environment, printing nothing)."""
    return re.compile(r"\$\{?" + re.escape(var) + r"\b")


_EXPANSION_RES: dict[str, re.Pattern[str]] = {
    var: _expansion_re(var) for var in CREDENTIAL_ENV_VARS
}

#: A command word invoked BARE -- nothing after it but whitespace then
#: end-of-segment (end of string, `;`, `&`, `|`, a newline, or a `#`
#: shell comment -- a real shell strips everything from `#` to the end
#: of the line, so `env # comment` is exactly as bare as `env` alone;
#: measured, not assumed: the NO-ESCAPE test that appends
#: `# routing-allow: ...` to every denied shape caught this once with
#: `#` left out of the terminator class). This is what distinguishes the
#: "dump everything" idiom (`env`, `set` with no arguments) from the
#: ordinary, harmless use of the same word with arguments (`env -i
#: FOO=bar cmd`, `set -euo pipefail` -- ubiquitous at the top of nearly
#: every harness script and must never be denied).
_BARE_TAIL = r"(?=\s*(?:$|[;&|#\n]))"
_ENV_BARE_RE = re.compile(r"\benv\b" + _BARE_TAIL, re.MULTILINE)
_SET_BARE_RE = re.compile(r"\bset\b" + _BARE_TAIL, re.MULTILINE)
#: `printenv` has no exec-a-command form the way `env` does -- unlike
#: `env`, ANY invocation of it only ever prints (either everything, bare,
#: or one named variable's value), so it is denied unconditionally
#: whenever the word appears, with no bare-tail qualifier.
_PRINTENV_RE = re.compile(r"\bprintenv\b")


def _matches_variable_print(command: str) -> str | None:
    """A reason string when *command* would print a protected credential
    variable's value, or ``None``."""
    for var, pattern in _EXPANSION_RES.items():
        if pattern.search(command):
            return f"a shell expansion of `${var}`"
    if _PRINTENV_RE.search(command):
        return "`printenv`"
    if _ENV_BARE_RE.search(command):
        return "a bare `env`"
    if _SET_BARE_RE.search(command):
        return "a bare `set`"
    return None


def _matched_reason(command: str) -> str | None:
    """The first denial reason for *command*, or ``None`` to allow."""
    return (
        _matches_keychain_read(command)
        or _matches_credential_file_read(command)
        or _matches_variable_print(command)
    )


def _contains_failsafe_marker(command: str) -> bool:
    """The FAILURE BEHAVIOUR check: a plain substring test, never a
    regex, so a bug in the matching logic above can never be what
    decides whether the failure path itself denies."""
    return any(marker in command for marker in _FAILSAFE_MARKERS)


def body(payload: dict) -> None:
    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()
        return

    # NO ESCAPE (RDR-219): `_lib.should_skip_for_reason` is never called.
    # A `# routing-allow: <reason>` comment on an otherwise-denied command
    # changes nothing here -- the only sanctioned redirect is
    # `claude_credentials.py status`/`run --`, named in the deny message.
    try:
        reason = _matched_reason(command)
    except Exception as exc:  # noqa: BLE001 -- FAILURE BEHAVIOUR, see module docstring
        if _contains_failsafe_marker(command):
            _lib.log_routing_event(
                rule=RULE_NAME, outcome="deny_fail_closed", tool_name="Bash",
                command_fragment=command,
            )
            _lib.deny(
                f"{_REDIRECT}\n\n(guard error while matching this command; "
                f"denied on the failure-mode substring check because a "
                f"protected marker is present: {exc})"
            )
            return
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="allow_fail_open", tool_name="Bash",
            command_fragment=command,
        )
        _lib.allow()
        return

    if reason:
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        _lib.deny(f"{_REDIRECT}\n\nMatched: {reason}.")
        return
    _lib.allow()


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
