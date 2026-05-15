# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-side subspace registry with SQLite persistence (RDR-112 P1.5 nexus-x98k).

``RegistryStore`` accepts third-party subspace YAML schemas via the
``subspace_add`` admin RPC, validates them using
``nexus.tuplespace.registry.SubspaceSchema``, enforces reserved-prefix rules,
and persists accepted schemas to the ``subspace_registry`` table in
``tuples.db``.

Design decisions:
- Persistence: ``tuples.db`` (same SQLite file as tuple stores) to keep the
  SQLite file count low and enable single-transaction cross-table queries in
  future phases.
- Validation: reuses the JSON Schema validator and parameter-pattern regexes
  from ``nexus.tuplespace.registry`` (``_VALIDATOR``, ``_ANGLE_TOKEN``,
  ``_PARAM_PATTERN``) so the daemon and the loader stay in lockstep. We do
  not call ``_load_one`` directly because that function takes a ``Path``;
  the registry admin RPC accepts a YAML string in-memory, so the parse
  pipeline is mirrored here (yaml.safe_load + the same checks). When the
  loader gains a new rule, mirror it here.
- Digest: sha256 over ``json.dumps(sorted({name: schema_digest}))`` where
  ``schema_digest`` is sha256 of the raw YAML bytes. The overall digest is
  cached and invalidated on every ``add()``.
- Reserved prefixes: ``tuples/`` (RDR-110 namespace) and ``daemon/``
  (RDR-112 lifecycle) are rejected.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import structlog
import yaml
from jsonschema.exceptions import ValidationError

from nexus.tuplespace.registry import (
    REGISTRY_FORMAT_SCHEMA,
    _ANGLE_TOKEN,
    _PARAM_PATTERN,
    _VALIDATOR,
    SubspaceSchema,
)

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Reserved name prefixes (RDR-112 §8, RDR-110 §Subspace registry)
# ---------------------------------------------------------------------------

#: Subspace name prefixes that are reserved for the daemon's own internal
#: event channels. Third-party YAML schemas must not use these prefixes.
_RESERVED_PREFIXES: tuple[str, ...] = ("tuples/", "daemon/")

# ---------------------------------------------------------------------------
# DDL (also in migrations.py via migrate_subspace_registry_table)
# ---------------------------------------------------------------------------

_SUBSPACE_REGISTRY_DDL: str = """
CREATE TABLE IF NOT EXISTS subspace_registry (
    name          TEXT    PRIMARY KEY,
    yaml          TEXT    NOT NULL,
    schema_digest TEXT    NOT NULL,
    added_at      REAL    NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class SubspaceValidationError(Exception):
    """YAML is malformed or does not satisfy the subspace registry JSON Schema."""


class ReservedPrefixError(Exception):
    """Subspace name starts with a reserved prefix (``tuples/`` or ``daemon/``)."""


class DuplicateSubspaceError(Exception):
    """A subspace with this name is already registered."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _schema_digest(yaml_bytes: bytes) -> str:
    """Return the sha256 hex digest of raw YAML bytes."""
    return hashlib.sha256(yaml_bytes).hexdigest()


def _parse_and_validate(yaml_str: str) -> SubspaceSchema:
    """Parse, JSON-schema validate, and load-time-check a YAML string.

    Reuses ``nexus.tuplespace.registry`` internals for validation so we do
    not duplicate the validator. Raises ``SubspaceValidationError`` on any
    problem.
    """
    try:
        raw = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        raise SubspaceValidationError(f"malformed YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SubspaceValidationError(
            f"top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    try:
        _VALIDATOR.validate(raw)
    except ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise SubspaceValidationError(
            f"schema validation failed at {path}: {exc.message}"
        ) from exc

    # Reject malformed param names (mirrors _load_one logic)
    bad_params = [
        token
        for token in _ANGLE_TOKEN.findall(raw["name"])
        if _PARAM_PATTERN.fullmatch(f"<{token}>") is None
    ]
    if bad_params:
        raise SubspaceValidationError(
            f"invalid param identifier(s) {bad_params!r} in name {raw['name']!r}; "
            "params must match [a-zA-Z_][a-zA-Z0-9_]* (no dashes)"
        )

    take = raw["take"]
    if take.get("mode") == "exact":
        keys = take.get("match_keys") or []
        if not keys:
            raise SubspaceValidationError(
                "take.mode='exact' requires non-empty match_keys (RDR-110 §C2)"
            )

    return SubspaceSchema(
        name=raw["name"],
        tier=raw["tier"],
        content_type=raw["content_type"],
        embed_from=raw["embed_from"],
        dimensions=dict(raw.get("dimensions", {})),
        take=dict(take),
        read=dict(raw["read"]),
        tiers=list(raw["tiers"]),
        retention_seconds=int(raw["retention_seconds"]),
        source_path=None,
    )


# ---------------------------------------------------------------------------
# RegistryStore
# ---------------------------------------------------------------------------


class RegistryStore:
    """Persists third-party subspace schemas to ``tuples.db``.

    Constructor injection: pass an explicit ``tuples_db_path``. The DDL is
    applied at construction so the table is always present before the first
    ``add()`` call.

    Thread-safety: each method opens a short-lived connection. The daemon
    invokes ``add()`` via ``run_in_executor`` (one call at a time), so no
    connection pooling is needed here.
    """

    def __init__(self, *, tuples_db_path: Path) -> None:
        self._db_path = tuples_db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._cached_digest: str | None = None
        self._apply_ddl()

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def _apply_ddl(self) -> None:
        conn = self._connect()
        try:
            conn.execute(_SUBSPACE_REGISTRY_DDL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, yaml_str: str) -> dict[str, str]:
        """Validate and persist a subspace schema from a YAML string.

        The parameter name avoids shadowing the module-level ``yaml`` import.

        Args:
            yaml_str: Raw YAML text for the subspace schema.

        Returns:
            ``{"name": <name>, "digest": <schema_digest>}`` on success.

        Raises:
            SubspaceValidationError: YAML is malformed or fails JSON Schema.
            ReservedPrefixError: Name starts with a reserved prefix.
            DuplicateSubspaceError: A schema with this name already exists.
        """
        schema = _parse_and_validate(yaml_str)
        name = schema.name

        # Reserved-prefix check
        for prefix in _RESERVED_PREFIXES:
            if name.startswith(prefix):
                raise ReservedPrefixError(
                    f"subspace name {name!r} starts with reserved prefix {prefix!r}; "
                    "reserved prefixes: tuples/ (RDR-110), daemon/ (RDR-112)"
                )

        yaml_bytes = yaml_str.encode("utf-8")
        s_digest = _schema_digest(yaml_bytes)
        now = time.time()

        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO subspace_registry (name, yaml, schema_digest, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, yaml_str, s_digest, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise DuplicateSubspaceError(
                    f"subspace {name!r} is already registered"
                )
        finally:
            conn.close()

        # Invalidate cached digest
        self._cached_digest = None

        _log.info(
            "daemon/t2/lifecycle",
            op="subspace-added",
            name=name,
            digest=s_digest,
        )
        return {"name": name, "digest": s_digest}

    def digest(self) -> str:
        """Return a sha256 digest over the sorted ``{name: schema_digest}`` map.

        Cached between ``add()`` calls. Invalidated on every successful add.
        Empty registry returns the sha256 of an empty JSON object (``{}``).
        """
        if self._cached_digest is not None:
            return self._cached_digest

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, schema_digest FROM subspace_registry ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

        mapping = {row[0]: row[1] for row in rows}
        canonical = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
        self._cached_digest = hashlib.sha256(canonical.encode()).hexdigest()
        return self._cached_digest
