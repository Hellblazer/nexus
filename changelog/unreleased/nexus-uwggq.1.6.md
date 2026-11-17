### Fixed

- **`nx catalog sensitive --collection` with a blank name is refused instead of flagging unrelated documents.** The engine read a blank collection filter as absent and answered a page of the whole tenant, so `--collection " "` flagged documents that were in no such collection. The catalog client now refuses a blank collection name everywhere (`list_by_collection` raises before any request); the verb answers it with a usage error and changes nothing.
