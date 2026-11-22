### Fixed

- **`nx doctor` run from a development checkout counts catalog garbage and ghost collections again.** Both rows count through a dry run (`purge_trash(dry_run=True)`, `ghost_sweep(dry_run=True)`) that the engine answers read-only, but the client sent it as a write, so the checkout's production-write guard refused it against the real endpoint: "Catalog garbage: could not count" and "Ghost collections: not applicable". A dry run is now sent as a read and a live purge or sweep (or any `dry_run` that is not literally `True`) is still refused by the guard.
