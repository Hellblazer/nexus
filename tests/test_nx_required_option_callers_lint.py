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
command tree (``nexus.cli.main``) and reading each command's
``click.Option.required`` flag, exactly the way
``test_release_artifact_verb_rot.py``'s ``_click_tree()`` resolves verbs
against the live tree rather than a hand-kept list. A hand-kept list is the
seven-enumerations problem: the next ``required=True`` lands and nobody adds
the row. A GROUP's own required options (none exist today) are inherited by
every one of its subcommands, since Click runs the group's callback -- and
therefore requires the group's own options -- before dispatching to any
subcommand at all.

SCOPE: required OPTIONS only (``click.Option``, not ``click.Argument`` —
required positionals are a different failure shape: an omitted positional is
a wrong-arity error Click itself raises immediately and loudly at every call
site, not a silently-accepted-then-later-refused flag). Every alias/secondary
name a required option carries counts as satisfying it (``-c`` and
``--collection`` are the same requirement). An option that is ``required=True``
but ALSO carries ``envvar=`` or ``prompt=`` is satisfiable without the flag at
all (Click reads the environment variable, or asks interactively) -- these are
excluded from the derived required set entirely rather than ever being
flagged as "missing" (none exist on any live required option today; see
``test_envvar_and_prompt_required_options_are_excluded_not_flagged`` for the
mechanism pinned against a synthetic command, since there is nothing live to
pin it against).

CALLER DISCOVERY is imported from the sibling, not duplicated: same
``CALLER_SCOPES`` / ``_caller_files()``, so a caller that would be swept for
a retired-command violation is swept here too.

CALLER RECOGNITION beyond the literal word ``nx`` is shared with the sibling
via ``tests/_nx_shell_lint.py`` (see that module's docstring for the full
account): a shell wrapper function that injects env/config around a real
``nx`` call and forwards every argument (``_nx()``, ``_client_nx()``, ...) is
DISCOVERED structurally (its body invokes ``nx``/a path ending in ``/nx``/
``uv run nx``, forwarding ``"$@"``) and calls to it are treated exactly like
calls to ``nx`` itself; ``uv run nx ...`` (with or without flags between
``run`` and the command) is recognized as a lead-in on its own. Both were
found missing in code review (nexus-egei6 fix round) -- the wrapper gap alone
made 3 of the 4 files the ``--collection`` incident itself fixed invisible to
this lint's first cut.

WHAT THIS SCANNER STILL CANNOT SEE, disclosed rather than silently mishandled:

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
  * The shell wrapper detector (``tests/_nx_shell_lint.py``) only bounds a
    SIMPLE, non-nested ``name() { ... }`` function body (one-liner or a
    multi-line block closed by a lone ``}``). A wrapper whose body contains
    its own nested ``if``/``case``/subshell block is invisible to it and
    silently NOT treated as a wrapper -- fail-closed, same posture as an
    unresolved invocation, but undetected rather than counted. Not observed
    in this corpus: every real wrapper here is one `env ...`/`uv run ...`
    statement.
  * The ``uv run [flags] <name>`` recognizer assumes each ``--flag`` between
    ``run`` and the real command consumes at most one following value token
    (``--project X``); an unusual multi-value flag shape between them could
    defeat it. No real caller in this corpus puts a flag there at all.
  * A required option passed in Click's glued short form (``-cvalue``) is not
    recognized as present. This fails LOUD, as a false violation, unlike the
    gaps above; no real caller uses the glued form.
