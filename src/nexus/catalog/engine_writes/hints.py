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


# ── repository hints (Gaps 2 and 3) and the census flag ─────────────────────────────────────────────────────────


def is_root_derived(source_uri: str, file_path: str) -> bool:
    """A repo file's row: a ``file://`` URI that ends in ``/<file_path>`` with a relative ``file_path`` (the rule the
    engine's owner move uses to decide which rows it rewrites)."""
    if not file_path or not source_uri.startswith("file://") or isabs(file_path):
        return False
    norm = normpath(file_path)
    return norm != "." and not norm.startswith("..") and source_uri.endswith("/" + norm)


def count_docs_outside_root(docs: Iterable[Any], repo_root: str) -> int:
    """How many of *docs* are root-derived and do not start with ``file://<repo_root>/``."""
    prefix = f"file://{repo_root.rstrip('/')}/"
    return sum(
        1 for d in docs
        if is_root_derived(d.source_uri or "", d.file_path or "") and not (d.source_uri or "").startswith(prefix)
    )


def first_page(reader: Any, owner_prefix: str) -> list[Any]:
    """The first :data:`OWNER_FIRST_PAGE` live documents of an owner, in tumbler order. One request; never a second."""
    return list(reader.owner_documents_page(owner_prefix, OWNER_FIRST_PAGE))


def docs_outside_root_flag(reader: Any, owner_prefix: str, repo_root: str) -> bool | None:
    """The census flag of one owner: ``True`` when its first page holds a root-derived row outside ``repo_root``,
    ``False`` when not, ``None`` when the page could not be read (fail-open: no flag, no line)."""
    page = fail_open("outside-root", first_page, reader, owner_prefix, default=None)
    if page is None or not repo_root:
        return None
    return count_docs_outside_root(page, repo_root) > 0


def rename_hint_lines(reader: Any, new_owner: str, new_root: str, page: list[Any],
                      vanished: list[str], budget: Budget) -> list[str]:
    """Owner-move lines for the vanished owners (prefixes in *vanished*) whose documents match those of *new_owner*.

    Takes the first :data:`RENAME_SAMPLE` documents of *page* that carry ``meta.content_hash``, asks the metadata route
    once per hash, and groups the hits by owner prefix. A vanished owner that has a live row for at least
    :data:`RENAME_MATCH_FRACTION` of the sampled hashes gets a line, highest match count first (ties in prefix order).
    Rows without a hash are not counted; no sampled hash means no line. May raise: callers wrap it in
    :func:`fail_open`."""
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
    for h in hashes:
        if budget.spent():
            _log.warning("move_hints.budget_spent", hint="rename", asked=len(hashes))
            return []
        rows, _count = reader.documents_by_meta(meta=[("content_hash", h)], limit=1000)
        owners = {_owner_prefix(r.tumbler) for r in rows if not r.alias_of}
        for prefix in owners & wanted:
            hits[prefix] = hits.get(prefix, 0) + 1
    need = len(hashes) * RENAME_MATCH_FRACTION
    ranked = sorted(((n, p) for p, n in hits.items() if n >= need), key=lambda t: (-t[0], t[1]))
    return [owner_move_line(p, new_root) for _n, p in ranked]


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
    if count_docs_outside_root(page, root) > 0:
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
    gains ``move_command`` (the owner move with the :data:`NEW_ROOT_PLACEHOLDER`, since its own root is gone) and
    any other flagged row gains ``move_command`` with its own root. Fail-open per owner: a page that cannot be read
    leaves that row with ``docs_outside_root = None`` and no line."""
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
            elif flag:
                row["move_command"] = owner_move_line(prefix, root)
