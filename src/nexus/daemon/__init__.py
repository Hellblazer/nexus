"""nexus daemon, single-writer process owning T2 SQLite + chromadb.

See docs/rdr/rdr-112-storage-as-service-container-boundary.md and
docs/rdr/rdr-113-host-trust-model.md for the substrate design.

Import contract (nexus-9n32 S2): callers reach the public types
through their defining submodules, not through this package root.
Re-exports are intentionally absent so the package surface is
self-documenting through ``rg`` / IDE jump-to-definition rather
than a curated facade that has to track each new addition.

  - ``from nexus.daemon.t2_client import T2Client, T2DaemonError,
    RpcTimeoutError, EventStreamUnavailable``
  - ``from nexus.daemon.tuplespace_service import TuplespaceService,
    BlockingTakeResourceExhausted``
  - ``from nexus.daemon.discovery import find_t2_daemon``
"""
