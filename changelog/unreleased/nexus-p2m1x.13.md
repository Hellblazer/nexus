### Fixed

- **A collection is found by its catalog row, not by a rendered name (RDR-229 P2.8).** `nx index repo`, `nx catalog collection-name` and the RDR hook now use the collection a repo is registered under, a legacy slug included, instead of the conformant name a render would produce; only a new collection or a version bump still renders a name. A knowledge subject (`--collection field-notes`) now finds a collection registered for that subject under a label of any shape. If the catalog read fails, the name resolution answers as before.
