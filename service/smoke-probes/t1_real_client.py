# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-cm5km: extracted verbatim from the inline `uv run python -c '...'`
# heredoc in service/native-smoke.sh's "T1 via the REAL Python client"
# section (nexus-97oz3). Living in its own file lets the unit suite
# (tests/test_native_smoke_client_probes.py) run the IDENTICAL code the
# native-smoke gate runs, instead of a hand-copied approximation that can
# drift out of sync with the shell script. See native-smoke.sh for the env
# contract (NEXUS_CONFIG_DIR, NX_SERVICE_HOST/PORT/TOKEN,
# NX_STORAGE_BACKEND=service, NATIVE_SMOKE_CLEANUP_ROWS) this script expects
# to already be set in its environment.
import os

from nexus.db.t1 import get_t1_database

t1 = get_t1_database()
doc_id = t1.put("t1 native smoke via real python client", tags="native-smoke-py")
assert doc_id, "put returned no id"

got = t1.get(doc_id)
assert got is not None, "get returned None for a just-put id"
assert got["content"] == "t1 native smoke via real python client", got

results = t1.search("native smoke via real python", n_results=5)
assert any(r["id"] == doc_id for r in results), f"search did not find {doc_id}: {results}"

entries = t1.list_entries()
assert any(e["id"] == doc_id for e in entries), f"list_entries did not find {doc_id}: {entries}"

if os.environ.get("NATIVE_SMOKE_CLEANUP_ROWS") == "1":
    # Best-effort, deliberately isolated from the assertions above (review
    # follow-up, substantive-critic): a cleanup-only exception here must
    # never read as an assertion failure to whoever is debugging a FAIL --
    # print CLEANUP-WARN and keep going rather than let it propagate past
    # print("OK"), which would make grep -q "^OK$" fail identically to a
    # genuine assertion failure despite every real check above having passed.
    try:
        t1.delete(doc_id)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, see comment above
        print(f"CLEANUP-WARN: t1.delete({doc_id!r}) failed: {exc}")

print("OK")
