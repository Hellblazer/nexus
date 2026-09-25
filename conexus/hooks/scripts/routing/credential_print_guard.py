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
   ``head``, ``tail``, ``jq``, ``awk``, ``sed``, ``more``, ``od``,
   ``xxd``, ``strings``, ``base64``, or a ``python -c``/heredoc
   (``python3 - <<EOF``) open-and-print shape.
3. Printing a protected credential environment variable
   (:data:`CREDENTIAL_ENV_VARS`): a shell expansion of it (``echo
   $CLAUDE_CODE_OAUTH_TOKEN``, ``printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN"``,
   redirected to a file or not -- the expansion itself is what is denied,
   regardless of where it goes); ``printenv`` invoked bare (no names, so
   it prints EVERY variable) or naming a protected variable explicitly
   (``printenv PATH`` naming only an unprotected variable stays allowed);
   or a bare ``env``/``set`` dump (``env``, ``env -0``, ``set`` invoked
   with nothing after them, redirected to a file, or piped into anything
   that is not a provably-safe filter -- ``grep -c``/``grep -q`` count or
   quiet modes, ``wc``, or a ``grep`` pattern that cannot match a
   protected name all stay allowed; a ``grep`` pattern that COULD match
   one, or any other pipe target, is denied).
4. A ``python -c``/heredoc whose code names a protected variable via
   ``os.environ[...]``, ``os.getenv(...)``, or ``environ.get(...)``.
   Running a python SCRIPT BY PATH (``python3 some/script.py ...``, no
   ``-c`` and no heredoc) is unaffected.