"""
from __future__ import annotations

import ast
import functools
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

from tests._nx_shell_lint import (
    NX_LEAD_IN,
    alias_invocation,
    discover_nx_wrapper_names,
    join_continuations as _join_continuations,
    strip_shell_comment as _strip_shell_comment,
)
from tests.test_retired_command_callers_lint import CALLER_SCOPES, REPO_ROOT, _caller_files

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


def _required_options_of(cmd: click.Command) -> tuple[RequiredOption, ...]:
    """*cmd*'s own required options, excluding one satisfiable without the
    flag: ``envvar=`` (settable via the environment) or ``prompt=`` (Click
    asks interactively when the flag is omitted). Neither exists on any live
    required option today (dumped and confirmed against the full tree); a
    future one would otherwise be flagged as a false "missing" violation for
    every caller that legitimately relies on the environment variable or the
    prompt instead of the flag.
    """
    return tuple(
        RequiredOption(p.name, tuple(dict.fromkeys([*p.opts, *p.secondary_opts])))
        for p in cmd.params
        if isinstance(p, click.Option) and p.required and not p.envvar and not p.prompt
    )


def _walk_required(
    cmd: click.Command, path: list[str], inherited: tuple[RequiredOption, ...] = ()
) -> dict[str, tuple[RequiredOption, ...]]:
    """The recursive walk, factored out of :func:`_leaf_required_options` so
    it can be exercised directly against a SYNTHETIC command tree (see
    ``test_group_level_required_options_are_inherited_by_subcommands`` /
    ``test_envvar_and_prompt_required_options_are_excluded_not_flagged``),
    since neither shape exists in the live tree to pin a regression against.

    A GROUP's own required options are folded into ``inherited`` and passed
    down to every subcommand: Click runs a group's callback (and therefore
    requires ITS OWN required options) before dispatching to any subcommand,
    so ``nx <group> <leaf>`` must satisfy both the group's and the leaf's.
    """
    own = _required_options_of(cmd)
    combined = inherited + own
    found: dict[str, tuple[RequiredOption, ...]] = {}
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            found.update(_walk_required(sub, [*path, name], combined))
        return found
    if combined:
        found[" ".join(["nx", *path])] = combined
    return found


def _leaf_required_options() -> dict[str, tuple[RequiredOption, ...]]:
    """``{"nx <group...> <cmd>": (RequiredOption, ...)}``, walked live.

    Imported at call time (mirrors ``test_release_artifact_verb_rot.py``'s
    ``_click_tree()``), never memoized at collection time -- a stale import
    would be the same rot at one remove.
    """
    from nexus.cli import main  # noqa: PLC0415 -- call-time import, not collection-time

    return _walk_required(main, [])


# ── Shell scanning: command position, line continuations, quote-aware tail ─


def _shell_command_position_re(invocation: str) -> re.Pattern[str]:
    """"Command position" for *invocation* -- see :data:`tests._nx_shell_lint.NX_LEAD_IN`
    for the shared lead-in class (now including ``uv run``).

    The TRAILING boundary deliberately does NOT use a plain ``\\b``: several
    Click subcommand names in this tree are hyphenated prefixes of each
    other (``daemon service install`` vs. ``daemon service install-binary``),
    and ``\\b`` treats the boundary between a word character and a hyphen as
    a match -- ``r"install\\b"`` matches inside ``"install-binary"`` too,
    which would wrongly charge every ``install-binary`` caller with
    ``install``'s required ``--autostart`` (found empirically: 5 files, all
    actually calling the unrelated ``install-binary`` verb). ``(?![\\w-])``
    additionally excludes a following hyphen, which the sibling's
    retired-command set never needed to because none of ITS entries collide
    with a longer hyphenated sibling verb.
    """
    words = re.escape(invocation).replace(r"\ ", r"\s+")
    return re.compile(NX_LEAD_IN + r"\s*" + words + r"(?![\w-])")


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


def _shell_file_hits(
    text: str, required: dict[str, tuple[RequiredOption, ...]], *, file_label: str
) -> list[Hit]:
    """Every required-option invocation in *text*, scanning ONCE per file.

    Line continuations are joined and wrapper names discovered ONCE (not
    once per invocation, per nexus-egei6 fix-round's performance finding),
    then every invocation is searched for under EVERY alias -- ``nx`` itself
    plus any discovered wrapper name -- using the same command-position
    rule for each.
    """
    joined = _join_continuations(text)
    aliases = {"nx", *discover_nx_wrapper_names(text)}
    hits: list[Hit] = []
    for invocation in required:
        for alias in aliases:
            variant = invocation if alias == "nx" else alias_invocation(invocation, alias)
            pattern = _shell_command_position_re(variant)
            for lineno, raw_line in joined:
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


def _python_file_hits(
    text: str, required: dict[str, tuple[RequiredOption, ...]], *, file_label: str, path: Path
) -> list[Hit]:
    """Every required-option invocation in *text*, parsing the AST ONCE.

    The original per-(file, invocation) shape called ``ast.parse`` once for
    every one of the 26 required-option commands on every Python file --
    the measured cost of the whole suite's ~33-37s-per-call runtime
    (nexus-egei6 fix round). Parsing is the expensive step; walking an
    already-parsed tree per invocation is comparatively cheap.
    """
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    hits: list[Hit] = []
    for invocation in required:
        hits.extend(_python_argv_hits(tree, invocation, file_label=file_label))
        hits.extend(_python_string_hits(tree, invocation, file_label=file_label))
    return hits


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


@functools.cache
def _all_hits() -> tuple[Hit, ...]:
    """The whole scan, computed ONCE per process (nexus-egei6 fix round:
    three tests each called this independently, ~33-37s per call; the lint
    bucket must stay fast).
    """
    required = _leaf_required_options()
    active = {inv: opts for inv, opts in required.items() if inv not in ALLOWED_CALLERS}
    hits: list[Hit] = []
    for path in _caller_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".py":
            hits.extend(_python_file_hits(text, active, file_label=rel, path=path))
        else:
            hits.extend(_shell_file_hits(text, active, file_label=rel))
    return tuple(hits)


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
def test_group_level_required_options_are_inherited_by_subcommands() -> None:
    """No live group has its own required option today, so this is pinned
    against a SYNTHETIC tree -- the mechanism, not a live regression."""
    @click.group()
    @click.option("--tenant", required=True)
    def grp(tenant: str) -> None: ...

    @grp.command("leaf")
    def leaf_cmd() -> None: ...

    found = _walk_required(grp, ["grp"])
    assert "nx grp leaf" in found, found
    names = {o.name for o in found["nx grp leaf"]}
    assert "tenant" in names, names


@pytest.mark.lint
def test_envvar_and_prompt_required_options_are_excluded_not_flagged() -> None:
    """No live required option carries ``envvar=``/``prompt=`` today, so this
    is pinned against a SYNTHETIC command -- the mechanism, not a live
    regression. A required option satisfiable via the environment or an
    interactive prompt must never be counted as "missing" from a caller that
    legitimately relies on either instead of the flag."""
    @click.group()
    def grp2() -> None: ...

    @grp2.command("leaf")
    @click.option("--from-env", required=True, envvar="X_FROM_ENV")
    @click.option("--from-prompt", required=True, prompt=True)
    @click.option("--plain", required=True)
    def leaf2(from_env: str, from_prompt: str, plain: str) -> None: ...

    found = _walk_required(grp2, ["grp2"])
    names = {o.name for o in found.get("nx grp2 leaf", ())}
    assert names == {"plain"}, names


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
        # Regression pin (kill-control finding, a since-deleted rehearsal script): a
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
def test_uv_run_is_recognized_as_a_command_position_lead_in() -> None:
    """Undisclosed gap from code review (nexus-egei6 fix round): `uv run nx
    ...` puts `uv` where bash actually looks for a command, with `nx`
    arriving as `run`'s own argument -- neither lint's original lead-in
    class recognised this at all. 6 real in-scope files used this shape."""
    plain = 'uv run nx store put ./f.md --collection foo'
    assert _shell_command_position_re("nx store put").search(plain) is not None, plain

    with_env_prefix = 'NX_LOCAL=1 NEXUS_CONFIG_DIR="$X" uv run nx store put ./f.md --collection foo'
    assert _shell_command_position_re("nx store put").search(with_env_prefix) is not None, with_env_prefix

    with_uv_flag = 'uv run --project /repo nx store put ./f.md --collection foo'
    assert _shell_command_position_re("nx store put").search(with_uv_flag) is not None, with_uv_flag


@pytest.mark.lint
def test_prefix_words_before_nx_keep_it_in_command_position() -> None:
    """Substantive review (nexus-egei6): ``"${NXTOK[@]}" nx tuple ack ...``
    (20 live sites in a since-deleted rehearsal script) and a bare
    ``NAME=value nx ...`` were invisible, compliant or not. Each shape must
    be SEEN, and a missing required flag behind it must be a violation."""
    required = _leaf_required_options()
    assert any("--claimant" in o.aliases for o in required["nx tuple ack"]), required.get("nx tuple ack")
    shapes = [
        'if OUT=$("${NXTOK[@]}" nx tuple ack CLAIM {flag}); then :; fi\n',
        '${NXTOK[@]} nx tuple ack CLAIM {flag}\n',
        'NX_SERVICE_TOKEN="$tok" nx tuple ack CLAIM {flag}\n',
        'env NX_SERVICE_TOKEN=x nx tuple ack CLAIM {flag}\n',
    ]
    for shape in shapes:
        ok = _shell_file_hits(shape.replace("{flag}", "--claimant me"), required, file_label="s.sh")
        bad = _shell_file_hits(shape.replace("{flag}", ""), required, file_label="s.sh")
        assert [h.status for h in ok if h.invocation == "nx tuple ack"] == ["ok"], (shape, ok)
        assert [h.status for h in bad if h.invocation == "nx tuple ack"] == ["violation"], (shape, bad)

    # Naming is not invoking: a prefix word only counts in command position.
    mention = 'echo "run FOO=1 nx tuple ack by hand"\n'
    assert not _shell_file_hits(mention, required, file_label="s.sh"), mention


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


@pytest.mark.lint
def test_discover_nx_wrapper_names_recognizes_every_real_shape() -> None:
    """One regression case per wrapper shape found in the caller corpus
    (code review, nexus-egei6 fix round): the CRITICAL finding that made 3
    of the 4 files the lint's own motivating incident fixed invisible."""
    multiline_env_injection = (
        '_nx() {\n'
        '    env -i \\\n'
        '        HOME="$HOME_DIR" \\\n'
        '        "$BIN_DIR/nx" "$@"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(multiline_env_injection) == {"_nx"}, "fresh-install-mvv.sh shape"

    oneliner_uv_run = 'print_cli() { uv run nx "$@"; }\n'
    assert discover_nx_wrapper_names(oneliner_uv_run) == {"print_cli"}, "scripts/validate/03-cli.sh shape"

    uv_run_with_env_prefix = (
        '_provisioner_nx() {\n'
        '  NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx "$@"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(uv_run_with_env_prefix) == {"_provisioner_nx"}, (
        "a since-deleted gate's wrapper shape"
    )

    chained_wrapper = (
        '_nx() {\n'
        '    "$BIN_DIR/nx" "$@"\n'
        '}\n'
        '_nx_poisoned() {\n'
        '    NX_SERVICE_TOKEN=poison _nx "$@"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(chained_wrapper) == {"_nx", "_nx_poisoned"}, (
        "synthetic: a wrapper calling a previously-discovered wrapper (fixed point); "
        "a since-deleted gate's _nx_poisoned re-implemented the body instead"
    )

    keyword_form = 'function _kw_nx {\n    "$BIN_DIR/nx" "$@"\n}\n'
    assert discover_nx_wrapper_names(keyword_form) == {"_kw_nx"}, "bash `function name {` form"

    prose_path_plus_forward = (
        '_log_and_run() {\n'
        '    echo "binary is $DIR/nx" && "$1" "$@"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(prose_path_plus_forward) == set(), (
        "a quoted string ending in /nx is prose, not an invocation"
    )

    not_a_wrapper_no_forward = (
        '_describe_nx() {\n'
        '    echo "nx lives at $BIN_DIR/nx"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(not_a_wrapper_no_forward) == set(), (
        "mentions nx but never forwards args -- not a wrapper"
    )

    not_a_wrapper_unrelated_forward = (
        '_run_anything() {\n'
        '    "$1" "$@"\n'
        '}\n'
    )
    assert discover_nx_wrapper_names(not_a_wrapper_unrelated_forward) == set(), (
        "forwards args to something unrelated to nx -- not a wrapper"
    )


@pytest.mark.lint
def test_a_wrapper_call_site_missing_a_required_flag_is_a_violation() -> None:
    """Regression pin for the code-review CRITICAL finding (nexus-egei6 fix
    round), reproducing the exact shape of tests/e2e/fresh-install-mvv.sh:837
    against a synthetic copy. Manually kill-controlled against the REAL file
    too (removed --collection there, confirmed this exact test's underlying
    mechanism went red via test_every_caller_passes_every_required_click_option,
    then restored -- see the fix-round report)."""
    text = (
        '_nx() {\n'
        '    env -i \\\n'
        '        HOME="$HOME_DIR" \\\n'
        '        "$BIN_DIR/nx" "$@"\n'
        '}\n'
        'echo "$SENTINEL" | _nx store put - --title "probe"\n'
    )
    required = _leaf_required_options()
    hits = _shell_file_hits(text, required, file_label="synthetic.sh")
    violations = [h for h in hits if h.status == "violation" and h.invocation == "nx store put"]
    assert violations, hits


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
