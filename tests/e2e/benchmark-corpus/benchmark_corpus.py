# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic parts of the pinned local-mode benchmark corpus (nexus-2rs76).

``tests/e2e/benchmark-corpus.sh`` calls this module for everything that is
data or a verdict, so ``test_benchmark_corpus.py`` drives the real code the
script runs instead of grepping its text:

* the pins (release tag, commit, conexus version, chunk ranges);
* the snapshot path filter and the snapshot materialiser;
* the 30-note, 2-subject knowledge fixture;
* the verdicts on the collection listing, the store listing, the search
  JSON and the process list.

Standard library only. The script resolves an interpreter (3.10+) through
``tests/e2e/lib/python.sh`` and runs this file with it from the host, where
the installed ``nx`` venv is not importable.

Sub-commands (see ``main``): ``pin``, ``snapshot``, ``notes``, ``probes``,
``check-counts``, ``check-listing``, ``check-search``, ``leftover``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

# ── pins ────────────────────────────────────────────────────────────────────

#: The release the snapshot is cut from. ``PIN_SHA`` is what ``git rev-list -n1
#: PIN_TAG`` printed on 2026-10-12; the integration-marked test re-derives it.
PIN_TAG = "v7.77.1"
PIN_SHA = "aa226556f4d0ae6ef8841c31873c03336c086efd"

#: The published conexus the sandbox installs. Pinned with the snapshot so a
#: measurement names one (client, corpus) pair; ``--conexus-version`` overrides
#: it on purpose, and the PASSED line then says so.
CONEXUS_VERSION = "7.77.1"

#: Snapshot filter. Only ``src/nexus`` and ``docs`` are taken. ``tests/`` is
#: never under those roots; ``docs/rdr/`` is the RDR archive and is cut out by
#: name. ``EXCLUDE_PREFIXES`` is also where a trim goes if the corpus outgrows
#: its range.
SNAPSHOT_ROOTS: tuple[str, ...] = ("src/nexus/", "docs/")
EXCLUDE_PREFIXES: tuple[str, ...] = (
    "docs/rdr/",
    # Trimmed after the first measurement: src/nexus plus docs minus docs/rdr
    # indexed to 15,623 chunks at v7.77.1 (code 13,150, docs 2,473), over the
    # 8,000 to 12,000 target. Cut, largest first: CLI wiring (click wrappers,
    # about 2,700 chunks) and its generated reference, the wire-ledger file,
    # post-mortem logs, plan templates, install scripts, hooks and console assets.
    "src/nexus/commands/",
    "src/nexus/plans/",
    "src/nexus/_install/",
    "src/nexus/hooks/",
    "src/nexus/console/",
    "docs/cli-reference.md",
    "docs/wire-contract-pending.md",
    "docs/postmortem/",
)
#: Binary or generated suffixes the indexer would only skip.
EXCLUDE_SUFFIXES: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".pyc", ".woff", ".woff2",
)

#: Hash of the filtered ``(mode, blob, path)`` list at ``PIN_SHA`` and its size.
#: A change to the filter, or a different tree behind the sha, moves these.
SNAPSHOT_FILE_COUNT = 337
SNAPSHOT_DIGEST = "29f5e117f4dabdca8106840652a026544431c17f7d7684e9f76f0c0c621208c3"

#: Chunk-count ranges the first index must land in (inclusive). Measured on the
#: pinned conexus; see ``CHUNK_RANGE_NOTE``.
CHUNK_RANGE_TOTAL: tuple[int, int] = (10000, 11800)
CHUNK_RANGE_CODE: tuple[int, int] = (8500, 10000)
CHUNK_RANGE_DOCS: tuple[int, int] = (1500, 1750)
CHUNK_RANGE_NOTE = (
    "Measured 2026-10-12 on chas (lab-m grant, 8 CPUs, 2 index workers, conexus 7.77.1, bge-base-en-v15-768): "
    "code 9,263 + docs 1,620 = 10,883 chunks, first index 1,557 s (6.99 chunks/s, box shared with CI). "
    "Ranges are measured -8%/+8%, rounded: a chunker change inside a conexus release moves the count a little, "
    "a changed snapshot moves it a lot."
)

