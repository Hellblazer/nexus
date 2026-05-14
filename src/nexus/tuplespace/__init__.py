"""nexus.tuplespace — semantic tuple-space primitives (RDR-110).

Phase 1 lands the substrate: the subspace registry (this module's
``registry``), the Chroma collection layout (``index``), the SQLite schema,
the core MCP tools, and a direct-mode watcher. Daemon-mode integration ships
in RDR-112 follow-up beads.

This package is intentionally substrate-only — no daemon HTTP, no
embedder wiring. The registry validates YAML schemas; index.py wraps
ChromaDB; downstream beads supply the full storage and network plane.
"""

from nexus.tuplespace.index import TupleIndex, collection_name
from nexus.tuplespace.registry import (
    Registry,
    RegistryLoadError,
    SubspaceSchema,
    UnknownSubspaceError,
    default_builtin_dir,
)

__all__ = [
    "Registry",
    "RegistryLoadError",
    "SubspaceSchema",
    "TupleIndex",
    "UnknownSubspaceError",
    "collection_name",
    "default_builtin_dir",
]
