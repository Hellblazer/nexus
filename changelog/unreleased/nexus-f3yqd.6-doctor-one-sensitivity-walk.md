### Fixed

- **`nx doctor` walks the catalog once for its three sensitivity rows, not three times** (nexus-f3yqd.6, F30). The rows sensitive-in-aspect-eligible, sensitive-with-aspects and sensitive-in-external-embedder each read every catalog document to find the sensitive ones. Measured on a laptop tenant, a default `nx doctor` took 2 m 16 s and these three rows took about 23 s each. They now share one walk within a doctor run. `nx catalog doctor` with one of these flags still walks for that row alone.
