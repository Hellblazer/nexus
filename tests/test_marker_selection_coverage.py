# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-s6dei: every pytest marker declared in pyproject.toml must be WIRED
to a real invocation, not merely documented.

FOUND 2026-08-01: ``slow`` and ``stress`` were declared in
``[tool.pytest.ini_options] markers`` and deselected by default addopts,
but selected by ZERO commands repo-wide — every occurrence outside
pyproject.toml was a comment or a docstring claiming coverage that did not
exist. A deselected test with no home is operationally deleted: it produces
no output, no skip line, no count, and nothing notices its absence. This
test is the mechanization the bead asked for regardless of which way the
``slow``/``stress`` decision went: it fails the day a marker goes dark
again, instead of three months and three independent docstrings later.

A marker counts as wired if either:

1. It is affirmatively SELECTED by a real ``pytest`` invocation with ``-m``
   somewhere under ``.github/``, ``tests/e2e/``, or ``scripts/`` — a gate,
   a scheduled workflow leg, or a documented script invocation. Anchored on
   the token ``pytest`` actually appearing before the ``-m`` flag in the
   same logical command (backslash-continued shell lines are joined before
   matching, since that is how these commands are actually wrapped in this
   repo's workflows) — a bare ``-m <marker>`` inside an unrelated string
   (an ``echo``, a docstring, a comment) does NOT count. This is stricter
   than the first cut of this test, which matched ``-m`` + marker anywhere
   on a non-comment line and would have been satisfied by e.g.
   ``echo "run with -m slow"`` (code-review-expert finding, 2026-08-02).
2. It is consumed PROGRAMMATICALLY via
   ``request.node.get_closest_marker("<name>")`` (or equivalent) anywhere
   under ``tests/`` or ``src/`` outside a comment — the pattern used by
   ``no_service_jar`` / ``needs_stamped_jar``, which gate fixture behavior
   directly rather than through ``-m`` CLI selection.

Either path is real wiring; a comment, docstring, or echoed string
describing either path is not.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_THIS_FILE = Path(__file__).resolve()

_MARKER_NAME_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:")

# Directories searched for an actual `-m` selection command. Deliberately
# narrow: docs (*.md) describing how to invoke a marker are not an
# invocation, and this bead's whole point is that a claim of coverage is
# not coverage.
_SELECTION_SEARCH_DIRS = (".github", "tests/e2e", "scripts")
_SELECTION_GLOBS = ("*.yml", "*.yaml", "*.sh", "*.py")

# Directories searched for a get_closest_marker() consumer.
_CONSUMER_SEARCH_DIRS = ("tests", "src")
_CONSUMER_GLOBS = ("*.py",)


def _declared_markers() -> list[str]:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    raw = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    names: list[str] = []
    for entry in raw:
        m = _MARKER_NAME_RE.match(entry)
        names.append(m.group(1) if m else entry.strip())
    return names


def _non_comment_lines(path: Path) -> list[str]:
    """Physical lines with pure comment lines dropped.

    Good enough for YAML, shell, and Python: all three use ``#`` as the
    comment prefix, and a GitHub Actions ``run: |`` block is embedded
    shell, so stripping ``#``-prefixed lines works uniformly without a
    real per-language parser.
    """
    out = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(line)
    return out


def _logical_lines(path: Path) -> list[str]:
    """Non-comment lines with backslash-line-continuations joined.

    Real invocations in this repo's workflows are routinely wrapped like::

        uv run pytest \\
            -o addopts="" -m slow -q -rs --junit-xml=slow-s6dei.xml

    A per-physical-line check would miss ``pytest`` and ``-m slow`` living
    on different lines of the SAME command. Joining ``\\``-continued lines
    into one logical line before matching fixes that without widening the
    match window across unrelated commands (continuation is an explicit,
    single-command signal — unlike "search the whole file").
    """
    logical: list[str] = []
    buf = ""
    for line in _non_comment_lines(path):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            logical.append(buf + stripped)
            buf = ""
    if buf:
        logical.append(buf)
    return logical


_M_ARG_RE = re.compile(r"""-m[= ]\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")


def _is_affirmative_selection_line(marker: str, line: str) -> bool:
    """True iff ``line`` is a genuine ``pytest -m`` invocation that
    AFFIRMATIVELY selects *marker*: pytest precedes ``-m``, the marker
    appears in the ``-m`` expression NOT under a ``not``, and the
    invocation is not ``--collect-only``.

    nexus-vqnls: the previous matcher counted ANY ``pytest ... -m ...
    <marker>`` co-occurrence — including ``-m "integration and not
    lived_in"`` and collect-only counts — so a marker whose every
    reference DESELECTED it still reported WIRED while its tests had zero
    executing runner. "Someone references it" and "something executes it"
    are different properties; the module header promises the second.
    """
    if not re.search(r"\bpytest\b", line):
        return False
    if "--collect-only" in line or "--co" in line.split():
        return False
    m = _M_ARG_RE.search(line)
    if not m:
        return False
    expr = next(g for g in m.groups() if g is not None)
    if not re.search(rf"\b{re.escape(marker)}\b", expr):
        return False
    if re.search(rf"\bnot\s+{re.escape(marker)}\b", expr):
        return False
    return True


def _iter_files(dirs: tuple[str, ...], globs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for d in dirs:
        base = _REPO_ROOT / d
        if not base.exists():
            continue
        for pattern in globs:
            files.extend(base.rglob(pattern))
    return files


def _marker_is_selected(marker: str) -> bool:
    for path in _iter_files(_SELECTION_SEARCH_DIRS, _SELECTION_GLOBS):
        for line in _logical_lines(path):
            if _is_affirmative_selection_line(marker, line):
                return True
    return False


def _marker_is_consumed_programmatically(marker: str) -> bool:
    pattern = re.compile(
        rf"get_closest_marker\(\s*['\"]{re.escape(marker)}['\"]"
    )
    for path in _iter_files(_CONSUMER_SEARCH_DIRS, _CONSUMER_GLOBS):
        if path.resolve() == _THIS_FILE:
            continue
        for line in _non_comment_lines(path):
            if pattern.search(line):
                return True
    return False


# ── Unit tests for the detector itself ──────────────────────────────────────
# These pin the detector's behavior against synthetic strings, independent
# of what currently lives in the repo, so a future edit to the regex can't
# silently regress back to the overclaiming version.


class TestIsAffirmativeSelectionLine:
    def test_real_pytest_invocation_is_selected(self) -> None:
        assert _is_affirmative_selection_line("slow", "uv run pytest -m slow -q")

    def test_real_invocation_with_combined_expression_is_selected(self) -> None:
        assert _is_affirmative_selection_line(
            "lived_in", 'uv run pytest -m "integration and lived_in" -q'
        )

    def test_negated_marker_does_NOT_count_as_wiring(self) -> None:
        """FLIPPED PIN (nexus-vqnls). The previous version of this test
        pinned the opposite — that a deselection counts as wiring — which
        is exactly how lived_in reported WIRED with 39 tests and zero
        executing runner. A deselection proves someone knows the marker
        exists; it does not execute a single test carrying it."""
        assert not _is_affirmative_selection_line(
            "lived_in", 'uv run pytest -m "integration and not lived_in" -q'
        )

    def test_collect_only_does_NOT_count_as_wiring(self) -> None:
        """Collected is not executed (nexus-no210 bound drift, not
        execution — 39 collected is still 39 never run)."""
        assert not _is_affirmative_selection_line(
            "lived_in",
            'uv run pytest -m "integration and lived_in" --collect-only -q',
        )

    def test_negation_of_another_marker_still_counts_for_this_one(self) -> None:
        assert _is_affirmative_selection_line(
            "integration",
            'uv run pytest -m "integration and not lived_in" -q',
        )

    def test_bare_flag_without_pytest_token_is_not_selected(self) -> None:
        """The exact overclaim this test caught in review: a non-comment
        line mentioning ``-m <marker>`` with no real pytest invocation
        must NOT count as wiring."""
        assert not _is_affirmative_selection_line("slow", 'echo "-m slow"')
        assert not _is_affirmative_selection_line(
            "slow", 'echo "run with -m slow explicitly"'
        )

    def test_comment_line_is_excluded_before_matching(self) -> None:
        # _non_comment_lines drops these before _is_real_selection_line
        # ever sees them; assert the exclusion directly.
        lines = _non_comment_lines
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False
        ) as f:
            f.write("# uv run pytest -m slow\n")
            f.write('echo "not a real invocation"\n')
            path = Path(f.name)
        try:
            kept = lines(path)
            assert not any("pytest" in line for line in kept), (
                "comment-only pytest invocation must be excluded"
            )
        finally:
            path.unlink()

    def test_backslash_continuation_is_joined_before_matching(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False
        ) as f:
            f.write("uv run pytest \\\n")
            f.write('  -o addopts="" -m slow -q -rs\n')
            path = Path(f.name)
        try:
            logical = _logical_lines(path)
            assert any(
                _is_affirmative_selection_line("slow", line) for line in logical
            ), "continuation-joined pytest -m slow invocation must be detected"
        finally:
            path.unlink()


# ── Repo-wide coverage check ─────────────────────────────────────────────────

#: ACKNOWLEDGED-DARK markers (nexus-vqnls): declared, tests exist, and NO
#: affirmative executing selection exists — a named, tracked state, never a
#: silent false-WIRED. Each entry carries the owning decision bead; the
#: staleness guard below deletes entries the moment a real runner appears,
#: so this table can only shrink truthfully, never hide new darkness.
_ACKNOWLEDGED_DARK: dict[str, str] = {
    "lived_in": (
        "39 tests need a lived-in install; local-service-gate deliberately "
        "runs -m 'integration and not lived_in'. Runner decision: nexus-699ot"
    ),
    "cloud_mode": (
        "3 tests need a cloud-mode box; absence acknowledged in "
        "local-service-gate.sh. Runner decision: nexus-699ot"
    ),
}


@pytest.mark.parametrize("marker", sorted(_ACKNOWLEDGED_DARK))
def test_acknowledged_dark_entries_are_still_dark(marker: str) -> None:
    """Ratchet staleness: the moment an acknowledged-dark marker gains a
    real affirmative runner, its entry must be REMOVED — a stale entry
    could later hide a regression back to darkness."""
    # Both halves of the OR the main gate uses (substantive-critic wave-2
    # Significant: checking only the selection half would let a marker
    # that gains a get_closest_marker() consumer keep its stale dark
    # entry, silently defeating the ratchet's own guarantee).
    assert not (
        _marker_is_selected(marker) or _marker_is_consumed_programmatically(marker)
    ), (
        f"'{marker}' is now WIRED (affirmative selection or programmatic "
        f"consumer) — remove it from _ACKNOWLEDGED_DARK (and close/annotate "
        f"its decision bead)."
    )
    assert marker in set(_declared_markers()), (
        f"'{marker}' is no longer declared in pyproject.toml — remove the "
        "stale _ACKNOWLEDGED_DARK entry."
    )


@pytest.mark.parametrize("marker", _declared_markers())
def test_marker_is_wired_to_a_real_invocation(marker: str) -> None:
    """Every marker declared in pyproject.toml is AFFIRMATIVELY selected or
    programmatically consumed somewhere real — never dark-by-construction
    (nexus-s6dei), and never WIRED-by-deselection (nexus-vqnls: a negated
    or collect-only reference proves awareness, not execution)."""
    if marker in _ACKNOWLEDGED_DARK:
        pytest.skip(f"acknowledged dark: {_ACKNOWLEDGED_DARK[marker]}")
    wired = _marker_is_selected(marker) or _marker_is_consumed_programmatically(
        marker
    )
    assert wired, (
        f"pytest marker '{marker}' is declared in pyproject.toml but is "
        f"selected by no real 'pytest ... -m' invocation under .github/, "
        f"tests/e2e/, or scripts/, and consumed by no get_closest_marker() "
        f"call under tests/ or src/ — it is operationally deleted. Either "
        f"wire it into a real gate/script, or remove the marker and any "
        f"tests/docstrings that claim it provides coverage (nexus-s6dei)."
    )
