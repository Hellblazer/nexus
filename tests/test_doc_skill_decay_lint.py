# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documentation and skill decay: four mechanical checks no existing lint runs.

Four real defects found in one session (2026-09-19) motivated this module,
and none of them was catchable by anything already in the lint bucket:

1. ``.claude/skills/release/SKILL.md`` had a fenced ``bash`` block whose last
   ``git add`` path ended in a trailing backslash, silently absorbing the
   following ``git commit -m "..."`` line as extra pathspec arguments -- the
   block staged files and never committed, in the skill that IS the
   executable checklist for cutting a release. **This incident is CONFIRMED
   STILL LIVE in this worktree's copy of that file** (line 375; found while
   building this module, not merely cited from the dispatching task) -- and
   it is syntactically VALID bash: a trailing backslash at end-of-input is
   accepted cleanly by ``bash -n``, and the joined
   ``git add ... git commit -m "..."`` is one long, syntactically fine
   ``git add`` invocation. ``bash -n`` alone (check (a) below) is
   structurally incapable of catching this class no matter how it is
   tuned. Check (a2), added specifically because of this finding, is a
   separate, narrower structural check for exactly this shape (a
   line-continuation immediately followed by what looks like an
   independent CLI command) -- it DOES catch it, and the one confirmed
   occurrence is named in ``ABSORPTION_ALLOWLIST`` rather than fixed
   (the dispatching task forbids editing that file). Check (a) itself
   still earns its place: it catches the large, real class of GENUINE
   fenced-shell syntax errors (unterminated quotes/heredocs, unmatched
   brackets, a dangling ``if``/``do``/``case``) that nothing else in this
   repo runs a parser over -- it simply would not, on its own, have caught
   THIS specific incident. See
   ``test_release_skills_are_in_scope_and_only_the_known_absorption_bug_hits``.
2. ``conexus/skills/rdr-create/SKILL.md`` told sessions to hand-scan
   ``$RDR_DIR/`` for files matching ``[0-9][0-9][0-9]-*.md``. Every one of
   nexus's 217 RDRs is named ``rdr-NNN-*.md``, so the glob matched zero
   files in the directory it was meant to describe, and the step fell
   through to its own "start at 001" clause. This incident is ALSO already
   fixed on the live tree (the file now narrates it as a cautionary
   example in prose, per its own lines 68-76) -- check (c) below is built
   and mutation-tested against a planted equivalent, not against this now-
   historical text, which coincidentally passes anyway because a *different*
   subdirectory (``docs/rdr/post-mortem/``) uses exactly the
   ``NNN-title.md`` shape the glob matches. That collision is a real,
   disclosed limitation of check (c) -- see its docstring below.
3. A skill referenced ``tests/e2e/lib/expectations.sh``, which had been
   deleted. This is exactly check (b)'s target.
4. ``.claude/skills/engine-release/SKILL.md`` widened "the human pushes the
   tag" into "human by default ... OR the AI pushes it when authorized",
   contradicting both this repo's ``AGENTS.md`` and the user-level
   ``CLAUDE.md``. This is a prose-drift class no mechanical AST/regex scan
   can catch (it requires reading and comparing MEANING, not shape) and is
   explicitly OUT OF SCOPE for this module. Named here so a reader does not
   mistake this module's silence on it for an oversight: it is a semantic
   drift, and ``tests/test_engine_release_skill_parity.py`` already proved
   its domain does not reach that specific rule (both wordings passed it) --
   a properly-scoped structural fix for THAT gap is a parity-test change,
   not something a filesystem-shape lint like this one can generalize into.

Filesystem scan, modelled on ``tests/test_skip_bound_lint.py`` and
``tests/test_storage_boundary_lint.py`` -- walks the tree on disk with
``pathlib.Path.rglob``, never ``request.session.items`` (see
``tests/AGENTS.md`` "The lint bucket is only safe for FILESYSTEM-scanned
censuses" for why a ``session.items``-based census would be blind under
``-m lint`` or ``--splits``).

Scope (``_target_files()``): every ``*.md`` under ``conexus/skills/``,
``conexus/agents/``, ``.claude/skills/``, plus the repo-root ``AGENTS.md``.
``CLAUDE.md`` is a symlink to ``AGENTS.md`` at the repo root -- this module
never globs the repo root (only ``AGENTS.md`` is referenced explicitly) so
the symlink is never independently discovered, and ``_target_files()``
additionally dedupes by resolved path as a structural guard against a
future glob doing so accidentally (``test_target_files_dedupes_claude_md_symlink``
pins both facts).

Three independent checks, each with its own scan function so a canary file
can be injected via ``extra_files`` for a mutation test without touching
the real tree (mirrors ``tests/test_skip_bound_lint.py``'s
``scan_tests_dir(extra_files=[canary])``):

