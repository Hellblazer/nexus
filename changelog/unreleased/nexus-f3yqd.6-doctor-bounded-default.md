### Changed

- **A default `nx doctor` no longer reads the whole catalog** (nexus-f3yqd.6, F30). Three rows read every catalog document (stale index-run fences, failed index runs with hidden chunks, tumbler allocator drift; 22 s on a laptop tenant, more on a bigger catalog), and the taxonomy link-drift row had the engine read every projection assignment (9-13 s). A default run now reports each as `not checked` and names the command that runs it: the new `nx doctor --check-index-runs` for the first three (one catalog read for all three), `nx doctor --check-taxonomy` for the last. These lines carry no warning mark and do not change the exit code. The git-hooks row checks the registered repos in parallel and no longer starts git again for each hook.

### Fixed

- **The tumbler-allocator-drift row counts tombstoned children** (nexus-f3yqd.6). A deleted document's tumbler is still taken and the engine's own drift sweep counts it, but the doctor row read live documents only, so drift caused by a deleted high child read as clean. The row now reads the trash too, and says so when it cannot.
