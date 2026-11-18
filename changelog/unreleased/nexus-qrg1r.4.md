### Changed

- **`nx catalog span` takes a title or source URI as well as a tumbler**, resolved the same way as `nx catalog link`: an exact title match wins, and a value that matches no document, or several with no exact match, is refused. A catalog error during the lookup is reported as an error instead of a traceback (nexus-qrg1r.4, RDR-171).
