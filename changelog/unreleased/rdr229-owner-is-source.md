### Changed

- **An imported collection's documents are registered under the collection's source.** A repo's `code__`, `docs__` and `rdr__` collections go to the repo owner, found from the export file's owner header, or from the `<name>-<hash8>` in the collection name, and a docs corpus goes to its corpus curator. Only a collection with no source at all still falls back to the knowledge curator, and then `nx store import` logs a warning and prints it in the summary. Exports now record the repo owner in the file header, and older files import as before (RDR-229).
- **`nx index repo` fills in a repo owner's missing root.** An owner that an import registered without a root gets the repo's real path on the next index run; an owner that already has a root is left alone (RDR-229).
