#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 P4 live gate — cloud→cloud server-side ingest end-to-end.

Exercises the REAL path the unit test fakes: seed a small collection into
ChromaCloud, trigger ``POST /v1/migration/ingest-cloud`` on a running nexus
service, and assert the service pulled the vectors server-side into pgvector
(count parity), then clean up the seeded ChromaCloud collection.

Portable: point ``NX_SERVICE_URL`` at a local dev service OR the deployed
``https://api.conexus-nexus.com``. The ChromaCloud source is the account named
by ``CHROMA_API_KEY`` / ``CHROMA_TENANT`` / ``CHROMA_DATABASE`` (already in
``.env``); this script SEEDS its own throwaway collection there and deletes it
on exit, so it never depends on pre-existing source data.

Env:
  NX_SERVICE_URL     base URL of the nexus service (e.g. http://127.0.0.1:PORT)
  NX_SERVICE_TOKEN   bearer token for the service
  NX_TENANT          target tenant (default "default")
  CHROMA_API_KEY / CHROMA_TENANT / CHROMA_DATABASE   source ChromaCloud creds

Exit 0 = GATE PASSED. Non-zero = failure (message on stderr).

NOTE: the Pillar 1b non-leak invariant (creds never persisted/logged) is pinned
by the Java unit test (MigrationHandlerIngestCloudTest); operators additionally
grep the live service logs for the api key after a run — this script does not
have access to the service's log stream.
"""
from __future__ import annotations

import os
import sys

# 4-segment conformant name so pgvector dimForCollection resolves (minilm → 384).
COLLECTION = "knowledge__ingestgate__minilm-l6-v2-384__v1"
DIM = 384
N_CHUNKS = 5


def _fail(msg: str) -> "int":
    print(f"INGEST-CLOUD GATE FAILED: {msg}", file=sys.stderr)
    return 1


def _chash(i: int) -> str:
    """Collision-free canonical chunk id (RDR-180: full 64-hex digest —
    the boundary now rejects non-hex/legacy-width ids)."""
    import hashlib
    return hashlib.sha256(f"ingestgate{i:022d}".encode()).hexdigest()


def main() -> int:
    url = os.environ.get("NX_SERVICE_URL", "").rstrip("/")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    tenant = os.environ.get("NX_TENANT", "default")
    src_tenant = os.environ.get("CHROMA_TENANT", "")
    src_db = os.environ.get("CHROMA_DATABASE", "")
    src_key = os.environ.get("CHROMA_API_KEY", "")
    missing = [k for k, v in {
        "NX_SERVICE_URL": url, "NX_SERVICE_TOKEN": token,
        "CHROMA_TENANT": src_tenant, "CHROMA_DATABASE": src_db,
        "CHROMA_API_KEY": src_key,
    }.items() if not v]
    if missing:
        return _fail(f"missing env: {', '.join(missing)}")

    import chromadb
    import httpx

    # ── 1. Seed a throwaway collection into ChromaCloud ──────────────────────
    client = chromadb.CloudClient(tenant=src_tenant, database=src_db, api_key=src_key)
    with_cleanup = False
    dest_cleanup_ids: list[str] = []
    try:
        try:
            client.delete_collection(COLLECTION)  # idempotent re-run
        except Exception:
            pass
        coll = client.create_collection(COLLECTION)
        with_cleanup = True
        ids = [_chash(i) for i in range(N_CHUNKS)]
        dest_cleanup_ids = list(ids)  # same chash ids land verbatim in pgvector
        embeddings = [[1.0 if j == i % DIM else 0.0 for j in range(DIM)] for i in range(N_CHUNKS)]
        docs = [f"ingest-cloud gate chunk {i}" for i in range(N_CHUNKS)]
        metas = [{"gate": "ingest-cloud", "i": i} for i in range(N_CHUNKS)]
        coll.upsert(ids=ids, embeddings=embeddings, documents=docs, metadatas=metas)
        print(f"[seed] ChromaCloud collection {COLLECTION}: {N_CHUNKS} chunks")

        # ── 2. Trigger server-side ingest-cloud ──────────────────────────────
        resp = httpx.post(
            f"{url}/v1/migration/ingest-cloud",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Nexus-Tenant": tenant,
                "Content-Type": "application/json",
            },
            json={
                "source_tenant": src_tenant,
                "source_database": src_db,
                "source_api_key": src_key,
                "collections": [COLLECTION],
                # RDR-178 Gap 5 made async (202 + job_id) the endpoint default;
                # this gate asserts the RDR-176 P4 synchronous contract, which
                # {"sync": true} preserves byte-for-byte.
                "sync": True,
            },
            timeout=120.0,
        )
        if resp.status_code != 200:
            # Never echo the request body (it carries the api key); status + body
            # (server-controlled, no cred) only.
            return _fail(f"ingest-cloud returned {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        print(f"[trigger] response: {body}")

        # ── 3. Assert count parity ───────────────────────────────────────────
        copied = (body.get("copied") or {}).get(COLLECTION)
        dest = (body.get("dest_counts") or {}).get(COLLECTION)
        if copied != N_CHUNKS:
            return _fail(f"copied[{COLLECTION}]={copied}, expected {N_CHUNKS}")
        if dest != N_CHUNKS:
            return _fail(f"dest_counts[{COLLECTION}]={dest}, expected {N_CHUNKS} "
                         f"(server-side pgvector count parity failed)")
        print(f"INGEST-CLOUD GATE PASSED — {N_CHUNKS} chunks copied server-side "
              f"(copied={copied}, dest={dest}); client sent zero vectors.")
        return 0
    finally:
        if with_cleanup:
            try:
                client.delete_collection(COLLECTION)
                print(f"[cleanup] deleted ChromaCloud SOURCE collection {COLLECTION}")
            except Exception as e:  # noqa: BLE001 — cleanup is best-effort
                print(f"[cleanup] WARNING: could not delete source {COLLECTION}: {e}", file=sys.stderr)
            # DEST-side cleanup: ingest-cloud lands the same chash ids verbatim in
            # pgvector on the target tenant; a passing run leaves them there
            # otherwise, so a repeat run against a real tenant would accumulate
            # residue. Idempotent — a 0-count delete is a silent no-op.
            try:
                del_resp = httpx.post(
                    f"{url}/v1/vectors/store-delete",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Nexus-Tenant": tenant,
                        "Content-Type": "application/json",
                    },
                    json={"collection": COLLECTION, "ids": dest_cleanup_ids},
                    timeout=30.0,
                )
                if del_resp.status_code == 200:
                    n = (del_resp.json() or {}).get("deleted", "?")
                    print(f"[cleanup] deleted {n} pgvector DEST chunks from {COLLECTION}")
                else:
                    print(f"[cleanup] WARNING: dest cleanup returned {del_resp.status_code}: "
                         f"{del_resp.text[:200]}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — cleanup is best-effort
                print(f"[cleanup] WARNING: could not delete dest chunks: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
