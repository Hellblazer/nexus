### Added

- A collection's row carries its `collection_id` (RDR-229 P3.5a, first step, read-only). The engine's `GET /v1/catalog/collections/list`, `/get` and `/for_tuple` answer the registry's own id as a number on every row; `HttpCatalogClient.get_collection` and `list_collections` hand it back as an `int`, or `None` from an older engine that sends no id. No route takes an id yet, and the id is not stable across a rename until P3.7's name-key drop and R6's `ids_stable` signal, so nothing caches by it before then.
