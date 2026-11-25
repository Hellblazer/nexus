# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQL seeds shared by tests/test_tenant_pool.py, kept apart so that file stays under its size ceiling."""

#: One chunk row in a partition leaf, under a collection that has a registry row. Since RDR-229 P3.1 (nexus-p2m1x.17)
#: a chunk carries the registry's ``collection_id`` beside the name (``chunks_collection_id_check``), so the row's
#: registry row is made first and the id read back from it, in the same statement text.
_STRAY_CHUNK_SQL = (
    "SET session_replication_role = replica; "
    "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state) "
    "VALUES (:'tenant', 'c', 'knowledge', 'test-seed', 'bge-base-en-v15-768', 'live'); "
    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, embedding_model, collection_id) "
    "VALUES (:'tenant', 'c', decode(repeat('00', 32), 'hex'), 'x', "
    "array_fill(0.1::real, ARRAY[768])::nexus.vector(768), 'bge-base-en-v15-768', "
    "(SELECT collection_id FROM nexus.catalog_collections WHERE tenant_id = :'tenant' AND name = 'c'));"
)