(a) ``scan_fenced_shell_blocks`` -- every fenced ```bash/```sh/```shell
    block is syntax-checked with ``bash -n`` (never executed). Angle-bracket
    placeholders (``<base-branch>``) are this repo's standing documentation
    convention and are read literally by a real shell as an unterminated
    redirection, so they are normalized to a bash-safe dummy token before
    the syntax check -- this is NOT a per-block skip, it is a text
    transform, so a genuine syntax error elsewhere in the same block is
    still caught (see ``test_placeholder_normalization_does_not_hide_a_real_syntax_error``).
    A block is additionally, EXPLICITLY skippable (never inferred from
    shape) by a leading ``...`` line or a bare ``# fragment`` comment
    line -- an opt-in the author reaches for, not a silent heuristic.

(a2) ``scan_trailing_backslash_absorption`` -- a fenced line ending in a
    lone trailing backslash (continuation), where the very next line
    starts with a recognizable CLI verb (``git``, ``nx``, ``gh``, ...)
    rather than a bare argument, is flagged as a likely accidental
    absorption of what the author meant to be an independent command --
    exactly incident 1 above, which check (a)'s syntax check cannot see.
    A continuation ending in ``|``/``&&``/``||``/``;``/``&`` before the
    backslash is intentional chaining and is never flagged (tuned against
    a real false positive in the live corpus:
    ``curl ... |`` (with a line-continuation) onto ``python3 -c "..."``).

(b) ``scan_referenced_paths`` -- inline single-backtick tokens that look
    like a literal, concrete repo path (see ``_looks_like_repo_path_token``
    for the exact, narrow rule) are asserted to exist. Deliberately does
    NOT attempt the "or has a known extension" branch a fully general
    version of this check might use: skill markdown under
    ``conexus/skills/*/SKILL.md`` commonly references paths relative to
    the PLUGIN root (resolved via ``$CLAUDE_PLUGIN_ROOT`` at runtime, e.g.
    ``resources/rdr/REGISTER.md`` naming
    ``conexus/resources/rdr/REGISTER.md``), and validating those needs a
    per-skill-relative-root resolution this check does not attempt -- a
    known, disclosed gap, not a silent one. Tokens containing a shell
    variable (``$``), a home-relative prefix (``~``), a brace/angle
    placeholder, a glob character, or a known template marker (``NNN``,
    ``X.Y.Z``, ``vX.Y.Z``) are out of scope for the same reason: none of
    them names a literal path this check could resolve. ``service/target/``
    is additionally excluded -- it is git-ignored build output (confirmed:
    ``git check-ignore service/target`` matches ``.gitignore:54``), so its
    presence or absence says nothing about DOCUMENTATION rot.

(c) ``scan_globs`` -- inline single-backtick tokens shaped like a glob
    (contains ``*``, or the specific ``[0-9]`` digit-class shape the
    rdr-create incident used) are asserted to match at least one real file
    "somewhere sensible in the repo" (this check's own wording, matched
    literally): a token with a ``/`` is resolved with ``Path.glob`` rooted
    at the repo (so it must start with a known top-level dir, same set as
    check (b)); a bare filename-shaped glob is resolved with
    ``Path.rglob`` against the WHOLE tree. That whole-tree fallback is
    deliberately generous and has a real, disclosed limitation: a glob
    that is syntactically fine and matches SOMEWHERE in the repo but not
    in the specific directory the surrounding prose actually means (the
    exact rdr-create shape: ``[0-9][0-9][0-9]-*.md`` matches nothing directly
    under ``docs/rdr/`` but DOES match 70 files under
    ``docs/rdr/post-mortem/``, which uses a different, undecorated
    ``NNN-title.md`` naming convention) is invisible to it. This module
    does not attempt to parse "the directory named in the surrounding
    sentence" out of prose -- that is real NLP, not a mechanical shape
    check -- so this limitation is disclosed rather than silently
    papered over with a fragile heuristic.

Do NOT duplicate
=================

This module does not re-implement or shadow:

* ``tests/test_release_artifact_verb_rot.py`` -- that module resolves
  ``nx``/``nx-hook`` VERB tokens against the live Click tree / hook verb
  table; this module never inspects a verb, only shell syntax, path
  existence, and glob-match shape.
* ``tests/test_engine_release_skill_parity.py`` -- flag-level parity
  between a skill and ``run.sh``'s own arg-parse loop; disjoint subject.
* ``tests/test_plan_template_inline_var_lint.py`` and friends -- template
  variable well-formedness inside a DIFFERENT surface (plan templates),
  not skill/agent markdown.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Shared scope
# ---------------------------------------------------------------------------

_SKILL_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "conexus" / "skills",
    REPO_ROOT / "conexus" / "agents",
    REPO_ROOT / ".claude" / "skills",
)
_AGENTS_MD = REPO_ROOT / "AGENTS.md"


def _target_files() -> list[Path]:
    """Every markdown file in scope, deduped by resolved path.

    The dedup is a structural guard, not a live no-op: nothing in
    ``_SKILL_DIRS`` today is a symlink (verified: ``find ... -type l``
    against the live tree returns nothing), so today it changes zero
    entries. It exists so a future glob that widens to the repo root (and
    so picks up ``CLAUDE.md``, a symlink to ``AGENTS.md``) fails to double
    count rather than silently doing so -- see
    ``test_target_files_dedupes_claude_md_symlink``.
    """
    files: list[Path] = []
    for d in _SKILL_DIRS:
        files.extend(sorted(d.rglob("*.md")))
    files.append(_AGENTS_MD)

    seen: set[Path] = set()
    deduped: list[Path] = []
    for f in files:
        if not f.is_file():
            continue
        resolved = f.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(f)
    return deduped


def _rel(f: Path) -> str:
    try:
        return str(f.relative_to(REPO_ROOT))
    except ValueError:
        return str(f)


_TRIPLE_FENCE_RE = re.compile(r"```.*?```", re.S)

#: Shared between checks (b) and (c): a literal repo path can never
#: legitimately contain a shell variable, a home-relative prefix, a brace
#: or angle-bracket placeholder, or whitespace.
_KNOWN_TOP_DIRS: tuple[str, ...] = (
    "src/",
    "tests/",
    "scripts/",
    "conexus/",
    "docs/",
    "service/",
    ".github/",
)
_TEMPLATE_MARKERS: tuple[str, ...] = ("NNN", "X.Y.Z", "vX.Y.Z")


# ---------------------------------------------------------------------------
# (a) Fenced shell blocks are syntactically valid
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.S)

#: `<base-branch>`-shaped angle-bracket placeholders -- this repo's
#: standing "substitute a real value here" documentation convention
#: (`git checkout <base-branch>`, `nx index repo <path>`, ...). Read
#: literally, `<foo>` is an input-redirection operator (`<`) followed by a
#: bare word and an unterminated `>` -- a syntax error with nothing to do
#: with whether the rest of the block is valid. Measured against the live
#: tree: 8 of 8 pre-substitution `bash -n` failures were exactly this
#: shape; after substitution, 0.
_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_.\-]*>")


def _fence_is_explicitly_skipped(body: str) -> bool:
    """Mechanical, EXPLICIT skip markers only -- never a silent heuristic.

    A block is skipped iff its first non-blank line starts with ``...``
    (a truncated/elided fragment marker) or any line is exactly
    ``# fragment`` (whitespace-trimmed). Both are opt-in: the author
    marks a block as illustrative rather than this check guessing from
    its shape.
    """
    lines = body.splitlines()
    first_nonblank = next((ln for ln in lines if ln.strip()), "")
    if first_nonblank.strip().startswith("..."):
        return True
    return any(ln.strip() == "# fragment" for ln in lines)


@dataclass(frozen=True)
class FenceViolation:
    file: str
    line: int
    error: str


@dataclass
class FenceScanResult:
    files_scanned: int = 0
    blocks_examined: int = 0
    explicitly_skipped: int = 0
    violations: list[FenceViolation] = field(default_factory=list)


def scan_fenced_shell_blocks(extra_files: Sequence[Path] = ()) -> FenceScanResult:
    result = FenceScanResult()
    for f in (*_target_files(), *extra_files):
        if not f.is_file():
            continue
        result.files_scanned += 1
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = _rel(f)
        for m in _FENCE_RE.finditer(text):
            body = m.group(1)
            if not body.strip():
                continue
            result.blocks_examined += 1
            if _fence_is_explicitly_skipped(body):
                result.explicitly_skipped += 1
                continue
            start_line = text.count("\n", 0, m.start()) + 1
            normalized = _PLACEHOLDER_RE.sub("PLACEHOLDER", body)
            proc = subprocess.run(
                ["bash", "-n"], input=normalized, capture_output=True, text=True
            )
            if proc.returncode != 0:
                result.violations.append(
                    FenceViolation(file=rel, line=start_line, error=proc.stderr.strip())
                )
    return result


# ---------------------------------------------------------------------------
# (a2) Trailing-backslash line-continuation swallows the next command
# ---------------------------------------------------------------------------
#
# Found DURING this module's own construction (2026-09-19), and it corrected
# the premise the module was commissioned on: `bash -n` genuinely does NOT
# catch incident 1. The person who commissioned this had claimed it would,
# having run `bash -n` only against the ALREADY-FIXED block and inferred the
# rest -- the same "a check whose domain does not contain the claim" defect
# this module exists to catch. A trailing backslash at the
# end of a bash script is accepted cleanly (verified against this exact
# file's content): the joined `git add ... git commit -m "..."` is one
# syntactically valid, very long `git add` invocation, so check (a) above
# is structurally incapable of catching this class no matter how it is
# tuned. This is a SEPARATE, narrower structural check: a fenced line
# ending in a lone trailing backslash (continuation), where the very next
# line starts with a recognizable CLI verb (`git`, `nx`, `gh`, ...) rather
# than a bare argument/path, is almost always an accidental absorption of
# what the author meant to be an independent command.
#
# False-positive guard, tuned against the live corpus (measured: 8
# candidate continuation lines, 2 raw hits, 1 after this guard): a
# continuation line ending in `|`, `&&`, `||`, `;`, or `&` BEFORE the
# backslash is intentional multi-command chaining (a real example in this
# repo: `curl -s "..." | \` continuing onto `python3 -c "..."`) and is
# never flagged.

_OPERATOR_TAIL_RE = re.compile(r"(\||&&|\|\||;|&)\s*$")
_COMMAND_VERB_RE = re.compile(
    r"^\s*(git|gh|nx|nx-hook|cd|make|uv|uvx|npm|pip|pip3|bash|sh|python|python3|"
    r"docker|curl|wget|cp|mv|rm|mkdir|touch|echo|export|source)\b"
)


@dataclass(frozen=True)
class AbsorptionViolation:
    file: str
    line: int
    continuation_line: str
    next_line: str


@dataclass
class AbsorptionScanResult:
    files_scanned: int = 0
    continuations_examined: int = 0
    violations: list[AbsorptionViolation] = field(default_factory=list)


def scan_trailing_backslash_absorption(
    extra_files: Sequence[Path] = (),
) -> AbsorptionScanResult:
    result = AbsorptionScanResult()
    for f in (*_target_files(), *extra_files):
        if not f.is_file():
            continue
        result.files_scanned += 1
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = _rel(f)
        for m in _FENCE_RE.finditer(text):
            body = m.group(1)
            lines = body.splitlines()
            start_line = text.count("\n", 0, m.start()) + 1
            for i, line in enumerate(lines[:-1]):
                stripped = line.rstrip()
                if not stripped.endswith("\\") or stripped.endswith("\\\\"):
                    continue
                before_backslash = stripped[:-1].rstrip()
                if _OPERATOR_TAIL_RE.search(before_backslash):
                    continue
                result.continuations_examined += 1
                nxt = lines[i + 1]
                if _COMMAND_VERB_RE.match(nxt):
                    result.violations.append(
                        AbsorptionViolation(
                            file=rel,
                            line=start_line + i,
                            continuation_line=line.strip(),
                            next_line=nxt.strip(),
                        )
                    )
    return result


#: (file, line) -> reason. Every entry names a CONFIRMED, live absorption
#: bug this module found but is forbidden from fixing (dispatching task
#: constraint). Shrink-only, same discipline as PATH_EXISTS_ALLOWLIST.
ABSORPTION_ALLOWLIST: dict[tuple[str, int], str] = {
    # EMPTY, and it should stay that way. The single entry this shipped
    # with -- the release skill's `git add` whose trailing backslash
    # absorbed its own `git commit` -- was fixed on develop at 25b274ece
    # while this module was being written, and the shrink-only test below
    # failed immediately on the stale entry. That failure is the proof the
    # ratchet works: it fired the moment the defect it described stopped
    # being true.
}
_ABSORPTION_ALLOWLIST_CEILING = 0


def _unallowlisted_absorption_violations(
    result: AbsorptionScanResult,
) -> list[AbsorptionViolation]:
    return [
        v for v in result.violations if (v.file, v.line) not in ABSORPTION_ALLOWLIST
    ]


# ---------------------------------------------------------------------------
# (b) Referenced repo paths exist
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"`([^`\n]+)`")
_DISALLOWED_TOKEN_CHARS: tuple[str, ...] = ("$", "~", "{", "}", "<", ">", " ", "*")

#: Confirmed: `git check-ignore service/target` matches `.gitignore:54`.
#: Build output; its presence/absence on disk says nothing about doc rot.
#:
#: BOTH FORMS, and the bare one is why: this shipped with the trailing-slash
#: form alone, which misses a doc citing the directory itself as
#: `service/target` (AGENTS.md:222 does). That reference exists on any box
#: that has built the engine and is absent on a fresh CI checkout, so the
#: check was green locally and red in CI for every author who had ever run
#: `scripts/build-gate-jar.sh` — a gate whose verdict depended on untracked
#: build output rather than on the tree. Allowlisting the citation would have
#: recorded it as an exception; it is not one, it is the same build output
#: this tuple already exempts.
_BUILD_OUTPUT_PREFIXES: tuple[str, ...] = ("service/target/", "service/target")


def _looks_like_repo_path_token(tok: str) -> bool:
    """Narrow, deliberate rule -- see the module docstring's check (b)
    section for exactly what this does not cover and why."""
    if any(c in tok for c in _DISALLOWED_TOKEN_CHARS):
        return False
    if not tok.startswith(_KNOWN_TOP_DIRS):
        return False
    return not any(marker in tok for marker in _TEMPLATE_MARKERS)


def _strip_locator_suffix(tok: str) -> str:
    """Strip a pytest nodeid (``::test_x``), a markdown anchor
    (``#heading``), or a trailing ``:N``/``:N-M`` line-number locator --
    none of these are part of the filesystem path itself."""
    tok = tok.split("::", 1)[0]
    tok = tok.split("#", 1)[0]
    return re.sub(r":\d+(-\d+)?$", "", tok)


@dataclass(frozen=True)
class PathViolation:
    file: str
    token: str


@dataclass
class PathScanResult:
    files_scanned: int = 0
    tokens_examined: int = 0
    violations: list[PathViolation] = field(default_factory=list)


def scan_referenced_paths(extra_files: Sequence[Path] = ()) -> PathScanResult:
    result = PathScanResult()
    for f in (*_target_files(), *extra_files):
        if not f.is_file():
            continue
        result.files_scanned += 1
        text = _TRIPLE_FENCE_RE.sub("", f.read_text(encoding="utf-8", errors="replace"))
        rel = _rel(f)
        seen_this_file: set[str] = set()
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(1).strip()
            if not _looks_like_repo_path_token(tok):
                continue
            path_part = _strip_locator_suffix(tok)
            if path_part.startswith(_BUILD_OUTPUT_PREFIXES):
                continue
            result.tokens_examined += 1
            if tok in seen_this_file:
                continue
            if not (REPO_ROOT / path_part).exists():
                seen_this_file.add(tok)
                result.violations.append(PathViolation(file=rel, token=tok))
    return result


#: (file, token) -> reason. Every entry is dated and names WHY the token is
#: not a real, checkable path rather than a genuine rot site. Shrink-only:
#: see ``_PATH_ALLOWLIST_CEILING`` and ``test_path_allowlist_is_shrink_only``.
PATH_EXISTS_ALLOWLIST: dict[tuple[str, str], str] = {
    ("AGENTS.md", "src/nexus/commands/your_cmd.py"): (
        "2026-09-19: 'Adding a CLI command' workflow example -- a "
        "placeholder filename for a command that does not exist yet, not "
        "a real path that decayed."
    ),
    ("AGENTS.md", "tests/test_your_cmd.py"): (
        "2026-09-19: same workflow example, the paired test-file placeholder."
    ),
    (
        "AGENTS.md",
        "conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py",
    ): (
        "2026-09-19: historical reference -- the sentence naming it says "
        "outright the file 'was deleted 2026-08-22'; kept as a "
        "retrospective pointer, the same shape as "
        "test_release_artifact_verb_rot.py's _RETIRED_SCRIPT_ALLOWLIST "
        "for a narrated-but-gone artifact."
    ),
}
#: Seeded 2026-09-19 at the exact count found on first live run. Bump only
#: with a deliberate edit naming the new entry's reason in the same commit.
_PATH_ALLOWLIST_CEILING = 3


def _unallowlisted_path_violations(result: PathScanResult) -> list[PathViolation]:
    return [
        v for v in result.violations if (v.file, v.token) not in PATH_EXISTS_ALLOWLIST
    ]


# ---------------------------------------------------------------------------
# (c) Globs match something
# ---------------------------------------------------------------------------

#: Deliberately narrow: only a path-shaped character set qualifies as a
#: glob CANDIDATE at all. This is what keeps markdown noise (`**bold**`,
#: `[ref]`, `[<class>]`, `{"key": [...]}`, prose with a stray `*`) out of
#: the candidate set without a fragile prose-aware heuristic -- see the
#: module docstring's check (c) section.
_STRICT_GLOB_CHARSET_RE = re.compile(r"^[A-Za-z0-9_./\-\[\]*]+$")


def _looks_like_glob_candidate(tok: str) -> bool:
    if "*" not in tok and "[0-9]" not in tok:
        return False
    if not _STRICT_GLOB_CHARSET_RE.match(tok):
        return False
    if any(marker in tok for marker in _TEMPLATE_MARKERS):
        return False
    if "/" in tok:
        return tok.startswith(_KNOWN_TOP_DIRS)
    return "." in tok  # a bare filename-glob must look like one (has an extension)


@dataclass(frozen=True)
class GlobViolation:
    file: str
    token: str


@dataclass
class GlobScanResult:
    files_scanned: int = 0
    candidates_examined: int = 0
    violations: list[GlobViolation] = field(default_factory=list)


def scan_globs(extra_files: Sequence[Path] = ()) -> GlobScanResult:
    result = GlobScanResult()
    for f in (*_target_files(), *extra_files):
        if not f.is_file():
            continue
        result.files_scanned += 1
        text = _TRIPLE_FENCE_RE.sub("", f.read_text(encoding="utf-8", errors="replace"))
        rel = _rel(f)
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(1).strip()
            if not _looks_like_glob_candidate(tok):
                continue
            result.candidates_examined += 1
            matches = (
                list(REPO_ROOT.glob(tok)) if "/" in tok else list(REPO_ROOT.rglob(tok))
            )
            if not matches:
                result.violations.append(GlobViolation(file=rel, token=tok))
    return result


# ---------------------------------------------------------------------------
# Tests: (a) fenced shell syntax
# ---------------------------------------------------------------------------


def test_fenced_shell_blocks_are_syntactically_valid():
    result = scan_fenced_shell_blocks()
    print(  # noqa: T201 -- acceptance evidence
        f"\n[doc-decay-lint:fence] files scanned: {result.files_scanned}\n"
        f"[doc-decay-lint:fence] blocks examined: {result.blocks_examined} "
        f"(explicitly skipped: {result.explicitly_skipped})\n"
        f"[doc-decay-lint:fence] violations: {len(result.violations)}"
    )
    assert result.violations == [], (
        "fenced bash/sh/shell block(s) fail `bash -n` syntax check:\n"
        + "\n".join(f"  {v.file}:{v.line}: {v.error}" for v in result.violations)
    )


def test_fence_scan_examines_something():
    result = scan_fenced_shell_blocks()
    assert result.files_scanned > 40, (
        f"only {result.files_scanned} file(s) scanned -- _target_files() is "
        "not walking the real skill/agent tree"
    )
    assert result.blocks_examined > 30, (
        f"only {result.blocks_examined} fenced bash/sh/shell block(s) "
        "found -- the fence extractor is almost certainly broken"
    )


def test_planted_fence_syntax_error_is_caught(tmp_path):
    canary = tmp_path / "test-canary-fence-broken.md"
    canary.write_text('# Canary\n\n```bash\necho "unterminated string\n```\n')
    result = scan_fenced_shell_blocks(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-fence-broken.md")
    ]
    assert len(hits) == 1, "planted unterminated-quote fenced block was not flagged"


def test_planted_valid_fence_passes(tmp_path):
    canary = tmp_path / "test-canary-fence-valid.md"
    canary.write_text("# Canary\n\n```bash\ncp <base-branch> dest\nls -la\n```\n")
    result = scan_fenced_shell_blocks(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-fence-valid.md")
    ]
    assert hits == [], (
        "a syntactically valid block (with a placeholder) must not be flagged"
    )


def test_placeholder_normalization_does_not_hide_a_real_syntax_error(tmp_path):
    """The critical soundness property: substituting `<foo>` placeholders
    must not accidentally swallow a genuine syntax error elsewhere in the
    same block."""
    canary = tmp_path / "test-canary-fence-placeholder-plus-error.md"
    canary.write_text(
        '# Canary\n\n```bash\ncp <base-branch> dest\necho "still unterminated\n```\n'
    )
    result = scan_fenced_shell_blocks(extra_files=[canary])
    hits = [
        v
        for v in result.violations
        if v.file.endswith("test-canary-fence-placeholder-plus-error.md")
    ]
    assert len(hits) == 1, (
        "a real syntax error alongside a placeholder must still be caught"
    )


def test_leading_ellipsis_marker_skips_block(tmp_path):
    canary = tmp_path / "test-canary-fence-ellipsis.md"
    canary.write_text('# Canary\n\n```bash\n...\necho "unterminated\n```\n')
    result = scan_fenced_shell_blocks(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-fence-ellipsis.md")
    ]
    assert hits == [], "a leading `...` marker must explicitly skip the block"
    assert result.explicitly_skipped >= 1


def test_hash_fragment_marker_skips_block(tmp_path):
    canary = tmp_path / "test-canary-fence-fragment.md"
    canary.write_text('# Canary\n\n```bash\n# fragment\necho "unterminated\n```\n')
    result = scan_fenced_shell_blocks(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-fence-fragment.md")
    ]
    assert hits == [], "a `# fragment` marker line must explicitly skip the block"
    assert result.explicitly_skipped >= 1


# ---------------------------------------------------------------------------
# Tests: (a2) trailing-backslash command absorption
# ---------------------------------------------------------------------------


def test_no_unallowlisted_trailing_backslash_absorption():
    result = scan_trailing_backslash_absorption()
    violations = _unallowlisted_absorption_violations(result)
    print(  # noqa: T201 -- acceptance evidence
        f"\n[doc-decay-lint:absorb] files scanned: {result.files_scanned}\n"
        f"[doc-decay-lint:absorb] continuations examined: {result.continuations_examined}\n"
        f"[doc-decay-lint:absorb] allowlisted: {len(ABSORPTION_ALLOWLIST)}\n"
        f"[doc-decay-lint:absorb] unallowlisted violations: {len(violations)}"
    )
    assert violations == [], (
        "fenced block line-continuation likely absorbs the next command as "
        "extra arguments:\n"
        + "\n".join(
            f"  {v.file}:{v.line}: `{v.continuation_line}` -> `{v.next_line}`"
            for v in violations
        )
    )


def test_absorption_scan_examines_something():
    result = scan_trailing_backslash_absorption()
    assert result.files_scanned > 40, f"only {result.files_scanned} file(s) scanned"
    # Genuinely rare: multi-line shell continuations inside skill/agent
    # markdown are uncommon to begin with, and most that exist chain onto
    # arguments, not a new command. A floor of 3 proves the scanner sees
    # real continuation lines without demanding volume this corpus lacks.
    assert result.continuations_examined > 3, (
        f"only {result.continuations_examined} continuation line(s) "
        "examined -- the scanner may be broken rather than the corpus "
        "genuinely having almost none"
    )


def test_planted_absorption_is_caught(tmp_path):
    canary = tmp_path / "test-canary-absorb-broken.md"
    canary.write_text(
        '# Canary\n\n```bash\ncp file1.txt file2.txt \\\ngit commit -m "message"\n```\n'
    )
    result = scan_trailing_backslash_absorption(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-absorb-broken.md")
    ]
    assert len(hits) == 1, "planted trailing-backslash absorption was not flagged"


def test_planted_pipe_continuation_is_not_flagged(tmp_path):
    """The false-positive guard: a continuation ending in `|` (or `&&`,
    `||`, `;`, `&`) before the backslash is intentional chaining."""
    canary = tmp_path / "test-canary-absorb-pipe.md"
    canary.write_text(
        "# Canary\n\n"
        "```bash\n"
        'curl -s "https://example.invalid/x" | \\\n'
        'python3 -c "print(1)"\n'
        "```\n"
    )
    result = scan_trailing_backslash_absorption(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-absorb-pipe.md")
    ]
    assert hits == [], "a pipe continuation before the backslash must never be flagged"


def test_planted_multi_arg_continuation_is_not_flagged(tmp_path):
    """A normal multi-line invocation of ONE command, continuing onto a
    bare path/argument (not a recognized CLI verb), must never be flagged."""
    canary = tmp_path / "test-canary-absorb-args.md"
    canary.write_text("# Canary\n\n```bash\ncp file1.txt \\\n   file2.txt dest/\n```\n")
    result = scan_trailing_backslash_absorption(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-absorb-args.md")
    ]
    assert hits == [], "a bare-argument continuation must never be flagged"


def test_absorption_allowlist_is_shrink_only():
    total = len(ABSORPTION_ALLOWLIST)
    assert total <= _ABSORPTION_ALLOWLIST_CEILING, (
        f"ABSORPTION_ALLOWLIST has {total} entries, exceeding "
        f"_ABSORPTION_ALLOWLIST_CEILING ({_ABSORPTION_ALLOWLIST_CEILING})"
    )
    result = scan_trailing_backslash_absorption()
    found = {(v.file, v.line) for v in result.violations}
    stale = [key for key in ABSORPTION_ALLOWLIST if key not in found]
    assert stale == [], (
        f"stale ABSORPTION_ALLOWLIST entr(y/ies), fix landed -- delete: {stale}"
    )


# ---------------------------------------------------------------------------
# Tests: (b) referenced repo paths exist
# ---------------------------------------------------------------------------


def test_referenced_repo_paths_exist():
    result = scan_referenced_paths()
    violations = _unallowlisted_path_violations(result)
    print(  # noqa: T201 -- acceptance evidence
        f"\n[doc-decay-lint:path] files scanned: {result.files_scanned}\n"
        f"[doc-decay-lint:path] path-shaped tokens examined: {result.tokens_examined}\n"
        f"[doc-decay-lint:path] allowlisted: {len(PATH_EXISTS_ALLOWLIST)}\n"
        f"[doc-decay-lint:path] unallowlisted violations: {len(violations)}"
    )
    assert violations == [], (
        "referenced repo path(s) do not exist on disk:\n"
        + "\n".join(f"  {v.file}: `{v.token}`" for v in violations)
        + "\nEither the reference rotted (fix the doc) or it never named a "
        "real repo path (add a dated, reasoned PATH_EXISTS_ALLOWLIST entry)."
    )


def test_path_scan_examines_something():
    result = scan_referenced_paths()
    assert result.files_scanned > 40, f"only {result.files_scanned} file(s) scanned"
    assert result.tokens_examined > 100, (
        f"only {result.tokens_examined} path-shaped token(s) examined -- "
        "the token extractor is almost certainly broken"
    )


def test_planted_dangling_path_reference_is_caught(tmp_path):
    canary = tmp_path / "test-canary-path-dangling.md"
    canary.write_text(
        "# Canary\n\nSee `tests/e2e/lib/this-file-does-not-exist.sh` for details.\n"
    )
    result = scan_referenced_paths(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-path-dangling.md")
    ]
    assert len(hits) == 1, "planted dangling repo-path reference was not flagged"
    assert all((v.file, v.token) not in PATH_EXISTS_ALLOWLIST for v in hits), (
        "a fresh canary path must not accidentally match a real allowlist entry"
    )


def test_planted_real_path_reference_passes(tmp_path):
    canary = tmp_path / "test-canary-path-real.md"
    canary.write_text(
        "# Canary\n\nSee `tests/AGENTS.md` and `AGENTS.md` for the conventions.\n"
    )
    result = scan_referenced_paths(extra_files=[canary])
    hits = [v for v in result.violations if v.file.endswith("test-canary-path-real.md")]
    assert hits == [], "a real, existing repo path must not be flagged"


def test_variable_and_template_paths_are_out_of_scope(tmp_path):
    """Documents the deliberate narrowing: `$VAR/...`, `~/...`, and a
    template-marker path never enter the candidate set at all."""
    canary = tmp_path / "test-canary-path-out-of-scope.md"
    canary.write_text(
        "# Canary\n\n"
        "Create `$RDR_DIR/NNN-kebab-title.md` from the template.\n"
        "See `~/.claude/settings.json` for user settings.\n"
        "Copy `resources/rdr/REGISTER.md` (plugin-root-relative, not repo-root).\n"
    )
    result = scan_referenced_paths(extra_files=[canary])
    hits = [
        v
        for v in result.violations
        if v.file.endswith("test-canary-path-out-of-scope.md")
    ]
    assert hits == [], (
        "variable-prefixed, home-relative, template-marker, and "
        "plugin-root-relative tokens must never be treated as checkable "
        "repo paths"
    )


def test_build_output_paths_are_excluded(tmp_path):
    canary = tmp_path / "test-canary-path-buildout.md"
    canary.write_text("See `service/target/nexus-service` after building.\n")
    result = scan_referenced_paths(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-path-buildout.md")
    ]
    assert hits == [], "a git-ignored service/target/ reference must never be checked"


def test_path_allowlist_is_shrink_only():
    total = len(PATH_EXISTS_ALLOWLIST)
    assert total <= _PATH_ALLOWLIST_CEILING, (
        f"PATH_EXISTS_ALLOWLIST has {total} entries, exceeding "
        f"_PATH_ALLOWLIST_CEILING ({_PATH_ALLOWLIST_CEILING}) -- a wider "
        "allowlist needs a deliberate ceiling bump in this same commit, "
        "with its own reason, never a silent grow."
    )
    result = scan_referenced_paths()
    found = {(v.file, v.token) for v in result.violations}
    stale = [key for key in PATH_EXISTS_ALLOWLIST if key not in found]
    assert stale == [], (
        "stale PATH_EXISTS_ALLOWLIST entr(y/ies), no longer a real "
        f"violation -- delete them: {stale}"
    )


# ---------------------------------------------------------------------------
# Tests: (c) globs match something
# ---------------------------------------------------------------------------


def test_globs_match_something():
    result = scan_globs()
    print(  # noqa: T201 -- acceptance evidence
        f"\n[doc-decay-lint:glob] files scanned: {result.files_scanned}\n"
        f"[doc-decay-lint:glob] glob candidates examined: {result.candidates_examined}\n"
        f"[doc-decay-lint:glob] violations: {len(result.violations)}"
    )
    assert result.violations == [], (
        "glob-shaped token(s) match zero files anywhere in the repo:\n"
        + "\n".join(f"  {v.file}: `{v.token}`" for v in result.violations)
    )


def test_glob_scan_examines_something():
    result = scan_globs()
    assert result.files_scanned > 40, f"only {result.files_scanned} file(s) scanned"
    # Glob-shaped tokens are genuinely rare in skill/agent prose (the whole
    # point of the rdr-create incident is that this pattern is unusual
    # enough to go unnoticed) -- a floor of 3 proves the extractor is
    # live without demanding a volume this corpus does not have.
    assert result.candidates_examined > 3, (
        f"only {result.candidates_examined} glob candidate(s) found -- the "
        "glob extractor may be broken rather than the corpus genuinely "
        "having almost none"
    )


def test_planted_nonmatching_glob_is_caught(tmp_path):
    canary = tmp_path / "test-canary-glob-broken.md"
    canary.write_text(
        "# Canary\n\nScan for files matching `zzz-totally-fake-nonexistent-*.qqzz`.\n"
    )
    result = scan_globs(extra_files=[canary])
    hits = [
        v for v in result.violations if v.file.endswith("test-canary-glob-broken.md")
    ]
    assert len(hits) == 1, "planted zero-match glob was not flagged"


def test_planted_matching_glob_passes(tmp_path):
    canary = tmp_path / "test-canary-glob-real.md"
    canary.write_text(
        "# Canary\n\nRDR post-mortems match `docs/rdr/post-mortem/*.md`.\n"
    )
    result = scan_globs(extra_files=[canary])
    hits = [v for v in result.violations if v.file.endswith("test-canary-glob-real.md")]
    assert hits == [], "a glob that genuinely matches real files must not be flagged"


def test_markdown_emphasis_is_never_a_glob_candidate(tmp_path):
    """`**bold**` and friends must never enter the candidate set at all --
    this is what keeps the check from drowning in markdown noise."""
    canary = tmp_path / "test-canary-glob-markdown-noise.md"
    canary.write_text(
        "# Canary\n\n"
        "This is **bold** and this is `**`.\n"
        "A list item: `[ref]` and `[<class>]` and `[NUMBER]`.\n"
        'A JSON shape: `{"items": [...], "ok": true}`.\n'
    )
    result = scan_globs(extra_files=[canary])
    hits = [
        v
        for v in result.violations
        if v.file.endswith("test-canary-glob-markdown-noise.md")
    ]
    assert hits == [], "markdown emphasis/bracket noise must never be treated as a glob"
    baseline = scan_globs()
    assert result.candidates_examined == baseline.candidates_examined, (
        "markdown noise in the canary file added to the candidate count -- "
        "the strict charset filter let something through"
    )


# ---------------------------------------------------------------------------
# Cross-cutting: scope hygiene
# ---------------------------------------------------------------------------


def test_target_files_dedupes_claude_md_symlink():
    """CLAUDE.md is a symlink to AGENTS.md at the repo root. This module
    never globs the repo root (only AGENTS.md is referenced explicitly),
    so the symlink is never independently discovered -- and the dedup-by-
    resolved-path in `_target_files()` is a structural guard against a
    future glob doing so anyway."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    agents_md = REPO_ROOT / "AGENTS.md"
    assert claude_md.is_symlink(), "expected CLAUDE.md to be a symlink at the repo root"
    assert claude_md.resolve() == agents_md.resolve()

    files = _target_files()
    assert claude_md not in files, "CLAUDE.md must never be independently scanned"
    assert agents_md in files, "AGENTS.md must be scanned"

    resolved = [f.resolve() for f in files]
    assert len(resolved) == len(set(resolved)), (
        "duplicate resolved path in _target_files() -- the dedup is broken"
    )

    # Defensive proof the dedup logic itself works, not just that today's
    # tree has no symlinks to exercise it: feed it AGENTS.md twice via
    # extra_files-shaped duplication of the underlying list construction.
    doubled = [*files, agents_md, claude_md]
    seen: set[Path] = set()
    deduped: list[Path] = []
    for f in doubled:
        r = f.resolve()
        if r in seen:
            continue
        seen.add(r)
        deduped.append(f)
    assert len(deduped) == len(files), "manual double-feed dedup sanity check failed"


def test_release_skills_are_in_scope_and_only_the_known_absorption_bug_hits():
    """`.claude/skills/release/SKILL.md` and
    `.claude/skills/engine-release/SKILL.md` carried the skill-decay
    incidents that motivated this module (see the module docstring,
    incident 1, and the engine-release prose-drift incident 4). Both are
    IN SCOPE here rather than excluded: excluding them would silently drop
    the exact files this module exists to watch, and a real regression on
    either would then go undetected.

    Incident 1 was fixed on develop at 25b274ece while this module was being
    written, so both files are now clean on all four checks and this test
    pins that. A failure here means one of these two files drifted, not that
    this module is wrong -- and a check (a2) violation in the release skill
    specifically means incident 1 came back, which is a regression to fix
    rather than a finding to allowlist.

    Note for anyone extending this: check (a)'s `bash -n` cannot see incident
    1's shape at all (a dangling continuation is valid bash). That is why
    check (a2) exists as a separate structural scan.
    """
    files_scanned = {_rel(f) for f in _target_files()}
    for rel_path in (
        ".claude/skills/release/SKILL.md",
        ".claude/skills/engine-release/SKILL.md",
    ):
        assert (REPO_ROOT / rel_path).is_file(), f"{rel_path}: no longer exists"
        assert rel_path in files_scanned, f"{rel_path}: unexpectedly out of scope"

    fence = scan_fenced_shell_blocks()
    paths = scan_referenced_paths()
    globs = scan_globs()
    absorption = scan_trailing_backslash_absorption()
    for rel_path in (
        ".claude/skills/release/SKILL.md",
        ".claude/skills/engine-release/SKILL.md",
    ):
        assert not any(v.file == rel_path for v in fence.violations), (
            f"{rel_path}: fenced-shell syntax violation -- note it, do not fix "
            "this file per the dispatching task"
        )
        assert not any(
            v.file == rel_path and (v.file, v.token) not in PATH_EXISTS_ALLOWLIST
            for v in paths.violations
        ), (
            f"{rel_path}: dangling path reference -- note it, do not fix this "
            "file per the dispatching task"
        )
        assert not any(v.file == rel_path for v in globs.violations), (
            f"{rel_path}: non-matching glob -- note it, do not fix this file "
            "per the dispatching task"
        )
        unallowlisted = [
            v
            for v in absorption.violations
            if v.file == rel_path and (v.file, v.line) not in ABSORPTION_ALLOWLIST
        ]
        assert unallowlisted == [], (
            f"{rel_path}: NEW absorption violation beyond the known, "
            f"allowlisted one -- note it, do not fix this file per the "
            f"dispatching task: {unallowlisted}"
        )

    # The motivating bug is FIXED (develop 25b274ece) and both files are now
    # clean on every check. This asserts it stays that way: the release skill
    # is the file this module exists to watch, so a new violation in it is a
    # regression of the exact incident, not a new finding.
    assert not any(
        v.file == ".claude/skills/release/SKILL.md" for v in absorption.violations
    ), (
        "the release skill regressed to a trailing-backslash absorption -- "
        "this is incident 1 returning; fix the block, do not allowlist it"
    )
