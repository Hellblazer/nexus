"""nexus.tuplespace — semantic tuple-space primitives (RDR-110).

Phase 1 lands the substrate: the subspace registry (this module's
``registry``), the SQLite schema, the core MCP tools, and a direct-mode
watcher. Daemon-mode integration ships in RDR-112 follow-up beads.

This package is intentionally substrate-only — no SQLite, no HTTP, no
embedder. The registry validates YAML schemas; downstream beads supply
the storage and the network plane.
"""

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
    "UnknownSubspaceError",
    "default_builtin_dir",
]
