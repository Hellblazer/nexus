# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spike A fixtures for RDR-093 (bead nexus-g8l9).

Probes partition-assignment stability of ``operator_groupby`` (not
yet implemented in core.py) — how often does the same input set land
in the same set-of-sets partition across 5 independent ``claude -p``
invocations? Mirrors RDR-088 Spike A's stability protocol but on the
groupby contract instead of the boolean-verdict contract.

Each fixture is a dict with keys:
    id: short identifier (``g-NN``)
    topic: short topical label
    key: natural-language partition expression passed to operator_groupby
    items_inline: list of 3-5 dicts each with ``id`` and ``quote``
    expected_modal_hint: stability category — "most-likely-stable" |
        "stable-under-noise" | "genuinely-ambiguous"
    expected_groups: optional ground-truth modal partition (set-of-sets
        of item ids) used for diagnostic tagging only — NOT for stability
        scoring. Stability is measured run-vs-run, not run-vs-truth.

All quotes are paraphrased from real distributed-systems research
covered in ``knowledge__delos`` (Paxos / BFT / CRDT / gossip
literature). Quotes are concise (50-180 chars) so each fixture's full
input fits well under the cardinality cap and the prompt stays
focused on partitioning rather than reading.

Distribution of the 20 fixtures:
    8 most-likely-stable (verbatim distinguishing field)
    8 stable-under-noise (field inferable but not stated)
    4 genuinely-ambiguous (humans would reasonably disagree)