#: The commit made inside the sandbox is reproducible: fixed identity and date.
COMMIT_IDENTITY = ("nexus-benchmark-corpus", "benchmark-corpus@example.invalid")
COMMIT_DATE = "2026-10-12T00:00:00+0000"

#: Subject areas of the knowledge fixture. Durable subjects per
#: docs/collections.md, never a placeholder.
SUBJECT_DISTRIBUTED = "distributed-systems"
SUBJECT_VECTOR = "vector-search"
SUBJECTS: tuple[str, ...] = (SUBJECT_DISTRIBUTED, SUBJECT_VECTOR)
NOTES_PER_SUBJECT = 15


# ── snapshot ────────────────────────────────────────────────────────────────


def in_snapshot(path: str) -> bool:
    """True when the repo-relative *path* belongs to the benchmark snapshot."""
    if not path.startswith(SNAPSHOT_ROOTS):
        return False
    if path.startswith(EXCLUDE_PREFIXES):
        return False
    if "/__pycache__/" in path:
        return False
    return not path.lower().endswith(EXCLUDE_SUFFIXES)


@dataclass(frozen=True)
class Entry:
    mode: str
    blob: str
    path: str


def _git(repo: str | os.PathLike[str], *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=text, check=True,
    )


def snapshot_entries(repo: str | os.PathLike[str], sha: str = PIN_SHA) -> list[Entry]:
    """The filtered, sorted regular-file entries of *sha*'s tree."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", sha, "--", "src/nexus", "docs"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    entries: list[Entry] = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, kind, blob = meta.split(" ")
        if kind != "blob" or mode not in ("100644", "100755"):
            continue
        if in_snapshot(path):
            entries.append(Entry(mode, blob, path))
    entries.sort(key=lambda e: e.path)
    return entries


def snapshot_digest(entries: list[Entry]) -> str:
    """sha256 over the sorted ``mode blob path`` lines."""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda e: e.path):
        h.update(f"{e.mode} {e.blob} {e.path}\n".encode())
    return h.hexdigest()


def materialize(repo: str | os.PathLike[str], dest: str | os.PathLike[str], sha: str = PIN_SHA) -> dict[str, object]:
    """Write the snapshot of *sha* into *dest* and commit it as a fresh repo.

    Refuses when the filtered tree does not match the pinned count and digest,
    so a snapshot that drifted never reaches the index.
    """
    entries = snapshot_entries(repo, sha)
    digest = snapshot_digest(entries)
    if len(entries) != SNAPSHOT_FILE_COUNT or digest != SNAPSHOT_DIGEST:
        raise SystemExit(
            f"snapshot of {sha} is {len(entries)} files / {digest}; "
            f"pinned {SNAPSHOT_FILE_COUNT} files / {SNAPSHOT_DIGEST}"
        )
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    if any(dest_path.iterdir()):
        raise SystemExit(f"snapshot destination {dest_path} is not empty")
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha, "--", *[e.path for e in entries]],
        capture_output=True, check=True,
    ).stdout
    wanted = {e.path for e in entries}
    written = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if not member.isfile() or member.name not in wanted:
                continue
            target = dest_path / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            fobj = tar.extractfile(member)
            assert fobj is not None
            target.write_bytes(fobj.read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            written += 1
    if written != len(entries):
        raise SystemExit(f"extracted {written} files, expected {len(entries)}")
    name, email = COMMIT_IDENTITY
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(dest_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": COMMIT_DATE,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": COMMIT_DATE,
    }

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(dest_path), *args], env=env, check=True, capture_output=True)

    run("init", "-q", "--template=", "-b", "main")
    run("add", "--", ".")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m", f"benchmark corpus snapshot of {PIN_TAG} ({sha})")
    head = subprocess.run(
        ["git", "-C", str(dest_path), "rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return {"files": written, "digest": digest, "head": head}


# ── knowledge fixture ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Note:
    subject: str
    title: str
    body: str


def _notes() -> tuple[Note, ...]:
    ds = SUBJECT_DISTRIBUTED
    vs = SUBJECT_VECTOR
    return (
        Note(ds, "ds-01-raft-leader-election",
             "Raft elects a leader with randomised election timeouts. A follower that hears nothing "
             "from a leader before its timeout becomes a candidate and asks its peers for votes."),
        Note(ds, "ds-02-paxos-two-phases",
             "Single-decree Paxos runs a prepare phase and an accept phase. A proposer needs "
             "promises from a majority of acceptors before it may send a value."),
        Note(ds, "ds-03-vector-clocks",
             "Vector clocks keep one counter per node so that two events can be ordered or found "
             "concurrent. Comparing two vectors entry by entry tells which event happened before."),
        Note(ds, "ds-04-crdt-merge",
             "A state-based CRDT merges replicas with a join that is commutative, associative and "
             "idempotent, so replicas converge whatever order the updates arrive in."),
        Note(ds, "ds-05-gossip-dissemination",
             "Gossip protocols spread an update by having each node tell a few random peers every "
             "round. The number of rounds to reach everyone grows with the logarithm of the cluster size."),
        Note(ds, "ds-06-consistent-hashing",
             "Consistent hashing places nodes and keys on a ring. Removing a node moves only the keys "
             "that node owned to its successor instead of reshuffling the whole keyspace."),
        Note(ds, "ds-07-two-phase-commit-blocking",
             "Two-phase commit blocks when the coordinator fails after participants voted yes. "
             "Participants hold their locks until the coordinator returns or an operator intervenes."),
        Note(ds, "ds-08-quorum-intersection",
             "Quorum reads and writes overlap when the read quorum plus the write quorum exceeds the "
             "replica count, so every read sees at least one copy of the latest write."),
        Note(ds, "ds-09-lamport-timestamps",
             "Lamport timestamps give a total order consistent with causality by taking the maximum "
             "of the local and received counters and adding one on every event."),
        Note(ds, "ds-10-phi-accrual-detector",
             "The phi accrual failure detector reports suspicion as a continuous value rather than a "
             "binary alive-or-dead verdict, derived from the distribution of heartbeat inter-arrival times."),
        Note(ds, "ds-11-log-compaction-snapshots",
             "A replicated log grows without bound unless the state machine writes a snapshot and the "
             "log prefix it covers is discarded. Slow followers then receive the snapshot instead."),
        Note(ds, "ds-12-chain-replication",
             "Chain replication sends writes to the head and acknowledges from the tail. Reads served "
             "by the tail are strongly consistent because they see only fully replicated writes."),
        Note(ds, "ds-13-pbft-three-f-plus-one",
             "Byzantine fault tolerance needs three f plus one replicas to survive f arbitrary "
             "failures, because a faulty node can lie to different peers in different ways."),
        Note(ds, "ds-14-lease-based-reads",
             "A leader that holds a time-bounded lease can answer reads locally without contacting a "
             "quorum, as long as clock drift between nodes stays below the lease margin."),
        Note(ds, "ds-15-hinted-handoff",
             "Hinted handoff lets a coordinator store a write meant for a down replica on another "
             "node, with a hint to deliver it when the replica comes back."),
        Note(vs, "vs-01-hnsw-layers",
             "HNSW builds a hierarchy of proximity graphs. A query enters at the sparse top layer and "
             "descends greedily, widening its beam only on the dense bottom layer."),
        Note(vs, "vs-02-ivf-nprobe",
             "An inverted file index clusters vectors around coarse centroids. At query time only the "
             "nprobe nearest clusters are scanned, trading recall for speed."),
        Note(vs, "vs-03-product-quantization",
             "Product quantization splits a vector into subvectors and encodes each with a small "
             "codebook, shrinking memory by an order of magnitude at some cost in accuracy."),
        Note(vs, "vs-04-cosine-versus-inner-product",
             "Cosine similarity equals the inner product once vectors are normalised to unit length, "
             "so a normalised index can use the cheaper dot product."),
        Note(vs, "vs-05-recall-at-k",
             "Recall at k is the share of the true k nearest neighbours that an approximate search "
             "returns. It is measured against an exact brute-force scan of the same data."),
        Note(vs, "vs-06-filtered-search",
             "Filtered vector search applies a metadata predicate along with the nearest-neighbour "
             "query. A very selective filter can leave a graph index with too few reachable candidates."),
        Note(vs, "vs-07-reciprocal-rank-fusion",
             "Reciprocal rank fusion merges a lexical ranking and a vector ranking by summing one "
             "over a constant plus the rank, which needs no score calibration between the two."),
        Note(vs, "vs-08-cross-encoder-rerank",
             "A cross-encoder reranker reads the query and a candidate together and scores the pair. "
             "It is far slower than a bi-encoder, so it runs only on the top few dozen candidates."),
        Note(vs, "vs-09-embedding-dimensionality",
             "Higher embedding dimensionality stores more information per vector but raises memory, "
             "index build time and distance computation cost in proportion."),
        Note(vs, "vs-10-diskann-ssd-graph",
             "DiskANN keeps a Vamana graph on an SSD and holds only compressed vectors in memory, so "
             "a billion-vector index fits on a single machine."),
        Note(vs, "vs-11-scalar-quantization",
             "Scalar quantization maps each float component to an eight-bit integer. It cuts memory to "
             "a quarter and speeds up distance computation with little loss of recall."),
        Note(vs, "vs-12-chunk-size-tradeoff",
             "Small chunks make retrieval precise but lose surrounding context, while large chunks "
             "keep context and dilute the embedding of any single idea."),
        Note(vs, "vs-13-query-expansion",
             "Query expansion adds related terms or a generated hypothetical answer to the query "
             "before embedding, to bridge the vocabulary gap between question and document."),
        Note(vs, "vs-14-build-versus-query-cost",
             "Raising the construction beam width of a graph index slows the build but improves recall "
             "at a given query latency, so build cost is paid once to save it on every query."),
        Note(vs, "vs-15-batch-embedding-throughput",
             "Embedding throughput depends on batching: grouping chunks of similar length wastes "
             "less compute on padding than feeding them in arrival order."),
    )


NOTES: tuple[Note, ...] = _notes()


def notes_manifest() -> list[tuple[str, str, str]]:
    """``(subject, title, body)`` for every note, in a fixed order."""
    return [(n.subject, n.title, n.body) for n in NOTES]


def write_notes(dest: str | os.PathLike[str]) -> list[tuple[str, str, Path]]:
    """Write each note to ``dest/<subject>/<title>.md``; return ``(subject, title, path)``."""
    out: list[tuple[str, str, Path]] = []
    for n in NOTES:
        d = Path(dest) / n.subject
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{n.title}.md"
        p.write_text(n.body + "\n", encoding="utf-8")
        out.append((n.subject, n.title, p))
    return out


# ── search probes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Probe:
    name: str
    corpus: str  #: value for ``nx search --corpus``
    query: str
    collection_prefix: str  #: a hit's collection must start with this
    must_contain: str  #: whitespace-normalised, case-insensitive text the hit must hold
    max_rank: int  #: the hit must be within the first N results


def _note(title: str) -> Note:
    return next(n for n in NOTES if n.title == title)


PROBES: tuple[Probe, ...] = (
    # A sentence from a pinned source file's module docstring; the hit must be a
    # code__ chunk holding it.
    Probe("repo-code", "code",
          "a version mismatch on a local install is a convergence step, never a user-facing refusal",
          "code__", "never a user-facing refusal", 10),
    # A sentence from docs/collections.md at the pin; the hit must be a docs__ chunk.
    Probe("repo-docs", "docs",
          "a collection with one document has the cost of a partition and none of the benefit",
          "docs__", "cost of a partition", 10),
    Probe("note-distributed", "knowledge",
          _note("ds-10-phi-accrual-detector").body.split(". ")[0],
          "knowledge__", "phi accrual failure detector", 3),
    Probe("note-vector", "knowledge",
          _note("vs-11-scalar-quantization").body.split(". ")[0],
          "knowledge__", "scalar quantization maps each float component", 3),
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def probe_verdict(probe: Probe, json_text: str) -> str | None:
    """None when *json_text* (``nx search --json``) satisfies *probe*, else why not."""
    try:
        hits = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return f"{probe.name}: search output is not JSON ({exc})"
    if not isinstance(hits, list) or not hits:
        return f"{probe.name}: search returned no hits"
    needle = _norm(probe.must_contain)
    for rank, hit in enumerate(hits[: probe.max_rank], start=1):
        coll = str(hit.get("collection", ""))
        hay = _norm(f"{hit.get('title', '')} {hit.get('content', '')}")
        if coll.startswith(probe.collection_prefix) and needle in hay:
            return None
    seen = ", ".join(str(h.get("collection", "?")).split("__")[0] for h in hits[: probe.max_rank])
    return (
        f"{probe.name}: none of the first {probe.max_rank} hits is a {probe.collection_prefix}* chunk "
        f"containing {probe.must_contain!r} (collections seen: {seen})"
    )


def probe_rank(probe: Probe, json_text: str) -> int | None:
    """1-based rank of the first hit that satisfies *probe*, or None."""
    hits = json.loads(json_text)
    needle = _norm(probe.must_contain)
    for rank, hit in enumerate(hits[: probe.max_rank], start=1):
        hay = _norm(f"{hit.get('title', '')} {hit.get('content', '')}")
        if str(hit.get("collection", "")).startswith(probe.collection_prefix) and needle in hay:
            return rank
    return None


# ── verdicts ────────────────────────────────────────────────────────────────

_ROW = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)(?:\s|$)")


def parse_collection_list(text: str) -> dict[str, int]:
    """``name -> CHUNKS`` from the collection listing (header skipped)."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        m = _ROW.match(line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def check_counts(counts: dict[str, int]) -> list[str]:
    """Why *counts* is not the pinned corpus; an empty list means it is."""
    problems: list[str] = []
    code = {n: c for n, c in counts.items() if n.startswith("code__")}
    docs = {n: c for n, c in counts.items() if n.startswith("docs__")}
    know = {n: c for n, c in counts.items() if n.startswith("knowledge__")}
    other = sorted(n for n in counts if not n.startswith(("code__", "docs__", "knowledge__")))
    if other:
        problems.append(f"unexpected collections: {', '.join(other)}")
    for label, group, rng in (("code", code, CHUNK_RANGE_CODE), ("docs", docs, CHUNK_RANGE_DOCS)):
        if len(group) != 1:
            problems.append(f"expected exactly one {label}__ collection, found {len(group)}: {sorted(group)}")
            continue
        n = next(iter(group.values()))
        if not rng[0] <= n <= rng[1]:
            problems.append(f"{label} collection holds {n} chunks, outside the pinned range {rng[0]}..{rng[1]}")
    total = sum(code.values()) + sum(docs.values())
    if not CHUNK_RANGE_TOTAL[0] <= total <= CHUNK_RANGE_TOTAL[1]:
        problems.append(
            f"repo collections hold {total} chunks, outside the pinned range "
            f"{CHUNK_RANGE_TOTAL[0]}..{CHUNK_RANGE_TOTAL[1]}"
        )
    subjects = sorted(n.split("__")[1] for n in know if n.count("__") >= 3)
    if subjects != sorted(SUBJECTS):
        problems.append(f"knowledge subjects are {subjects}, expected {sorted(SUBJECTS)}")
    for n, c in sorted(know.items()):
        if c != NOTES_PER_SUBJECT:
            problems.append(f"{n} holds {c} chunks, expected {NOTES_PER_SUBJECT} (one per note)")
    if sum(know.values()) != len(NOTES):
        problems.append(f"knowledge collections hold {sum(know.values())} chunks, expected {len(NOTES)}")
    return problems


