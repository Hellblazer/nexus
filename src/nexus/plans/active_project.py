# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 2 (nexus-jypmj.2.4, assumption A5): the active-project tier of named-source resolution.

A named source such as ``RDR-084`` can exist in several projects. The order that qualifies it is:

1. an explicit ``owner`` or ``project`` binding,
2. the owner scope (a catalog-subtree ``scope``),
3. **the active project**, the project of the session that made the call (this module).

``scope="rdr"`` alone is a corpus and never selects a project. With no reliable active project the
resolver reports an ambiguity naming every candidate; nothing is guessed.

An owner this tier supplies is ``inferred`` (:class:`~nexus.plans.evidence.OwnerResolution`), shown as
such in the envelope, and it qualifies **source resolution only**: it is not an owner binding, so on its own it
narrows no secondary stream (``owner_narrowing.request_owners`` reads explicit bindings only, nexus-jypmj.1.18).
The one-source rule (:mod:`nexus.plans.single_source`) may narrow a run to the document the inference chose, but
only when no other project carries the same number; with a foreign candidate the run is not narrowed and says why
(nexus-jypmj, Sam 2026-10-30).

What the server knows (the trace, bead nexus-jypmj.2.4). ``nx-mcp`` is a stdio server, one process per
Claude Code session. The plugin launcher moves it to ``CLAUDE_PROJECT_DIR`` before it execs the server,
so a server that Claude Code started has the project directory as its working directory and in its
environment. Another host starts the server in the plugin directory, or anywhere, with no such
variable, and one process may then serve several projects.

**Reliable** is therefore defined, and pinned in ``tests/test_rdr232_active_project.py``, as all of:

* ``CLAUDE_PROJECT_DIR`` is set, absolute, and an existing directory (the host named the project);
* the process working directory resolves to that same directory (a server that moved, or that is
  shared, disagrees with its own declaration);
* :func:`~nexus.catalog.rdr_canonical.current_rdr_owner` on that directory returns an owner, and the
  catalog has that owner registered.

Anything else is not reliable, and the reason goes in the envelope. In particular Claude Desktop and Cowork
start the server in the plugin directory with no ``CLAUDE_PROJECT_DIR``, so on those hosts the tier never
applies and a number that more than one project holds stays ambiguous: pass ``owner`` or ``project``. No path appears in the reason or in
the wire form. :func:`detect_active_project` does blocking I/O (git, two catalog reads) and never raises.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.plans.evidence import OwnerResolution, ScopeConstraint, scope_subtree

_log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVE_PROJECT_ORIGIN",
    "PROJECT_DIR_ENV",
    "ActiveProject",
    "ActiveProjectTier",
    "OWNER_NAMES_TTL_S",
    "REMEDY",
    "apply_active_project",
    "detect_active_project",
    "registered_owner_names",
    "reset_active_project_caches_for_tests",
]

#: The environment variable the host sets to the session's project directory.
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

#: The contribution origin of an inferred owner (``OwnerResolution.contributions``). It is not an explicit
#: origin, so ``owner_narrowing`` never counts it.
ACTIVE_PROJECT_ORIGIN = "active_project"

#: What to do when the session's project cannot be inferred. The same words are in the envelope's reason, the
#: ``bindings`` description of ``nx_answer``, ``docs/cli-reference.md`` and the CHANGELOG.
REMEDY = "pass owner or project (an owner name such as ART, or its tumbler) to choose"

#: How long a reliable detection and a read of the registered owner names are reused within one process
#: (nexus-jypmj.2 follow-up, code review S4). The environment and the working directory are stable for the life
#: of a server process, so the detection is keyed by them and by the catalog handle: a change of either is a new
#: key and a new lookup. The detection's lifetime is a safety bound on a catalog that changed under the process
#: (an owner re-registered); the owner names are kept for the short TTL because other sessions register owners.
DETECTION_TTL_S = 600.0
OWNER_NAMES_TTL_S = 60.0

