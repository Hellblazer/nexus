### Fixed

- **`nx collection verify --deep` and the MCP `collection_verify` work on a service-backed install.** The embedding probe sampled its documents through a Chroma-era `_client_for(...)` / `peek(...)` pair that the service client does not have, so every run ended "embedding probe failed: 'HttpVectorClient' object has no attribute '_client_for'" with exit 1. It now samples through the collection handle's public `get(limit=5)` and reports the cosine metric the service uses (nexus-9biyo).
