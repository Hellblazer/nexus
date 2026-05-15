# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-side subspace registry with SQLite persistence (RDR-112 P1.5 nexus-x98k).

``RegistryStore`` is the single source of truth for subspace schemas inside a
running daemon. Two structurally distinct entry points populate it:

* ``seed_from_builtin_dir(path)`` -- called once at daemon startup, before any
  socket binds. Idempotently upserts every YAML under
  ``nx/tuplespace/builtin/`` (and its conventional subdirs). Bypasses the
  reserved-prefix gate because the builtin subspaces *define* the reserved
  prefixes.
* ``add(yaml_str)`` -- third-party admission via the ``subspace_add`` admin
  RPC. Enforces the reserved-prefix gate so external callers cannot mint a
  schema that shadows a builtin namespace.

The split is structural (two methods) not just a runtime flag, so the
distinction shows up at the call site (RDR-112 nexus-me9y).

Design decisions:
- Persistence: ``tuples.db`` (same SQLite file as tuple stores) to keep the
  SQLite file count low and enable single-transaction cross-table queries in
  future phases.
- Validation: reuses the JSON Schema validator and parameter-pattern regexes
  from ``nexus.tuplespace.registry`` (``_VALIDATOR``, ``_ANGLE_TOKEN``,
  ``_PARAM_PATTERN``) so the daemon and the loader stay in lockstep.
- Digest: sha256 over ``json.dumps({name: schema_digest}, sort_keys=True,
  separators=(",", ":"))`` where ``schema_digest`` is sha256 of the raw
  YAML bytes. The overall digest is cached and invalidated on every write.
- Reserved prefixes: see ``_RESERVED_PREFIXES`` below. Every canonical
  builtin namespace plus the daemon lifecycle prefix is reserved. Builtins
  enter via ``seed_from_builtin_dir``; third parties cannot mint into them.
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
    Registry,
    SubspaceSchema,
)

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Reserved name prefixes (RDR-112 §8, RDR-110 §Subspace registry, nexus-me9y)
# ---------------------------------------------------------------------------

#: Subspace name prefixes that are reserved for builtin / daemon namespaces.
#: Third-party YAML schemas submitted via ``subspace_add`` must not use any of
#: these prefixes; builtin schemas under ``nx/tuplespace/builtin/`` enter the
#: store via ``seed_from_builtin_dir`` which deliberately bypasses this gate.
#:
#: Canonical builtin namespaces (RDR-110, RDR-111, RDR-112 nexus-0xaq):
#:   tasks/           -- RDR-110 project tasks
#:   mailbox/         -- RDR-110 agent mailboxes
#:   locks/           -- RDR-110 mutual-exclusion resources
#:   events/          -- RDR-110 generic event channels
#:   barriers/        -- RDR-110 synchronisation barriers
#:   hook_events/     -- RDR-111 Claude Code hook event channels
#:   layout_state/    -- RDR-111 cockpit layout state
#:   connection_manifest/ -- RDR-111 cockpit connection state
#:   bindings/        -- RDR-112 nexus-0xaq binding profiles
#:   derived/         -- RDR-112 nexus-0xaq derived event channel
#: Daemon internals:
#:   tuples/          -- RDR-110 namespace (raw tuple storage)
#:   daemon/          -- RDR-112 daemon lifecycle channel
_RESERVED_PREFIXES: tuple[str, ...] = (
    "tasks/",
    "mailbox/",
    "locks/",
    "events/",
    "barriers/",
    "hook_events/",
    "layout_state/",
    "connection_manifest/",
    "bindings/",
    "derived/",
    "tuples/",
    "daemon/",
)