#: The clock the caches age by; a test replaces it here, never ``time.monotonic`` itself.
_clock: Callable[[], float] = time.monotonic
_cache_lock = threading.Lock()
#: ``(key, catalog, value, stored at)`` of the last reliable detection; one entry, the process has one session.
_detection_cache: tuple[tuple[str, str], Any, "ActiveProject", float] | None = None
#: ``(catalog, names, stored at)`` of the last successful owner-name read.
_owner_names_cache: tuple[Any, frozenset[str], float] | None = None


def reset_active_project_caches_for_tests() -> None:
    """Drop both per-process caches (a test that reuses a catalog handle across states calls this)."""
    global _detection_cache, _owner_names_cache
    with _cache_lock:
        _detection_cache = None
        _owner_names_cache = None


#: An owner tumbler: two segments (``1.2``). Anything else the catalog returns is not an owner.
_OWNER_RE = re.compile(r"^\d+\.\d+$")

#: ``(catalog, project directory) -> owner tumbler or None``: the identity ``nx index rdr`` registers under.
OwnerOf = Callable[[Any, str], Any]


@dataclass(frozen=True)
class ActiveProject:
    """The session's project as the server can know it: ``reliable`` with its catalog ``owner`` tumbler and
    ``name``, or not reliable with the ``reason`` (free of paths)."""

    reliable: bool
    owner: str = ""
    name: str = ""
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"reliable": self.reliable, "owner": self.owner or None, "name": self.name or None, "reason": self.reason}


def _unreliable(reason: str) -> ActiveProject:
    return ActiveProject(False, reason=reason)


def _default_owner_of(cat: Any, root: str) -> Any:
    from nexus.catalog.rdr_canonical import current_rdr_owner  # noqa: PLC0415 — deferred: keeps this module's import light

    return current_rdr_owner(cat, root)


def detect_active_project(
    cat: Any = None,
    *,
    get_catalog: Callable[[], Any] | None = None,
    environ: Mapping[str, str] | None = None,
    getcwd: Callable[[], str] = os.getcwd,
    owner_of: OwnerOf = _default_owner_of,
) -> ActiveProject:
    """The session's project, if it can be known reliably (see the module docstring). Never raises.

    *environ*, *getcwd* and *owner_of* are the seams a test replaces; the defaults are the process's.
    The catalog is *cat*, or, when that is ``None``, the result of *get_catalog*, which is called only after
    the environment, the declared directory and the working directory have agreed: a host that named no
    project never touches the catalog.
    """
    try:
        return _detect(cat, get_catalog, os.environ if environ is None else environ, getcwd, owner_of)
    except Exception as exc:  # noqa: BLE001 — the disclosure stage never raises; an unexpected failure is "not reliable"
        _log.warning("active_project_detect_failed", error=type(exc).__name__)
        return _unreliable(f"the active project could not be determined ({type(exc).__name__})")


