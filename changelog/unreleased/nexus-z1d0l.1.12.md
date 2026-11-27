### Changed

- **A check or verify step confined to one document binds a shared chunk to that document.** A chunk the confined document shares with another document used to bind ambiguous; it now binds to the confined document when that document is one of the chunk's live owners (`chash_positions` rows, collection-matched), and stays ambiguous when it is not. Unconfined steps are unchanged (RDR-088 amendment (v), nexus-z1d0l.1.12).
