# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spike A fixtures for RDR-088 (bead nexus-ac40.7).

Probes verdict-stability of ``operator_check`` — how often does the boolean
``ok`` verdict agree across 5 independent ``claude -p`` invocations on the
same ``(items, check_instruction)`` input?

Each fixture is a dict with keys:
    id: short identifier (``q-NN``)
    topic: short topical label
    check_instruction: the yes/no question for operator_check
    items_inline: list of 3 dicts with ``id`` and ``quote``
    expected_verdict_hint: "most-likely-true" | "most-likely-false" | "genuinely-ambiguous"

All quotes are snapshots of real text excerpted from the ``knowledge__delos``
T3 corpus (Delos-lineage distributed-systems papers). Quotes have been
trimmed and minimally cleaned (whitespace, stray markdown figure refs) so
they read as self-contained 2-4 sentence fragments; content is otherwise
verbatim.

Distribution of the 20 fixtures:
    8 most-likely-true (items probably agree → ok=True)
    8 most-likely-false (at least one item contradicts → ok=False)
    4 genuinely-ambiguous (reasonable runs could land either way)

Source papers sampled (all in knowledge__delos):
    rapid-atc18, pBeeGees, lightweight-smr, prdts, bft-to-smr,
    aleph-bft, self-stabilizing-bft-overlay, fireflies-tocs, zanzibar
