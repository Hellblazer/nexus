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

GC_QUARANTINE_ORPHANS = r"""
from nexus.db import make_t3
from nexus.db.http_vector_client import VectorServiceError
# RDR-229 P3.5a: the gc moves answer a registry row a retiring verb holds as a typed 503 rename_busy. A move cannot be
# probed on a real collection, so this one is probed so that it cannot move anything: the NAME is registered nowhere,
# and the engine reads the origin's registry row before it runs the move, so it refuses with a typed 4xx and no
# chunk is touched. An engine without the route answers a bare 404, which carries no reason and fails the probe.
try:
    make_t3().gc_quarantine_orphans("@label@-gc-probe", "quarantine-@label@-gc-probe", "2026-01-01T00:00:00Z")
except VectorServiceError as exc:
    assert exc.code in (400, 404, 409, 422) and exc.reason, f"gc quarantine-orphans answered {exc.code} with no typed reason: {exc}"
    print("quarantine-orphans refused", exc.code, exc.reason)
else:
    raise SystemExit("gc quarantine-orphans accepted a collection nothing registers")
"""

GC_QUARANTINE_RESTORE = r"""
from nexus.db import make_t3
from nexus.db.http_vector_client import VectorServiceError
# RDR-229 P3.5a: as GC_QUARANTINE_ORPHANS, for the restore (which keeps its own 503, quarantine_restore_busy): a dry run
# for an origin nothing registers, refused before any sibling is looked at, so it cannot move or write anything.
try:
    make_t3().gc_quarantine_restore("@label@-gc-probe", chashes=["00" * 32], dry_run=True)
except VectorServiceError as exc:
    assert exc.code in (400, 404, 409, 422) and exc.reason, f"gc quarantine-restore answered {exc.code} with no typed reason: {exc}"
    print("quarantine-restore refused", exc.code, exc.reason)
else:
    raise SystemExit("gc quarantine-restore accepted an origin nothing registers")
"""

GC_RESTORE_REREFERENCED = r"""
from nexus.db import make_t3
from nexus.db.http_vector_client import VectorServiceError
# RDR-229 P3.5a: as GC_QUARANTINE_ORPHANS, for restore-rereferenced: an origin nothing registers, refused when the engine
# reads its registry row, before any chunk is looked at.
try:
    make_t3().gc_restore_rereferenced("quarantine-@label@-gc-probe", "@label@-gc-probe")
except VectorServiceError as exc:
    assert exc.code in (400, 404, 409, 422) and exc.reason, f"gc restore-rereferenced answered {exc.code} with no typed reason: {exc}"
    print("restore-rereferenced refused", exc.code, exc.reason)
else:
    raise SystemExit("gc restore-rereferenced accepted an origin nothing registers")
"""

GC_EXPIRE_QUARANTINE = r"""
from nexus.db import make_t3
from nexus.db.http_vector_client import VectorServiceError
# RDR-229 P3.5a: as GC_QUARANTINE_ORPHANS, for expire-quarantine (which deletes quarantine rows): an origin nothing registers,
# refused when the engine reads its registry row, before any quarantine row is looked at.
try:
    make_t3().gc_expire_quarantine("quarantine-@label@-gc-probe", "@label@-gc-probe", "2026-01-01T00:00:00Z", 1.0, 100, False)
except VectorServiceError as exc:
    assert exc.code in (400, 404, 409, 422) and exc.reason, f"gc expire-quarantine answered {exc.code} with no typed reason: {exc}"
    print("expire-quarantine refused", exc.code, exc.reason)
else:
    raise SystemExit("gc expire-quarantine accepted an origin nothing registers")
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
moved = store.rename_collection(old=source, new=source + "-renamed")
assert all(v == 0 for v in moved.values()), f"the probe's legal rename moved rows: {moved}"
print(moved)
try:
    store.rename_collection(old=source, new="c42")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"a minted-form target answered {exc.response.status_code}, not 400"
    print("minted target refused 400")
else:
    raise SystemExit("a minted-form target was accepted: the engine does not judge the telemetry rename target")
"""

CHASH_RENAME_REFUSAL = r"""
import httpx
from nexus.db.t2.http_chash_index import HttpChashIndex
# nexus-n3hhc's class: the chash rename judges its target with the label rule before any row moves. Refusal only: the
# probe proves its per-run source holds no chash row, then sends a minted-form target, which must be refused with 400.
store = HttpChashIndex()
source = "@label@-chash-probe"
if store.count_for_collection(source):
    raise SystemExit(f"refusing: chash rows are recorded under {source!r}")
try:
    store.rename_collection(old=source, new="c42")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"a minted-form target answered {exc.response.status_code}, not 400"
    print("chash minted target refused 400")
else:
    raise SystemExit("a minted-form target was accepted: the engine does not judge the chash rename target")
"""

