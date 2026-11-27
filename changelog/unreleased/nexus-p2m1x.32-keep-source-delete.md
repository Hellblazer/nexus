### Changed

- **A `--keep-source` re-embed source whose target is still live has no CLI delete.** `nx collection delete SOURCE` refuses its label because the source has a live successor, and `nx collection delete --id N` is refused by the engine because the source's row is superseded (409 `collection_superseded`); no printed remedy points anywhere else. A retired label with no live successor still deletes. P4.1's re-key revisits this case (nexus-p2m1x.32).
