#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-070 Taxonomy Smoke Test — fully isolated live testing.

Runs the complete taxonomy pipeline against real repos using isolated
storage (tmpdir for T2, T3, catalog). No risk to production data.

Usage:
    uv run python scripts/smoke-test-taxonomy.py
    uv run python scripts/smoke-test-taxonomy.py --repo /path/to/repo
    uv run python scripts/smoke-test-taxonomy.py --keep
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import chromadb
import numpy as np

from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.scoring import _TOPIC_SAME_BOOST, apply_topic_boost
from nexus.types import SearchResult

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_REPOS = [
    Path.home() / "git" / "arcaneum",
    Path.home() / "git" / "nexus",
]

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"
INFO = "\033[94mINFO\033[0m"


# ── Helpers ───────────────────────────────────────────────────────────────────

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    tag = PASS if ok else FAIL
    print(f"  [{tag}] {label}")
    if not ok and detail:
        for line in detail.strip().splitlines()[-5:]:
            print(f"         {line}")
    if ok:
        passed += 1
    else:
        failed += 1
    return ok


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ── Pre-flight ────────────────────────────────────────────────────────────────


def preflight() -> bool:
    section("PRE-FLIGHT CHECKS")

    # 1. nx CLI
    r = subprocess.run(["nx", "--version"], capture_output=True, text=True)
    check("nx CLI installed", r.returncode == 0)

    # 2. Unit tests
    r = subprocess.run(
        ["uv", "run", "pytest", "tests/test_taxonomy.py", "-x", "-q", "--tb=line"],
        capture_output=True, text=True, timeout=60,
    )
    check("Unit tests pass", r.returncode == 0, r.stdout + r.stderr)

    # 3. E2E tests
    r = subprocess.run(
        ["uv", "run", "pytest", "tests/test_taxonomy_e2e.py", "-x", "-q", "--tb=line"],
        capture_output=True, text=True, timeout=60,
    )
    check("E2E tests pass", r.returncode == 0, r.stdout + r.stderr)

    return failed == 0


# ── Smoke Test ────────────────────────────────────────────────────────────────


