# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-store backend-selection flag for RDR-152 transition rollback.

Resolves which storage backend is authoritative for reads/writes for a given
T2/T1 domain store.  The two backends are:

- ``sqlite``  — current SQLite + T2 daemon path (default everywhere)
- ``service`` — new HTTP→Java→Postgres path (enabled per store as each bead
                migrates the store; see the nexus-gmiaf.7+ beads)

Resolution precedence (narrowest wins):
  1. Per-store env var  ``NX_STORAGE_MODE_<STORE>=service|sqlite``
  2. Global env var     ``NX_STORAGE_MODE=service|sqlite``
  3. Hard default       ``'sqlite'``

A config-file layer is reserved for Phase 2+ when the service is in broader
use; it is not wired in this bead (nexus-gmiaf.4) to keep the seam minimal.

COPY-NOT-MOVE invariant
-----------------------
Flipping a store back to ``sqlite`` works because the ETL always *copies* data
to Postgres — it never deletes from SQLite until Phase 4 decommission.
``storage_mode_for(store)`` is therefore a pure routing switch with no
data-lifecycle side effects.

Invalid values raise :exc:`StorageModeFlagError` immediately.  There are no
silent fallbacks (per the project's no-silent-fallback rule).

Usage (in a store factory, e.g. nexus-gmiaf.7)::

    from nexus.db.storage_mode import StorageBackend, storage_mode_for

    if storage_mode_for("memory") == StorageBackend.SERVICE:
        return HttpMemoryStore(...)
    else:
        return MemoryStore(path)
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Literal


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
#: Matches the attributes on :class:`nexus.db.t2.T2Database` plus ``t1``
#: (the T1 scratch tier, which gains Postgres backing in a later bead).
#: Keys are the canonical lower-case store identifiers; the corresponding
#: env var is ``NX_STORAGE_MODE_<UPPER>`` (e.g. ``NX_STORAGE_MODE_MEMORY``).
VALID_STORE_NAMES: frozenset[str] = frozenset(
    {
        "memory",
        "plans",
        "taxonomy",
        "telemetry",
        "chash_index",
        "document_aspects",
        "aspect_queue",
        "catalog",
        "t1",
    }
)

#: Accepted backend value strings (case-insensitive comparison applied).
_VALID_BACKENDS: frozenset[str] = frozenset({"sqlite", "service"})

#: Global env-var name (no per-store suffix).
_GLOBAL_ENV: str = "NX_STORAGE_MODE"

#: Format string for per-store env vars.  ``{store}`` is upper-cased at call time.
_PER_STORE_ENV_FMT: str = "NX_STORAGE_MODE_{store}"


class StorageModeFlagError(ValueError):
    """Raised when an env var contains an invalid storage backend value, or
    when an unknown store name is passed to :func:`storage_mode_for`.

    Inherits :class:`ValueError` so callers can catch it without a nexus import
    if they only use the Python exceptions API (e.g. in tests or scripts that
    do not import from nexus.db.storage_mode directly).
    """


def storage_mode_for(store: str) -> StorageBackend:
    """Return the authoritative storage backend for *store*.

    Resolution precedence (narrowest wins):
      1. Per-store env var ``NX_STORAGE_MODE_<STORE>`` (after upper-casing *store*)
      2. Global env var    ``NX_STORAGE_MODE``
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

    # 1. Per-store env var: NX_STORAGE_MODE_<STORE>
    per_store_key = _PER_STORE_ENV_FMT.format(store=canonical.upper())
    per_store_raw = os.environ.get(per_store_key, "").strip()
    if per_store_raw:
        return _parse_backend(per_store_raw, env_key=per_store_key)

    # 2. Global env var: NX_STORAGE_MODE
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
