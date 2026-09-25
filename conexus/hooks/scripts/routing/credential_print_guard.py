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

**Environment-DUMP rules (bare ``env``/``env -0``/``set``/``printenv``,
and their pipe-filter carve-outs) were REMOVED after code review round 2**
(nexus-wauo1.22, T2 ``nexus/rdr219-p3-code-review-round2-findings``),
on the measured fact in T2 ``nexus_rdr/219-research-15``: Claude Code
deletes ``CLAUDE_CODE_OAUTH_TOKEN`` from its own environment before a
Bash-tool child ever starts, and under RDR-219's amendment ("The nx-mcp
dispatch grant") the harness's own automation-token env-var name
(``NX_HARNESS_CLAUDE_OAUTH_TOKEN``, not ``CLAUDE_CODE_OAUTH_TOKEN``)
reaches only an opted-in nx-mcp dispatch, never a plain Bash-tool call.
A ``env``/``set``/``printenv`` dump from Claude's own Bash tool therefore
CANNOT contain a protected value -- the rule protected nothing. It also
cost real false denials (a backtick-quoted ``env`` inside a single-quoted
heredoc, ``cat .env`` before command-position anchoring was narrowed) and
could not be made complete regardless: ``env 2>/dev/null``, ``eval env``,
``bash -c env``, and a brace-grouped ``{ env; }`` all defeated the
anchored bare-dump detector (round 2's own critical findings 1-2), so the
rule bought false confidence without buying safety. Deleted along with
it: the command-position anchoring (``_ENV_OR_SET_INVOKE_RE``) that
existed only to scope the bare-dump rule, and the pipe/filter logic
(``_pipe_target_prints_value``, ``_grep_pattern_matches_protected_name``)
that decided whether a dump's pipe target was a "safe" filter.

DENY, with a message naming ``claude_credentials.py status`` and
``claude_credentials.py run --`` (RDR-219 Technical Design, "The plugin
guard"):

1. ``security find-generic-password`` or ``security dump-keychain -d``
   naming either credential item (:data:`CREDENTIAL_KEYCHAIN_ITEMS`).
2. Reading a ``.credentials.json`` file to output: ``cat``, ``less``,
   ``head``, ``tail``, ``jq``, ``awk``, ``sed``, ``more``, ``od``,
   ``xxd``, ``strings``, ``base64``, or a ``python -c``/heredoc
   (``python3 - <<EOF``) open-and-print shape.
3. An EXPLICIT reference to a protected credential environment variable
   by NAME (:data:`CREDENTIAL_ENV_VARS`): a shell expansion of it (``echo
   $CLAUDE_CODE_OAUTH_TOKEN``, ``printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN"``,
   redirected to a file or not -- the expansion itself is what is denied,
   regardless of where it goes), or ``printenv`` naming a protected
   variable explicitly (``printenv CLAUDE_CODE_OAUTH_TOKEN``;
   ``printenv PATH`` naming only an unprotected variable stays allowed).
   These cost nothing to keep and catch a future case where the variable
   is genuinely present in the child's environment.
4. A ``python -c``/heredoc whose code names a protected variable via
   ``os.environ[...]``, ``os.getenv(...)``, or ``environ.get(...)``.
   Running a python SCRIPT BY PATH (``python3 some/script.py ...``, no
   ``-c`` and no heredoc) is unaffected.
5. Reading ANOTHER PROCESS's environment -- these are the one shape that
   really can leak the token, since the tmux server a harness's ``run --``
   started, or Claude's own exec-time environment, genuinely holds it:
   ``ps`` invoked with a flag that prints it (macOS ``-E``; BSD-style
   unclustered ``e``, e.g. ``ps auxeww``/``ps eww``); a read of
   ``/proc/*/environ`` via a common dump command (``cat``, ``strings``,
   ``tr``, and the rest of rule 2's reader set); or a bash builtin read of
   ``/proc/*/environ`` via input redirection (``< /proc/1234/environ``,
   ``$(< /proc/self/environ)``, with no reader command at all). Plain
   ``ps aux``, ``ps -ef``, and ``ps -p N -o args`` stay allowed -- only
   the FIRST option token after ``ps`` is inspected, matching the shapes
   the RDR names. Every pattern in this rule matches ANYWHERE in the
   command text, never anchored to command position, so ``eval``,
   ``bash -c '...'``, and ``sh -c '...'`` wrappers are covered by
   construction, not by a special case.

ALLOW: ``claude_credentials.py status`` and ``claude_credentials.py run
-- <command>`` -- these carry neither a keychain read, a
``.credentials.json`` reference, nor an explicit-name reference to a
protected variable, so they pass by construction, with no special-cased
carve-out. Also allowed, by the same construction: a bare variable NAME
with no ``$`` (``docker run -e CLAUDE_CODE_OAUTH_TOKEN`` -- docker reads
the value from ITS OWN environment, this guard sees only the name), the
token-shaped grep RDR-219 itself uses to hunt leaked copies
(``grep -rlE 'sk-ant-o(a|r)t' <dir>``), and, since the environment-dump
rules are gone, any bare ``env``/``set``/``printenv`` invocation
whatsoever -- ``env``, ``env 2>/dev/null``, ``eval env``, ``{ env; }``,
and a backtick-quoted ``env``/``set`` inside a single-quoted heredoc or
commit message all pass through untouched.

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

_FILE_READ_CMD_RE = re.compile(
    r"\b(cat|less|head|tail|jq|awk|sed|more|od|xxd|strings|base64)\b"
)
_PYTHON_DASH_C_RE = re.compile(r"\bpython3?\b[^\n]*\s-c\b")
#: ``python3 - <<EOF`` / ``python3 - <<'EOF'`` / ``python3 - <<-EOF`` --
#: any heredoc fed to a python invocation. Requires only "python3?" and a
#: "<<" on the SAME physical line (the command's first line); the heredoc
#: BODY that follows on subsequent lines is still part of the full,
#: multi-line ``command`` string these checks scan, never line-by-line.
_PYTHON_HEREDOC_RE = re.compile(r"\bpython3?\b[^\n]*<<-?\s*['\"]?\w+")


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
    if _PYTHON_HEREDOC_RE.search(command):
        return f"a python heredoc open-and-print of a `{CREDENTIAL_FILE_NAME}` file"
    return None


# ---------------------------------------------------------------------------
# Rule 3: an EXPLICIT reference to a protected credential environment
# variable by NAME. Environment-DUMP detection (bare `env`/`env -0`/
# `set`/`printenv`, command-position anchoring, and pipe-filter
# carve-outs) was REMOVED after code review round 2 -- see the module
# docstring for why.
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

#: `printenv` has no exec-a-command form the way `env` does -- unlike
#: `env`, ANY invocation of it only ever prints. Only a `printenv` naming
#: a protected variable explicitly is denied (`printenv PATH`/`printenv
#: HOME`, an UNPROTECTED variable named explicitly, stays allowed, and so
#: does a BARE `printenv` with no names at all -- the dump rule that used
#: to deny that shape is gone, see the module docstring). `printenv`'s
#: own argument list ends at a pipe, `;`, `&`, `#`, or newline. The match
#: is deliberately anywhere in the command text (not anchored to command
#: position), so `sh -c 'printenv CLAUDE_CODE_OAUTH_TOKEN'` is caught the
#: same as a bare invocation.
_PRINTENV_INVOKE_RE = re.compile(r"\bprintenv\b(?P<tail>[^;&#\n]*)", re.MULTILINE)


def _printenv_reason(command: str) -> str | None:
    match = _PRINTENV_INVOKE_RE.search(command)
    if not match:
        return None
    args_part = match.group("tail").split("|", 1)[0].strip()
    # Strip a trailing shell-quote/paren artifact left over when this
    # invocation sits inside `sh -c '...'`/`bash -c "..."`/`$(...)` and the
    # regex's tail capture runs up against the wrapper's own closing
    # delimiter (e.g. `sh -c 'printenv CLAUDE_CODE_OAUTH_TOKEN'` captures
    # a tail ending in `TOKEN'`) -- without this the exact `var in args`
    # check below misses a name that is genuinely present.
    args_part = args_part.rstrip("'\")")
    if not args_part:
        return None
    args = args_part.split()
    for var in CREDENTIAL_ENV_VARS:
        if var in args:
            return f"`printenv {var}`"
    return None


def _matches_variable_print(command: str) -> str | None:
    """A reason string when *command* explicitly names a protected
    credential variable via a shell expansion or `printenv <NAME>`, or
    ``None``."""
    for var, pattern in _EXPANSION_RES.items():
        if pattern.search(command):
            return f"a shell expansion of `${var}`"
    return _printenv_reason(command)


# ---------------------------------------------------------------------------
# Rule 4: a `python -c`/heredoc that names a protected variable via
# os.environ/os.getenv/environ.get.
# ---------------------------------------------------------------------------

_PYTHON_ENV_ACCESS_RES: dict[str, re.Pattern[str]] = {
    var: re.compile(
        r"\b(?:os\.)?(?:environ\s*\[\s*|environ\.get\(\s*|getenv\(\s*)"
        # An optional backslash before the quote: `python3 -c "...os.environ[\"NAME\"]..."`
        # is the ordinary way to nest a double-quoted literal in a double-quoted
        # -c argument (Phase 3 code review round 3).
        r"""\\?(['"])""" + re.escape(var)
    )
    for var in CREDENTIAL_ENV_VARS
}


def _matches_python_env_print(command: str) -> str | None:
    """A reason string when *command* is a `python -c`/heredoc invocation
    whose code names a protected variable via `os.environ`/`os.getenv`/
    `environ.get`, or ``None``. Running a python SCRIPT BY PATH (no `-c`,
    no heredoc -- `python3 tests/e2e/lib/claude_credentials.py run --
    ...`) is unaffected: neither detector this rule reuses (`-c`/heredoc)
    matches that shape."""
    invoked = _PYTHON_DASH_C_RE.search(command) or _PYTHON_HEREDOC_RE.search(command)
    if not invoked:
        return None
    for var, pattern in _PYTHON_ENV_ACCESS_RES.items():
        if pattern.search(command):
            return f"a `python -c`/heredoc reading `{var}` via os.environ/os.getenv"
    return None


# ---------------------------------------------------------------------------
# Rule 5: reading another process's environment -- `ps -E`/BSD `e`, a
# reader command on `/proc/*/environ`, or a bash builtin redirection read
# of it. These are the one shape that can genuinely leak the token (see
# the module docstring), and every pattern here matches ANYWHERE in the
# command text -- never anchored to command position -- so `eval`,
# `bash -c '...'`, and `sh -c '...'` wrappers are covered by construction.
# ---------------------------------------------------------------------------

#: Only the FIRST option-like token after `ps` is inspected -- BSD ps
#: syntax attaches the option cluster right after `ps`, and checking
#: every later token would false-positive on an ordinary `-o` format
#: keyword like `etime` (which contains "e" but names no flag). The
#: token excludes a quote/backtick/closing-paren so a wrapped invocation
#: (`eval "ps eww"`, `` `ps auxeww` ``) doesn't have the wrapper's own
#: closing delimiter glued onto the captured flag.
_PS_INVOKE_RE = re.compile(r"""\bps\b\s+(?P<flag>[^\s"'`)]+)""")
_PROC_ENVIRON_RE = re.compile(r"/proc/\S*/environ\b")
_PROC_ENVIRON_READER_RE = re.compile(
    r"\b(cat|strings|tr|xxd|od|more|less|head|tail|awk|sed)\b"
)
#: A bash builtin read via input redirection -- `< /proc/1234/environ`
#: or `$(< /proc/self/environ)` -- prints the same content as any of the
#: reader commands above but names no reader command at all.
_PROC_ENVIRON_REDIRECT_RE = re.compile(r"<\s*/proc/\S*/environ\b")


def _ps_flag_prints_environment(flag: str) -> bool:
    """macOS `-E` (case-sensitive -- `-e` alone is a different, harmless
    flag) or a BSD-style unclustered lowercase `e` (`aux`, `auxww` stay
    allowed; `auxeww`, `eww` are denied)."""
    if flag.startswith("-"):
        return "E" in flag[1:]
    if re.fullmatch(r"[a-z]+", flag):
        return "e" in flag
    return False


def _matches_process_environment_read(command: str) -> str | None:
    """A reason string when *command* reads another process's
    environment via `ps -E`/BSD `e`, a reader command on
    `/proc/*/environ`, or a bash builtin redirection read of it, or
    ``None``."""
    for match in _PS_INVOKE_RE.finditer(command):
        flag = match.group("flag")
        if _ps_flag_prints_environment(flag):
            return f"`ps` with an environment-printing flag ({flag!r})"
    if _PROC_ENVIRON_RE.search(command):
        reader = _PROC_ENVIRON_READER_RE.search(command)
        if reader:
            return f"`{reader.group(1)}` on `/proc/*/environ`"
        if _PROC_ENVIRON_REDIRECT_RE.search(command):
            return "a shell redirection read of `/proc/*/environ`"
    return None


def _matched_reason(command: str) -> str | None:
    """The first denial reason for *command*, or ``None`` to allow."""
    return (
        _matches_keychain_read(command)
        or _matches_credential_file_read(command)
        or _matches_variable_print(command)
        or _matches_python_env_print(command)
        or _matches_process_environment_read(command)
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
