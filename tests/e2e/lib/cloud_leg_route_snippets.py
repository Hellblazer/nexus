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

COMPLETION_COUNTS = r"""
import json
from nexus.catalog.engine_reads.rdr229_completion_counts import read_rdr229_completion_counts
from nexus.commands.catalog import _get_catalog
# RDR-229 P3.1 review N2: the caller's own completion counts (the doctor's RDR-229 rows), never another tenant's.
print(json.dumps(read_rdr229_completion_counts(_get_catalog()), default=str)[:400])
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

ENGINE_STATUS = r"""
from nexus.db.http_engine_status import fetch_engine_status
# RDR-229 P3.1 (nexus-p2m1x.17): the boot-time completion check reports in an optional top-level key of this
# body, `rdr229_completion`. The key is absent until the engine's first read, so the probe asserts the route
# answers a JSON object and names the key only when it is there; fetch_engine_status returns None on any failure.
status = fetch_engine_status(timeout=20.0)
assert isinstance(status, dict), "GET /v1/status did not answer a JSON object"
completion = status.get("rdr229_completion")
assert completion is None or isinstance(completion, dict), "rdr229_completion is not an object"
print("status keys:", len(status), "rdr229_completion:", "present" if completion is not None else "absent")
"""

DOC_REGISTER_UNREGISTERED = r"""
from nexus.commands.catalog import _get_catalog
from nexus.registration_retry import unregistered_collection_name
# RDR-229 P3.1 (nexus-p2m1x.17): POST /v1/catalog/doc/register answers 422 `unregistered_collection` for a document
# whose physical_collection has no registry row in the caller's tenant, and writes nothing. The probe names a
# collection nothing registers (the run label makes the name unique), under the "knowledge" curator owner the note
# puts use (an idempotent upsert), so the refusal that answers is the collection's and not the owner's. This
# process never cached the name, so the client's one retry (which re-registers a name it cached) does not fire.
cat = _get_catalog()
owner = cat.curator_owner_tumbler_by_name("knowledge") or cat.register_owner("knowledge", "curator")
name = "@label@-docreg-probe"
try:
    cat.register(owner, "@label@ doc register probe", content_type="knowledge", physical_collection=name)
except Exception as exc:  # noqa: BLE001 - narrowed below to the one typed refusal
    assert unregistered_collection_name(exc) == name, f"not the typed unregistered_collection 422: {exc!r}"
    print("doc/register refused the unregistered collection with 422")
else:
    raise SystemExit("doc/register accepted a document under an unregistered collection: the engine does not refuse it")
"""