def _detect(
    cat: Any, get_catalog: Callable[[], Any] | None, environ: Mapping[str, str], getcwd: Callable[[], str],
    owner_of: OwnerOf,
) -> ActiveProject:
    global _detection_cache
    declared = str(environ.get(PROJECT_DIR_ENV, "") or "").strip()
    if not declared:
        return _unreliable(
            f"the host did not name the session's project ({PROJECT_DIR_ENV} is unset; Claude Desktop and Cowork "
            f"start the server without it, so the session's project is never inferred there): {REMEDY}"
        )
    if not os.path.isabs(declared):
        return _unreliable(f"the declared project directory is not an absolute path ({PROJECT_DIR_ENV})")
    if not os.path.isdir(declared):
        return _unreliable("the declared project directory does not exist")
    try:
        cwd = getcwd()
    except OSError:
        return _unreliable("the server's working directory cannot be read")
    if os.path.realpath(cwd) != os.path.realpath(declared):
        return _unreliable(
            "the server's working directory is not the declared project directory (a server that moved, or one "
            "shared across projects, cannot say which project this session is in)"
        )
    if cat is None and get_catalog is not None:
        try:
            cat = get_catalog()
        except Exception as exc:  # noqa: BLE001 — a missing catalog is "not reliable", not a failure
            _log.warning("active_project_catalog_unavailable", error=type(exc).__name__)
            cat = None
    if cat is None:
        return _unreliable("the catalog is unavailable, so the project's owner cannot be looked up")
    root = os.path.realpath(declared)
    # Reused only on the production path (the process's own environment, directory and owner lookup): a caller
    # that replaced one of those seams is testing the lookup and wants it made.
    cacheable = owner_of is _default_owner_of and environ is os.environ and getcwd is os.getcwd
    key = (declared, os.path.realpath(cwd))
    if cacheable:
        with _cache_lock:
            hit = _detection_cache
        if hit is not None and hit[0] == key and hit[1] is cat and _clock() - hit[3] < DETECTION_TTL_S:
            return hit[2]
    found = _lookup(cat, root, owner_of)
    if cacheable and found.reliable:
        with _cache_lock:
            _detection_cache = (key, cat, found, _clock())
    return found


def _lookup(cat: Any, root: str, owner_of: OwnerOf) -> ActiveProject:
    """The catalog half of the detection: *root*'s owner, and that owner's registry row."""
    try:
        owner = owner_of(cat, root)
    except Exception as exc:  # noqa: BLE001 — reported as the reason, never raised
        _log.warning("active_project_owner_lookup_failed", error=type(exc).__name__)
        return _unreliable(f"the catalog lookup of the project's owner failed ({type(exc).__name__})")
    if owner is None:
        return _unreliable("the project directory has no catalog owner (it is not an indexed repository)")
    tumbler = str(owner)
    if not _OWNER_RE.match(tumbler):
        return _unreliable("the catalog returned an owner that is not an owner tumbler")
    try:
        row = cat.get_owner_by_prefix(tumbler)
    except Exception as exc:  # noqa: BLE001 — reported as the reason, never raised
        _log.warning("active_project_owner_probe_failed", error=type(exc).__name__)
        return _unreliable(f"the catalog lookup of owner {tumbler} failed ({type(exc).__name__})")
    if not row:
        return _unreliable(f"owner {tumbler} is not in the catalog")
    return ActiveProject(True, owner=tumbler, name=str(row.get("name") or ""))


def registered_owner_names(cat: Any, *, ttl: float = 0.0) -> frozenset[str] | None:
    """The registered owner names, case-folded, or ``None`` when the registry cannot be read. Never raises.

    A name in the question counts as a project only if it is in this set (``nexus`` and ``Nexus`` both are, ``in
    detail`` is not). ``None`` is not an empty registry: :func:`apply_active_project` reads it as "no name can
    be vouched for" and declines to infer while the question has a word that could name a project. Blocking
    catalog I/O: call it from a worker thread.

    With *ttl* above zero a successful read of the same catalog handle is reused for that many seconds (one
    ``list_owners`` call per process per TTL, not one per question); a failed read is never kept.
    """
    global _owner_names_cache
    if cat is None:
        return None
    if ttl > 0:
        with _cache_lock:
            hit = _owner_names_cache
        if hit is not None and hit[0] is cat and _clock() - hit[2] < ttl:
            return hit[1]
    try:
        rows = cat.list_owners()
        names = frozenset(str(r.get("name") or "").strip().casefold() for r in rows if isinstance(r, Mapping)) - {""}
        if ttl > 0:
            with _cache_lock:
                _owner_names_cache = (cat, names, _clock())
        return names
    except Exception as exc:  # noqa: BLE001 — an unreadable registry is reported as None, never raised
        _log.warning("active_project_owner_names_failed", error=type(exc).__name__)
        return None


