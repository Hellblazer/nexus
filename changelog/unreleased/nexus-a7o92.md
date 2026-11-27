### Fixed

- **`nx collection rename` no longer asks for `--force-prefix-change` to give a minted `c<id>` collection a plain label.** A minted label carries no content type or embedding model, so the prefix and model guards read neither from it; a `knowledge` collection renames to a plain label freely, while a minted `code`, `docs` or `rdr` collection still needs the flag, since a plain label is a `knowledge` collection. The `label_reserved` message and the rename help now say a renamed-away label frees when the engine drops the name columns (nexus-a7o92, nexus-258de).
