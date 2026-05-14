"""nexus.tuplespace — semantic tuple-space primitives (RDR-110).

Phase 1 lands the substrate: the subspace registry (this module's
``registry``), the Chroma collection layout (``index``), the SQLite schema,
the core MCP tools, and a direct-mode watcher. Daemon-mode integration ships
in RDR-112 follow-up beads.

This package is intentionally substrate-only — no daemon HTTP, no
embedder wiring. The registry validates YAML schemas; index.py wraps
ChromaDB; downstream beads supply the full storage and network plane.
"""

from nexus.tuplespace.api import (
    BlockingNotSupported,
    ClaimNotFoundError,
    ClaimOwnershipError,
    SubspaceSchemaError,
    TakeDisabledError,
    ack,
    list_subspaces,
    nack,
    out,
    read,
    subspace_schema,
    subspace_stats,
    take,
)
from nexus.tuplespace.index import TupleIndex, collection_name
from nexus.tuplespace.registry import (
    Registry,
    RegistryLoadError,
    SubspaceSchema,
    UnknownSubspaceError,
    default_builtin_dir,
)

__all__ = [
    "BlockingNotSupported",
    "ClaimNotFoundError",
    "ClaimOwnershipError",
    "Registry",
    "RegistryLoadError",
    "SubspaceSchema",
    "SubspaceSchemaError",
    "TakeDisabledError",
    "TupleIndex",
    "UnknownSubspaceError",
    "ack",
    "collection_name",
    "default_builtin_dir",
    "list_subspaces",
    "nack",
    "out",
    "read",
    "subspace_schema",
    "subspace_stats",
    "take",
]