TAXONOMY_RENAME_REFUSAL = r"""
import httpx
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
# nexus-n3hhc's class: the taxonomy rename judges its target with the label rule before any row moves. Refusal only:
# the probe proves its per-run source holds no topic, then sends a minted-form target, which must be refused with 400.
store = HttpTaxonomyStore()
source = "@label@-tax-probe"
if store.get_topics_for_collection(source):
    raise SystemExit(f"refusing: topics are recorded under {source!r}")
try:
    store.rename_collection(source, "c42")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 400, f"a minted-form target answered {exc.response.status_code}, not 400"
    print("taxonomy minted target refused 400")
else:
    raise SystemExit("a minted-form target was accepted: the engine does not judge the taxonomy rename target")
"""

PERSIST_SPLIT_REFUSAL = r"""
import httpx
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
# RDR-229 P3.5a (nexus-p2m1x.23): persist_split acts on a topic only when it is a topic of the named collection, and refuses
# otherwise (400 for a topic of another collection, or a typed 404/409/422 for a collection or a registry row that cannot
# take the write), before any row moves. The probe names a topic nothing holds under a collection nothing registers, so
# whatever refusal answers, it moved and deleted nothing.
store = HttpTaxonomyStore()
name = "@label@-split-probe"
try:
    store.persist_split({"topic_id": 2**62, "collection_name": name, "child_specs": []})
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code in (400, 404, 409, 422), f"persist_split answered {exc.response.status_code}, not a typed refusal"
    print("persist_split refused", exc.response.status_code)
else:
    raise SystemExit("persist_split accepted a topic nothing holds under a collection nothing registers")
"""

PURGE_TRASH_DRY_RUN = r"""
from nexus.commands import catalog as _cat_cmd
# RDR-229 P3.5a (nexus-p2m1x.23): POST /v1/catalog/purge-trash reads the registry rows of the collections its aged tombstones
# reach (FOR KEY SHARE) before a real purge deletes. The probe is the engine's read-only preview (dry_run true: nothing is
# deleted, no registry row is locked), which proves the route answers its counts; the wait itself is the engine test's.
result = _cat_cmd._get_catalog_writer().purge_trash(older_than_days=36500, dry_run=True)
assert isinstance(result, dict) and result.get("dry_run") is True, f"purge-trash dry run answered {result!r}"
print("purge-trash dry run answered", sorted(result))
"""

#: The rename routes that judge their target with the label rule (nexus-n3hhc and its class): route -> (step name, the
#: probe, the line it must print). cloud_leg.ROUTE_MAP builds one Step from each.
LABEL_RULE_RENAME_PROBES: dict[str, tuple[str, str, str]] = {
    "POST /v1/telemetry/rename_collection": ("route-telemetry-rename", TELEMETRY_RENAME, "minted target refused 400"),
    "POST /v1/chash/rename_collection": ("route-chash-rename-refusal", CHASH_RENAME_REFUSAL,
                                         "chash minted target refused 400"),
    "POST /v1/taxonomy/rename_collection": ("route-taxonomy-rename-refusal", TAXONOMY_RENAME_REFUSAL,
                                            "taxonomy minted target refused 400"),
}

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
# RDR-229 P3.12: /v1/status names live_label_index_present (absent on an older engine or when its probe could not run;
# the doctor reads an absent key as "not checked", never false).
label_index = status.get("live_label_index_present")
assert label_index is None or isinstance(label_index, bool), "live_label_index_present is not a boolean"
print("live_label_index_present:", "absent" if label_index is None else label_index)
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

#: RDR-229 P3.5a: the gc routes' registry-row wait answers 503 rename_busy (quarantine-restore keeps quarantine_restore_busy);
#: route -> (step name, snippet, stdout the probe prints). Each is probed by a name nothing registers, so it cannot move anything.
GC_PROBES = {
    "POST /v1/vectors/gc/quarantine-orphans": ("route-gc-quarantine-orphans", GC_QUARANTINE_ORPHANS, "quarantine-orphans refused"),
    "POST /v1/vectors/gc/quarantine-restore": ("route-gc-quarantine-restore", GC_QUARANTINE_RESTORE, "quarantine-restore refused"),
    "POST /v1/vectors/gc/restore-rereferenced": ("route-gc-restore-rereferenced", GC_RESTORE_REREFERENCED, "restore-rereferenced refused"),
    "POST /v1/vectors/gc/expire-quarantine": ("route-gc-expire-quarantine", GC_EXPIRE_QUARANTINE, "expire-quarantine refused"),
}

