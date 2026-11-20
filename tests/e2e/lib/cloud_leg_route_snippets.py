# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client snippets cloud_leg.ROUTE_MAP runs to exercise wire-ledger routes, kept beside it to hold
cloud_leg.py under its file-size cap. Placeholders (@label@, @identity@) are filled by cloud_leg."""

BY_AUTHOR = r"""
import json
from nexus.mcp.catalog import catalog_search
print(catalog_search(author="@label@"))
"""

TUPLE_ACK = r"""
from nexus.db.t2.http_tuple_store import HttpTupleStore
# RDR-252 (nexus-igea9): /v1/tuples/ack now refuses a reply that claims from=engine on board/ci; the probe acks a
# claim of the seat's own mailbox row, the path every client ack takes.
store = HttpTupleStore()
store.out("mailbox/@identity@", {"to": "@identity@"}, {"from": "@identity@", "kind": "cloudleg"}, "@label@ ack probe",
          nonce="@label@-ack")
claimed = store.in_("mailbox/@identity@", {"to": "@identity@"}, claimant="@identity@", lease_s=60)
assert claimed is not None, "the probe row was not claimable"
print(store.ack(claimed[1], "@identity@"))
"""

FOR_TUPLE = r"""
from nexus.commands.catalog import _get_catalog
# RDR-229 P1.4: collection_for reads the next version from the for_tuple row. Its owner is a tumbler (the client
# derives the owner segment from it and raises before any request on a subject name), so the probe names owner
# 1.9999, which no tenant registers: the route answers 404 and the client falls back to v1, a read either way.
print(_get_catalog().collection_for("knowledge", "1.9999", "voyage-context-3"))
"""

URI_SEGMENTS = r"""
import json
from nexus.commands.catalog import _get_catalog
# RDR-229 P1.2: the residue pre-flight's one engine read (counts only, never a walk).
print(json.dumps(_get_catalog().uri_segment_census(limit=10), default=str)[:400])
"""

LOCATORS = r"""
import json
from nexus.commands.catalog import _get_catalog
print(json.dumps(_get_catalog().sensitive_locators(limit=10), default=str)[:400])
"""

PRUNE_PROJECTION = r"""
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
# RDR-229 P2.4: a destructive route, probed so that it cannot delete: the name matches no collection and the floor
# is 0.0 (no stored similarity is below it), so a healthy route answers {"removed": 0}.
print(HttpTaxonomyStore().prune_projection_below("cloudleg-@label@", 0.0))
"""