5. Reading another process's environment: ``ps`` invoked with a flag that
   prints it (macOS ``-E``; BSD-style unclustered ``e``, e.g. ``ps
   auxeww``/``ps eww``), or a read of ``/proc/*/environ`` via a common
   dump command (``cat``, ``strings``, ``tr``, and the rest of rule 2's
   reader set). Plain ``ps aux``, ``ps -ef``, and ``ps -p N -o args``
   stay allowed -- only the FIRST option token after ``ps`` is inspected,
   matching the shapes the RDR names.

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

#: `printenv` has no exec-a-command form the way `env` does -- unlike
#: `env`, ANY invocation of it only ever prints. Unlike the old
#: unconditional match, `printenv PATH`/`printenv HOME` (an UNPROTECTED
#: variable named explicitly) must stay allowed -- only a BARE invocation
#: (no names -- prints every variable) or one naming a protected variable
#: is denied. `printenv`'s own argument list ends at a pipe, `;`, `&`,
#: `#`, or newline; whatever it is piped INTO doesn't change what
#: `printenv` itself already wrote to that pipe, so (unlike `env`/`set`
#: below) no downstream-filter carve-out applies here.
_PRINTENV_INVOKE_RE = re.compile(r"\bprintenv\b(?P<tail>[^;&#\n]*)", re.MULTILINE)


def _printenv_reason(command: str) -> str | None:
    match = _PRINTENV_INVOKE_RE.search(command)
    if not match:
        return None
    args_part = match.group("tail").split("|", 1)[0].strip()
    if not args_part:
        return "a bare `printenv` (prints every variable)"
    args = args_part.split()
    for var in CREDENTIAL_ENV_VARS:
        if var in args:
            return f"`printenv {var}`"
    return None


#: `env`/`set` invoked with nothing after them but a segment terminator
#: (end of string, `;`, `&`, `#`, a newline) is the "dump everything"
#: idiom and always denied. A real shell strips everything from `#` to
#: end of line, so `env # comment` is exactly as bare as `env` alone.
#: Ordinary, harmless use with real arguments (`env -i FOO=bar cmd`,
#: `set -euo pipefail`, `set -x` -- ubiquitous at the top of nearly every
#: harness script) must never be denied. `|` is handled separately below
#: (a pipe target can be a provably-safe filter), unlike the old bare-tail
#: regex that treated ANY pipe as bare.
#: `env`/`set` must be in COMMAND position -- the start of the command, or
#: right after `;`, `&`, `|`, `(`, a newline, `$(` or a backtick, with
#: optional whitespace and `VAR=value` prefixes allowed -- so `cat .env`,
#: `source .env` and `tmux set-environment` are not read as a dump, while
#: `$(env)` and a backticked env are. The tail stops at a segment terminator,
#: a closing `)` or a backtick, so a substitution's own close does not read
#: as an argument.
_ENV_OR_SET_INVOKE_RE = re.compile(
    r"(?:^|[;&|(\n`]|\$\()\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(env|set)(?![\w.-])"
    r"(?P<tail>[^;&#\n)`]*)",
    re.MULTILINE,
)
#: A `grep`/`egrep`/`fgrep` invocation in COUNT (`-c`/`--count`) or QUIET
#: (`-q`/`--quiet`/`--silent`) mode never prints a matched line's value,
#: so it is safe regardless of pattern -- `env | grep -c NAME`, `env |
#: grep -q NAME` stay allowed.
_GREP_CMD_RE = re.compile(r"\b(?:e|f)?grep\b")
_GREP_COUNT_OR_QUIET_RE = re.compile(
    r"(?:^|\s)-\w*[cq]\w*\b|--count\b|--quiet\b|--silent\b"
)
_WC_CMD_RE = re.compile(r"\bwc\b")


def _grep_pattern_matches_protected_name(grep_segment: str) -> str | None:
    """Best-effort: does this `grep` invocation's PATTERN look like it
    would match one of the protected env-var names? Returns the matched
    var, or ``None``. Structure-agnostic like the git-write precedent:
    takes the first non-flag token after `grep` as the pattern and checks
    it, case-insensitively, as a substring of each protected name --
    `env | grep OAUTH`/`env | grep TOKEN` match (both are substrings of
    `CLAUDE_CODE_OAUTH_TOKEN`); `env | grep FOO` does not."""
    tokens = grep_segment.split()
    patterns: list[str] = []
    positional_taken = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-e", "--regexp") and i + 1 < len(tokens):
            patterns.append(tokens[i + 1])
            i += 2
            continue
        if tok.startswith("--regexp="):
            patterns.append(tok.split("=", 1)[1])
        elif tok.startswith("-e") and len(tok) > 2:
            patterns.append(tok[2:])
        elif not tok.startswith("-") and not positional_taken and not patterns:
            patterns.append(tok)
            positional_taken = True
        i += 1
    for raw in patterns:
        pattern_lower = raw.strip("'\"").lower()
        if not pattern_lower:
            continue
        for var in CREDENTIAL_ENV_VARS:
            if pattern_lower in var.lower():
                return var
    return None


def _pipe_target_prints_value(segment: str) -> str | None:
    """*segment* is the text right after a `|` following a bare
    `env`/`set`. Returns a reason string if this pipe stage could still
    print a protected value, or ``None`` if it is a recognized-safe
    filter (`grep -c`/`-q`, a `grep` pattern that cannot match a
    protected name, or `wc`). Any OTHER pipe target is denied --
    conservative by design, since this guard cannot prove it safe."""
    stage = segment.split("|", 1)[0]
    if _GREP_CMD_RE.search(stage):
        if _GREP_COUNT_OR_QUIET_RE.search(stage):
            return None
        matched_var = _grep_pattern_matches_protected_name(stage)
        if matched_var:
            return f"piped into `grep` with a pattern that could match `{matched_var}`"
        return None
    if _WC_CMD_RE.search(stage):
        return None
    return "piped into a filter that is not provably safe"


def _env_or_set_reason(command: str) -> str | None:
    for match in _ENV_OR_SET_INVOKE_RE.finditer(command):
        word = match.group(1)
        core = match.group("tail").strip()
        if word == "env":
            # `env -0` changes the output separator, not whether every
            # variable is dumped -- still a bare dump when nothing else
            # follows it.
            core = re.sub(r"^-0\s*", "", core)
        if not core:
            return f"a bare `{word}` (prints every variable)"
        if core.startswith("|"):
            pipe_reason = _pipe_target_prints_value(core[1:])
            if pipe_reason:
                return f"a bare `{word}` {pipe_reason}"
            continue
        if core.startswith(">"):
            return f"a bare `{word}` redirected to a file"
        # Real arguments follow (`env -i FOO=bar cmd`, `set -euo
        # pipefail`) -- execs a command or sets shell options, not a dump.
    return None


def _matches_variable_print(command: str) -> str | None:
    """A reason string when *command* would print a protected credential
    variable's value, or ``None``."""
    for var, pattern in _EXPANSION_RES.items():
        if pattern.search(command):
            return f"a shell expansion of `${var}`"
    reason = _printenv_reason(command)
    if reason:
        return reason
    return _env_or_set_reason(command)


# ---------------------------------------------------------------------------
# Rule 4: a `python -c`/heredoc that names a protected variable via
# os.environ/os.getenv/environ.get.
# ---------------------------------------------------------------------------

_PYTHON_ENV_ACCESS_RES: dict[str, re.Pattern[str]] = {
    var: re.compile(
        r"\b(?:os\.)?(?:environ\s*\[\s*|environ\.get\(\s*|getenv\(\s*)"
        r"""(['"])""" + re.escape(var)
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
# Rule 5: reading another process's environment -- `ps -E`/BSD `e`, or
# `/proc/*/environ`.
# ---------------------------------------------------------------------------

#: Only the FIRST option-like token after `ps` is inspected -- BSD ps
#: syntax attaches the option cluster right after `ps`, and checking
#: every later token would false-positive on an ordinary `-o` format
#: keyword like `etime` (which contains "e" but names no flag).
_PS_INVOKE_RE = re.compile(r"\bps\b\s+(?P<flag>\S+)")
_PROC_ENVIRON_RE = re.compile(r"/proc/\S*/environ\b")
_PROC_ENVIRON_READER_RE = re.compile(
    r"\b(cat|strings|tr|xxd|od|more|less|head|tail|awk|sed)\b"
)


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
    environment via `ps -E`/BSD `e`, or a read of `/proc/*/environ`, or
    ``None``."""
    for match in _PS_INVOKE_RE.finditer(command):
        flag = match.group("flag")
        if _ps_flag_prints_environment(flag):
            return f"`ps` with an environment-printing flag ({flag!r})"
    if _PROC_ENVIRON_RE.search(command) and _PROC_ENVIRON_READER_RE.search(command):
        reader = _PROC_ENVIRON_READER_RE.search(command)
        return f"`{reader.group(1)}` on `/proc/*/environ`"
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
