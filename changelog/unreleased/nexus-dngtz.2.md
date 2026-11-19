### Added

- **`nx doctor --probe-edge` reports what the path does to `X-Correlation-ID`** (RDR-167 Phase 2). The probe's keyed no-op POST now carries a known correlation id, and a second row names one of four outcomes from the engine's echo: pass-through, inbound strip (the engine generated its own `nxe-` id), overwrite, or response strip (no echo, which an engine without Phase 1 also gives). The row is read only from an answer the engine sent, never from an edge refusal, and it never changes the probe's exit code (nexus-dngtz.2).