def listing_problems(subject: str, listing: str) -> list[str]:
    """Why the store listing of *subject* does not show every note of it."""
    problems: list[str] = []
    ids = re.findall(r"^\s+([0-9a-f]{64})\s", listing, re.M)
    expected = [n for n in NOTES if n.subject == subject]
    if len(ids) != len(expected):
        problems.append(f"{subject}: listing shows {len(ids)} entries, expected {len(expected)}")
    for n in expected:
        if n.title[:40] not in listing:
            problems.append(f"{subject}: note {n.title} is missing from the listing")
    return problems


def leftover_processes(ps_text: str, sandbox: str, ignore: tuple[str, ...] = ("benchmark-corpus.sh", "benchmark_corpus.py")) -> list[str]:
    """Lines of ``ps -axww -o pid= -o args=`` whose command line names *sandbox*.

    Matches the sandbox path as a fixed string in the arguments, never a
    pattern, and skips this tooling's own command lines (they carry the path
    as an argument).
    """
    if not sandbox:
        raise ValueError("an empty sandbox path would match every process")
    return [
        line.strip()
        for line in ps_text.splitlines()
        if sandbox in line and not any(tok in line for tok in ignore)
    ]


def report(counts: dict[str, int], index_seconds: float) -> list[str]:
    repo_total = sum(c for n, c in counts.items() if n.startswith(("code__", "docs__")))
    lines = ["collections (chunks):"]
    lines += [f"  {n}  {c}" for n, c in sorted(counts.items())]
    lines.append(f"repo chunks: {repo_total}")
    lines.append(f"first index wall time: {index_seconds:.0f}s")
    rate = repo_total / index_seconds if index_seconds > 0 else 0.0
    lines.append(f"chunks per second: {rate:.2f}")
    return lines


