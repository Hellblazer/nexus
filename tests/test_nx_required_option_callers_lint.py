# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nothing may INVOKE an `nx` command while omitting an option Click marks required.

Origin (Sam, 2026-09-26). 7.37.0 (nexus-0fw11) made ``--collection`` required
on ``nx store put``. Five release-time gate scripts kept calling it without
the flag; nothing caught them until the release PR, because the fact that
``--collection`` had become required lived only in ``commands/store.py``'s
decorator, and nothing connected that fact to the callers that pre-dated it.
Same mechanizable-connection shape as ``test_retired_command_callers_lint.py``
(the retired-command sibling this module is built beside): the repo already
holds the fact in checkable form (Click's own ``required=True``); what is
missing is a scan wiring caller to requirement.

THE REQUIRED-OPTION SET IS DERIVED, NEVER LISTED. Walking the LIVE Click
command tree (``nexus.cli.main``) and reading each leaf command's
``click.Option.required`` flag, exactly the way
``test_release_artifact_verb_rot.py``'s ``_click_tree()`` resolves verbs
against the live tree rather than a hand-kept list. A hand-kept list is the
seven-enumerations problem: the next ``required=True`` lands and nobody adds
the row.

SCOPE: required OPTIONS only (``click.Option``, not ``click.Argument`` —
required positionals are a different failure shape: an omitted positional is
a wrong-arity error Click itself raises immediately and loudly at every call
site, not a silently-accepted-then-later-refused flag). Every alias/secondary
name a required option carries counts as satisfying it (``-c`` and
``--collection`` are the same requirement).

CALLER DISCOVERY is imported from the sibling, not duplicated: same
``CALLER_SCOPES`` / ``CALLER_SUFFIXES`` / ``_caller_files()``, so a caller
that would be swept for a retired-command violation is swept here too.

WHAT THIS SCANNER CANNOT SEE, disclosed rather than silently mishandled:

  * A required flag added to an argv list by a LATER ``+=``/``.append`` in
    the same Python function is invisible to a single-AST-node list scan.
    Not observed in this corpus (grep found zero Python argv-list callers of
    any required-option command at authoring time); if one appears, add it
    to ``ALLOWED_CALLERS`` with a reason, exactly like the retired lint's own
    escape valve.
  * An invocation whose flag would arrive via an unexpanded shell variable
    (``$EXTRA_ARGS``), ``"$@"``, a Python ``*args`` splat, or an f-string
    substitution is NOT silently treated as compliant NOR flagged as a
    violation — it is UNRESOLVED, counted and reported separately (see
    ``MAX_UNRESOLVED``), because the parser genuinely cannot tell whether the
    missing flag is hiding inside the opaque piece.
  * Depth is whatever the live Click tree says: nested groups (``nx service
    token issue``) are walked to their true leaf, unlike the retired lint's
    shallower AST-decorator matching, which does not need to (this module
    resolves against LIVE ``click.Command`` objects, not decorators, so
    "how deep is a group" is never a design constraint here).
"""
from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

from tests.test_retired_command_callers_lint import (
    CALLER_SCOPES,  # noqa: F401 -- re-exported for anyone importing this module
    CALLER_SUFFIXES,  # noqa: F401
    REPO_ROOT,
    _caller_files,
)

#: Non-vacuity floor for the derivation (nexus-moht0): 26 required-option
#: leaf commands exist today; the floor sits comfortably below that so
#: un-requiring one option does not trip it, and well above zero so a
#: collapse (e.g. the walk silently stops descending into groups) does.
MIN_REQUIRED_OPTION_COMMANDS = 15

#: ``"<invocation>": "<why>"``. Empty by design, same discipline as the
#: sibling's ``ALLOWED_CALLERS`` -- a hit here means either a genuinely
#: un-fixable caller (document why) or the DETECTOR is wrong (fix the
#: detector, don't paper over it).
ALLOWED_CALLERS: dict[str, str] = {}

#: Ceiling for invocations the parser cannot verify reliably (shell variable
#: expansion, Python splats/f-strings standing in for the flag). Measured 0
#: at authoring time; a small floor above that absorbs an occasional
#: legitimate wrapper-script indirection without masking a real jump. A
#: count above this means the scanner just lost precision on something new
#: -- go look, don't just raise the ceiling.
MAX_UNRESOLVED = 5

#: Sentinel standing in for an f-string's interpolated part when
#: reconstructing a JoinedStr's literal text for tokenizing -- distinct
#: enough that it can never collide with real caller text.
_FSTRING_VAR_SENTINEL = "\x00NX_LINT_FSTRING_VAR\x00"

#: A bare unquoted ``$VAR``/``${VAR}``/``$(...)``/``$@``/``$*``, matched at
#: the START of the risky span (used by :func:`_risky_opaque_snippet`, which
#: only ever applies this outside quotes -- see that function for why
#: quoting changes the answer).
_UNQUOTED_EXPANSION_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$@|\$\*|\$\([^)]*\)?")


@dataclass(frozen=True)
class RequiredOption:
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Hit:
    file: str
    lineno: int
    invocation: str
    status: str  # "ok" | "violation" | "unresolved"
    detail: str


# ── Deriving the required-option set from the LIVE Click tree ──────────────


def _leaf_required_options() -> dict[str, tuple[RequiredOption, ...]]:
    """``{"nx <group...> <cmd>": (RequiredOption, ...)}``, walked live.

    Imported at call time (mirrors ``test_release_artifact_verb_rot.py``'s
    ``_click_tree()``), never memoized at collection time -- a stale import
    would be the same rot at one remove.
    """
    from nexus.cli import main  # noqa: PLC0415 -- call-time import, not collection-time

    found: dict[str, tuple[RequiredOption, ...]] = {}

    def walk(cmd: click.Command, path: list[str]) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, [*path, name])
            return
        required = tuple(
            RequiredOption(p.name, tuple(dict.fromkeys([*p.opts, *p.secondary_opts])))
            for p in cmd.params
            if isinstance(p, click.Option) and p.required
        )
        if required:
            found[" ".join(["nx", *path])] = required

    walk(main, [])
    return found


# ── Shell scanning: command position, line continuations, quote-aware tail ─

#: Same lead-in class as the sibling's ``_shell_invocations``, kept
#: identical on purpose so "is this in command position" means the same
#: thing in both lints. The TRAILING boundary deliberately does NOT reuse
#: the sibling's plain ``\b``: several Click subcommand names in this tree
#: are hyphenated prefixes of each other (``daemon service install`` vs.
#: ``daemon service install-binary``), and ``\b`` treats the boundary
#: between a word character and a hyphen as a match -- ``r"install\b"``
#: matches inside ``"install-binary"`` too, which would wrongly charge
#: every ``install-binary`` caller with ``install``'s required
#: ``--autostart`` (found empirically: 5 files, all actually calling the
#: unrelated ``install-binary`` verb). ``(?![\w-])`` additionally excludes a
#: following hyphen, which the sibling's retired-command set never needed to
#: because none of ITS entries collide with a longer hyphenated sibling verb.
def _shell_command_position_re(invocation: str) -> re.Pattern[str]:
    words = re.escape(invocation).replace(r"\ ", r"\s+")
    return re.compile(
        r"(?:^|[;&|(]|&&|\|\||\$\(|\b(?:if|then|else|do|sudo|exec|time)\s+)"
        r"\s*" + words + r"(?![\w-])"
    )


def _join_continuations(text: str) -> list[tuple[int, str]]:
    """``[(starting_lineno, logical_line), ...]``, joining backslash continuations.

    A line ending in a single, unescaped backslash is joined with the next
    physical line (its own leading whitespace stripped, a single space
    inserted). A line ending in an escaped backslash (``\\\\``, a literal
    backslash character, not a continuation) is left alone.
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


def _strip_shell_comment(line: str) -> str:
    return re.sub(r"(^|\s)#.*$", "", line)


def _statement_tail(line: str, start: int) -> str:
    """Quote-aware scan from *start* to the first unquoted statement end.

    Stops at an unquoted ``;``, ``|``, ``&`` (covers ``;``/``&&``/``||``/
    pipes/background) or ``)`` (closes an enclosing ``$(...)``/subshell) --
    or end of line. Never stops mid-quote.
    """
    in_squote = in_dquote = False
    i = start
    n = len(line)
    while i < n:
        c = line[i]
        if in_squote:
            if c == "'":
                in_squote = False
        elif in_dquote:
            if c == '"' and line[i - 1] != "\\":
                in_dquote = False
        else:
            if c == "'":
                in_squote = True
            elif c == '"':
                in_dquote = True
            elif c in ";|&)":
                break
        i += 1
    return line[start:i]


def _tokenize_shell(tail: str) -> list[str] | None:
    try:
        return shlex.split(tail, posix=True)
    except ValueError:
        return None  # unbalanced quote from truncation or a genuine typo


def _risky_opaque_snippet(tail: str) -> str | None:
    """The first substring in *tail* that could hide an extra shell word.

    Quoting changes the answer: a DOUBLE- or SINGLE-quoted ``"$VAR"``
    expands to exactly one shell word (bash performs no word-splitting
    inside quotes), so it can occupy at most the ONE argv slot it already
    sits in -- it cannot spell out a whole separate ``--flag value`` pair,
    so it is not treated as hiding a missing required option. An UNQUOTED
    ``$VAR``/``${VAR}``/``$(...)`` is subject to word-splitting and COULD
    expand into multiple words including a flag, so it is risky. ``"$@"`` is
    risky even quoted -- bash's own special case re-splits it into one word
    per positional parameter regardless of the surrounding quotes. The
    f-string sentinel (:data:`_FSTRING_VAR_SENTINEL`) is always risky: it
    marks a Python-level interpolation whose value this scanner never sees.
    """
    if _FSTRING_VAR_SENTINEL in tail:
        return _FSTRING_VAR_SENTINEL
    in_squote = False
    i = 0
    n = len(tail)
    while i < n:
        c = tail[i]
        if in_squote:
            if c == "'":
                in_squote = False
            i += 1
            continue
        if c == "'":
            in_squote = True
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and not (tail[j] == '"' and tail[j - 1] != "\\"):
                j += 1
            quoted = tail[i:min(j + 1, n)]
            if quoted == '"$@"':
                return quoted
            i = j + 1
            continue
        if c == "$":
            m = _UNQUOTED_EXPANSION_RE.match(tail, i)
            return m.group(0) if m else "$"
        i += 1
    return None


def _shell_hits(text: str, invocation: str, *, file_label: str) -> list[Hit]:
    pattern = _shell_command_position_re(invocation)
    hits: list[Hit] = []
    for lineno, raw_line in _join_continuations(text):
        line = _strip_shell_comment(raw_line)
        for m in pattern.finditer(line):
            tail = _statement_tail(line, m.end())
            tokens = _tokenize_shell(tail)
            if tokens is None:
                hits.append(
                    Hit(file_label, lineno, invocation, "unresolved",
                        f"shlex could not parse the tail: {tail!r}")
                )
                continue
            hits.append(_evaluate(file_label, lineno, invocation, tokens, tail))
    return hits


# ── Python scanning: argv lists and leading strings/f-strings ──────────────


def _string_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _joinedstr_text(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for v in node.values:
        s = _string_of(v)
        parts.append(s if s is not None else f" {_FSTRING_VAR_SENTINEL} ")
    return "".join(parts)


def _python_argv_hits(tree: ast.Module, invocation: str, *, file_label: str) -> list[Hit]:
    """``["nx", "store", "put", ...]``-style argv lists starting with *invocation*."""
    words = invocation.split()
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        elts = node.elts
        if len(elts) < len(words):
            continue
        literal = [_string_of(e) for e in elts[: len(words)]]
        if literal != words:
            continue
        tail_literal = [_string_of(e) for e in elts[len(words):]]
        hits.append(_evaluate_python(file_label, node.lineno, invocation, tail_literal))
    return hits


def _python_string_hits(tree: ast.Module, invocation: str, *, file_label: str) -> list[Hit]:
    """A standalone string (or f-string) constant that LEADS with *invocation*
    AND carries at least one more token after it.

    "Leads" means the WHOLE string value, stripped, starts with the
    invocation followed by a word boundary -- a multi-line docstring whose
    Examples section merely MENTIONS the invocation partway through does not
    qualify, because the check is anchored to the start of the node's own
    value, exactly mirroring the sibling's ``_python_invocations``.

    The "at least one more token" half is NOT in the sibling (which only
    needs to know a retired command is invoked at all, so a bare exact-match
    string is already damning there). Here a bare exact match is the
    ubiquitous ``PRODUCED_BY = "nx memory rollup"`` / ``actor="nx t3 gc"``
    shape -- a LABEL naming the command for an audit/attribution field, not
    an invocation of it. A real invocation of a required-option command
    always carries at least the required argument(s) after the verb, so
    "nothing follows" is the same "naming, not invoking" signal the sibling's
    command-position rule uses for shell prose. Only the first line of the
    tail is read: a docstring that happens to have more invocation-shaped
    text on later lines is not this node's own argument list.
    """
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = _joinedstr_text(node)
        else:
            continue
        stripped = text.strip()
        if not stripped.startswith(invocation):
            continue
        rest = stripped[len(invocation):]
        if rest and not rest[0].isspace():
            continue  # e.g. "nx store puts" -- not a word-boundary match
        tail_line = rest.splitlines()[0] if rest.strip() else ""
        if not tail_line.strip():
            continue  # bare label naming the command, not invoking it
        tokens = _tokenize_shell(tail_line)
        if tokens is None:
            hits.append(
                Hit(file_label, node.lineno, invocation, "unresolved",
                    f"shlex could not parse the tail: {tail_line!r}")
            )
            continue
        hits.append(_evaluate(file_label, node.lineno, invocation, tokens, tail_line))
    return hits


def _python_hits(text: str, invocation: str, *, file_label: str, path: Path) -> list[Hit]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    return (
        _python_argv_hits(tree, invocation, file_label=file_label)
        + _python_string_hits(tree, invocation, file_label=file_label)
    )


# ── Shared evaluation: is every required option present, missing, or opaque ─

_REQUIRED: dict[str, tuple[RequiredOption, ...]] | None = None


def _required_for(invocation: str) -> tuple[RequiredOption, ...]:
    global _REQUIRED
    if _REQUIRED is None:
        _REQUIRED = _leaf_required_options()
    return _REQUIRED[invocation]


def _missing_options(tokens: list[str], required: tuple[RequiredOption, ...]) -> list[str]:
    missing = []
    for opt in required:
        present = any(
            tok in opt.aliases or any(tok.startswith(a + "=") for a in opt.aliases)
            for tok in tokens
        )
        if not present:
            missing.append(opt.name)
    return missing


def _evaluate(file_label: str, lineno: int, invocation: str, tokens: list[str], raw_tail: str) -> Hit:
    required = _required_for(invocation)
    missing = _missing_options(tokens, required)
    if not missing:
        return Hit(file_label, lineno, invocation, "ok", "")
    risky = _risky_opaque_snippet(raw_tail)
    if risky:
        return Hit(
            file_label, lineno, invocation, "unresolved",
            f"variable/opaque expansion present ({risky!r}); cannot verify {missing}",
        )
    return Hit(file_label, lineno, invocation, "violation", f"missing {missing}")


def _evaluate_python(
    file_label: str, lineno: int, invocation: str, tail_literal: list[str | None]
) -> Hit:
    """Same verdict logic as :func:`_evaluate`, but over a partially-literal
    Python argv tail (``None`` marks a non-constant element -- a variable,
    a call, a starred unpack)."""
    required = _required_for(invocation)
    literal_tokens = [t for t in tail_literal if t is not None]
    missing = _missing_options(literal_tokens, required)
    if not missing:
        return Hit(file_label, lineno, invocation, "ok", "")
    if any(t is None for t in tail_literal):
        return Hit(
            file_label, lineno, invocation, "unresolved",
            f"argv contains a non-literal element; cannot verify {missing}",
        )
    return Hit(file_label, lineno, invocation, "violation", f"missing {missing}")


# ── Full scan ────────────────────────────────────────────────────────────


def _all_hits() -> list[Hit]:
    required = _leaf_required_options()
    hits: list[Hit] = []
    for path in _caller_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for invocation in required:
            if invocation in ALLOWED_CALLERS:
                continue
            if path.suffix == ".py":
                hits.extend(_python_hits(text, invocation, file_label=rel, path=path))
            else:
                hits.extend(_shell_hits(text, invocation, file_label=rel))
    return hits


# ── Non-vacuity ──────────────────────────────────────────────────────────


@pytest.mark.lint
def test_required_options_are_derivable_and_non_empty() -> None:
    required = _leaf_required_options()
    assert len(required) >= MIN_REQUIRED_OPTION_COMMANDS, (
        f"derived only {len(required)} required-option command(s) "
        f"({sorted(required)}), expected at least {MIN_REQUIRED_OPTION_COMMANDS}. "
        "Either the walk stopped descending into groups, or that many "
        "options were genuinely un-required -- lower the floor in that diff."
    )
    assert "nx store put" in required, sorted(required)
    aliases = {a for opt in required["nx store put"] for a in opt.aliases}
    assert "--collection" in aliases, aliases


@pytest.mark.lint
def test_the_scan_examines_something() -> None:
    files = _caller_files()
    assert len(files) >= 200, (
        f"only {len(files)} candidate caller file(s) under {CALLER_SCOPES}; "
        "the scopes are wrong or the repo moved."
    )


@pytest.mark.lint
def test_the_scan_finds_real_invocations_of_a_required_option_command() -> None:
    """Non-vacuity for the SCAN, not just the derivation: an empty result set
    would make every other test in this module pass by finding nothing."""
    hits = _all_hits()
    assert hits, "the scan found zero invocations of any required-option command"
    exercised = {h.invocation for h in hits}
    assert "nx store put" in exercised, (
        f"expected at least one real 'nx store put' invocation somewhere in "
        f"{CALLER_SCOPES}; found invocations of: {sorted(exercised)}"
    )


@pytest.mark.lint
def test_the_evaluator_tells_present_from_missing_from_opaque() -> None:
    """The three-way distinction the whole lint rests on, pinned directly."""
    required = (RequiredOption("collection", ("--collection", "-c")),)
    # patch the memo so _evaluate resolves against this fixture, not the live tree
    global _REQUIRED
    saved = _REQUIRED
    _REQUIRED = {"nx store put": required}
    try:
        ok = _evaluate(
            "x.sh", 1, "nx store put", ["./f.md", "--collection", "foo"],
            " ./f.md --collection foo",
        )
        assert ok.status == "ok"
        ok2 = _evaluate(
            "x.sh", 1, "nx store put", ["./f.md", "-c", "foo"], " ./f.md -c foo",
        )
        assert ok2.status == "ok"
        violation = _evaluate(
            "x.sh", 1, "nx store put", ["./f.md", "--ttl", "30d"], " ./f.md --ttl 30d",
        )
        assert violation.status == "violation"
        assert "collection" in violation.detail
        # Regression pin (kill-control finding, rehearse_shakeout.sh:252): a
        # QUOTED single-value variable occupies exactly one argv slot and
        # cannot spell out a whole separate flag+value pair -- a genuinely
        # missing flag next to one must still be reported as a VIOLATION,
        # never waved through as merely unresolved.
        quoted_var_violation = _evaluate(
            "x.sh", 1, "nx store put", ["$PROBE_MD", "--title", "x"],
            ' "$PROBE_MD" --title "x"',
        )
        assert quoted_var_violation.status == "violation", quoted_var_violation
        unresolved = _evaluate(
            "x.sh", 1, "nx store put", ["./f.md", "$EXTRA_ARGS"], " ./f.md $EXTRA_ARGS",
        )
        assert unresolved.status == "unresolved"
    finally:
        _REQUIRED = saved


@pytest.mark.lint
def test_the_shell_tail_scanner_respects_quotes_and_terminators() -> None:
    line = 'nx store put - --collection "a; b" | sed "s/x/y/"'
    m = _shell_command_position_re("nx store put").search(line)
    assert m is not None
    tail = _statement_tail(line, m.end())
    tokens = _tokenize_shell(tail)
    assert tokens == ["-", "--collection", "a; b"], tokens


@pytest.mark.lint
def test_the_continuation_joiner_merges_a_backslash_continued_line() -> None:
    text = "nx store put ./f.md \\\n  --collection foo\n"
    joined = _join_continuations(text)
    assert len(joined) == 1
    lineno, line = joined[0]
    assert lineno == 1
    assert "--collection" in line and "foo" in line


@pytest.mark.lint
def test_the_python_argv_scanner_finds_a_literal_and_an_opaque_list() -> None:
    tree = ast.parse(
        "run(['nx', 'store', 'put', path, '--collection', 'ok'])\n"
        "run(['nx', 'store', 'put', path, *extra])\n"
        "run(['nx', 'store', 'put', './f.md'])\n"
    )
    hits = _python_argv_hits(tree, "nx store put", file_label="x.py")
    assert [h.status for h in hits] == ["ok", "unresolved", "violation"], hits


# ── The gates themselves ────────────────────────────────────────────────


@pytest.mark.lint
def test_unresolved_invocations_stay_within_the_known_ceiling() -> None:
    """Unresolved is never silent: a rising count is reported here, not swallowed."""
    unresolved = [h for h in _all_hits() if h.status == "unresolved"]
    assert len(unresolved) <= MAX_UNRESOLVED, (
        f"{len(unresolved)} invocation(s) could not be statically verified "
        f"(ceiling {MAX_UNRESOLVED}):\n  "
        + "\n  ".join(f"{h.file}:{h.lineno}  {h.invocation!r} -- {h.detail}" for h in unresolved)
        + "\n\nEither teach the scanner to resolve the new shape, or raise "
        "MAX_UNRESOLVED with a reason if it is genuinely unresolvable."
    )


@pytest.mark.lint
def test_every_caller_passes_every_required_click_option() -> None:
    violations = [h for h in _all_hits() if h.status == "violation"]
    assert not violations, (
        "These invoke an `nx` command while omitting an option Click marks "
        "required, so the call will refuse at runtime:\n  "
        + "\n  ".join(f"{h.file}:{h.lineno}  {h.invocation!r} -- {h.detail}" for h in violations)
        + "\n\nAdd the missing option(s) to the caller, or, if the omission is "
        "provably safe (e.g. the caller cannot reach this code path), add an "
        "ALLOWED_CALLERS entry with a reason."
    )
