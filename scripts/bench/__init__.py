# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-090 retrieval-bench harness.

Lifted from ``scripts/spikes/spike_rdr090_5q.py`` (the gate-decision
spike). The spike is intentionally not refactored to depend on this
package; it remains a frozen artifact.

Public surface:

  * ``bench.metrics`` — pure scoring functions (NDCG@k, dedupe-by-doc,
    GT grading, multi-hop precision)
  * ``bench.schema`` — YAML loader + ``Query`` dataclass
  * ``bench.paths`` — three retrieval-path handlers (A: nx search CLI,
    B: nx_answer plan-routed, C: nx_answer force_dynamic)
  * ``bench.runner`` — orchestrator + JSON reporter

CLI entry: ``uv run python scripts/bench/runner.py <yaml> [--out PATH]``.
"""