# ── command line ────────────────────────────────────────────────────────────

_PIN_KEYS = {
    "PIN_TAG": PIN_TAG,
    "PIN_SHA": PIN_SHA,
    "CONEXUS_VERSION": CONEXUS_VERSION,
}


def _probe_by_name(name: str) -> Probe:
    for p in PROBES:
        if p.name == name:
            return p
    raise SystemExit(f"unknown probe {name!r}; known: {[p.name for p in PROBES]}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "pin":
        print(_PIN_KEYS[args[0]])
        return 0
    if cmd == "snapshot":
        info = materialize(args[0], args[1])
        print(f"snapshot files={info['files']} digest={info['digest']} head={info['head']}")
        return 0
    if cmd == "notes":
        for subject, title, path in write_notes(args[0]):
            print(f"{subject}\t{title}\t{path}")
        return 0
    if cmd == "probes":
        for p in PROBES:
            print(f"{p.name}\t{p.corpus}\t{p.query}")
        return 0
    if cmd == "check-counts":
        counts = parse_collection_list(sys.stdin.read())
        seconds = float(args[0]) if args else 0.0
        for line in report(counts, seconds):
            print(line)
        problems = check_counts(counts)
        if seconds <= 0:
            problems.append("first-index wall time is missing or zero")
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1 if problems else 0
    if cmd == "check-listing":
        problems = listing_problems(args[0], sys.stdin.read())
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1 if problems else 0
    if cmd == "check-search":
        probe = _probe_by_name(args[0])
        text = sys.stdin.read()
        why = probe_verdict(probe, text)
        if why:
            print(f"FAIL: {why}", file=sys.stderr)
            return 1
        print(f"probe {probe.name}: hit at rank {probe_rank(probe, text)}")
        return 0
    if cmd == "leftover":
        lines = leftover_processes(sys.stdin.read(), args[0])
        for line in lines:
            print(line)
        return 1 if lines else 0
    print(f"unknown sub-command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
