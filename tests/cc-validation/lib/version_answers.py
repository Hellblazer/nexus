#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Look a cc-validation observation up in docs/tables/cc-version-answers.toml.

    version_answers.py lookup <table.toml> <scenario> <claude-version> <answer>

prints one line, ``<verdict>\\t<since>\\t<observed>``, where verdict is

    agrees      the table records this version with this answer
    changed     the table records this version with the OTHER answer
    unrecorded  the table has no row for this version (including "unknown")

Stdlib only: the container's python3 has no nexus package. The table is a
RDR-201 decision table (linted by tests/test_tables_lint.py); this reads its
rows directly, the same way ``nexus.tables.resolve`` would: a row applies when
its ``match.scenario`` is the scenario and its guard names the version (or
"unrecorded", for a version the table does not list) and the answer.
"""
from __future__ import annotations

import sys
import tomllib

UNRECORDED = "unrecorded"


def _atoms(value: object) -> list[str]:
    return [value] if isinstance(value, str) else list(value)  # type: ignore[arg-type]


def lookup(table_path: str, scenario: str, version: str, answer: str) -> tuple[str, str, str]:
    with open(table_path, "rb") as fh:
        table = tomllib.load(fh)
    recorded = [v for v in table["dimensions"]["version"]["domain"] if v != UNRECORDED]
    key = version if version in recorded else UNRECORDED
    answer_dim = f"answer_{scenario}"
    hits = []
    for row in table["row"]:
        if scenario not in _atoms(row["match"]["scenario"]):
            continue
        guard = row["guard"]
        if key in _atoms(guard["version"]) and answer in _atoms(guard[answer_dim]):
            hits.append(row["emit"])
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} rows of {table_path} apply to scenario={scenario} version={key} answer={answer}")
    emit = hits[0]
    return emit["verdict"], emit.get("since", ""), emit.get("observed", "")


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[1] != "lookup":
        print(__doc__, file=sys.stderr)
        return 2
    print("\t".join(lookup(*argv[2:6])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
