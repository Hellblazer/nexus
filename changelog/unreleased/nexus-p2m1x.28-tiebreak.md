### Changed

- **A tuple's winner at equal version is the lowest `collection_id`.** `GET /v1/catalog/collections/for_tuple` and the re-embed target selection order by highest `model_version`, then the lowest `collection_id`, then `created_at`, then the name (it was `created_at`, then the name). The answer's shape is unchanged; which row answers can change only where two live rows share a tuple.
