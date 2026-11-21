### Changed

- `--corpus` and the MCP corpus fan-out resolve a short form like `knowledge__foo` or `knowledge__foo__voyage-context-3` by the collection's catalog row first (same content type and owner, same embedding model when the value names one, live only), then by the old name-prefix match when no row matches, so a repo short form such as `code__<repo>-<hash8>` still resolves. A collection that is quarantined, dormant or disputed no longer comes back through the name-prefix match for a short form. RDR-229 P2.7 (nexus-p2m1x.12).