"""

from __future__ import annotations

FIXTURES: list[dict] = [
    # ------------------------------------------------------------------
    # GROUP 1: most-likely-stable (8) — partition key surfaces verbatim
    # ------------------------------------------------------------------
    {
        "id": "g-01",
        "topic": "publication year buckets (verbatim)",
        "key": "publication year",
        "items_inline": [
            {"id": "A", "quote": "Published at SOSP 2018, the system extends Paxos with batched commits."},
            {"id": "B", "quote": "Presented at OSDI 2020, this BFT protocol pipelines view changes."},
            {"id": "C", "quote": "VLDB 2018 introduced a CRDT-based replication design for edge devices."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-02",
        "topic": "fault model (Byzantine vs crash)",
        "key": "fault model",
        "items_inline": [
            {"id": "A", "quote": "Byzantine fault tolerance with 3f+1 replicas; replicas may forge votes arbitrarily."},
            {"id": "B", "quote": "Crash-only fault tolerance assumes nodes fail by stopping; majority quorums suffice."},
            {"id": "C", "quote": "Byzantine adversaries may equivocate across views; protocol uses signed messages."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-03",
        "topic": "synchrony assumption (sync / partial / async)",
        "key": "synchrony model",
        "items_inline": [
            {"id": "A", "quote": "Under partial synchrony with bounded delay after GST, safety holds always."},
            {"id": "B", "quote": "Asynchronous network model with no timing assumptions; FLP applies."},
            {"id": "C", "quote": "Partial synchrony provides safety always, liveness once GST is reached."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-04",
        "topic": "consensus family (Paxos vs BFT)",
        "key": "consensus family",
        "items_inline": [
            {"id": "A", "quote": "Multi-Paxos extends Paxos to a sequence of decisions with a stable leader."},
            {"id": "B", "quote": "PBFT is the canonical Byzantine consensus protocol with three message phases."},
            {"id": "C", "quote": "Fast Paxos optimises latency by skipping the prepare phase under fast quorums."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-05",
        "topic": "topology (leader-based vs leaderless)",
        "key": "topology",
        "items_inline": [
            {"id": "A", "quote": "Leader-based BFT systems perform view changes when leader timeouts fire."},
            {"id": "B", "quote": "Leaderless gossip protocols converge without any designated coordinator."},
            {"id": "C", "quote": "Leader-based Paxos centralises ordering decisions through one elected proposer."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-06",
        "topic": "quorum-size threshold (3f+1 vs 2f+1 vs >3f+1)",
        "key": "quorum size",
        "items_inline": [
            {"id": "A", "quote": "Tolerating f Byzantine faults requires n=3f+1 replicas with 2f+1 votes per quorum."},
            {"id": "B", "quote": "Crash-only consensus needs a majority quorum: n=2f+1 with f+1 votes."},
            {"id": "C", "quote": "Byzantine fast paths require n>5f to commit in one round-trip."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-07",
        "topic": "deployment regime (geo vs datacenter)",
        "key": "deployment regime",
        "items_inline": [
            {"id": "A", "quote": "Geo-distributed Byzantine consensus across wide-area links of 50-200ms RTT."},
            {"id": "B", "quote": "Datacenter-scale crash-tolerant SMR with sub-millisecond latency budgets."},
            {"id": "C", "quote": "Wide-area Paxos with read leases tolerates cross-region partitions."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    {
        "id": "g-08",
        "topic": "primary metric (latency vs throughput)",
        "key": "primary metric",
        "items_inline": [
            {"id": "A", "quote": "Throughput-oriented BFT achieves over one million transactions per second."},
            {"id": "B", "quote": "Latency-focused Fast Paxos reduces commit to a single round-trip."},
            {"id": "C", "quote": "Throughput-optimised gossip pipelines aggregation across 10k nodes."},
        ],
        "expected_modal_hint": "most-likely-stable",
    },
    # ------------------------------------------------------------------
    # GROUP 2: stable-under-noise (8) — key is inferable, not verbatim
    # ------------------------------------------------------------------
    {
        "id": "g-09",
        "topic": "method family (Paxos-shape vs BFT-shape, no names)",
        "key": "method family",
        "items_inline": [
            {"id": "A", "quote": "Replicas exchange phase-1a/1b promises before phase-2 accept under a stable leader."},
            {"id": "B", "quote": "Pre-prepare, prepare, and commit phases run in lockstep with view changes on timeout."},
            {"id": "C", "quote": "A two-phase approach where the leader broadcasts proposals to acceptors that promise."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-10",
        "topic": "fault assumption (inferred from adversary behavior)",
        "key": "fault assumption",
        "items_inline": [
            {"id": "A", "quote": "Adversaries may forge messages and deviate arbitrarily from the protocol."},
            {"id": "B", "quote": "Faulty nodes halt without sending further messages; surviving nodes never lie."},
            {"id": "C", "quote": "Malicious replicas can equivocate by sending conflicting votes to different peers."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-11",
        "topic": "synchrony (inferred from algorithmic behavior)",
        "key": "synchrony assumption",
        "items_inline": [
            {"id": "A", "quote": "Bounded-delay assumption holds eventually; protocol uses growing timeouts to wait it out."},
            {"id": "B", "quote": "No timing assumptions whatsoever; FLP impossibility forbids deterministic termination."},
            {"id": "C", "quote": "Once a stabilization point is reached all messages deliver within delta."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-12",
        "topic": "convergence semantics (consensus vs CRDT)",
        "key": "convergence semantics",
        "items_inline": [
            {"id": "A", "quote": "Replicas chain blocks via certificates from supermajority votes; total order emerges."},
            {"id": "B", "quote": "A sequence of independent ballots, each agreeing on one operation under a leader."},
            {"id": "C", "quote": "State merges over a join-semilattice; conflicts resolve without coordination."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-13",
        "topic": "communication topology (inferred from message pattern)",
        "key": "communication topology",
        "items_inline": [
            {"id": "A", "quote": "Every replica broadcasts its vote to every other replica each round."},
            {"id": "B", "quote": "The leader sends, followers acknowledge, leader commits and broadcasts decision."},
            {"id": "C", "quote": "Each node randomly picks a peer per round and exchanges state with it."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-14",
        "topic": "fault tolerance bound (arithmetic from descriptions)",
        "key": "fault model",
        "items_inline": [
            {"id": "A", "quote": "Three replicas are deployed and the system tolerates one malicious replica without losing safety."},
            {"id": "B", "quote": "Five servers tolerate two stop-failure faults via majority commits."},
            {"id": "C", "quote": "Four replicas; the protocol halts if two attempt to forge competing decisions."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-15",
        "topic": "deployment scale (inferred from latencies)",
        "key": "deployment scale",
        "items_inline": [
            {"id": "A", "quote": "Cross-datacenter round-trip times of 50ms dominate commit latency."},
            {"id": "B", "quote": "Single-rack RDMA delivers messages in under 100 microseconds."},
            {"id": "C", "quote": "Cross-continent replication tolerates 200ms RTT with read leases."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    {
        "id": "g-16",
        "topic": "primary contribution (latency vs throughput, paraphrased)",
        "key": "primary contribution",
        "items_inline": [
            {"id": "A", "quote": "We reduce commit time from two network round-trips to a single round-trip."},
            {"id": "B", "quote": "We support over one million independent operations per second on commodity hardware."},
            {"id": "C", "quote": "Single-round-trip commit in the common case avoids the second phase entirely."},
        ],
        "expected_modal_hint": "stable-under-noise",
    },
    # ------------------------------------------------------------------
    # GROUP 3: genuinely-ambiguous (4) — multiple defensible partitions
    # ------------------------------------------------------------------
    {
        "id": "g-17",
        "topic": "method family with hybrid overlap",
        "key": "method family",
        "items_inline": [
            {"id": "A", "quote": "Hybrid CRDT plus BFT for state convergence under Byzantine churn and merging concurrent updates."},
            {"id": "B", "quote": "A merge-semilattice that converges even when adversaries inject arbitrary updates."},
            {"id": "C", "quote": "Asynchronous replicated state machine combining gossip with periodic certificate rounds."},
        ],
        "expected_modal_hint": "genuinely-ambiguous",
    },
    {
        "id": "g-18",
        "topic": "novelty assessment (subjective)",
        "key": "novelty level",
        "items_inline": [
            {"id": "A", "quote": "Our protocol modestly extends prior work with a tighter latency analysis and one new optimisation."},
            {"id": "B", "quote": "We introduce the first asynchronous BFT protocol with logarithmic communication complexity."},
            {"id": "C", "quote": "We refine known techniques to deliver a cleaner safety proof and a 10% throughput gain."},
        ],
        "expected_modal_hint": "genuinely-ambiguous",
    },
    {
        "id": "g-19",
        "topic": "domain (theory and systems mixed)",
        "key": "research domain",
        "items_inline": [
            {"id": "A", "quote": "A lower-bound proof on async Byzantine quorum size, validated experimentally on 100-node clusters."},
            {"id": "B", "quote": "Implementation of HotStuff at one million tx/s, backed by a TLA+ model and machine-checked proof."},
            {"id": "C", "quote": "Combinatorial proof of safety with end-to-end simulation results on a deployed testbed."},
        ],
        "expected_modal_hint": "genuinely-ambiguous",
    },
    {
        "id": "g-20",
        "topic": "primary metric improved (multi-objective)",
        "key": "primary metric improved",
        "items_inline": [
            {"id": "A", "quote": "Reduces latency by 40% while maintaining throughput parity with the prior baseline."},
            {"id": "B", "quote": "Doubles throughput compared to the baseline without any latency degradation."},
            {"id": "C", "quote": "Halves latency at 90% of the baseline's throughput; net win in geo-distributed regimes."},
        ],
        "expected_modal_hint": "genuinely-ambiguous",
    },
]


assert len(FIXTURES) == 20, f"expected 20 fixtures, got {len(FIXTURES)}"
assert all({"id", "topic", "key", "items_inline", "expected_modal_hint"} <= set(f) for f in FIXTURES)
