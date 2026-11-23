### Changed

- `POST /v1/catalog/collections/set_aspects_enabled` reads the collection's registry row in its own write transaction and changes that row by its `collection_id` (RDR-229 P3.5a). A request by `collection_id` whose id was freed, with its label then registered again by another row, answers a typed 404 `unknown_collection_id` and changes nothing of the label's new holder. By label it now acts only on the row the label resolves to (the live row, else the newest tombstone); before, it updated every row bearing the name, tombstones included. The wire shape is unchanged.
