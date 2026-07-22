# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-whh61.3: structlog event-name snake_case lint.

The ls88v ruff audit wanted "event-name snake_case" enforcement: the first
positional argument to a structlog level call (``_log.info("event", ...)``)
should be a snake_case event key, not a free-text prose message. Ruff has no
stock rule for this, so this is a self-contained AST scan (the same shape as
the taxonomy-write lint in ``test_structlog_events.py``).

Rule
----
For every ``<logger>.<level>(...)`` call where the receiver is a known logger
name and the first positional argument is a string LITERAL, the literal must
be a dotted chain of snake_case segments::

    ok:   "wal_mode_unavailable"
    ok:   "catalog_etl.start"            (dotted namespace — each segment snake)
    bad:  "WAL mode not available"       (spaces, capitals — prose, not an event)
    bad:  "Migrated: created table"      (punctuation/capitals)

The dotted-namespace allowance is deliberate: ~140 call sites in this codebase
intentionally namespace events as ``component.event`` (``http_taxonomy_store.init``,
``service.token.issue``). A strict single-token regex would flag every one of
them as a false positive against the dominant real convention (over 100 such
``component.event`` sites), so the rule is snake_case *per dot-delimited
segment*.

Receivers
---------
The scan matches level calls on (a) the canonical ``_log`` / ``log`` names, (b)
any ``*_log`` / ``*_logger`` per-component logger (``_hb_log``, ``_sweep_log``,
...), and (c) inline ``structlog.get_logger().<level>(...)`` calls. Suffix
matching is used rather than a fixed name list so a future ``_worker_log`` is
covered automatically, while non-loggers that merely end in ``log``
(``catalog``, ``response``) are NOT matched.

Baseline ratchet
----------------
A whole-``src`` scan found 84 pre-existing prose-message events (operational
logs in ``migrations.py`` / ``indexer.py`` and friends). Those events are read
by humans tailing migration / index runs and are de-facto monitoring surfaces,
so renaming all 84 is an operational-contract change well outside this P3 lint's
scope. Instead this gate GRANDFATHERS the existing 84 via a baseline ratchet
(``<= BASELINE``) and blocks NEW prose-message events — exactly "scope to new
code". When a grandfathered site is later fixed to a snake_case event, ratchet
``BASELINE`` down to lock the improvement in.

The ratchet is a global COUNT, so a change that fixes one site and adds a prose
event elsewhere nets to the same total and passes (the fix-one-add-one gap). It
is accepted as a known limit: it catches the common case (a naive new prose
event with no offsetting fix), and a per-file table would be brittle for little
real gain given the violations cluster in a few files.

Blind spots (pinned, not silent): a non-literal first argument (f-string,
variable) cannot be statically verified and is NOT flagged; a ``.log(level,
event, ...)`` form (event in the SECOND position) is not scanned; a two-step
aliased receiver (``s = some.logger; s.info("Prose")``) is not resolved.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

# structlog bound-logger level methods that take an event name as arg 0.
# ``.log`` is deliberately EXCLUDED — its arg 0 is the level, event is arg 1.
_LEVEL_METHODS: frozenset[str] = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical",
    "fatal", "msg",
})

# Receiver names that denote a module-level structlog logger in this codebase
# (``_log = structlog.get_logger(__name__)`` is the dominant form; ``log`` and
# a few capitalised variants also appear).
_LOGGER_NAMES: frozenset[str] = frozenset({
    "_log", "log", "_logger", "logger", "_LOG",
})


