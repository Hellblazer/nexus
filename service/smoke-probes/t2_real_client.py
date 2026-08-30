# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-cm5km: extracted verbatim from the inline `uv run python -c '...'`
# heredoc in service/native-smoke.sh's "memory/plans/taxonomy/chash via the
# REAL Python client" section (nexus-rxqqd). Living in its own file lets
# the unit suite (tests/test_native_smoke_client_probes.py) run the
# IDENTICAL code the native-smoke gate runs, instead of a hand-copied
# approximation that can drift out of sync with the shell script. See
# native-smoke.sh for the env contract (NEXUS_CONFIG_DIR,
# NX_SERVICE_HOST/PORT/TOKEN, NATIVE_SMOKE_CLEANUP_ROWS) this script
# expects to already be set in its environment.
import os

from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

mem = HttpMemoryStore()
mem.put(project="native-smoke-py", title="a", content="memory native smoke", tags="t")
got = mem.get(project="native-smoke-py", title="a")
assert got is not None, "memory.get returned None for a just-put entry"
assert got["content"] == "memory native smoke", got
results = mem.search("native smoke", project="native-smoke-py")
assert any(r.get("title") == "a" for r in results), f"memory.search did not find title=a: {results}"
entries = mem.list_entries(project="native-smoke-py")
assert any(e.get("title") == "a" for e in entries), f"memory.list_entries did not find title=a: {entries}"

plans = HttpPlanLibrary()
plan_id = plans.save_plan(query="native smoke plan query", plan_json="{\"steps\": []}", project="native-smoke-py", verb="research")  # verb required since hygiene-001 (b9ab65606): the client raises before the wire without it; burned engine-service-v0.1.90
plan_results = plans.search_plans("native smoke plan", project="native-smoke-py")
assert plan_results, f"plans.search_plans found nothing: {plan_results}"

tax = HttpTaxonomyStore()
# Fixed, not random (nexus-rxqqd review): deterministic per project convention,
# and import_topic is an ID-preserving upsert -- reusing the same src_id on a
# rerun overwrites in place instead of accumulating a fresh row every run.
src_id = 999_888_777
tax.import_topic(
    src_id=src_id, label="native-smoke-topic", parent_id=None,
    collection="native-smoke-py", centroid_hash=None, doc_count=1,
    created_at="2026-01-01T00:00:00Z", review_status="approved", terms=None,
)
topics = tax.get_all_topics(collection="native-smoke-py")
assert any(t.get("label") == "native-smoke-topic" for t in topics), f"taxonomy.get_all_topics did not find the seeded topic: {topics}"

# RDR-187 P6 410-flip (nexus-piwya.11, engine-service-v0.1.53): the
# /v1/chash/* WRITE endpoints are now 410 Gone (the accept-and-no-op window
# closed). Chunk ingest is the sole write path; the chash router is retired.
# So the leg pins the RETIRED-write contract: upsert now RAISES HTTP 410
# (the production caller dual_write_chash_index swallows this best-effort;
# the smoke calls the client directly so it observes the raise), while the
# surviving READ endpoints still respond and surface nothing.
import httpx
chash = HttpChashIndex()
try:
    chash.upsert(chash="deadbeef" * 8, collection="native-smoke-py")
    raise AssertionError("chash.upsert must 410 post-RDR-187 P6, but it did not raise")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 410, f"chash.upsert must 410 (RDR-187 retirement), got HTTP {exc.response.status_code}"
collections = chash.distinct_collections()
assert "native-smoke-py" not in collections, f"retired chash.upsert must leave no trace: {collections}"
rows = chash.lookup("deadbeef" * 8)
assert rows == [], f"retired chash.upsert must leave no readable row: {rows}"

if os.environ.get("NATIVE_SMOKE_CLEANUP_ROWS") == "1":
    # nexus-rxqqd review follow-up (substantive-critic): best-effort row
    # cleanup, only when pointed at an external (non-throwaway) NX_DB_URL --
    # see the NATIVE_SMOKE_CLEANUP_ROWS comment near the top of this file for why.
    #
    # Each delete wrapped individually and isolated from the assertions above
    # (2nd-round review follow-up, substantive-critic): a cleanup-only
    # exception must never masquerade as an assertion failure to whoever is
    # debugging a FAIL -- print CLEANUP-WARN and keep going for each surface
    # independently, rather than let one failure (a) abort the remaining
    # three deletes and (b) skip print("OK"), making grep -q "^OK$" fail
    # identically to a genuine assertion failure despite every real check
    # above having already passed.
    for _label, _cleanup in (
        ("memory", lambda: mem.delete(project="native-smoke-py", title="a")),
        ("plans", lambda: plans.delete_plan(plan_id)),
        ("taxonomy", lambda: tax.delete_topic(src_id)),
        ("chash", lambda: chash.delete_collection("native-smoke-py")),
    ):
        try:
            _cleanup()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup, see comment above
            print(f"CLEANUP-WARN: {_label} cleanup failed: {exc}")

print("OK")