"""

from __future__ import annotations

FIXTURES: list[dict] = [
    # ------------------------------------------------------------------
    # GROUP A: most-likely-true (8)
    # All three items, read straightforwardly, support the check.
    # ------------------------------------------------------------------
    {
        "id": "q-01",
        "topic": "Classical Paxos requires majority quorum",
        "check_instruction": "Do the items agree that classical Paxos-style consensus requires at least a majority (or equivalent super-majority) quorum to accept a value?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Processes elect a leader; the leader proposes a value. If a majority accepts, this value becomes the final decision. If either phase halts, processes terminate the attempt and start a new round.",
            },
            {
                "id": "B",
                "quote": "Fast Paxos reaches a decision if there is a quorum larger than three quarters of the membership set with an identical proposal. If there is no fast-quorum support, Fast Paxos falls back to classical Paxos to make progress.",
            },
            {
                "id": "C",
                "quote": "A QC proves that more than n - f replicas have voted for a block in a given view. It serves as evidence that the block is certified, with n = 3f + 1 replicas tolerating at most f faults.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-02",
        "topic": "PBFT tolerates f faults with 3f+1 replicas",
        "check_instruction": "Do the items agree that tolerating f Byzantine faults requires n = 3f + 1 replicas?",
        "items_inline": [
            {
                "id": "A",
                "quote": "PBFT (Practical Byzantine Fault Tolerance) requires 3f + 1 replicas to tolerate f Byzantine faults and is live under the partial synchronous system model (no synchrony is needed for safety).",
            },
            {
                "id": "B",
                "quote": "We consider participants consisting of n nodes, where at most f nodes are malicious (n = 3f + 1). These malicious nodes may deviate arbitrarily from the protocol, but they cannot forge digital signatures or message digests.",
            },
            {
                "id": "C",
                "quote": "A Quorum Certificate proves that more than n - f replicas have voted for a block in a given view; with n = 3f + 1 this is at least 2f + 1 votes, strictly more than two-thirds of replicas.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-03",
        "topic": "FLP impossibility in async networks",
        "check_instruction": "Do the items agree that deterministic consensus is impossible in a purely asynchronous network if even one process can crash?",
        "items_inline": [
            {
                "id": "A",
                "quote": "The FLP impossibility theorem, established by Fischer, Lynch, and Paterson, states that in a purely asynchronous network, consensus cannot be guaranteed in a finite time if even a single process crashes.",
            },
            {
                "id": "B",
                "quote": "Liveness and safety are impossible to achieve simultaneously for any consensus protocol in a completely asynchronous network with faults, as proved by the FLP impossibility result.",
            },
            {
                "id": "C",
                "quote": "This fundamental limitation led subsequent consensus algorithms to adopt one of two approaches: assuming a partially synchronous network model, or employing probabilistic methods in asynchronous settings to ensure consensus is reached with arbitrarily high probability.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-04",
        "topic": "Safety vs liveness distinction",
        "check_instruction": "Do the items agree that safety and liveness are distinct properties, with safety meaning 'nothing bad happens' and liveness meaning 'something good eventually happens'?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Besides safety, liveness is another sought-after property of consensus protocols. While safety ensures that a protocol only leads to valid decisions, liveness requires that it comes to a decision eventually.",
            },
            {
                "id": "B",
                "quote": "Our approach guarantees liveness as long as at most a constant fraction of servers are blocked, ensures safety under any number of blocked servers, and supports fast recovery from massive blocking attacks.",
            },
            {
                "id": "C",
                "quote": "PBFT is live under the partial synchronous system model; no synchrony is needed for safety. This separation means safety holds under arbitrary asynchrony while progress depends on eventual timing bounds.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-05",
        "topic": "Leader-based BFT uses view changes on timeout",
        "check_instruction": "Do the items agree that leader-based BFT protocols handle faulty or unresponsive leaders by initiating a view change driven by timeouts?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Each process p maintains its high_vote field, which records its voting information for the block with the highest view. Upon a timeout, process p sends a timeout message containing high_vote to the next leader.",
            },
            {
                "id": "B",
                "quote": "A Timeout Certificate is formed by collecting at least n - f timeout messages for view v from different processes, proving that view v has timed out and prompting others to move to view v + 1.",
            },
            {
                "id": "C",
                "quote": "This phase is started when a timeout event is triggered for a sub-set M of pending messages in ToOrder. When the timers are triggered, the requests are forwarded to all replicas and a regency change procedure initiates.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-06",
        "topic": "Digital signatures unforgeable under standard model",
        "check_instruction": "Do the items agree that Byzantine nodes in these protocols cannot forge digital signatures?",
        "items_inline": [
            {
                "id": "A",
                "quote": "These malicious nodes may deviate arbitrarily from the protocol, but they have limited computational power and cannot forge digital signatures or message digests.",
            },
            {
                "id": "B",
                "quote": "If a faulty leader does not send the same set of logs to a set Q, each entry in the log contains the proof associated with each value decided, which in turn prevents the replicas from providing incorrect decision values. Such logs are signed by the replicas that sent them.",
            },
            {
                "id": "C",
                "quote": "We assume that members send signed messages, and members ignore any message that is not signed properly or does not carry the matching epoch. Byzantine members cannot produce valid signatures of other members.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-07",
        "topic": "CRDT/PRDT state convergence via join semilattice",
        "check_instruction": "Do the items agree that CRDT-style replicated data types achieve state convergence via a commutative, idempotent, associative merge (join) operation?",
        "items_inline": [
            {
                "id": "A",
                "quote": "The join semilattice structure ensures state convergence of ARDTs across the distributed system. Join semilattices are partially ordered sets where any two elements have a unique least upper bound.",
            },
            {
                "id": "B",
                "quote": "A merge (join) operation for composing states that is commutative, idempotent, and associative. This algebraic structure is what gives replicated data types their deterministic convergence property under arbitrary message reordering.",
            },
            {
                "id": "C",
                "quote": "By taking monotonicity for granted, protocols can potentially cut corners and loosen certain ordering requirements, which would be necessary in a traditional message passing setting. This is enabled by the semilattice merge being commutative and idempotent.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    {
        "id": "q-08",
        "topic": "Partial synchrony: bounded delay after GST",
        "check_instruction": "Do the items agree that the partial synchrony model assumes message delays are bounded after some (unknown) Global Stabilization Time?",
        "items_inline": [
            {
                "id": "A",
                "quote": "We operate under a partial synchrony model. After GST, all transmissions arrive within a known bound to their destinations.",
            },
            {
                "id": "B",
                "quote": "PBFT is live under the partial synchronous system model; the system is assumed to alternate between periods of asynchrony and periods where message delays satisfy a known bound.",
            },
            {
                "id": "C",
                "quote": "This phase occurs when the system is passing through a period of asynchrony, or there is a faulty leader that does not deliver client requests before their associated timers expire. Once synchrony returns, timeouts suffice to drive progress.",
            },
        ],
        "expected_verdict_hint": "most-likely-true",
    },
    # ------------------------------------------------------------------
    # GROUP B: most-likely-false (8)
    # At least one item directly contradicts the check or its premise.
    # ------------------------------------------------------------------
    {
        "id": "q-09",
        "topic": "All consensus requires a leader",
        "check_instruction": "Do the items agree that every state machine replication protocol in these papers relies on a designated leader to order requests?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Processes elect a leader; the leader proposes a value. If a majority accepts, this value becomes the final decision. Leader election is the critical first phase.",
            },
            {
                "id": "B",
                "quote": "In particular, our solution is robust against adversaries that target key servers (which captures insider-based denial-of-service attacks), whereas leader-based approaches fail under such a blocking model. Our method is fully decentralized, unlike other near-optimal solutions that rely on leaders.",
            },
            {
                "id": "C",
                "quote": "PBFT operates with a primary replica chosen per view, and all client requests flow through that primary before being multicast to backups.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-10",
        "topic": "Synchronous model sufficient for consensus",
        "check_instruction": "Do the items agree that a synchronous network model is sufficient and required to achieve consensus in the protocols described?",
        "items_inline": [
            {
                "id": "A",
                "quote": "PBFT is live under the partial synchronous system model; no synchrony is needed for safety. The protocol requires only that message delays eventually satisfy a known bound.",
            },
            {
                "id": "B",
                "quote": "The FLP impossibility theorem states that in a purely asynchronous network, consensus cannot be guaranteed in a finite time if even a single process crashes. Synchrony is one way to sidestep this, but not the only one.",
            },
            {
                "id": "C",
                "quote": "We employ probabilistic methods in asynchronous settings to ensure consensus is reached with arbitrarily high probability, without assuming any synchrony bound whatsoever.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-11",
        "topic": "2f+1 replicas suffice for Byzantine fault tolerance",
        "check_instruction": "Do the items agree that 2f + 1 replicas are sufficient to tolerate f Byzantine faults?",
        "items_inline": [
            {
                "id": "A",
                "quote": "PBFT (Practical Byzantine Fault Tolerance) requires 3f + 1 replicas to tolerate f Byzantine faults. This bound is tight under the partial synchrony model.",
            },
            {
                "id": "B",
                "quote": "We consider participants consisting of n nodes, where at most f nodes are malicious (n = 3f + 1). A Quorum Certificate proves that more than n - f replicas have voted.",
            },
            {
                "id": "C",
                "quote": "Byzantine consensus is theta(n squared): the Dolev-Reischuk bound is tight even in partial synchrony. Crash fault models allow 2f + 1 replicas but Byzantine requires 3f + 1.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-12",
        "topic": "Paxos always commits in a single round-trip",
        "check_instruction": "Do the items agree that Paxos always commits a value in a single round-trip between proposer and acceptors?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Paxos allows multiple voting rounds consisting of two phases: first, processes elect a leader; second, the leader proposes a value. If either phase halts, processes terminate the attempt and start a new round.",
            },
            {
                "id": "B",
                "quote": "If there is no fast-quorum support for any proposal because there are conflicting proposals, or a timeout is reached, Fast Paxos falls back to a recovery path, where we use classical Paxos to make progress.",
            },
            {
                "id": "C",
                "quote": "Consensus with three nodes requires two votes to make a decision. Therefore, as soon as the node to which our client is connected receives a vote from the leader, it can add its own vote and can directly confirm the result to the client.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-13",
        "topic": "Faulty leaders can silently forge decisions",
        "check_instruction": "Do the items agree that a faulty (Byzantine) leader in MOD-SMART can unilaterally fabricate a decision value without detection?",
        "items_inline": [
            {
                "id": "A",
                "quote": "If such leader is faulty, it can deviate from the protocol during this phase. However, its behavior is severely constrained since it can not create fake logs (such logs are signed by the replicas that sent them in the STOPDATA messages).",
            },
            {
                "id": "B",
                "quote": "Additionally, each entry in the log contains the proof associated with each value decided in a consensus instance, which in turn prevents the replicas from providing incorrect decision values.",
            },
            {
                "id": "C",
                "quote": "Because of this, the worst a faulty leader can do is not send the SYNC message to a correct replica, or send conflicting messages to partition the correct replicas, but never to fabricate a decision.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-14",
        "topic": "Lightweight SMR requires a leader",
        "check_instruction": "Do the items agree that the Lightweight SMR / median-rule protocol of Cachin, Dou, Scheideler, Schneider is a leader-based protocol?",
        "items_inline": [
            {
                "id": "A",
                "quote": "In addition to offering near-optimal performance in several respects, our method is fully decentralized, unlike other near-optimal solutions that rely on leaders.",
            },
            {
                "id": "B",
                "quote": "Our solution is robust against adversaries that target key servers (which captures insider-based denial-of-service attacks), whereas leader-based approaches fail under such a blocking model.",
            },
            {
                "id": "C",
                "quote": "We adapt a simple median rule from the stabilizing consensus problem to operate in a client-server setting where arbitrary servers may be blocked adaptively. Every server runs the same median update with no distinguished coordinator.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-15",
        "topic": "Zanzibar reads reflect userset rewrite rules",
        "check_instruction": "Do the items agree that Zanzibar's Read operation returns results that reflect the effect of userset rewrite rules such as inheritance between relations?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Read results only depend on contents of relation tuples and do not reflect userset rewrite rules. For example, even if the viewer userset always includes the owner userset, reading tuples with the viewer relation will not return tuples with the owner relation.",
            },
            {
                "id": "B",
                "quote": "Clients that need to understand the effective userset can use the Expand API; Read is deliberately restricted to stored tuples only, for predictable auditing and backup semantics.",
            },
            {
                "id": "C",
                "quote": "Authorization checks take the form of 'does user U have relation R to object O?' and may entail following a long chain of nested group memberships, but Read itself does not perform this traversal.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    {
        "id": "q-16",
        "topic": "BeeGees commits every certified block immediately",
        "check_instruction": "Do the items agree that BeeGees commits every certified block as soon as it has a quorum certificate, without further checks?",
        "items_inline": [
            {
                "id": "A",
                "quote": "If non-consecutive certified blocks are allowed to be committed, it may result in the commitment of conflicting blocks. To address the security issues caused by this commitment of non-consecutive blocks, BeeGees inspects the blockchain to detect conflicting QCs before committing.",
            },
            {
                "id": "B",
                "quote": "If such conflicts are found, BeeGees suspends the commit; otherwise, the block is committed. This conflict check is an essential safety mechanism on top of the raw certificate.",
            },
            {
                "id": "C",
                "quote": "BeeGees requires two consecutive certified views to commit; a block B obtains a QC, and then the next view block also obtaining a QC is the trigger for commit, not the raw QC on B alone.",
            },
        ],
        "expected_verdict_hint": "most-likely-false",
    },
    # ------------------------------------------------------------------
    # GROUP C: genuinely ambiguous (4)
    # Plausible disagreement on how to interpret the check; runs could
    # reasonably split on role assignment and verdict.
    # ------------------------------------------------------------------
    {
        "id": "q-17",
        "topic": "Leader-based vs decentralized near-optimal",
        "check_instruction": "Do the items agree that decentralized (non-leader) SMR protocols achieve performance comparable to the best leader-based protocols under typical conditions?",
        "items_inline": [
            {
                "id": "A",
                "quote": "In addition to offering near-optimal performance in several respects, our method is fully decentralized, unlike other near-optimal solutions that rely on leaders.",
            },
            {
                "id": "B",
                "quote": "This is because crash faults, especially when a leading process fails to propose, cause discontinuities in the blockchain's view. This prevents FHS and CHS from committing blocks, forcing them to wait. Leader-based protocols thus have worst-case slowdowns that decentralized ones avoid.",
            },
            {
                "id": "C",
                "quote": "Our system achieved a mean latency of approximately 380ms, while etcd exhibited a mean latency of around 490ms. This latency difference translated into a throughput advantage for our system: 2.5 operations per second versus 2 ops/s, in a three-node geo-distributed configuration.",
            },
        ],
        "expected_verdict_hint": "genuinely-ambiguous",
    },
    {
        "id": "q-18",
        "topic": "Gossip protocols 'always' reach every node",
        "check_instruction": "Do the items agree that gossip-based dissemination protocols guarantee delivery to every correct node in the system?",
        "items_inline": [
            {
                "id": "A",
                "quote": "A gossip protocol over BSS-overlay completes with high probability within Delta = (ln N + c_0) times d, where N is the number of currently active and correct members.",
            },
            {
                "id": "B",
                "quote": "The counting protocol itself uses gossip to disseminate and aggregate a bitmap of votes for each unique proposal. As soon as a process has a proposal for which three quarters of the cluster has voted, it decides on that proposal.",
            },
            {
                "id": "C",
                "quote": "A crashed member will be removed from the view of every correct and active member within 3 Delta, with high probability. When the system becomes coherent correct members exchange messages within Delta whp.",
            },
        ],
        "expected_verdict_hint": "genuinely-ambiguous",
    },
    {
        "id": "q-19",
        "topic": "Paxos guarantees single decision across rounds",
        "check_instruction": "Do the items agree that Paxos guarantees that any two rounds that reach a decision decide on the same value?",
        "items_inline": [
            {
                "id": "A",
                "quote": "Line 58 shows the decision function of the Paxos PRDT. This function checks whether any round of proposals lead to a decision and, if yes, returns that decision. Using any decision is safe because Paxos guarantees that every round that makes a decision decides on the same value.",
            },
            {
                "id": "B",
                "quote": "Instead of organizing rounds as a sequential list of PaxosRound instances, we use a map-based structure where each round is uniquely identified by a monotonically increasing ballot number. This design enables efficient representation of PRDT delta-states.",
            },
            {
                "id": "C",
                "quote": "Phase 2a starts whenever a process is confirmed as a leader. The leader selects a value to propose by examining all prior rounds and choosing the proposed value from the most recent round based on the ballot ID; phase1b guarantees this value represents the latest information known by any process.",
            },
        ],
        "expected_verdict_hint": "genuinely-ambiguous",
    },
    {
        "id": "q-20",
        "topic": "Aleph-BFT achieves bounded latency in async",
        "check_instruction": "Do the items agree that Aleph-BFT provides a bounded (constant or small-factor) worst-case latency for ordering transactions under fully asynchronous conditions?",
        "items_inline": [
            {
                "id": "A",
                "quote": "ch-RBC instantiated by an honest node terminates within three async-rounds for all honest nodes. Latency is a strictly stronger condition than Termination, and similarly Fast Agreement is stronger than Agreement.",
            },
            {
                "id": "B",
                "quote": "More generally, this also justifies that in any asynchronous BFT protocol it is never correct to wait for one fixed node to send a particular message. The protocol therefore cannot have a deterministic bound on wall-clock time.",
            },
            {
                "id": "C",
                "quote": "Different nodes might hold different versions of the ch-DAG at any specific point in time, but every unit is eventually received by all honest nodes. The OrderUnits function is designed so that even when called on different versions, the respective outputs agree.",
            },
        ],
        "expected_verdict_hint": "genuinely-ambiguous",
    },
]
