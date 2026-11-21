# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 Phase 3: move hints.

A hint is ONE line naming the exact ``nx catalog owners --execute move`` command, printed at the end of an index run
or in the owner census. A hint DETECTS and never acts: nothing here calls a write route, and every line is the
command's DRY-RUN form (no ``--no-dry-run``, no ``--confirm``), so the user sees the counts and the absorbed
tumblers before choosing to write (RDR-254 Technical Design, Move hints; Undo).

Three rules hold for every hint:

* FAIL OPEN. A hint is advisory. Every catalog query it makes runs inside :func:`fail_open`, which logs and drops an
  exception (a refusal, a timeout, an engine without the route) and returns the caller's default. A hint never
  changes an exit code and never stops an index run.
* BOUNDED. The renamed-repository hint stops asking when its time budget (:data:`HINT_BUDGET_S`, measured from when the
  hint starts) is spent. The curator hint, which runs inside the write path once per newly written file, asks about at
  most :data:`CURATOR_HINT_MAX_FILES` files per run, reads each owner's kind once per run, stops asking for the rest of
  the run after its first failed query, and shares one :data:`HINT_BUDGET_S` budget across the files that counts ONLY
  the seconds spent inside its own catalog queries: extraction, chunking and embedding before the first stamp, and the
  time between the files of a ``--dir`` batch, never spend it. A run that did not check every file ends its hints with
  one line saying how many it checked.
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
import re
import shlex
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from posixpath import isabs, normpath
from typing import Any, TypeVar

import structlog

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

#: The documents of an owner a hint reads: the engine's own default page of ``GET /list``. The census flag and the
#: renamed-repository sample both come from this one page and never read a second one.
OWNER_FIRST_PAGE: int = 200
#: How many of the new owner's documents (in tumbler order, those that carry ``meta.content_hash``) the
#: renamed-repository hint asks the metadata route about. A starting value to tune on a real renamed repository.
RENAME_SAMPLE: int = 20
#: A vanished owner matches when at least this fraction of the sampled hashes also have a live row under it.
RENAME_MATCH_FRACTION: float = 0.5
#: ... and for at least this many of them: one hashed document that also sits under a vanished owner is not a rename.
RENAME_MIN_MATCHES: int = 2
#: One hint's whole query budget, in seconds: the renamed-repository hint's, and for the curator hint the whole run's
#: (every file of a ``--dir`` batch together). A hint that spends it stops asking and drops what it has not read.
HINT_BUDGET_S: float = 30.0
#: The most files one run's curator hint asks about (each costs up to three requests, the first of an owner four).
CURATOR_HINT_MAX_FILES: int = 50
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