#: Subdirectories of the builtin dir that ``seed_from_builtin_dir`` will
#: walk in addition to the top level. Matches the ``subdirs`` arg used by
#: existing client-side ``Registry.load`` callers (``hooks/``, ``bindings/``).
_BUILTIN_SUBDIRS: tuple[str, ...] = ("hooks", "bindings")

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
    """Subspace name starts with a reserved prefix.

    Raised by ``RegistryStore.add`` (third-party path) when the schema's
    name matches one of the prefixes in ``_RESERVED_PREFIXES``. Builtin
    schemas use the ``seed_from_builtin_dir`` path and are exempt.
    """


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
    """Persists subspace schemas to ``tuples.db``.

    Two entry points:
      * ``seed_from_builtin_dir`` -- idempotent bulk-upsert from
        ``nx/tuplespace/builtin/``. Bypasses reserved-prefix gate.
        Called at daemon startup before any socket binds.
      * ``add`` -- third-party admission via ``subspace_add`` admin RPC.
        Enforces reserved-prefix gate; raises on duplicate.

    Constructor injection: pass an explicit ``tuples_db_path``. The DDL is
    applied at construction so the table is always present before the first
    write.

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
    # Public API -- third-party admission
    # ------------------------------------------------------------------

    def add(self, yaml_str: str) -> dict[str, str]:
        """Validate and persist a third-party subspace schema.

        Enforces the reserved-prefix gate: names beginning with any prefix
        in ``_RESERVED_PREFIXES`` are rejected. Builtin schemas must enter
        via ``seed_from_builtin_dir`` instead.

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

        # Reserved-prefix check (third-party gate; seed path is exempt)
        for prefix in _RESERVED_PREFIXES:
            if name.startswith(prefix):
                raise ReservedPrefixError(
                    f"subspace name {name!r} starts with reserved prefix "
                    f"{prefix!r}; reserved prefixes are managed by the "
                    f"builtin seed path (nx/tuplespace/builtin/) and "
                    f"cannot be registered via subspace_add. "
                    f"All reserved prefixes: {', '.join(_RESERVED_PREFIXES)}"
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
            source="third_party",
        )
        return {"name": name, "digest": s_digest}

    # ------------------------------------------------------------------
    # Public API -- builtin seed
    # ------------------------------------------------------------------

    def seed_from_builtin_dir(self, builtin_dir: Path) -> int:
        """Idempotently upsert every builtin subspace YAML into the store.

        Walks *builtin_dir* and its conventional subdirs (``hooks/``,
        ``bindings/``) via ``Registry.load`` so YAML parsing and JSON
        Schema validation match the loader exactly. Bypasses the
        reserved-prefix gate enforced by ``add``: builtins *define* the
        reserved prefixes.

        Idempotency contract:
          * If a row for ``name`` does not exist, INSERT it.
          * If a row for ``name`` exists and its ``schema_digest`` matches
            the on-disk YAML digest, no write occurs.
          * If a row exists with a stale digest (YAML was bumped), the
            row is UPDATEd with the new yaml/digest/timestamp.

        Args:
            builtin_dir: Path to ``nx/tuplespace/builtin/`` (or test
                equivalent).

        Returns:
            Number of rows written or updated (0 means everything was
            already current).

        Raises:
            FileNotFoundError: builtin_dir does not exist.
            SubspaceValidationError: a YAML file under builtin_dir failed
                parse or schema validation (re-raised from the loader).
        """
        if not builtin_dir.is_dir():
            raise FileNotFoundError(
                f"builtin dir does not exist or is not a directory: {builtin_dir}"
            )

        # Reuse the client-side loader so parsing stays in lockstep.
        registry = Registry.load(builtin_dir, subdirs=_BUILTIN_SUBDIRS)
        schemas = registry.schemas()

        now = time.time()
        written = 0
        conn = self._connect()
        try:
            for schema in schemas:
                if schema.source_path is None:  # pragma: no cover -- defensive
                    continue
                yaml_bytes = schema.source_path.read_bytes()
                s_digest = _schema_digest(yaml_bytes)
                yaml_str = yaml_bytes.decode("utf-8")

                row = conn.execute(
                    "SELECT schema_digest FROM subspace_registry WHERE name = ?",
                    (schema.name,),
                ).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO subspace_registry "
                        "(name, yaml, schema_digest, added_at) "
                        "VALUES (?, ?, ?, ?)",
                        (schema.name, yaml_str, s_digest, now),
                    )
                    written += 1
                    _log.info(
                        "daemon/t2/lifecycle",
                        op="subspace-seeded",
                        name=schema.name,
                        digest=s_digest,
                        source="builtin",
                        action="insert",
                    )
                elif row[0] != s_digest:
                    conn.execute(
                        "UPDATE subspace_registry "
                        "SET yaml = ?, schema_digest = ?, added_at = ? "
                        "WHERE name = ?",
                        (yaml_str, s_digest, now, schema.name),
                    )
                    written += 1
                    _log.info(
                        "daemon/t2/lifecycle",
                        op="subspace-seeded",
                        name=schema.name,
                        digest=s_digest,
                        source="builtin",
                        action="update",
                        prior_digest=row[0],
                    )
                # else: row exists with current digest -- no-op
            conn.commit()
        finally:
            conn.close()

        if written:
            self._cached_digest = None

        _log.info(
            "daemon/t2/lifecycle",
            op="seed-complete",
            builtin_dir=str(builtin_dir),
            schemas_total=len(schemas),
            schemas_written=written,
        )
        return written

    # ------------------------------------------------------------------
    # Public API -- digest
    # ------------------------------------------------------------------

    def digest(self) -> str:
        """Return a sha256 digest over the sorted ``{name: schema_digest}`` map.

        Cached between writes. Invalidated on every successful add or
        seed-write. Empty registry returns the sha256 of an empty JSON
        object (``{}``).
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
