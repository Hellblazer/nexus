# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-store backend-selection flag for RDR-152 transition rollback.

Resolves which storage backend is authoritative for reads/writes for a given
T2/T1 domain store.  The two backends are:

- ``sqlite``  — current SQLite + T2 daemon path (default everywhere)
- ``service`` — new HTTP->Java->Postgres path (enabled per store as each bead
                migrates the store; see the nexus-gmiaf.7+ beads)

Resolution precedence (narrowest wins):
  1. Per-store env var  ``NX_STORAGE_BACKEND_<STORE>=service|sqlite``
  2. Global env var     ``NX_STORAGE_BACKEND=service|sqlite``
  3. Hard default       ``'sqlite'``

A config-file layer is reserved for Phase 2+ when the service is in broader
use; it is not wired in this bead (nexus-gmiaf.4) to keep the seam minimal.

Namespace note
--------------
The env var prefix ``NX_STORAGE_BACKEND`` (not ``NX_STORAGE_MODE``) is
intentional.  The legacy ``NX_STORAGE_MODE`` env var is already in use by
``nexus.config.storage_mode()`` with completely different semantics
(``daemon|direct``; RDR-120).  Using the same name would cause an operator
with ``NX_STORAGE_MODE=daemon`` in their environment to get a
``StorageModeFlagError`` from the new resolver.  ``NX_STORAGE_BACKEND``
is the RDR-152 namespace; ``NX_STORAGE_MODE`` is the RDR-120 namespace.

COPY-NOT-MOVE invariant
-----------------------
Flipping a store back to ``sqlite`` works because the ETL always *copies* data
to Postgres -- it never deletes from SQLite until Phase 4 decommission.
``storage_backend_for(store)`` is therefore a pure routing switch with no
data-lifecycle side effects.

Invalid values raise :exc:`StorageModeFlagError` immediately.  There are no
silent fallbacks (per the project's no-silent-fallback rule).

Usage (in a store factory, e.g. nexus-gmiaf.7)::

    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    if storage_backend_for("memory") == StorageBackend.SERVICE:
        return HttpMemoryStore(...)
    else:
        return MemoryStore(path)
"""
from __future__ import annotations

import os
from enum import Enum


class StorageBackend(str, Enum):
    """The two valid storage backends for a domain store."""

    SQLITE = "sqlite"
    SERVICE = "service"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StorageBackend):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other.lower()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


#: All domain store names that the flag mechanism recognises.
#:
#: Matches the *eagerly constructed* attributes on
#: :class:`nexus.db.t2.T2Database` plus ``catalog`` (lazily constructed via
#: the ``catalog`` property) and ``t1`` (the T1 scratch tier, which gains
#: Postgres backing in a later bead but is NOT a T2Database attribute).
#:
#: The canonical lower-case store identifier maps to the env var
#: ``NX_STORAGE_BACKEND_<UPPER>`` (e.g. ``NX_STORAGE_BACKEND_MEMORY``).
#:
#: Drift guard: :func:`test_valid_store_names_covers_t2database_attributes`
#: in ``tests/db/test_storage_mode.py`` enumerates T2Database's live domain-
#: store attributes and asserts they are all present here, so new stores added
#: to T2Database will fail that test until this set is updated.
VALID_STORE_NAMES: frozenset[str] = frozenset(
    {
        "memory",
        "plans",
        "taxonomy",
        "telemetry",
        "chash_index",
        "document_aspects",
        "document_highlights",
        "aspect_queue",
        "catalog",
        "t1",  # forward-declared: T1 scratch gains Postgres backing in a later bead
    }
)

#: Accepted backend value strings (case-insensitive comparison applied).
_VALID_BACKENDS: frozenset[str] = frozenset({"sqlite", "service"})

#: Global env-var name (no per-store suffix).  ``NX_STORAGE_BACKEND`` to avoid
#: collision with the legacy ``NX_STORAGE_MODE`` (RDR-120, daemon|direct).
_GLOBAL_ENV: str = "NX_STORAGE_BACKEND"

#: Format string for per-store env vars.  ``{store}`` is upper-cased at call time.
_PER_STORE_ENV_FMT: str = "NX_STORAGE_BACKEND_{store}"


class StorageModeFlagError(ValueError):
    """Raised when an env var contains an invalid storage backend value, or
    when an unknown store name is passed to :func:`storage_backend_for`.

    Inherits :class:`ValueError` so callers can catch it without a nexus import
    if they only use the Python exceptions API (e.g. in tests or scripts that
    do not import from nexus.db.storage_mode directly).
    """


def storage_backend_for(store: str) -> StorageBackend:
    """Return the authoritative storage backend for *store*.

    Resolution precedence (narrowest wins):
      1. Per-store env var ``NX_STORAGE_BACKEND_<STORE>`` (after upper-casing *store*)
      2. Global env var    ``NX_STORAGE_BACKEND``
      3. Hard default      :attr:`StorageBackend.SQLITE`

    Parameters
    ----------
    store:
        Lower-case (or any-case) domain store name.  Must be a member of
        :data:`VALID_STORE_NAMES` (case-insensitive match).

    Raises
    ------
    StorageModeFlagError
        If *store* is not in :data:`VALID_STORE_NAMES`, or if any relevant
        env var contains a value that is neither ``"sqlite"`` nor ``"service"``.
    """
    canonical = store.lower()
    if canonical not in VALID_STORE_NAMES:
        raise StorageModeFlagError(
            f"unknown store {store!r}: must be one of "
            f"{sorted(VALID_STORE_NAMES)}"
        )

    # 1. Per-store env var: NX_STORAGE_BACKEND_<STORE>
    per_store_key = _PER_STORE_ENV_FMT.format(store=canonical.upper())
    per_store_raw = os.environ.get(per_store_key, "").strip()
    if per_store_raw:
        return _parse_backend(per_store_raw, env_key=per_store_key)

    # 2. Global env var: NX_STORAGE_BACKEND
    global_raw = os.environ.get(_GLOBAL_ENV, "").strip()
    if global_raw:
        return _parse_backend(global_raw, env_key=_GLOBAL_ENV)

    # 3. Hard default
    return StorageBackend.SQLITE


def _parse_backend(raw: str, env_key: str) -> StorageBackend:
    """Validate *raw* and return the corresponding :class:`StorageBackend`.

    Raises :exc:`StorageModeFlagError` for unrecognised values.
    """
    normalized = raw.strip().lower()
    if normalized == "sqlite":
        return StorageBackend.SQLITE
    if normalized == "service":
        return StorageBackend.SERVICE
    raise StorageModeFlagError(
        f"{env_key}={raw!r} is not a recognised storage backend. "
        f"Valid values: {', '.join(sorted(_VALID_BACKENDS))}."
    )