def _is_logger_receiver(recv: ast.expr) -> bool:
    """True if *recv* denotes a structlog logger we should lint.

    Covers three forms:
    - a bare name in :data:`_LOGGER_NAMES` (``_log``, ``log``, ...);
    - a name with a ``_log`` / ``_logger`` suffix (the per-component loggers
      ``_hb_log`` / ``_sweep_log`` / ``_sanitizer_log`` / ``_spawn_log`` and any
      future sibling), while NOT matching non-loggers like ``catalog`` /
      ``response`` that merely end in ``log``;
    - an inline ``structlog.get_logger().<level>(...)`` call receiver
      (``ast.Call`` whose func resolves to ``get_logger``).
    """
    if isinstance(recv, ast.Name):
        return (
            recv.id in _LOGGER_NAMES
            or recv.id.endswith("_log")
            or recv.id.endswith("_logger")
        )
    if isinstance(recv, ast.Call):
        f = recv.func
        if isinstance(f, ast.Attribute) and f.attr == "get_logger":
            return True
        if isinstance(f, ast.Name) and f.id == "get_logger":
            return True
    return False


_SEGMENT = re.compile(r"^[a-z][a-z0-9_]*$")

# Grandfathered prose-message events as of nexus-whh61.3 (AST count over src/).
# Ratchet DOWN as sites are fixed; never UP (a new prose event must fail CI).
SNAKE_CASE_EVENT_BASELINE = 78  # 81 -> 80: RDR-186 .16 retired pipeline_buffer.py ("WAL mode not available" site gone)


def _event_is_snake_case(event: str) -> bool:
    """True if *event* is a dotted chain of snake_case segments."""
    if not event:
        return False
    return all(_SEGMENT.match(seg) for seg in event.split("."))


