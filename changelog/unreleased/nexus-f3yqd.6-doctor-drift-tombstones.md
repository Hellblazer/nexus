### Fixed

- **The tumbler-allocator-drift row counts tombstoned children** (nexus-f3yqd.6). A deleted document's tumbler is still taken and the engine's own drift sweep counts it, but the doctor row read live documents only, so drift caused by a deleted high child read as clean. The row now reads the trash too, and says so when it cannot.
