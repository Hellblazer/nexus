### Added

- **Text `search` hits carry a short chunk id, and `store_get_many` takes it.** Each hit prints `id <8 hex>`; `store_get_many` expands a short id this session's searches showed to the full chunk id, and says so when one is ambiguous or was never shown. Reaching the whole chunk from a snippet no longer needs a second search with `structured=True`.
- **A superseded document says so in search.** A hit whose owning documents are all the target of a catalog `supersedes` link is annotated `superseded by <title> (<date>)` and ranked lower; a chunk a current document still holds is left alone.