class Budget:
    """A hint's query time. ``spent()`` turns true once :attr:`seconds` have passed; a loop of queries checks it
    between queries and stops (the hint drops what it has not read)."""

    def __init__(self, seconds: float = HINT_BUDGET_S, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._deadline = clock() + seconds

    def spent(self) -> bool:
        return self._clock() >= self._deadline


class HintCollector:
    """The hint lines of one run, in the order recorded, each once, and the run's bound on what the curator hint may
    ask: a budget of query time, a cap on the files it asks about, and a stop after the first failed query. Thread-safe:
    the uploader of a streaming PDF run records from its own thread.

    The budget is the SUM of the seconds spent inside the curator hint's catalog queries (:meth:`timed`), not the time
    since the scope opened: a run whose extraction takes minutes before its first stamp, or a ``--dir`` batch that
    spends minutes between files, has spent none of it."""

    def __init__(self, budget_s: float = HINT_BUDGET_S, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._budget_s = budget_s
        self._clock = clock
        self._query_s = 0.0
        self._asked = 0
        self._refused = 0
        self._checked = 0
        self._stopped = False
        self._unsupported = False
        self._owner_is_curator: dict[str, bool] = {}

    def admit(self) -> bool:
        """May the curator hint ask about one more file? No once a query failed, the query-time budget is spent, or
        :data:`CURATOR_HINT_MAX_FILES` files were asked about. Counts the file it admits."""
        with self._lock:
            if self._stopped or self._query_s >= self._budget_s or self._asked >= CURATOR_HINT_MAX_FILES:
                self._refused += 1
                return False
            self._asked += 1
            return True

    def checked(self) -> None:
        """The curator hint read the catalog for one admitted file to the end (a failed query is not a check)."""
        with self._lock:
            self._checked += 1

    def stop(self) -> None:
        """A query failed: the curator hint asks about no further file this run."""
        with self._lock:
            self._stopped = True

    def unsupported(self) -> None:
        """The engine reports no move routes: the run prints no hint output at all, the closing note included."""
        with self._lock:
            self._unsupported = True

    def budget_spent(self) -> bool:
        """Has the curator hint spent its query-time budget?"""
        with self._lock:
            return self._query_s >= self._budget_s

    @contextlib.contextmanager
    def timed(self) -> Iterator[None]:
        """Count the seconds spent inside the block against the query-time budget."""
        started = self._clock()
        try:
            yield
        finally:
            elapsed = max(self._clock() - started, 0.0)
            with self._lock:
                self._query_s += elapsed

    def owner_is_curator(self, prefix: str, lookup: Callable[[str], bool]) -> bool:
        """Whether owner *prefix* is a curator owner, read once per run (*lookup* is called on a first sight)."""
        with self._lock:
            known = self._owner_is_curator.get(prefix)
        if known is None:
            known = lookup(prefix)
            with self._lock:
                self._owner_is_curator[prefix] = known
        return known

    def add(self, line: str) -> None:
        with self._lock:
            if line not in self._lines:
                self._lines.append(line)

    def drain(self) -> list[str]:
        """The recorded lines, once: the collector is empty afterwards, so a second drain prints nothing. When the curator
        hint refused any file (the cap, the spent budget, or a stop after a failed query) the lines end with one line
        saying how many of the run's files it checked. It is a note, not a command: it names no write form. An engine
        without the move routes gets no note (the probe-first rule)."""
        with self._lock:
            lines, self._lines = self._lines, []
            if self._refused and not self._unsupported:
                total = self._asked + self._refused
                lines.append(f"move hints checked for {self._checked} of {total} files; "
                             f"run nx index pdf --force on a moved file to check it again")
            self._asked = self._refused = self._checked = 0
        return lines


_scope_lock = threading.Lock()
_open_collector: HintCollector | None = None


def active_collector() -> HintCollector | None:
    """The collector of the open :func:`run_scope`, or ``None`` when no run is collecting."""
    with _scope_lock:
        return _open_collector


@contextlib.contextmanager
def run_scope(budget_s: float = HINT_BUDGET_S, *, clock: Callable[[], float] = time.monotonic) -> Iterator[HintCollector]:
    """Open the collector for one command run, with *budget_s* seconds of query time for its curator hint (the time it
    spends inside its own catalog queries, not the time since the scope opened). A nested scope reuses the outer one
    (the outer run prints)."""
    global _open_collector
    with _scope_lock:
        outer = _open_collector
        collector = outer if outer is not None else HintCollector(budget_s, clock=clock)
        _open_collector = collector
    try:
        yield collector
    finally:
        if outer is None:
            with _scope_lock:
                _open_collector = None


def prints_hints(command: Callable[..., _T]) -> Callable[..., _T]:
    """Decorate a click command body: open a :func:`run_scope`, and print the hints it collected, one per line, after its
    own output.

    The hints are printed when the command returns AND when it raises a ``click.ClickException`` (a ``--dir`` batch
    that ends ``N of M failed``): they describe writes that already completed, and the curator hint is not repeated on
    a later run, so dropping them with the error would lose them for good. They print before the error message. Any
    other exception (a bug, an interrupt) prints none: it says nothing is known about what was written. Apply it
    directly above the ``def``, under the ``click`` decorators."""

    @functools.wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        import click  # noqa: PLC0415 — deferred: the catalog layer stays importable without click

        with run_scope() as collector:
            try:
                result = command(*args, **kwargs)
            except click.ClickException:
                echo_hints(collector.drain())
                raise
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


def curator_hint_lines(reader: Any, doc_id: str, content_hash: str, collector: HintCollector | None = None) -> list[str]:
    """The move lines for document *doc_id*, a curator document the writer has just stamped with *content_hash*.

    Asks the metadata route for the live rows with that hash, keeps those under D's owner prefix other than D that
    are not aliases and name a file that is absent on this machine, and returns one document-move line per such row
    (D's path is the new path). A row whose file is present is a copy and gets no line (nexus-o19i0); a row with no
    ``meta.content_hash`` is not in the answer at all. "Absent on this machine" is read as "moved": a row of a
    catalog shared with another machine, or on an unmounted volume, is named too (the line is a dry run).
    Up to three requests (the document, its owner's kind, the metadata page); the owner's kind is read once per run
    when *collector* is given, and the run's time budget is checked between requests. May raise: callers wrap it in
    :func:`fail_open`."""
    if not engine_supports_moves(reader):
        if collector is not None:
            collector.unsupported()
        return []
    here = reader.resolve(doc_id)
    if here is None or getattr(here, "alias_of", "") or not here.file_path or not Path(here.file_path).is_absolute():
        return []
    owner = _owner_prefix(here.tumbler)

    def _is_curator(prefix: str) -> bool:
        row = reader.get_owner_by_prefix(prefix)
        return bool(row) and row.get("owner_type") == "curator"

    is_curator = collector.owner_is_curator(owner, _is_curator) if collector is not None else _is_curator(owner)
    if not is_curator:
        return []  # an in-repo file: the repository hints cover it
    if collector is not None and collector.budget_spent():
        return []
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
    (and queries nothing) when no :func:`run_scope` is open. Fail-open, and bounded per run: it asks about at most
    :data:`CURATOR_HINT_MAX_FILES` files, stops when the seconds spent in its own queries reach :data:`HINT_BUDGET_S`
    (time spent elsewhere in the run does not count), and stops for the rest of the run after the first query that
    fails."""
    collector = active_collector()
    if collector is None or not doc_id or not content_hash or not collector.admit():
        return

    def _lines() -> list[str]:
        return curator_hint_lines(_reader(reader), doc_id, content_hash, collector)

    with collector.timed():
        lines = fail_open("curator", _lines, default=None)
    if lines is None:
        collector.stop()
        return
    collector.checked()
    for line in lines:
        collector.add(line)


# ── repository hints (Gaps 2 and 3) and the census flag ─────────────────────────────────────────────────────────


def derived_root_uri(file_path: str, source_uri: str, repo_root: str) -> str | None:
    """The URI a fresh registration at *repo_root* gives a row, or ``None`` when the engine's owner move would skip it.

    This is ``CatalogMoves.rootDerivedUri`` with ``CatalogRepository.deriveSourceUri``, on the engine's POSIX host (the
    table in ``tests/catalog/test_move_hints_root_parity.py`` evaluates the Java by hand, and a tripwire test fails when
    a clause of it changes). A row is root-derived when its ``source_uri`` is ``file://`` and ends in ``/<norm>``, where
    *norm* is the normalized, relative, non-blank ``file_path`` that does not start with ``..``. The derived URI is
    ``file://`` plus the root joined with *norm* and normalized. A root that is not a POSIX absolute path (a Windows
    ``C:\\repo``, a relative path, an empty one) derives nothing, as on the engine, whose move skips every row then."""
    if not file_path.strip() or not source_uri.startswith("file://") or "\x00" in file_path or isabs(file_path):
        return None
    norm = normpath(file_path)
    if norm == "." or norm.startswith("..") or not source_uri.endswith("/" + norm):
        return None
    if not isabs(repo_root) or "\x00" in repo_root:
        return None
    joined = re.sub(r"^/+", "/", normpath(f"{repo_root}/{norm}"))
    return "file://" + joined


def count_docs_outside_root(docs: Iterable[Any], repo_root: str) -> int | None:
    """How many of *docs* the engine's owner move would REWRITE at *repo_root*: root-derived rows whose current
    ``source_uri`` differs from :func:`derived_root_uri`. An entry of an unexpected shape (a non-string field) is
    skipped and logged, so one malformed row costs its own count, not the whole answer. When EVERY entry of a
    non-empty page is malformed nothing was read, so the answer is ``None`` (unknown), never a count of zero."""
    n = read = total = 0
    for d in docs:
        total += 1
        try:
            uri = d.source_uri or ""
            derived = derived_root_uri(d.file_path or "", uri, repo_root)
        except Exception as exc:  # noqa: BLE001 — one malformed catalog entry must not cost the hint or the run
            _log.warning("move_hints.entry_skipped", error_type=type(exc).__name__, error=str(exc))
            continue
        read += 1
        if derived is not None and derived != uri:
            n += 1
    return None if total and not read else n


def first_page(reader: Any, owner_prefix: str) -> list[Any]:
    """The first :data:`OWNER_FIRST_PAGE` live documents of an owner, in tumbler order. One request; never a second."""
    return list(reader.owner_documents_page(owner_prefix, OWNER_FIRST_PAGE))


def docs_outside_root_flag(reader: Any, owner_prefix: str, repo_root: str) -> bool | None:
    """The census flag of one owner: ``True`` when its first page holds a root-derived row outside ``repo_root``,
    ``False`` when not, ``None`` when the page could not be read (fail-open: no flag, no line)."""
    page = fail_open("outside-root", first_page, reader, owner_prefix, default=None)
    if page is None or not repo_root:
        return None
    count = fail_open("outside-root-count", count_docs_outside_root, page, repo_root, default=None)
    return None if count is None else count > 0


def rename_hint_lines(reader: Any, new_owner: str, new_root: str, page: list[Any],
                      vanished: list[str], budget: Budget) -> list[str]:
    """Owner-move lines for the vanished owners (prefixes in *vanished*) whose documents match those of *new_owner*.

    Takes the first :data:`RENAME_SAMPLE` documents of *page* that carry ``meta.content_hash``, asks the metadata route
    once per hash, and groups the hits by owner prefix. A vanished owner that has a live row for at least
    :data:`RENAME_MATCH_FRACTION` of the hashes asked, and for at least :data:`RENAME_MIN_MATCHES` of them (one hashed
    document and one match is a coincidence, not a rename), gets a line, highest match count first; ties are broken in
    tumbler order (``1.9`` before ``1.10``). When the budget runs out the hashes not yet asked are dropped and the
    owners are judged on the ones asked, so the matches already found are kept. Rows without a hash are not counted;
    no sampled hash means no line. May raise: callers wrap it in :func:`fail_open`."""
    hashes: list[str] = []
    for d in page:
        h = (getattr(d, "meta", None) or {}).get("content_hash")
        if isinstance(h, str) and h and h not in hashes:
            hashes.append(h)
        if len(hashes) >= RENAME_SAMPLE:
            break
    if not hashes or not vanished:
        return []
    wanted = set(vanished)
    hits: dict[str, int] = {}
    asked = 0
    for h in hashes:
        if budget.spent():
            _log.warning("move_hints.budget_spent", hint="rename", asked=asked, sampled=len(hashes))
            break
        rows, _count = reader.documents_by_meta(meta=[("content_hash", h)], limit=1000)
        asked += 1
        owners = {_owner_prefix(r.tumbler) for r in rows if not r.alias_of}
        for prefix in owners & wanted:
            hits[prefix] = hits.get(prefix, 0) + 1
    need = max(RENAME_MIN_MATCHES, asked * RENAME_MATCH_FRACTION)
    ranked = sorted(((n, p) for p, n in hits.items() if n >= need), key=lambda t: (-t[0], _tumbler_key(t[1])))
    return [owner_move_line(p, new_root) for _n, p in ranked]


def _tumbler_key(prefix: str) -> tuple[int, ...]:
    """Sort key of an owner prefix in tumbler order: ``1.9`` before ``1.10``."""
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred: a leaf import kept out of the load path

    return Tumbler.parse(prefix).segments


def vanished_owner_prefixes(reader: Any, *, exclude: str = "") -> list[str]:
    """Prefixes of the active repo owners whose recorded root is gone on this machine (``path_vanished``)."""
    from nexus.commands.catalog_cmds.owners import _classify_owner_root  # noqa: PLC0415 — deferred: the command module is heavy

    out: list[str] = []
    for o in reader.list_owners_by_type("repo"):
        prefix, root = str(o.get("tumbler_prefix") or ""), o.get("repo_root") or ""
        if prefix and prefix != exclude and root and not o.get("deactivated_at") and _classify_owner_root(root) == "path_vanished":
            out.append(prefix)
    return out


def repo_hints(repo: Path, *, reader: Any = None) -> list[str]:
    """The hint lines for the end of ``nx index repo`` of *repo*: the documents-outside-root line for the repository's
    owner, then a renamed-repository line for each vanished owner whose documents match. Reads durable catalog state, so
    it repeats on every run until the move is made. Each hint sits in its own :func:`fail_open`, so one that cannot be
    read drops alone; the function returns ``[]`` when the owner itself cannot be found."""

    def _owner() -> tuple[Any, str, str, list[Any]] | None:
        r = _reader(reader)
        if not engine_supports_moves(r):
            return None
        from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415 — deferred: the identity helpers run a subprocess

        _name, repo_hash, _main = _repo_identity_with_main(repo)
        owner = r.owner_for_repo(repo_hash)
        if owner is None:
            return None
        prefix = str(owner)
        root = (r.get_owner_by_prefix(prefix) or {}).get("repo_root") or ""
        if not root:
            return None
        return r, prefix, root, first_page(r, prefix)

    found = fail_open("repo-owner", _owner, default=None)
    if found is None:
        return []
    r, prefix, root, page = found
    lines: list[str] = []
    if (fail_open("outside-root-count", count_docs_outside_root, page, root, default=None) or 0) > 0:
        lines.append(owner_move_line(prefix, root))

    def _renamed() -> list[str]:
        vanished = vanished_owner_prefixes(r, exclude=prefix)
        return rename_hint_lines(r, prefix, root, page, vanished, Budget()) if vanished else []

    return lines + fail_open("rename", _renamed, default=[])


def annotate_census(reader: Any, classes: dict[str, list[dict]]) -> None:
    """Add the move fields to the census rows of ``nx catalog owners --census`` (RDR-254, Census), in place.

    Each row (``{"tumbler", "repo_root", ...}``, grouped by root class) gains ``docs_outside_root``: ``True``/``False``
    from the owner's first page of documents (one ``/list`` request each, never a second page), or ``None`` when the
    page could not be read. The flag is information beside the root class and never a class: it does not decide
    which owners the deactivate arm may act on. When the engine reports the move routes, a ``path_vanished`` row also
    gains ``move_command`` (the owner move with the :data:`NEW_ROOT_PLACEHOLDER`, since its own root is gone) and a
    flagged ``healthy`` row gains ``move_command`` with its own root. A flagged row of any other class (a root that is
    not a git work tree, empty, or unreadable) keeps the flag and gets no line: ``--execute move --owner`` refuses such
    a root. Fail-open per owner: a page that cannot be read leaves that row with ``docs_outside_root = None`` and no
    line."""
    supported = engine_supports_moves(reader)
    for cls, rows in classes.items():
        for row in rows:
            prefix, root = str(row.get("tumbler") or ""), row.get("repo_root") or ""
            flag = docs_outside_root_flag(reader, prefix, root) if prefix else None
            row["docs_outside_root"] = flag
            if not supported or not prefix:
                continue
            if cls == "path_vanished":
                row["move_command"] = owner_move_line(prefix)
            elif flag and cls == "healthy":
                row["move_command"] = owner_move_line(prefix, root)
