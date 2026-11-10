### Changed

- **`nx doctor`'s sensitivity rows read a bounded list when the engine has one** (nexus-f3yqd.6, F30). Against an engine that advertises `catalog_sensitive_list_supported` (0.1.188), the three RDR-243 rows ask `GET /v1/catalog/sensitive` for the tenant's live sensitive documents instead of reading every catalog document, which took about 23 s on a laptop tenant. An older engine, a failed read, or a tenant with more than 1,000 sensitive documents gets the full read as before, so the gating row loses no coverage.