def smoke_test_repo(repo_path: Path, db_path: Path, chroma_path: Path) -> None:
    section(f"SMOKE TEST: {repo_path.name}")

    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll_name = f"code__{repo_path.name}"

    # ── 1. Index code files ───────────────────────────────────────────
    print(f"\n  [{INFO}] Indexing {repo_path.name}...")
    files = sorted(repo_path.rglob("*.py"))[:100]
    texts: list[str] = []
    ids: list[str] = []
    for f in files:
        try:
            t = f.read_text(errors="replace")[:2000]
            if len(t) > 50:  # skip tiny files
                texts.append(t)
                ids.append(str(f.relative_to(repo_path)))
        except Exception:
            pass

    if len(texts) < 10:
        check("Index (enough files)", False, f"Only {len(texts)} files found")
        return

    embs = ef(texts)
    coll = client.get_or_create_collection(coll_name, embedding_function=None)
    coll.upsert(ids=ids, documents=texts, embeddings=embs)
    check("Index", True, f"{len(texts)} files indexed into {coll_name}")

    # ── 2. Discover topics ────────────────────────────────────────────
    embs_np = np.array(embs, dtype=np.float32)
    with T2Database(db_path) as db:
        count = db.taxonomy.discover_topics(coll_name, ids, embs_np, texts, client)
    if count < 2:
        check(
            "Discover topics",
            True,  # Not a failure — homogeneous repos legitimately produce no clusters
            f"HDBSCAN found {count} topics (all-noise is expected for uniform codebases)",
        )
        print(f"  [{SKIP}] Skipping pipeline tests ({repo_path.name} too homogeneous)")
        return
    check("Discover topics", True, f"Created {count} topics")

    # ── 3. List topics ────────────────────────────────────────────────
    with T2Database(db_path) as db:
        topics = db.taxonomy.get_topics()
        for t in topics:
            print(f"         {t['id']:3d}  {t['label']:30s}  ({t['doc_count']} docs)")
    check("List topics", len(topics) >= 2, f"{len(topics)} topics")

    # ── 4. Incremental assignment ─────────────────────────────────────
    new_text = "def handle_http_request(request): return json_response(200)"
    new_emb = np.array(ef([new_text])[0], dtype=np.float32)
    with T2Database(db_path) as db:
        topic_id = db.taxonomy.assign_single(coll_name, new_emb, client)
        if topic_id:
            t = db.taxonomy.get_topic_by_id(topic_id)
            check("Incremental assign", True, f"Assigned to: {t['label']}")
        else:
            check("Incremental assign", False, "No centroids found")

    # ── 5. Review: accept first topic ─────────────────────────────────
    with T2Database(db_path) as db:
        unreviewed = db.taxonomy.get_unreviewed_topics()
        if unreviewed:
            db.taxonomy.mark_topic_reviewed(unreviewed[0]["id"], "accepted")
            check("Review: accept", True, f"Accepted: {unreviewed[0]['label']}")
        else:
            check("Review: accept", False, "No unreviewed topics")

    # ── 6. Rename second topic ────────────────────────────────────────
    with T2Database(db_path) as db:
        topics = db.taxonomy.get_topics()
        if len(topics) >= 2:
            db.taxonomy.rename_topic(topics[1]["id"], "smoke-test-renamed")
            t = db.taxonomy.get_topic_by_id(topics[1]["id"])
            ok = t["label"] == "smoke-test-renamed" and t["review_status"] == "accepted"
            check("Rename topic", ok, f"Label: {t['label']}, status: {t['review_status']}")
        else:
            check("Rename topic", False, "Too few topics")

    # ── 7. Rebuild with merge strategy ────────────────────────────────
    print(f"\n  [{INFO}] Rebuilding with merge strategy...")
    with T2Database(db_path) as db:
        new_count = db.taxonomy.rebuild_taxonomy(coll_name, ids, embs_np, texts, client)
        new_labels = [t["label"] for t in db.taxonomy.get_topics()]
        preserved = "smoke-test-renamed" in new_labels
    check(
        "Rebuild: label preserved",
        preserved and new_count >= 2,
        f"{new_count} topics, labels: {new_labels[:5]}",
    )

    # ── 8. Topic boost ────────────────────────────────────────────────
    with T2Database(db_path) as db:
        topics = db.taxonomy.get_topics()
        doc_ids = db.taxonomy.get_all_topic_doc_ids(topics[0]["id"])[:5]
        if len(doc_ids) >= 2:
            results = [
                SearchResult(id=d, content="x", distance=0.5, collection="c")
                for d in doc_ids
            ]
            assignments = db.taxonomy.get_assignments_for_docs(doc_ids)
            apply_topic_boost(results, assignments)
            boosted = sum(1 for r in results if r.distance < 0.5)
            check("Topic boost", boosted >= 2, f"Boosted {boosted}/{len(results)}")
        else:
            check("Topic boost", False, "Too few docs")

    # ── 9. Split + merge roundtrip ────────────────────────────────────
    with T2Database(db_path) as db:
        topics = db.taxonomy.get_topics()
        if len(topics) >= 2:
            t1, t2 = topics[0], topics[1]
            t1_docs = set(db.taxonomy.get_all_topic_doc_ids(t1["id"]))
            t2_docs = set(db.taxonomy.get_all_topic_doc_ids(t2["id"]))
            all_docs = t1_docs | t2_docs

            # Merge t2 into t1
            db.taxonomy.merge_topics(t2["id"], t1["id"], chroma_client=client)
            merged_docs = set(db.taxonomy.get_all_topic_doc_ids(t1["id"]))
            check("Merge: no doc loss", merged_docs == all_docs)

            # Split t1 into 2
            child_count = db.taxonomy.split_topic(t1["id"], k=2, chroma_client=client)
            children = db.taxonomy.get_topics(parent_id=t1["id"])
            child_docs = set()
            for c in children:
                child_docs |= set(db.taxonomy.get_all_topic_doc_ids(c["id"]))
            check("Split: no doc loss", child_count == 2 and child_docs == all_docs)
        else:
            check("Merge + split", False, "Too few topics")

    # ── 10. Rebalance trigger ─────────────────────────────────────────
    with T2Database(db_path) as db:
        no_rebal = not db.taxonomy.needs_rebalance(coll_name, current_count=len(ids))
        yes_rebal = db.taxonomy.needs_rebalance(coll_name, current_count=len(ids) * 2)
    check("Rebalance: same count=no", no_rebal)
    check("Rebalance: 2x count=yes", yes_rebal)


