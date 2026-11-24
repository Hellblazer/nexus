### Fixed

- A document move before gate 3 latches reads the registry rows of every collection it writes in, not only its documents' own: the collections of the documents' aspect and highlight rows and of the documents that name one as `alias_of` (RDR-229 P3.5a). Before, a verb holding such a collection's row ran beside the move; from the latch the move already read them by id. No new answer.
