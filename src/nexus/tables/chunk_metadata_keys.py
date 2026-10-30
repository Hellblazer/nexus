# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chunk-metadata key table, read from ``chunk-metadata-keys.toml`` (RDR-236 Phase 0, nexus-qyx35).

One row per key of a chunk row's metadata (and the columns and tables the RDR's
key table names beside them): the class that fixes where the key lives after
normalization, where the key stands today, its home and the census remedy.
The TOML carries the field meanings and the decisions Phase 0 settled; this
module loads it through the house loader, closes the vocabularies, and answers
the questions the parity test, the read-site lint and the Java generator ask.

The legacy-fallback surface the read lint exempts is
``src/nexus/catalog/note_ttl.py`` (see ``LEGACY_FALLBACK_MODULES``); every other
row read of a non-CHUNK key is a listed site in
``tests/chunk_metadata_read_sites.toml``.
"""
from __future__ import annotations

import importlib.resources
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "chunk-metadata-keys.toml"

CLASSES: Final[tuple[str, ...]] = ("CHUNK", "DOCUMENT", "POSITION", "ROW", "LEGACY", "NOT_ROW")
KINDS: Final[tuple[str, ...]] = ("metadata-key", "column", "table", "catalog")
ON_ROW: Final[tuple[str, ...]] = ("allowed", "unvalidated", "engine", "stored", "absent")
REMEDIES: Final[tuple[str, ...]] = ("R1", "R2", "R3", "R4", "R5", "R6", "keep", "n/a")

#: Files whose row reads of a non-CHUNK key are the legacy fallback by design:
#: the note TTL rules read the row until the keyless-note count is zero (RDR-236
#: RF-8). Repo-relative POSIX paths.
LEGACY_FALLBACK_MODULES: Final[frozenset[str]] = frozenset({"src/nexus/catalog/note_ttl.py"})


@dataclass(frozen=True)
class KeyRow:
    key: str
    cls: str
    kind: str
    on_row: str
    home: str
    remedy: str
    moved_by: str


@lru_cache(maxsize=1)
def load_table() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


@lru_cache(maxsize=1)
def rows() -> tuple[KeyRow, ...]:
    """Every key row, in file order; a row outside the closed vocabularies is refused."""
    out: list[KeyRow] = []
    for row in load_table().rows:
        emit = row.outcome
        assert isinstance(emit, dict)
        key = str(row.match["key"])
        built = KeyRow(
            key=key, cls=emit["class"], kind=emit["kind"], on_row=emit["on_row"],
            home=emit["home"], remedy=emit["remedy"], moved_by=emit["moved_by"],
        )
        for field, value, domain in (
            ("class", built.cls, CLASSES), ("kind", built.kind, KINDS),
            ("on_row", built.on_row, ON_ROW), ("remedy", built.remedy, REMEDIES),
        ):
            if value not in domain:
                raise ValueError(f"{TABLE_RESOURCE}: key {key!r} has {field} {value!r}, not one of {domain}")
        out.append(built)
    return tuple(out)


@lru_cache(maxsize=1)
def _raw() -> dict:
    data = importlib.resources.files("nexus.tables").joinpath(TABLE_RESOURCE).read_bytes()
    return tomllib.loads(data.decode("utf-8"))


#: Collections whose writes bypass the canonical chunk schema; named once here and pinned
#: equal to ``nexus.db.t3._BYPASS_SCHEMA_PREFIXES``.
BYPASS_PREFIXES: Final[tuple[str, ...]] = tuple(_raw()["chunk_metadata"]["bypass_prefixes"])


def keys_of(cls: str, *, kind: str = "metadata-key") -> frozenset[str]:
    """The keys of class *cls* that are metadata keys (or of *kind*)."""
    if cls not in CLASSES:
        raise KeyError(cls)
    return frozenset(r.key for r in rows() if r.cls == cls and r.kind == kind)


def non_chunk_row_keys() -> frozenset[str]:
    """The metadata keys a reader must not take from a shared chunk row: DOCUMENT, POSITION, LEGACY."""
    return keys_of("DOCUMENT") | keys_of("POSITION") | keys_of("LEGACY")


def row_for(key: str) -> KeyRow:
    for r in rows():
        if r.key == key:
            return r
    raise KeyError(key)