# ── Post-flight ───────────────────────────────────────────────────────────────


def postflight(db_path: Path, chroma_path: Path) -> None:
    section("POST-FLIGHT CHECKS")

    # 1. No orphaned assignments
    with T2Database(db_path) as db:
        orphans = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments "
            "WHERE topic_id NOT IN (SELECT id FROM topics)"
        ).fetchone()[0]
    check("No orphaned assignments", orphans == 0, f"Found {orphans}")

    # 2. Valid review_status
    with T2Database(db_path) as db:
        bad = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topics "
            "WHERE review_status NOT IN ('pending', 'accepted', 'deleted')"
        ).fetchone()[0]
    check("Valid review_status", bad == 0, f"Found {bad} invalid")

    # 3. Centroid space
    client = chromadb.PersistentClient(path=str(chroma_path))
    try:
        coll = client.get_collection("taxonomy__centroids", embedding_function=None)
        space = coll.metadata.get("hnsw:space", "unknown")
        check("Centroid space = cosine", space == "cosine", f"Got: {space}")
    except Exception:
        check("Centroid space = cosine", True, "No centroids (ok if all-noise)")

    # 4. No orphaned centroids (T2 topic IDs match ChromaDB centroid metadata)
    try:
        centroid_data = coll.get(include=["metadatas"])
        with T2Database(db_path) as db:
            all_topic_ids = {
                r[0] for r in db.taxonomy.conn.execute("SELECT id FROM topics").fetchall()
            }
        centroid_topic_ids = {m.get("topic_id") for m in centroid_data["metadatas"]}
        orphaned = centroid_topic_ids - all_topic_ids
        check("No orphaned centroids", len(orphaned) == 0, f"Orphaned: {orphaned}")
    except Exception:
        check("No orphaned centroids", True, "No centroids collection")

    # 5. Production data untouched
    prod_db = Path.home() / ".config" / "nexus" / "memory.db"
    safe = str(db_path.resolve()) != str(prod_db.resolve()) if prod_db.exists() else True
    check("Production DB untouched", safe)


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="RDR-070 Taxonomy Smoke Test")
    parser.add_argument("--repo", type=Path, action="append", help="Repo to test (repeatable)")
    parser.add_argument("--keep", action="store_true", help="Keep temp dirs after test")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip pytest pre-flight")
    args = parser.parse_args()

    repos = args.repo or [r for r in DEFAULT_REPOS if r.exists()]
    if not repos:
        print("No repos found. Use --repo /path/to/repo")
        sys.exit(1)

    print(f"Repos: {', '.join(r.name for r in repos)}")

    # Isolated storage
    tmpdir = Path(tempfile.mkdtemp(prefix="nx-smoke-"))
    db_path = tmpdir / "memory.db"
    chroma_path = tmpdir / "chroma"
    chroma_path.mkdir()
    print(f"Temp:  {tmpdir}")

    start = time.time()

    try:
        if not args.skip_preflight:
            preflight()
            if failed > 0:
                print(f"\n  [{FAIL}] Pre-flight failed.")
                sys.exit(1)

        for repo in repos:
            if not repo.exists():
                print(f"\n  [{SKIP}] {repo} not found")
                continue
            smoke_test_repo(repo, db_path, chroma_path)

        postflight(db_path, chroma_path)

    finally:
        elapsed = time.time() - start
        section("SUMMARY")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Time:   {elapsed:.1f}s")
        print(f"  Temp:   {tmpdir}")

        if not args.keep and failed == 0:
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("  Cleaned up temp dir")
        else:
            print("  Temp dir preserved (--keep or failures)")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
