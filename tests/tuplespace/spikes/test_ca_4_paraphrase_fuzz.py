# SPDX-License-Identifier: Apache-2.0
"""Spike CA-4: 100-paraphrase semantic fuzz test.

RDR-110 Phase 1 Step 7 CA spike (nexus-tq96).

Validates CA #4: "Per-subspace take.floor + take.margin calibration prevents
false-positive destructive reads under realistic query paraphrase distributions."

CA-4 is a *false-positive prevention* assertion, NOT a recall assertion.  The
floor + margin gates are designed to ensure that a query intended for tuple A
can never erroneously take tuple B (a decoy with very different content).  When
a paraphrase is too semantically drifted to match the target above floor, the
correct behaviour is a safe abstention (return None) -- not a false take.

Design:
- For each of 'mailbox/<agent>' and 'tasks/<project>' subspaces:
  - Insert a target tuple and one clearly-different decoy tuple.
  - Generate 100 paraphrases of a canonical query using seeded synonym
    substitution + word-order shuffle (no LLM required).
  - Classify each paraphrase outcome:
      correct_take: target is top-K AND above floor AND margin holds
      silent_abstain: no candidate above floor (safe -- no destructive read)
      false_positive: decoy would be selected (above floor AND above margin) when
                      target was intended (design failure if this occurs)
  - Report: per-paraphrase distance histogram + classification summary.
- PASS if false_positives == 0 across all 100 paraphrases per subspace.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db

# ---------------------------------------------------------------------------
# Schema YAML definitions
# ---------------------------------------------------------------------------

_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.45
  margin: 0.05
  default_lease_seconds: 300
read:
  default_floor: 0.35
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

_MAILBOX_YAML = """
name: mailbox/<agent>
tier: session
content_type: text
embed_from: content
dimensions:
  sender:   { type: string, required: true }
  priority: { type: enum, values: [urgent, normal, low], required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.40
  margin: 0.05
  default_lease_seconds: 120
read:
  default_floor: 0.30
  default_n: 5
tiers: [session]
retention_seconds: 3600
"""

# ---------------------------------------------------------------------------
# Paraphrase generation (no LLM)
# ---------------------------------------------------------------------------

# Synonym substitutions keyed by word.
_SYNONYMS: dict[str, list[str]] = {
    "implement": ["build", "create", "develop", "write", "add", "construct"],
    "implementing": ["building", "creating", "developing", "writing", "adding"],
    "authentication": ["auth", "login", "user-auth", "sign-in", "credential-check"],
    "feature": ["capability", "functionality", "module", "component"],
    "user": ["person", "client", "agent", "account", "member"],
    "users": ["people", "clients", "agents", "accounts", "members"],
    "service": ["system", "module", "server", "component", "backend"],
    "web": ["HTTP", "REST", "API", "online"],
    "message": ["notification", "alert", "signal", "note", "memo"],
    "send": ["dispatch", "deliver", "transmit", "forward", "push"],
    "coordination": ["orchestration", "management", "scheduling", "synchronisation"],
    "task": ["job", "work-item", "assignment", "ticket"],
    "tasks": ["jobs", "work-items", "assignments", "tickets"],
    "assignment": ["allocation", "delegation", "dispatch", "routing"],
    "agent": ["worker", "actor", "process", "service"],
    "agents": ["workers", "actors", "processes", "services"],
    "for": ["for", "targeting"],
    "the": ["the", "this", "our"],
    "to": ["to", "for"],
}

_FILLER_PREFIXES: list[str] = [
    "",
    "Please ",
    "We need to ",
    "Kindly ",
    "Action required: ",
    "We should ",
]

_FILLER_SUFFIXES: list[str] = [
    "",
    " now",
    " asap",
    " in this sprint",
    " before the deadline",
    " for the project",
]


def _substitute_synonyms(words: list[str], rng: random.Random) -> list[str]:
    """Replace words with synonyms from _SYNONYMS with 50% probability."""
    result = []
    for w in words:
        key = w.lower().rstrip(".,;:'\"")
        if key in _SYNONYMS and rng.random() < 0.5:
            replacement = rng.choice(_SYNONYMS[key])
            if w[0].isupper():
                replacement = replacement.capitalize()
            result.append(replacement)
        else:
            result.append(w)
    return result


def _shuffle_middle(words: list[str], rng: random.Random) -> list[str]:
    """Shuffle a small window of the middle words with 20% chance."""
    if len(words) <= 3 or rng.random() > 0.2:
        return words
    start = max(1, len(words) // 4)
    end = min(len(words) - 1, 3 * len(words) // 4)
    if start >= end:
        return words
    middle = words[start:end]
    rng.shuffle(middle)
    return words[:start] + middle + words[end:]


def generate_paraphrases(canonical: str, n: int, seed: int = 42) -> list[str]:
    """Generate *n* paraphrases of *canonical* using seeded randomness.

    Index 0 is always the canonical query itself.
    """
    rng = random.Random(seed)
    paraphrases: list[str] = [canonical]
    base_words = canonical.split()

    while len(paraphrases) < n:
        words = list(base_words)
        words = _substitute_synonyms(words, rng)
        words = _shuffle_middle(words, rng)
        text = " ".join(words)
        prefix = rng.choice(_FILLER_PREFIXES)
        suffix = rng.choice(_FILLER_SUFFIXES)
        text = (prefix + text + suffix).strip()
        paraphrases.append(text)

    return paraphrases[:n]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "mailbox.yml").write_text(_MAILBOX_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "tuples.db"
    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture()
def index(registry: Registry, chroma_client: chromadb.EphemeralClient) -> TupleIndex:
    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _out_tuple(
    conn: sqlite3.Connection,
    index: TupleIndex,
    registry: Registry,
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
) -> str:
    from nexus.tuplespace.api import out
    return out(
        conn=conn,
        index=index,
        registry=registry,
        subspace=subspace,
        content=content,
        dimensions=dimensions,
    )


def _classify_paraphrase(
    index: TupleIndex,
    schema_name: str,
    subspace: str,
    query: str,
    target_id: str,
    decoy_id: str,
    floor: float,
    margin: float,
) -> tuple[str, float, float]:
    """Classify a paraphrase query outcome against the floor+margin gate.

    Returns:
        (outcome, target_sim, decoy_sim)
        outcome is one of:
          'correct_take'   -- target is top hit, above floor, margin holds vs decoy
          'silent_abstain' -- target below floor (no take; safe)
          'missed_target'  -- target found but floor/margin gate blocks (safe abstention)
          'false_positive' -- decoy would be taken instead of target (DESIGN FAILURE)
          'no_results'     -- chroma returned nothing (safe)
    """
    results = index.read(
        template_name=schema_name,
        subspace=subspace,
        query=query,
        where=None,
        n_results=5,
    )

    if not results:
        return "no_results", 0.0, 0.0

    # Map results to (id, similarity).
    hits = [(r["id"], 1.0 - r["distance"]) for r in results]

    # Find target and decoy positions + similarities.
    target_sim: float = 0.0
    decoy_sim: float = 0.0
    top_id = hits[0][0] if hits else None
    top_sim = hits[0][1] if hits else 0.0

    for hit_id, hit_sim in hits:
        if hit_id == target_id:
            target_sim = hit_sim
        elif hit_id == decoy_id:
            decoy_sim = hit_sim

    # The gate logic (mirrors take() in api.py):
    #   best = top of chroma results
    #   passes floor: best_similarity >= floor
    #   passes margin: best_similarity - second_similarity >= margin
    #
    # We evaluate the gate from the decoy's perspective to detect false positives.

    if top_id == decoy_id:
        # Decoy is the top hit.
        second_sim = hits[1][1] if len(hits) > 1 else 0.0
        decoy_passes_floor = decoy_sim >= floor
        decoy_passes_margin = (decoy_sim - second_sim) >= margin
        if decoy_passes_floor and decoy_passes_margin:
            return "false_positive", target_sim, decoy_sim
        else:
            # Gate would block the decoy -- safe abstention.
            return "silent_abstain", target_sim, decoy_sim

    # Target is (or would be) the top hit, or target is not in results.
    if target_sim < floor:
        return "silent_abstain", target_sim, decoy_sim

    # Target above floor. Check margin vs the next-best candidate.
    second_sim = hits[1][1] if len(hits) > 1 and hits[0][0] == target_id else (
        hits[0][1] if hits and hits[0][0] != target_id else (hits[1][1] if len(hits) > 1 else 0.0)
    )
    # Simpler: the runner-up is the highest-scoring result that is not the target.
    runner_up_sim = max((sim for h_id, sim in hits if h_id != target_id), default=0.0)
    margin_ok = (target_sim - runner_up_sim) >= margin

    if margin_ok:
        return "correct_take", target_sim, decoy_sim
    else:
        return "missed_target", target_sim, decoy_sim


# ---------------------------------------------------------------------------
# Spike tests
# ---------------------------------------------------------------------------

class TestParaphraseFuzz:
    """CA #4: floor + margin calibration prevents false-positive destructive reads."""

    def _run_subspace_fuzz(
        self,
        subspace: str,
        target_content: str,
        decoy_content: str,
        target_dims: dict[str, Any],
        decoy_dims: dict[str, Any],
        canonical_query: str,
        schema_name: str,
        floor: float,
        margin: float,
        db_conn: sqlite3.Connection,
        index: TupleIndex,
        registry: Registry,
    ) -> dict[str, Any]:
        """Run the 100-paraphrase false-positive check for one subspace."""
        target_id = _out_tuple(db_conn, index, registry, subspace, target_content, target_dims)
        decoy_id = _out_tuple(db_conn, index, registry, subspace, decoy_content, decoy_dims)

        paraphrases = generate_paraphrases(canonical_query, n=100, seed=42)

        counts: dict[str, int] = {
            "correct_take": 0,
            "silent_abstain": 0,
            "missed_target": 0,
            "false_positive": 0,
            "no_results": 0,
        }
        target_sims: list[float] = []
        decoy_sims: list[float] = []
        fp_examples: list[str] = []

        for query in paraphrases:
            outcome, t_sim, d_sim = _classify_paraphrase(
                index=index,
                schema_name=schema_name,
                subspace=subspace,
                query=query,
                target_id=target_id,
                decoy_id=decoy_id,
                floor=floor,
                margin=margin,
            )
            counts[outcome] += 1
            target_sims.append(t_sim)
            decoy_sims.append(d_sim)
            if outcome == "false_positive":
                fp_examples.append(f"  query={query!r} target_sim={t_sim:.3f} decoy_sim={d_sim:.3f}")

        # Similarity histograms (target).
        buckets = [0] * 5
        for s in target_sims:
            if s < 0.3:
                buckets[0] += 1
            elif s < 0.5:
                buckets[1] += 1
            elif s < 0.7:
                buckets[2] += 1
            elif s < 0.9:
                buckets[3] += 1
            else:
                buckets[4] += 1

        result = {
            "subspace": subspace,
            "counts": counts,
            "false_positives": counts["false_positive"],
            "correct_takes": counts["correct_take"],
            "silent_abstains": counts["silent_abstain"],
            "target_sim_mean": sum(target_sims) / len(target_sims) if target_sims else 0.0,
            "target_sim_max": max(target_sims) if target_sims else 0.0,
            "decoy_sim_mean": sum(decoy_sims) / len(decoy_sims) if decoy_sims else 0.0,
            "histogram": {
                "[0.0,0.3)": buckets[0],
                "[0.3,0.5)": buckets[1],
                "[0.5,0.7)": buckets[2],
                "[0.7,0.9)": buckets[3],
                "[0.9,1.0]": buckets[4],
            },
            "fp_examples": fp_examples,
        }

        print(
            f"\n[CA-4 {subspace}] "
            f"correct_take={counts['correct_take']} "
            f"silent_abstain={counts['silent_abstain']} "
            f"missed_target={counts['missed_target']} "
            f"false_positive={counts['false_positive']} "
            f"no_results={counts['no_results']}"
        )
        print(
            f"  target_sim_mean={result['target_sim_mean']:.3f} "
            f"target_sim_max={result['target_sim_max']:.3f} "
            f"decoy_sim_mean={result['decoy_sim_mean']:.3f}"
        )
        print(f"  histogram (target): {result['histogram']}")
        if fp_examples:
            print(f"  FALSE POSITIVES:")
            for ex in fp_examples[:5]:
                print(ex)

        return result

    def test_tasks_subspace_paraphrase_fuzz(
        self,
        db_conn: sqlite3.Connection,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """100 paraphrases of an authentication task query; zero false positives."""
        result = self._run_subspace_fuzz(
            subspace="tasks/nexus",
            target_content="implement user authentication feature for the web service",
            decoy_content="deploy containerised application to production kubernetes cluster",
            target_dims={"status": "open", "priority": "P1", "created_by": "agent-x"},
            decoy_dims={"status": "open", "priority": "P2", "created_by": "agent-y"},
            canonical_query="implement authentication feature for user service",
            schema_name="tasks/<project>",
            floor=0.45,
            margin=0.05,
            db_conn=db_conn,
            index=index,
            registry=registry,
        )

        # PASS: zero false positives -- the decoy is never erroneously taken.
        assert result["false_positives"] == 0, (
            f"tasks/<project>: {result['false_positives']} false-positive take(s) detected. "
            f"Floor+margin calibration fails to prevent destructive misreads.\n"
            f"Examples:\n" + "\n".join(result["fp_examples"])
        )
        print(
            f"[CA-4 tasks] PASS -- 0 false positives across 100 paraphrases. "
            f"correct_take={result['correct_takes']} silent_abstain={result['silent_abstains']}. "
            f"floor=0.45 margin=0.05 are safe."
        )

    def test_mailbox_subspace_paraphrase_fuzz(
        self,
        db_conn: sqlite3.Connection,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """100 paraphrases of a coordination message query; zero false positives."""
        result = self._run_subspace_fuzz(
            subspace="mailbox/coordinator",
            target_content="send coordination message to orchestrator agent for task assignment",
            decoy_content="upload binary artefact to cloud storage for deployment pipeline",
            target_dims={"sender": "planner", "priority": "normal"},
            decoy_dims={"sender": "deployer", "priority": "low"},
            canonical_query="send message to coordinate task assignment for agent",
            schema_name="mailbox/<agent>",
            floor=0.40,
            margin=0.05,
            db_conn=db_conn,
            index=index,
            registry=registry,
        )

        assert result["false_positives"] == 0, (
            f"mailbox/<agent>: {result['false_positives']} false-positive take(s) detected. "
            f"Floor+margin calibration fails to prevent destructive misreads.\n"
            f"Examples:\n" + "\n".join(result["fp_examples"])
        )
        # Use floor and margin from the schema values defined above
        print(
            f"[CA-4 mailbox] PASS -- 0 false positives across 100 paraphrases. "
            f"correct_take={result['correct_takes']} silent_abstain={result['silent_abstains']}. "
            f"floor=0.40 margin=0.05 are safe."
        )
