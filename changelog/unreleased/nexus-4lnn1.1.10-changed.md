### Changed

- The reconciler recounts an index's rows when its size changed in a way that could change the size rule's verdict (bytes over the stale row count, an upper bound on bytes per row, reaches the trigger), so the size rule divides bytes by a count that is current whenever it matters, and ingest that cannot trigger it costs no count.
- A red canary rebuilds an index only a day after its anchor (a build, an adoption or the last REINDEX), so a persistent red is not rebuilt on every engine restart or change of holder. A REINDEX whose fresh anchor could not be written is retried at the next pass and the index is not judged against its old anchor meanwhile. A bulk delete seen while an index is backed off stays owed. The REINDEX phase can no longer fail a pass.