@dataclass(frozen=True)
class ActiveProjectTier:
    """What the tier decided: the ``owner`` resolution to use (the input unchanged unless ``applied``),
    whether it ``applied``, and the ``reason`` it did not (or what it applied)."""

    owner: OwnerResolution
    applied: bool
    reliable: bool
    project_owner: str
    project_name: str
    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "reliable": self.reliable,
            "owner": self.project_owner or None,
            "name": self.project_name or None,
            "reason": self.reason,
        }


def _decline(owner: OwnerResolution, active: ActiveProject, reason: str) -> ActiveProjectTier:
    return ActiveProjectTier(owner, False, active.reliable, active.owner, active.name, reason)


def apply_active_project(
    owner: OwnerResolution,
    active: ActiveProject,
    *,
    scope: str | None,
    project_names: Sequence[str] = (),
    registered: Collection[str] | None,
) -> ActiveProjectTier:
    """The third tier: the active project qualifies a request that no explicit tier qualified.

    *owner* is the explicit tiers' result (:func:`~nexus.plans.evidence.resolve_owner_qualifier`: the
    ``owner``/``project`` bindings and a subtree ``scope``). Any status but ``none`` stands as it is: a
    resolved qualifier outranks the active project, and a conflicting or unresolved one is never replaced
    by a looser one. A catalog-subtree *scope* is the owner-scope tier even when too shallow to qualify, so
    the active project is not consulted; a conformant collection-name *scope* (its owner is in its name) is
    likewise a scope the caller chose, and the tier steps aside. A corpus scope (``rdr``) is no project and
    changes nothing.

    *project_names* are the words the question's text puts where a project name could stand. The text is not a
    qualifier, but a registered project name that is not the active project's is evidence the session's
    project is not what the question means, so the inference is withheld and the ambiguity stands. A word counts
    only if it matches a name in *registered* (case-insensitively; ``in detail`` and ``Summarize`` are no
    project). *registered* has no default: ``None`` says the registry could not be read, and then, if the
    question has any word that could name a project, the inference is declined (``name check unavailable``)
    rather than made on a check that did not run. A caller that has already filtered passes the filtered names.
    """
    if owner.status != "none":
        return _decline(owner, active, "an explicit owner, project or owner-scope qualifier was given")
    if scope_subtree(scope) is not None:
        return _decline(owner, active, "a catalog-subtree scope was given; it is the owner scope")
    if ScopeConstraint.parse(scope).kind == "collection":
        return _decline(owner, active, "a collection-name scope was given; the collection names its own owner")
    if not active.reliable:
        return _decline(owner, active, active.reason)
    if not _OWNER_RE.match(active.owner):
        return _decline(owner, active, "the active project's owner is not an owner tumbler")
    words = [n.strip() for n in project_names if n.strip()]
    if words and registered is None:
        return _decline(
            owner, active,
            f"the question has {', '.join(repr(w) for w in dict.fromkeys(words))} where a project name could stand, "
            f"and the catalog's owner names could not be read to check it (name check unavailable), so the session's "
            f"project was not inferred; {REMEDY}",
        )
    known = {r.casefold() for r in registered or ()}
    named = [n for n in words if n.casefold() in known]
    if named and any(n.strip().casefold() != active.name.casefold() for n in named):
        return _decline(
            owner, active,
            f"the question names project {', '.join(repr(n) for n in dict.fromkeys(named))}, but the session's "
            f"project is {active.name!r}; {REMEDY}",
        )
    inferred = OwnerResolution(
        "inferred", active.owner, ((ACTIVE_PROJECT_ORIGIN, active.owner),),
        f"the session's project {active.name!r} (owner {active.owner}); no owner or project binding confirmed it",
    )
    return ActiveProjectTier(
        inferred, True, True, active.owner, active.name, f"inferred from the session's project {active.name!r}",
    )
