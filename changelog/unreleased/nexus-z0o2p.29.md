### Fixed

- `write_many` with `chunks` no longer embeds or refreshes chunks that no registered document in the request lists, and the response reports how many it left out as `chunks_unreferenced`. Before, a chunk the collection already held was refreshed (its last-written time reset to now) even when no document of the request listed it.
- `append` and `append_many` refuse two rows at one position in one document, with a 400 naming the position (on `append_many` that document fails and the rest of the request is unaffected). Before, both chunks were inserted and only the second row was kept, leaving the first chunk with no owner.

A chunk dropped by a re-index whose cleanup step times out can still end with no owner and no orphan record; that remaining case is open. The change is in the engine service and reaches users with the next engine release.
