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

COLLECTION_UPSERT = r"""
from nexus.commands.catalog import _get_catalog
from nexus.corpus import t3_collection_name
from nexus.db import make_t3
# RDR-229 P2.5 D1: re-register the probe collection the store-put step writes to (the same name store put
# resolves, for_write=True). It is an existing row, or the row store-put would create, with the client's
# default model_version, so the upsert route is driven and nothing new is left behind.
name = t3_collection_name("cloud-leg-probe", t3=make_t3(), for_write=True)
_get_catalog().register_collection(name)
print(name)
"""
PRUNE_PROJECTION = r"""
import httpx
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
# RDR-229 P2.4: a destructive route, probed so that it cannot delete. What makes it safe is the NAME: nothing is
# registered under it, so no source collection matches whatever the mode or the floor (a raw cosine can be negative,
# so the floor alone would prove nothing). A healthy route answers {"removed": 0}.
store = HttpTaxonomyStore()
name = "@label@-prune-probe"
print(store.prune_projection_below(name, 0.0))
# The second half proves the body field `match` crosses the edge and reaches the handler: an engine that reads it
# refuses an unknown value with 400. An engine that drops the field would answer 200 and fail the probe.
try:
    store._post("/assignments/prune_projection",
                {"source_collection_prefix": name, "min_similarity": 0.0, "match": "bogus"}, idempotent=False)
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"match=bogus answered {exc.response.status_code}, not 400"
    print("match=bogus refused 400")
else:
    raise SystemExit("match=bogus was accepted: the engine ignores the match field, or the edge strips it")
"""
