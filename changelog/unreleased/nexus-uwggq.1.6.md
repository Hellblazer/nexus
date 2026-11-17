### Fixed

- **`nx catalog sensitive --collection` with a blank name is refused instead of flagging unrelated documents.** The engine read a blank collection filter as absent and answered a page of the whole tenant, so `--collection " "` flagged documents that were in no such collection. The catalog client's `list_by_collection` now refuses a blank name before any request; `nx catalog sensitive`, `prune-stale` and `remediate-paths` answer a blank `--collection` with a usage error and change nothing.
