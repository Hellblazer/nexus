### Changed

- **A check or verify step in a run confined to one document binds a shared chunk to that document.** A chunk the confined document shares with another document used to bind ambiguous; it now binds to the confined document when that document is one of the chunk's live owners (`chash_positions` rows), on an owner row in the chunk's collection (else the confinement's own collection). It stays ambiguous when the document is not an owner. Runs not confined to one document are unchanged (RDR-088 amendment (v), nexus-z1d0l.1.12).