def _event_name_violations(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (lineno, event) for non-snake_case literal event names in *path*.

    Only literal-string first arguments to ``<known_logger>.<level>(...)`` are
    considered; dynamic first args are skipped (documented blind spot).
    """
    try:
        source = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LEVEL_METHODS:
            continue
        if not _is_logger_receiver(func.value):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue  # dynamic first arg — cannot statically verify, skip
        if not _event_is_snake_case(first.value):
            violations.append((node.lineno, first.value))
    return violations


def _scan_src() -> dict[str, list[tuple[int, str]]]:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for py in SRC_ROOT.rglob("*.py"):
        hits = _event_name_violations(py)
        if hits:
            offenders[py.relative_to(REPO_ROOT).as_posix()] = hits
    return offenders


# ── Whole-src ratchet ────────────────────────────────────────────────────────


def test_no_new_prose_event_names_in_src() -> None:
    """nexus-whh61.3: the count of non-snake_case literal event names must not
    exceed the grandfathered baseline. A NEW prose-message event in any source
    file pushes the count above ``SNAKE_CASE_EVENT_BASELINE`` and fails here.

    Fix: give the event a snake_case key (dotted namespaces allowed, e.g.
    ``component.event``) and move the prose into a structured field.
    """
    offenders = _scan_src()
    total = sum(len(v) for v in offenders.values())
    assert total <= SNAKE_CASE_EVENT_BASELINE, (
        f"non-snake_case structlog event names rose to {total} "
        f"(baseline {SNAKE_CASE_EVENT_BASELINE}). New prose-message event(s) "
        f"must use a snake_case key. Offenders:\n" + "\n".join(
            f"  {path}:\n    " + "\n    ".join(
                f"line {ln}: {ev!r}" for ln, ev in hits
            )
            for path, hits in sorted(offenders.items())
        )
    )


def test_baseline_matches_current_count() -> None:
    """Pin the baseline to the exact current count so a fix (count drops) is
    noticed and the constant can be ratcheted down — and so the baseline can
    never silently drift UP without this exact-equality guard failing first."""
    total = sum(len(v) for v in _scan_src().values())
    assert total == SNAKE_CASE_EVENT_BASELINE, (
        f"genuine count is {total}, baseline constant is "
        f"{SNAKE_CASE_EVENT_BASELINE}. If you FIXED a site, lower the constant "
        f"to {total}. If you ADDED one, use a snake_case event key instead."
    )


# ── Rule unit tests ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", [
    "wal_mode_unavailable",
    "catalog_etl.start",
    "http_taxonomy_store.init",
    "service.token.issue",
    "x",
    "a1_b2.c3",
])
def test_snake_case_events_accepted(event: str) -> None:
    assert _event_is_snake_case(event)


@pytest.mark.parametrize("event", [
    "WAL mode not available",
    "Migrated: created table",
    "indexing code files",
    "CamelCase",
    "has-hyphen",
    "trailing.",
    ".leading",
    "double..dot",
    "1starts_with_digit",
    "",
])
def test_prose_events_rejected(event: str) -> None:
    assert not _event_is_snake_case(event)


# ── Synthetic offender / blind-spot self-tests ───────────────────────────────


def test_synthetic_prose_event_caught(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "offender.py"
    fake.write_text(
        "import structlog\n"
        "_log = structlog.get_logger(__name__)\n"
        "def bad():\n"
        "    _log.info('this is a prose message')\n"
    )
    hits = _event_name_violations(fake)
    assert len(hits) == 1
    assert hits[0][1] == "this is a prose message"


def test_synthetic_snake_event_not_flagged(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "good.py"
    fake.write_text(
        "import structlog\n"
        "_log = structlog.get_logger(__name__)\n"
        "def ok():\n"
        "    _log.warning('component.event_happened', detail='x')\n"
    )
    assert _event_name_violations(fake) == []


def test_dynamic_first_arg_is_not_flagged(tmp_path: pathlib.Path) -> None:
    """Documented blind spot: a non-literal first arg (f-string / variable)
    cannot be statically checked and must NOT produce a false positive."""
    fake = tmp_path / "dynamic.py"
    fake.write_text(
        "import structlog\n"
        "_log = structlog.get_logger(__name__)\n"
        "def f(name):\n"
        "    _log.info(f'event {name}')\n"
        "    _log.error(name)\n"
    )
    assert _event_name_violations(fake) == []


def test_non_logger_receiver_not_matched(tmp_path: pathlib.Path) -> None:
    """``other.info('Prose')`` where ``other`` is not a logger receiver must not
    match. ``response`` / ``catalog`` end in ``log``/``logger``-adjacent text
    but lack the ``_log`` / ``_logger`` suffix, so the matcher skips them."""
    fake = tmp_path / "other.py"
    fake.write_text(
        "def f(response, catalog):\n"
        "    response.info('Some Prose Here')\n"
        "    catalog.warning('More Prose')\n"
    )
    assert _event_name_violations(fake) == []


def test_component_logger_name_is_matched(tmp_path: pathlib.Path) -> None:
    """A per-component ``*_log`` logger (``_hb_log`` / ``_sweep_log`` / ...) is a
    recognised receiver, so a prose event on it IS flagged."""
    fake = tmp_path / "component.py"
    fake.write_text(
        "import structlog\n"
        "_sweep_log = structlog.get_logger(__name__)\n"
        "def f():\n"
        "    _sweep_log.info('Prose On Component Logger')\n"
    )
    hits = _event_name_violations(fake)
    assert len(hits) == 1
    assert hits[0][1] == "Prose On Component Logger"


def test_inline_get_logger_receiver_is_matched(tmp_path: pathlib.Path) -> None:
    """An inline ``structlog.get_logger().<level>(...)`` receiver IS scanned —
    this closed a live escape (corpus.py resolve_corpus) the named-only matcher
    missed."""
    fake = tmp_path / "inline.py"
    fake.write_text(
        "import structlog\n"
        "def f():\n"
        "    structlog.get_logger().debug('inline prose event')\n"
    )
    hits = _event_name_violations(fake)
    assert len(hits) == 1
    assert hits[0][1] == "inline prose event"


def test_log_method_with_level_first_is_not_scanned(tmp_path: pathlib.Path) -> None:
    """``.log(level, event, ...)`` carries the event in arg 1, not arg 0; it is
    deliberately out of scope and must not be flagged on its arg-0 level."""
    fake = tmp_path / "logcall.py"
    fake.write_text(
        "import logging, structlog\n"
        "_log = structlog.get_logger(__name__)\n"
        "def f():\n"
        "    _log.log(logging.INFO, 'Prose Event Here')\n"
    )
    assert _event_name_violations(fake) == []
