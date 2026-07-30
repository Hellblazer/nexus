# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage-backend env validation for the post-SQLite era (RDR-158 P3/P4).

The service (HTTP -> Java -> Postgres) path is the ONLY storage backend.
The SQLite T2 stores and the local SQLite catalog were deleted in the
RDR-158 P4 retirement (nexus-i711w); the ``=sqlite`` opt-out this module
used to resolve was removed in P3 (nexus-7bomn). What remains is the
fail-loud guard: a shell still carrying ``NX_STORAGE_BACKEND[_<STORE>]``
gets an explicit answer instead of a silent ignore.

Resolution precedence (narrowest wins), unchanged from the selector era:
  1. Per-store env var  ``NX_STORAGE_BACKEND_<STORE>``
  2. Global env var     ``NX_STORAGE_BACKEND``
  3. Hard default       ``'service'``

``=service`` (any case) still resolves — it matches the default.
``=sqlite`` raises :exc:`StorageModeFlagError` pointing at the
stranded-install redirect (see :mod:`nexus.stranded_install`): silently
handing the engine to an operator who explicitly asked for the old
SQLite baseline would be the silent-fallback class the project bans.
Any other value raises the generic invalid-value error.

Namespace note
--------------
The env var prefix ``NX_STORAGE_BACKEND`` (not ``NX_STORAGE_MODE``) is
intentional.  The legacy ``NX_STORAGE_MODE`` env var is used by
``nexus.config.storage_mode()`` with completely different semantics
(``daemon|direct``; RDR-120).  ``NX_STORAGE_BACKEND`` is the RDR-152
namespace; ``NX_STORAGE_MODE`` is the RDR-120 namespace.
"""
from __future__ import annotations

import os
from enum import Enum


class StorageBackend(str, Enum):
    """The storage backend. Service is the only member since RDR-158 P3
    (nexus-7bomn) removed the ``=sqlite`` opt-out; the enum survives so
    callers and tests keep a typed spelling for the resolver's return."""

    SERVICE = "service"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StorageBackend):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other.lower()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


#: All domain store names that the env-var mechanism recognises.
#:
#: Matches the *eagerly constructed* attributes on
#: :class:`nexus.db.t2.T2Database` plus ``catalog`` (the engine catalog,
#: no longer a T2Database attribute) and ``t1`` (the T1 scratch tier).
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
        "t1",
    }
)

#: The T2 facade's eagerly-constructed stores — the set
#: :class:`nexus.db.t2.T2Database` validates at construction time.
#: Deliberately EXCLUDES ``catalog`` (not a facade store — the catalog
#: routes through ``nexus.catalog.factory``, and no live seam resolves
#: ``storage_backend_for("catalog")`` since the local catalog's deletion;
#: the ``local_catalog_backend`` fixture that once pinned it was removed
#: in the nexus-i711w Stage 5 sweep) and ``t1`` (validated where T1
#: routing actually happens: :func:`nexus.db.t1.get_t1_database` and
#: ``nexus.mcp.core``).
T2_FACADE_STORES: tuple[str, ...] = (
    "memory",
    "plans",
    "taxonomy",
    "telemetry",
    "chash_index",
    "document_aspects",
    "document_highlights",
    "aspect_queue",
)

#: Accepted backend value strings. ``sqlite`` is recognised-but-retired:
#: it gets the dedicated stranded-install redirect, not the generic
#: invalid-value error.
_VALID_BACKENDS: frozenset[str] = frozenset({"service"})

#: Global env-var name (no per-store suffix).  ``NX_STORAGE_BACKEND`` to avoid
#: collision with the legacy ``NX_STORAGE_MODE`` (RDR-120, daemon|direct).
_GLOBAL_ENV: str = "NX_STORAGE_BACKEND"

#: Format string for per-store env vars.  ``{store}`` is upper-cased at call time.
_PER_STORE_ENV_FMT: str = "NX_STORAGE_BACKEND_{store}"


class StorageModeFlagError(ValueError):
    """Raised when an env var selects the retired ``sqlite`` backend or an
    unrecognised value, or when an unknown store name is passed to
    :func:`storage_backend_for`.

    Inherits :class:`ValueError` so callers can catch it without a nexus import
    if they only use the Python exceptions API (e.g. in tests or scripts that
    do not import from nexus.db.storage_mode directly).
    """


def storage_backend_for(store: str) -> StorageBackend:
    """Validate the backend env vars for *store*; return the service backend.

    Since RDR-158 P3 (nexus-7bomn) there is exactly one backend, so the
    return value is always :attr:`StorageBackend.SERVICE`; the call's real
    job is the fail-loud validation. Resolution precedence (narrowest wins):

      1. Per-store env var ``NX_STORAGE_BACKEND_<STORE>``
      2. Global env var    ``NX_STORAGE_BACKEND``
      3. Hard default      :attr:`StorageBackend.SERVICE`

    Raises
    ------
    StorageModeFlagError
        If *store* is not in :data:`VALID_STORE_NAMES`; if any relevant env
        var says ``sqlite`` (the retired opt-out — the message carries the
        stranded-install redirect); or if any relevant env var holds a value
        other than ``service``.
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

    # 3. Hard default: SERVICE (the only backend).
    return StorageBackend.SERVICE


def _retired_sqlite_message(env_key: str) -> str:
    """The hard-error text for the retired ``=sqlite`` opt-out.

    Points at the stranded-install redirect (:mod:`nexus.stranded_install`,
    Hal-confirmed two-hop contract): the remedy for a box that still holds
    unmigrated SQLite data is a round-trip through the last migration-capable
    release, never unset-and-continue.
    """
    return (
        f"{env_key}=sqlite selects the retired SQLite storage backend. "
        f"The SQLite T2 stores and the local SQLite catalog were deleted "
        f"in the RDR-158 P4 retirement (conexus 7.0.0); the service "
        f"(Postgres) backend is the only storage path and is the default "
        f"— unset {env_key} to proceed.\n"
        f"\n"
        f"If this install still holds unmigrated SQLite data (memory.db / "
        f"catalog/.catalog.db under ~/.config/nexus), do NOT just unset the "
        f"variable and continue on this version: install the last "
        f"migration-capable 6.x release, run `nx upgrade` there (the ladder "
        f"migrates copy-not-move; the SQLite files stay behind as rollback "
        f"sources), then upgrade back. `nx doctor` runs the stranded-install "
        f"detector and names the exact pinned release."
    )


def _parse_backend(raw: str, env_key: str) -> StorageBackend:
    """Validate *raw* and return the corresponding :class:`StorageBackend`.

    Raises :exc:`StorageModeFlagError` for ``sqlite`` (retired, with the
    stranded-install redirect) and for unrecognised values.
    """
    normalized = raw.strip().lower()
    if normalized == "service":
        return StorageBackend.SERVICE
    if normalized == "sqlite":
        raise StorageModeFlagError(_retired_sqlite_message(env_key))
    raise StorageModeFlagError(
        f"{env_key}={raw!r} is not a recognised storage backend. "
        f"Valid values: {', '.join(sorted(_VALID_BACKENDS))}."
    )
