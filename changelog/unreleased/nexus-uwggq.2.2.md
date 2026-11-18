### Fixed

- **`nx catalog gc` skips a document whose `miss_count` is not an integer instead of failing (nexus-uwggq.2.2).** A `miss_count` such as `"2.9"` used to raise `ValueError` and stop the command; `meta_at_least` in `nexus.catalog.meta_filter` is now the one integer lower-bound rule. A `miss_count` stored as a float such as `2.0` is not an integer under that rule either, so such a document is no longer listed as an orphan (it was, through `int(2.0)`).
