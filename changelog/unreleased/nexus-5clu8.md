### Fixed

- The `X-Nexus-Skipped-Collections` response header carries each skipped label form-encoded as UTF-8, so a label holding a space, a comma or a non-ASCII letter survives the header. A legacy name encodes to itself, so older clients read legacy names unchanged; the client decodes either form (nexus-5clu8).
