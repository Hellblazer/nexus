# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Seed a local install for the upgrade-boot write gate (nexus-f3yqd.1).

Run by ``scripts/upgrade_boot_writes.py`` under the PREVIOUS release's client
(``uvx --from conexus==<prev> python upgrade_boot_seed.py ...``) against the
previous engine, so the fixture is what a user of that release holds. It uses
only client APIs that release ships: ``nx store put`` mints each collection the
way a user's first note does, then catalog ``register`` and one combined
``write_manifest_many`` per page of documents (manifest rows, chunk text and a
client-supplied vector per chunk, RDR-223 P1.5) add owned documents in bulk.

Vectors are seeded unit vectors of the collection's own dimension (HNSW build
cost does not depend on their meaning). Chunk text is unique per row, because a
chunk's id is sha256 of its text and identical text collapses to one row.

Prints one JSON line: the physical collection names and the rows written to each.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys

_WORDS = (
    "index vector query cluster partition replica schema tenant manifest chunk catalog "
    "document collection embedding search rank filter merge segment shard ledger commit "
    "boundary migration rebuild planner threshold recall latency budget window sample"
).split()
_CHUNKS_PER_DOC = 50
_BATCH = 300


def _text(rng: random.Random, subject: str, doc: int, pos: int) -> str:
    body = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(40, 90)))
    return f"{subject} document {doc}, section {pos}. {body.capitalize()}."


def _unit_vectors(rng: random.Random, n: int, dim: int) -> list[list[float]]:
    try:
        import numpy as np  # noqa: PLC0415 - optional speedup; the client ships it with onnxruntime
    except ImportError:
        out = []
        for _ in range(n):
            v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
            s = sum(x * x for x in v) ** 0.5
            out.append([x / s for x in v])
        return out
    g = np.random.default_rng(rng.randrange(2**32))
    m = g.standard_normal((n, dim), dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m.tolist()


def sentinel_text(subject: str) -> str:
    """The first note of each collection, stored through ``nx store put`` so the engine embeds it for real:
    the one row a semantic search can find (the bulk rows carry random vectors). Title ``<subject>-first-note``."""
    return (f"Sentinel note for the upgrade-boot fixture in {subject}: the heron counts amber lanterns "
            f"at the quiet ferry before dawn.")


def _physical(t3, subject: str) -> str:
    names = [c["name"] if isinstance(c, dict) else str(getattr(c, "name", c)) for c in t3.list_collections()]
    hits = [n for n in names if n.startswith(f"knowledge__{subject}__")]
    if len(hits) != 1:
        sys.exit(f"upgrade_boot_seed: expected one collection for {subject}, found {hits} in {names}")
    return hits[0]


def seed(subject: str, rows: int, dim: int, rng: random.Random) -> tuple[str, int]:
    subprocess.run(["nx", "store", "put", "-", "--collection", subject, "--title", f"{subject}-first-note"],
                   input=sentinel_text(subject),
                   text=True, check=True, capture_output=True)
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    t3 = make_t3()
    name = _physical(t3, subject)
    reader, writer = make_catalog_reader(), make_catalog_writer()
    if reader is None or writer is None:
        sys.exit("upgrade_boot_seed: no catalog (is the install's service running?)")
    owner = reader.curator_owner_tumbler_by_name("knowledge") or writer.register_owner("knowledge", "curator")
    model = name.split("__")[2]
    written = 0
    doc = 0
    while written < rows:
        # One combined write per page of documents: manifests, chunk text and vectors in one request, the
        # only shape an ownership-enforcing engine accepts for new chunks (0.1.184 refuses a bare upsert).
        docs: list[tuple[str, list[dict]]] = []
        chunks: list[dict] = []
        while written < rows and len(chunks) + _CHUNKS_PER_DOC <= _BATCH:
            n = min(_CHUNKS_PER_DOC, rows - written)
            title = f"{subject} seeded document {doc}"
            texts = [_text(rng, subject, doc, p) for p in range(n)]
            ids = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
            tumbler = writer.register(owner, title=title, content_type="knowledge", file_path="",
                                      physical_collection=name, chunk_count=n,
                                      source_uri=f"nx-seed://{subject}/{doc}", meta={"seeded_by": "nexus-f3yqd.1"})
            docs.append((str(tumbler), [
                {"chash": c, "position": p, "line_start": None, "line_end": None, "char_start": None,
                 "char_end": None} for p, c in enumerate(ids)]))
            for p, (c, t, v) in enumerate(zip(ids, texts, _unit_vectors(rng, n, dim))):
                chunks.append({"chash": c, "text": t, "embedding": v,
                               "metadata": {"title": title, "source_title": title, "chunk_index": p,
                                            "content_type": "knowledge"}})
            written += n
            doc += 1
        res = writer.write_manifest_many(docs, chunks=chunks, collection=name, embedding_model=model)
        if res.get("failed_doc_ids") or res.get("chunks_written") != len(chunks):
            sys.exit(f"upgrade_boot_seed: write_many on {name} wrote {res.get('chunks_written')} of "
                     f"{len(chunks)} chunks, failed docs {res.get('failed_doc_ids')}")
    return name, written


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed a local install for the upgrade-boot write gate.")
    ap.add_argument("--subject", action="append", required=True,
                    help="SUBJECT=ROWS, repeatable, e.g. bootw-a=27000")
    ap.add_argument("--dim", type=int, default=768)
    ap.add_argument("--seed", type=int, default=20261110)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = {}
    for spec in a.subject:
        subject, _, rows = spec.partition("=")
        name, n = seed(subject, int(rows), a.dim, rng)
        out[name] = n
        sys.stderr.write(f"upgrade_boot_seed: {name} {n} rows\n")
    sys.stdout.write(json.dumps({"collections": out}) + "\n")


if __name__ == "__main__":
    main()