#: RDR-229 rows 15 and 16 (nexus-p2m1x.22): the retiring verbs, asked by collection_id, read and lock that id in their own
#: transaction. A retiring verb cannot be probed on a real collection, so each is asked about an id no tenant's row has: the
#: route boundary answers 404 unknown_collection_id naming it, and nothing is read by label, retired or deleted. An engine
#: without the id selector refuses the body for its missing label, with no such reason, and fails the probe.
_RETIRING_BY_ID = r"""
import httpx
from nexus.commands.catalog import _get_catalog
probe_id = 9_000_000_000_000_000_000   # below bigint's top, far above any id the registry's sequence hands out
try:
    _get_catalog()._post("@path@", {"collection_id": probe_id, @extra@}, idempotent=False)
except httpx.HTTPStatusError as exc:
    body = exc.response.text
    assert exc.response.status_code == 404 and "unknown_collection_id" in body and str(probe_id) in body, (
        f"@verb@ by an unregistered collection_id answered {exc.response.status_code}: {body[:200]}")
    print("@verb@ by id refused 404 unknown_collection_id")
else:
    raise SystemExit("@verb@ accepted a collection_id no row has")
"""


def _retiring_probe(verb: str, path: str, extra: str) -> tuple[str, str, str]:
    code = _RETIRING_BY_ID.replace("@path@", path).replace("@extra@", extra).replace("@verb@", verb)
    return (f"route-collections-{verb}-by-id", code, f"{verb} by id refused 404 unknown_collection_id")


RETIRING_BY_ID_PROBES: dict[str, tuple[str, str, str]] = {
    "POST /v1/catalog/collections/supersede": _retiring_probe("supersede", "/collections/supersede",
                                                              '"superseded_by": "@label@-supersede-probe"'),
    "POST /v1/catalog/collections/rename": _retiring_probe("rename", "/collections/rename",
                                                           '"new_name": "@label@-rename-probe"'),
    "POST /v1/catalog/collections/delete": _retiring_probe("delete", "/collections/delete", ""),
}

#: RDR-229 P3.5c (nexus-p2m1x.25): the move routes name a tumbler or an owner and no collection, so no collection_id can be
#: sent to them and the 404 unknown_collection_id of a by-id route is not reachable from a request. What a probe can send is a
#: DRY RUN (a rolled-back transaction, whatever the answer) naming a document or an owner this tenant has none of. The engine
#: reads that row after it has taken the registry locks and before any write, and refuses 409 `{"error": <message>}` (a
#: MergeRefused, no `reason` key) naming the tumbler. The probe asserts that exact body, so an engine without the route (a bare
#: 404) or one that moved something (a 200) fails it. Owner 1.9999 is the one FOR_TUPLE already uses: no tenant registers it.
_MOVE_REFUSAL = r"""
import httpx
from nexus.catalog.engine_writes.move import @call@
from nexus.commands.catalog import _get_catalog
try:
    @call@(_get_catalog(), @args@, dry_run=True)
except httpx.HTTPStatusError as exc:
    body = exc.response.json()
    expected = {"error": "@message@"}
    assert exc.response.status_code == 409 and body == expected, (
        f"@verb@ move of @named@ (a @what@ this tenant has none of) answered {exc.response.status_code} {exc.response.text[:200]}, not 409 {expected}")
    print("@verb@ move dry run refused 409 naming @named@")
else:
    raise SystemExit("@verb@ move accepted @named@, which this tenant has none of")
"""

_MOVE_ABSENT_DOCUMENT = "document not found (absent, trashed, or not visible to this tenant): 1.9999.1"
_MOVE_ABSENT_OWNER = "owner not found (absent or not visible to this tenant): 1.9999"


def _move_probe(verb: str, call: str, args: str, message: str, what: str, named: str) -> tuple[str, str, str]:
    code = (_MOVE_REFUSAL.replace("@call@", call).replace("@args@", args).replace("@message@", message)
            .replace("@verb@", verb).replace("@what@", what).replace("@named@", named))
    return (f"route-{verb}-move-dry-run-refusal", code, f"{verb} move dry run refused 409 naming {named}")


MOVE_ROUTE_PROBES: dict[str, tuple[str, str, str]] = {
    "POST /v1/catalog/documents/move": _move_probe(
        "documents", "post_document_move",
        '"1.9999.1", new_source_uri="file:///cloud-leg-probe/@label@.pdf", new_file_path="/cloud-leg-probe/@label@.pdf"',
        _MOVE_ABSENT_DOCUMENT, "document", "1.9999.1"),
    "POST /v1/catalog/owners/move": _move_probe(
        "owners", "post_owner_move",
        '"1.9999", new_root="/cloud-leg-probe/@label@", new_repo_hash="@label@-move-probe"',
        _MOVE_ABSENT_OWNER, "owner", "1.9999"),
}


#: RDR-229 P3.5a row 14 (nexus-p2m1x.23): the ledger lines of the taxonomy split's 400 and the trash purge's registry read; route ->
#: (step name, snippet, stdout the probe prints). Neither probe can change a row: a topic nothing holds under a collection nothing
#: registers, and a read-only preview.
ROW14_PROBES = {
    "POST /v1/taxonomy/topics/persist_split": ("route-taxonomy-persist-split", PERSIST_SPLIT_REFUSAL, "persist_split refused"),
    "POST /v1/catalog/purge-trash": ("route-purge-trash-dry-run", PURGE_TRASH_DRY_RUN, "purge-trash dry run answered"),
}
