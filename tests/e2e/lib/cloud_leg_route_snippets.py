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
# resolves, for_write=True), so the upsert route is driven and nothing new is left behind. RDR-229 P3.8: a
# registration states its four attributes and derives nothing from the name, so they are the row's own.
cat = _get_catalog()
name = t3_collection_name("cloud-leg-probe", t3=make_t3(), for_write=True)
row = cat.get_collection(name)
if row is None:
    raise SystemExit(f"{name} is not registered: the store-put step has not run")
cat.register_collection(name, content_type=row["content_type"], owner_id=row["owner_id"],
                        embedding_model=row["embedding_model"], model_version=row["model_version"])
print(name)
"""
COLLECTIONS_LIST = r"""
from nexus.commands.catalog import _get_catalog
# RDR-229 P3.5a: list and get rows carry collection_id (an int, or None from an older engine).
rows = _get_catalog().list_collections()
print(len(rows), sorted({type(r.get("collection_id")).__name__ for r in rows}))
"""
COLLECTIONS_GET = r"""
from nexus.commands.catalog import _get_catalog
from nexus.corpus import t3_collection_name
from nexus.db import make_t3
# RDR-229 P3.5a: the probe collection's own row, with its collection_id.
name = t3_collection_name("cloud-leg-probe", t3=make_t3(), for_write=True)
row = _get_catalog().get_collection(name)
print(name, None if row is None else row.get("collection_id"))
"""
IMPORT_COLLECTION_REFUSES_AN_ID = r"""
import httpx
from nexus.commands.catalog import _get_catalog
from nexus.corpus import t3_collection_name
from nexus.db import make_t3
# RDR-229 P3.8: no client calls this route (the engine's own import path does), so the probe drives the one change the
# client side can see, and it cannot write: the probe collection's own complete row (an import never overwrites, so an
# engine without the refusal skips it) plus a collection_id, which this engine refuses 400 naming the field.
cat = _get_catalog()
name = t3_collection_name("cloud-leg-probe", t3=make_t3(), for_write=True)
row = cat.get_collection(name)
if row is None:
    raise SystemExit(f"{name} is not registered: the store-put step has not run")
body = {k: row[k] for k in ("name", "content_type", "owner_id", "embedding_model", "model_version")}
try:
    cat._post("/import/collection", {**body, "collection_id": 1})
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"a collection_id body answered {exc.response.status_code}, not 400"
    assert "collection_id" in exc.response.text, f"the 400 does not name collection_id: {exc.response.text[:200]}"
    print("collection_id refused 400")
else:
    raise SystemExit("a collection_id body was accepted: the engine does not refuse a client-supplied id")
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

TELEMETRY_RENAME = r"""
import httpx
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
# nexus-n3hhc: the telemetry rename judges its target with the label rule. This runs against the production engine at
# release time, where a data write is Sam's alone, so the probe proves before it renames that nothing is recorded under
# its source label (unique per run), and refuses to run otherwise (linda's condition). The legal rename then moves zero
# rows, and the minted-form target must be refused with 400 before either table is touched.
store = HttpTelemetryStore()
source = "@label@-tel-probe"
search_rows = int(store.query_collection_stats(source, days=36500).get("row_count") or 0)
hooks = store.list_hook_failures(days=0, limit=1)
if hooks["total"] > 10000:
    raise SystemExit(f"refusing: {hooks['total']} hook_failures rows is too many to prove none is under {source!r}")
hook_rows = sum(1 for r in store.list_hook_failures(days=0, limit=max(hooks["total"], 1))["rows"]
                if r.get("collection") == source)
if search_rows or hook_rows:
    raise SystemExit(f"refusing: {search_rows} search and {hook_rows} hook rows are recorded under {source!r}")
print("no rows under the probe source")
print(store.rename_collection(old=source, new=source + "-renamed"))
try:
    store.rename_collection(old=source, new="c42")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"a minted-form target answered {exc.response.status_code}, not 400"
    print("minted target refused 400")
else:
    raise SystemExit("a minted-form target was accepted: the engine does not judge the telemetry rename target")
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
# RDR-229 P3.8: /v1/status names collection_tuple_index_present (absent on an older engine; the doctor reads it).
index = status.get("collection_tuple_index_present")
assert index is None or isinstance(index, bool), "collection_tuple_index_present is not a boolean"
print("collection_tuple_index_present:", "absent" if index is None else index)
"""

IMPORT_MARKER = r"""
from nexus.backup import cloud_import
# RDR-229 P3.11 (nexus-p2m1x.31): GET /v1/tenant/import-marker answers the caller's own tenant's restore marker
# (None when nothing was restored). After a relabelling restore it carries `collections_relabelled` (a list) and
# `collections_relabelled_count` (an int); both are absent on an older engine. Read-only, nothing is written.
client = cloud_import.connect()
try:
    marker = client.read_marker()
    assert marker is None or isinstance(marker, dict), "the import marker is neither absent nor an object"
    if isinstance(marker, dict):
        relabelled = marker.get("collections_relabelled")
        assert relabelled is None or isinstance(relabelled, list), "collections_relabelled is not a list"
        count = marker.get("collections_relabelled_count")
        assert count is None or isinstance(count, int), "collections_relabelled_count is not an int"
    print("import marker:", "absent" if marker is None else "present")
finally:
    for name in ("close_http", "close_all"):
        close = getattr(client, name, None)
        if callable(close):
            close()
            break
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
