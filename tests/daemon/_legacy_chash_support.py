# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures of the legacy-chash guard's tests (nexus-f3yqd): a fake cluster that answers the guard's SQL, the shape
of a ``pg_restore -l`` listing, and the oracle read from the engine's own changelog."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from nexus.daemon import legacy_chash_guard as guard

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class _Cluster:
    """A fake cluster answering the guard's SQL from a declared shape; records what was asked."""

    def __init__(self, *, changelog: bool = True, ran: tuple[str, ...] = (), types: dict[str, str] | None = None,
                 counts: dict[str, int] | None = None, sid: str = "7001", role_ok: bool = True) -> None:
        self.changelog = changelog
        self.ran = ran
        self.types = types if types is not None else {"chunks": "bytea", "catalog_document_chunks": "bytea"}
        self.counts = counts or {}
        self.sid = sid
        self.role_ok = role_ok
        self.asked: list[str] = []
        self.sentinels: list[str] = []  # schemas created by CREATE SCHEMA [IF NOT EXISTS]
        self.schemas: set[str] = set()

    def __call__(self, sql: str) -> str:
        self.asked.append(sql)
        if "rolsuper" in sql:
            return "t" if self.role_ok else "f"
        if "system_identifier" in sql:
            return self.sid
        if "to_regclass" in sql:
            return "t" if self.changelog else "f"
        if "databasechangelog WHERE" in sql:
            return "\n".join(self.ran)
        if "pg_attribute" in sql:
            return "\n".join(f"{t}|{d}" for t, d in self.types.items())
        if sql.startswith("SELECT EXISTS (SELECT 1 FROM nexus."):
            return "t" if self.counts.get(sql.split("FROM nexus.")[1].split(" ")[0], 0) else "f"
        if "count(*)" in sql:
            return str(min(self.counts.get(sql.split("FROM nexus.")[1].split(" ")[0], 0), guard.COUNT_CAP))
        if sql.startswith("CREATE SCHEMA "):
            name = sql.split(" ")[-1] if "IF NOT EXISTS" in sql else sql.split(" ")[2]
            assert name.startswith("nx_legacy_guard_"), sql
            if "IF NOT EXISTS" not in sql:
                self.sentinels.append(name)
            self.schemas.add(name)
            return ""
        if sql.startswith("COMMENT ON SCHEMA nx_legacy_guard_"):
            return ""
        if sql.startswith("DROP SCHEMA IF EXISTS nx_legacy_guard_"):
            self.schemas.discard(sql.split(" ")[4])
            return ""
        raise AssertionError(f"unscripted SQL: {sql}")

    def counted(self) -> list[str]:
        """The statements that look at the chunk and manifest tables (the EXISTS probes and the capped counts)."""
        return [q for q in self.asked if "FROM nexus." in q]


def _listing(*entries: str) -> bytes:
    """A ``pg_restore -l`` listing: ``SCHEMA - name owner`` entries as written, anything else as a TABLE DATA entry."""
    return "".join(
        f"; {i}; 2615 {16390 + i} {e}\n" if e.startswith("SCHEMA - ") else f"; {i}; 0 {16390 + i} TABLE DATA {e}\n"
        for i, e in enumerate(entries)
    ).encode()


def _record(sentinel: str | None = "nx_legacy_guard_abcdef012345", required=(("chunks", True),
                                                                          ("catalog_document_chunks", False))) -> dict:
    return {"refused_at": T0.isoformat(), "counts": {"chunks": 3}, "sentinel": sentinel,
            "required": [list(r) for r in required], "rejected": {}}


_MARK = "SCHEMA - nx_legacy_guard_abcdef012345 nexus"
_FULL = _listing(_MARK, "nexus chunks nexus", "nexus catalog_document_chunks nexus")


_CHANGELOG_XML = (Path(__file__).resolve().parents[2] / "service" / "src" / "main" / "resources" / "db" / "changelog"
                  / "rdr180-001-bytea-chash.xml")


def _rdr180_using() -> str:
    """rdr180-3's USING expression, read from the changelog itself: the value the converted bytea column holds. A
    change to the engine's rule changes this oracle, and the text-count test below goes red."""
    xml = _CHANGELOG_XML.read_text(encoding="utf-8")
    body = xml.split('<changeSet id="rdr180-3-convert-chunks-384"')[1].split("</changeSet>")[0]
    match = re.search(r"USING\s+(CASE.*?END)\s*;", body, re.DOTALL)
    assert match, "rdr180-3's USING expression moved: read the changelog again"
    return " ".join(match.group(1).split())


_RDR180_USING = _rdr180_using()
