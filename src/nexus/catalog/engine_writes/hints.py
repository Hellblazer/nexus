# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 Phase 3: move hints.

A hint is ONE line naming the exact ``nx catalog owners --execute move`` command, printed at the end of an index run
or in the owner census. A hint DETECTS and never acts: nothing here calls a write route, and every line is the
command's DRY-RUN form (no ``--no-dry-run``, no ``--confirm``), so the user sees the counts and the absorbed
tumblers before choosing to write (RDR-254 Technical Design, Move hints; Undo).

Three rules hold for every hint:

* FAIL OPEN. A hint is advisory. Every catalog query it makes runs inside :func:`fail_open`, which logs and drops an
  exception (a refusal, a timeout, an engine without the route) and a spent time budget, and returns the caller's
  default. A hint never changes an exit code and never stops an index run.
* PROBE FIRST. No query is made, and no line is printed, unless the engine reports the move routes
  (:func:`engine_supports_moves`): a client never prints a command its engine refuses.
* NOT A SINGLETON. The curator hint is recorded from deep inside the single-file writers
  (:func:`nexus.catalog.multi_batch_write.stamp_registered_content_hash`), which have no handle on the command. The
  command opens a :func:`run_scope`; the helper records into the collector of the open scope and does nothing when
  none is open (so a library caller, or an in-repo file, queries nothing).

The module sits in this subpackage because ``nexus/catalog/`` is at its module cap (and not as ``move_hints.py``,
which would make a ``move`` family beside ``move.py``)."""
from __future__ import annotations

import contextlib
import functools
import shlex
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import structlog

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

#: One hint's whole query budget, in seconds. A hint that spends it stops asking and drops what it has not read.
HINT_BUDGET_S: float = 30.0
#: The placeholder a census row prints where the new root is not known.
NEW_ROOT_PLACEHOLDER: str = "<new root>"

_MOVE_COMMAND = "nx catalog owners --execute move"


# ── the lines ───────────────────────────────────────────────────────────────────────────────────────────────────


def document_move_line(tumbler: str, new_path: str) -> str:
    """The DRY-RUN form of the document move. Every argument goes through ``shlex.quote``, so a path with a space or a
    quote pastes into a shell as the one argument it is. Never carries ``--no-dry-run`` or ``--confirm``."""
    return f"{_MOVE_COMMAND} --document {shlex.quote(str(tumbler))} --new-path {shlex.quote(new_path)}"


def owner_move_line(owner: str, new_root: str | None = None) -> str:
    """The DRY-RUN form of the owner move. ``new_root=None`` prints the literal :data:`NEW_ROOT_PLACEHOLDER` (an
    angle-bracketed word is not a path, and a shell would read it as a redirect, which is what stops a user pasting
    it unedited)."""
    root = NEW_ROOT_PLACEHOLDER if new_root is None else shlex.quote(new_root)
    return f"{_MOVE_COMMAND} --owner {shlex.quote(str(owner))} --new-root {root}"


# ── the collector ───────────────────────────────────────────────────────────────────────────────────────────────


class HintCollector:
    """The hint lines of one run, in the order recorded, each once. Thread-safe: the uploader of a streaming PDF run
    records from its own thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def add(self, line: str) -> None:
        with self._lock:
            if line not in self._lines:
                self._lines.append(line)

    def drain(self) -> list[str]:
        """The recorded lines, once: the collector is empty afterwards, so a second drain prints nothing."""
        with self._lock:
            lines, self._lines = self._lines, []
        return lines


_scope_lock = threading.Lock()
_open_collector: HintCollector | None = None


def active_collector() -> HintCollector | None:
    """The collector of the open :func:`run_scope`, or ``None`` when no run is collecting."""
    with _scope_lock:
        return _open_collector


@contextlib.contextmanager
def run_scope() -> Iterator[HintCollector]:
    """Open the collector for one command run. A nested scope reuses the outer one (the outer run prints)."""
    global _open_collector
    with _scope_lock:
        outer = _open_collector
        collector = outer if outer is not None else HintCollector()
        _open_collector = collector
    try:
        yield collector
    finally:
        if outer is None:
            with _scope_lock:
                _open_collector = None


def prints_hints(command: Callable[..., _T]) -> Callable[..., _T]:
    """Decorate a click command body: open a :func:`run_scope`, and when the command returns normally print the
    hints it collected, one per line, after its own output. A command that raises prints none (its error is the
    message). Apply it directly above the ``def``, under the ``click`` decorators."""

    @functools.wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        with run_scope() as collector:
            result = command(*args, **kwargs)
            echo_hints(collector.drain())
            return result

    return wrapper


