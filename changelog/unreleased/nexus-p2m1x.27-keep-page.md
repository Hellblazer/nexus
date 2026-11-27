### Fixed

- **A data move that needs an operator keeps its page when a later failure counts nothing.** A stopped worker pass, a failure handler that throws, and a handler that cannot record the retry count (a lost admin connection, or a row no longer open) no longer clear the needs-an-operator page a counted failure latched; as with a deferral behind an index build, nothing is announced a second time (RDR-229 P3.7, nexus-p2m1x.27).
