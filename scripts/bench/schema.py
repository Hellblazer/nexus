# SPDX-License-Identifier: AGPL-3.0-or-later
"""``Query`` dataclass + YAML loader for the bench harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_CATEGORIES = frozenset({"factual", "comparative", "compositional"})


@dataclass
class Query:
    """One bench query with manually-labeled GT.

    ``ground_truth`` maps an RDR-file basename substring (e.g.
    ``"rdr-049-"``) to a relevance grade in {0, 1, 2, 3}, where 3 is
    most relevant. See ``bench.metrics.grade_for_path`` for the matching
    semantics.

    ``scope`` is forwarded to the path-B/C handlers when set; an empty
    string means "let the handler choose its own default".
    """

    qid: str
    category: str
    text: str
    ground_truth: dict[str, int] = field(default_factory=dict)
    scope: str = ""


def _validate(query: dict, idx: int) -> None:
    for required in ("qid", "category", "text"):
        if required not in query:
            raise ValueError(
                f"queries[{idx}]: missing required field {required!r}"
            )
    if query["category"] not in VALID_CATEGORIES:
        raise ValueError(
            f"queries[{idx}]: unknown category {query['category']!r}; "
            f"must be one of {sorted(VALID_CATEGORIES)}"
        )


def load_queries(path: Path) -> list[Query]:
    """Load a YAML file of queries.

    Schema:

    .. code-block:: yaml

        queries:
          - qid: Q1-factual-tumblers
            category: factual
            text: "Which RDR introduced catalog tumblers?"
            ground_truth:
              "rdr-049-": 3
              "rdr-053-": 1
            scope: "rdr"   # optional
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or "queries" not in data:
        raise ValueError(f"{path}: top-level 'queries' key required")
    raw = data["queries"]
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'queries' must be a list")
    out: list[Query] = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise ValueError(f"{path}: queries[{i}] must be a mapping")
        _validate(q, i)
        out.append(Query(
            qid=str(q["qid"]),
            category=str(q["category"]),
            text=str(q["text"]),
            ground_truth=dict(q.get("ground_truth", {})),
            scope=str(q.get("scope", "")),
        ))
    return out