def echo_hints(lines: Iterable[str]) -> None:
    """Print *lines* (a hint is one line each) through ``click.echo``. Fail-open: a closed stream is not a failed run."""
    import click  # noqa: PLC0415 — deferred: the catalog layer stays importable without click

    try:
        for line in lines:
            click.echo(line)
    except Exception:  # noqa: BLE001 — an advisory line must never fail the run it follows
        _log.warning("move_hints.echo_failed", exc_info=True)


# ── fail-open, budget, probe ────────────────────────────────────────────────────────────────────────────────────


class Budget:
    """A hint's query time. ``spent()`` turns true once :attr:`seconds` have passed; a loop of queries checks it
    between queries and stops (the hint drops what it has not read)."""

    def __init__(self, seconds: float = HINT_BUDGET_S, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._deadline = clock() + seconds

    def spent(self) -> bool:
        return self._clock() >= self._deadline


def fail_open(label: str, fn: Callable[..., _T], *args: Any, default: _T, **kwargs: Any) -> _T:
    """Call ``fn(*args, **kwargs)`` and return its value; on ANY exception (a refused or failed query, a timeout, an
    engine without the route) log it under *label* and return *default*. The one wrapper every hint query sits in."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the contract of this function: a hint never fails its run
        _log.warning("move_hints.query_dropped", hint=label, error_type=type(exc).__name__, error=str(exc))
        return default


def engine_supports_moves(reader: Any) -> bool:
    """Does the engine report the move routes (``catalog_move_supported`` a JSON ``true`` on ``GET /version``)? A handle
    with no probe, a failed probe and an edge-trimmed answer all read False."""
    probe = getattr(reader, "supports_catalog_move", None)
    if not callable(probe):
        return False
    return fail_open("probe", probe, default=False) is True


def _owner_prefix(tumbler: Any) -> str:
    """``1.2`` of ``1.2.7``: the owner prefix of a document tumbler."""
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred: a leaf import kept out of the load path

    return str(Tumbler.parse(str(tumbler)).owner_address())


def _reader(reader: Any) -> Any:
    if reader is not None:
        return reader
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: the factory pulls in the service handles

    return make_catalog_reader()


# ── curator hint (Gap 1) ────────────────────────────────────────────────────────────────────────────────────────


def curator_hint_lines(reader: Any, doc_id: str, content_hash: str) -> list[str]:
    """The move lines for document *doc_id*, a curator document the writer has just stamped with *content_hash*.

    Asks the metadata route for the live rows with that hash, keeps those under D's owner prefix other than D that
    are not aliases and name a file that is absent on this machine, and returns one document-move line per such row
    (D's path is the new path). A row whose file is present is a copy and gets no line (nexus-o19i0); a row with no
    ``meta.content_hash`` is not in the answer at all. May raise: callers wrap it in :func:`fail_open`."""
    if not engine_supports_moves(reader):
        return []
    here = reader.resolve(doc_id)
    if here is None or getattr(here, "alias_of", "") or not here.file_path or not Path(here.file_path).is_absolute():
        return []
    owner = _owner_prefix(here.tumbler)
    row = reader.get_owner_by_prefix(owner)
    if not row or row.get("owner_type") != "curator":
        return []  # an in-repo file: the repository hints cover it
    matches, _count = reader.documents_by_meta(meta=[("content_hash", content_hash)], limit=1000)
    lines: list[str] = []
    for old in matches:
        if str(old.tumbler) == str(here.tumbler) or _owner_prefix(old.tumbler) != owner or old.alias_of:
            continue
        if not old.file_path or Path(old.file_path).exists():
            continue
        lines.append(document_move_line(str(old.tumbler), here.file_path))
    return lines


def record_curator_hint(doc_id: str, content_hash: str, *, reader: Any = None) -> None:
    """Record the curator hints of a document the writer has just stamped, into the open run's collector. Does nothing
    (and queries nothing) when no :func:`run_scope` is open. Fail-open."""
    collector = active_collector()
    if collector is None or not doc_id or not content_hash:
        return

    def _lines() -> list[str]:
        return curator_hint_lines(_reader(reader), doc_id, content_hash)

    for line in fail_open("curator", _lines, default=[]):
        collector.add(line)
