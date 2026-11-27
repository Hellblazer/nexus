### Fixed

- **A data move that needs an operator keeps its page when the engine stops, or the failure handler itself fails, on a later pass.** The worker's stopped-pass and handler-failed reports no longer clear a needs-an-operator page a counted failure latched, as a deferral behind an index build already did; nothing is announced a second time (RDR-229 P3.7, nexus-p2m1x.27).
