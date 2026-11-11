# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the Python oracle of the index-run census answers the shared census table.

``nx doctor`` reads stale index-run fences and failed documents with hidden chunks from the engine route
``GET /v1/catalog/index-run/census``, three bounded buckets (stale, fence, failed_hidden). The same rules, written in
Python over a list of documents, are the oracle: ``nexus.health._stale_fence_rows_over`` and ``_failed_hidden_rows_over``
(the second runs ``heal_manifest_gaps(dry_run=True)`` over the failed ones). The two halves are written in different
languages, so one table (``service/src/test/resources/index-run-census-cases.json``) is read by both: the Java
``IndexRunCensusParityTest`` holds the SQL to it, and this test holds the oracle to it, scenario by scenario, over the same
documents, with the table's clock and the table's chunk rows behind a fake T3.
``test_index_run_census_route_parity.py`` then holds the route-fed rows to the oracle's. A change to either rule that the
other does not follow fails one of them.

The rows answer in sentences, so each scenario's numbers are read back out of the row's detail text; the readers below
are the only coupling to its wording, and a non-vacuity test holds every branch of the fence row to at least one scenario.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import db, health
from nexus.errors import CollectionNotFoundError

_TABLE = Path(__file__).resolve().parents[2] / "service/src/test/resources/index-run-census-cases.json"
_NOW = datetime.fromisoformat(json.loads(_TABLE.read_text(encoding="utf-8"))["now"].replace("Z", "+00:00"))


def _table() -> dict:
    return json.loads(_TABLE.read_text(encoding="utf-8"))


def _scenarios(family: str) -> list[dict]:
    return [s for s in _table()["scenarios"] if family in s["expect"]]


class _FixedDatetime(datetime):
    """``datetime`` whose ``now`` is the table's clock."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return _NOW


def _entry(d: dict) -> SimpleNamespace:
    """The catalog entry a document row of the table is, as the doctor's walk reads it (live documents only)."""
    return SimpleNamespace(
        tumbler=d["tumbler"], content_type=d["content_type"], source_uri=d["source_uri"], file_path=d["file_path"],
        chunk_count=d["chunk_count"], index_state=d["index_state"], index_state_reported=True,
        index_started_at=(d["index_started_at"] or "").replace("Z", "+00:00") if d["index_started_at"] else "",
        indexed_at=(d["indexed_at"] or "").replace("Z", "+00:00") if d["indexed_at"] else "",
        index_run_id=d["index_run_id"], index_content_hash=d["index_content_hash"], meta=d["meta"],
        physical_collection=d["collection"],
    )


def _walk(scenario: dict) -> list[SimpleNamespace]:
    return [_entry(d) for d in scenario["documents"] if not d["deleted"]]


class _Catalog:
    """The catalog reader the rows ask beside the walk: owners, roots, and the manifest rows of the heal's candidates."""

    def __init__(self, scenario: dict) -> None:
        self._manifest_rows = {d["tumbler"]: d["manifest_rows"] for d in scenario["documents"]}

    def list_owners(self, include_deactivated: bool = False) -> list:
        return []

    def owners_with_roots(self) -> dict:
        return {}

    def get_manifests(self, tumblers: list[str]) -> dict:
        return {t: [object()] * self._manifest_rows[t] for t in tumblers if self._manifest_rows.get(t)}

    def close(self) -> None:
        pass


class _Collection:
    """One collection's chunk rows, read the way the heal reads them: by ``content_hash`` ``$in``, in pages."""

    def __init__(self, rows: list[tuple[str, dict]]) -> None:
        self._rows = rows

    def get(self, include: list[str], limit: int, offset: int, sensitivity: str, where: dict | None = None,
            include_non_live: bool = False) -> dict:
        assert include == ["metadatas"] and sensitivity == "include" and include_non_live, "the heal reads non-live rows too"
        wanted = set(where["content_hash"]["$in"]) if where else None
        hits = [(i, m) for i, m in self._rows if wanted is None or m.get("content_hash") in wanted]
        page = hits[offset:offset + limit]
        return {"ids": [i for i, _ in page], "metadatas": [m for _, m in page]}


class _T3:
    def __init__(self, scenario: dict) -> None:
        self._registered = set(scenario.get("collections", []))
        self._rows: dict[str, list[tuple[str, dict]]] = {}
        for g in scenario.get("chunk_groups", []):
            rows = self._rows.setdefault(g["collection"], [])
            rows.extend((f"{g['hash']}-{k:04d}", {"content_hash": g["hash"]}) for k in range(g["count"]))

    def get_collection(self, name: str) -> _Collection:
        if name not in self._registered:
            raise CollectionNotFoundError(name)
        return _Collection(self._rows.get(name, []))

    def close(self) -> None:
        pass


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "datetime", _FixedDatetime)


def _wire(monkeypatch: pytest.MonkeyPatch, scenario: dict) -> None:
    """The fake T3 the manifest heal reads (``_failed_hidden_rows_over`` builds it through ``nexus.db.make_t3``)."""
    monkeypatch.setattr(db, "make_t3", lambda *a, **k: _T3(scenario))


# ── the table and its constants ──────────────────────────────────────────────


def test_the_table_is_read_whole() -> None:
    """A table that lost its rows would pass the parametrized tests below with nothing to check."""
    table = _table()
    assert len(_scenarios("stale")) >= 3 and len(_scenarios("fence")) >= 7 and len(_scenarios("failed_hidden")) >= 4
    ids = [s["id"] for s in table["scenarios"]]
    assert len(set(ids)) == len(ids)
    assert any(d["deleted"] for s in table["scenarios"] for d in s["documents"]), "no scenario holds a tombstone"


def test_the_clients_constants_are_the_ones_the_engine_echoes() -> None:
    """The route echoes ``threshold_s`` and ``floor``; the Java parity test checks them against this same table."""
    constants = _table()["constants"]
    assert health._STALE_INDEXING_THRESHOLD_HOURS * 3600 == constants["stale_threshold_s"]
    assert health._PRODUCER_FENCE_RELEASE_DT == datetime.fromisoformat(constants["floor"].replace("Z", "+00:00"))


# ── stale ────────────────────────────────────────────────────────────────────


def _stale_view(rows: list[health.HealthResult]) -> dict:
    stale = [r for r in rows if "stranded in index_state='indexing'" in r.detail]
    if not stale:
        return {"count": 0, "notes": 0, "names": []}
    detail = stale[0].detail
    count = int(re.match(r"(\d+) document\(s\) stranded", detail).group(1))
    listing = re.search(r"beyond 6h: (.*?)\. A normal re-index run", detail).group(1)
    names = [item.rsplit(" (", 1)[0] for item in listing.split("; ") if not re.fullmatch(r"\+\d+ more", item)]
    notes = re.search(r"(\d+) of them are notes", detail)
    return {"count": count, "notes": int(notes.group(1)) if notes else 0, "names": names}


@pytest.mark.parametrize("scenario", _scenarios("stale"), ids=lambda s: s["id"])
def test_the_stale_fences_row_counts_what_the_tables_stale_bucket_counts(scenario: dict, monkeypatch, clock) -> None:
    _wire(monkeypatch, scenario)
    got = _stale_view(health._stale_fence_rows_over(_walk(scenario), _Catalog(scenario)))
    assert got == scenario["expect"]["stale"]


# ── fence ────────────────────────────────────────────────────────────────────

def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fence_view(rows: list[health.HealthResult]) -> tuple[str, dict]:
    """The branch of the NULL-fence logic the row took and the numbers its text carries."""
    for r in rows:
        d = r.detail
        if "stranded in index_state='indexing'" in d:
            continue
        if m := re.match(r"(\d+) document\(s\) report index_state but carry no stamp, despite being indexed after this "
                         r"install's fence baseline \(([^,]+), (a batched index run|its earliest stamped document)", d):
            names = re.search(r"\): (.*?)\. (?:A producer wrote|Two explanations fit)", d).group(1)
            listed = [i.rsplit(" (", 1)[0] for i in names.split("; ") if not re.fullmatch(r"\+\d+ more", i)]
            legacy = re.search(r"The other (\d+) reported-but-NULL", d)
            undated = re.search(r"(\d+) further reported-but-NULL document\(s\)", d)
            post, legacy_n, undated_n = int(m.group(1)), int(legacy.group(1)) if legacy else 0, int(undated.group(1)) if undated else 0
            return "post_anchor", {
                "post_anchor_count": post, "post_anchor": listed, "anchor_kind": "proven" if m.group(3).startswith("a batched") else "earliest",
                "anchor_at": _iso(m.group(2)), "undated_reported_null": undated_n, "reported_null": post + legacy_n + undated_n,
            }
        if m := re.match(r"cannot attribute: (\d+) document\(s\) indexed after full producer coverage", d):
            checked = re.search(r"none of the (\d+) stamped document\(s\)", d)
            return "no_baseline", {"floor_candidates": int(m.group(1)), "checked": int(checked.group(1)) if checked else 0}
        if m := re.match(r"none \((\d+) fenced document\(s\) checked\)", d):
            attributed = re.search(r"; (\d+) unstamped document\(s\) predate", d)
            undated = re.search(r"; (\d+) unstamped document\(s\) carry no usable indexed_at", d)
            a, u = int(attributed.group(1)) if attributed else 0, int(undated.group(1)) if undated else 0
            return "clean", {"checked": int(m.group(1)), "reported_null": a + u, "undated_reported_null": u}
        if m := re.match(r"fence live, 0 stale runs \((\d+) document\(s\) report index_state", d):
            undated = re.search(r"; (\d+) carry no usable indexed_at", d)
            return "quiet", {"checked": 0, "reported_null": int(m.group(1)),
                             "undated_reported_null": int(undated.group(1)) if undated else 0}
        if d.startswith("skipped"):
            return "skipped", {}
    raise AssertionError(f"no fence outcome in {[r.detail for r in rows]}")


@pytest.mark.parametrize("scenario", _scenarios("fence"), ids=lambda s: s["id"])
def test_the_fence_rows_branches_read_the_tables_fence_numbers(scenario: dict, monkeypatch, clock) -> None:
    _wire(monkeypatch, scenario)
    want = scenario["expect"]["fence"]
    branch, got = _fence_view(health._stale_fence_rows_over(_walk(scenario), _Catalog(scenario)))
    if branch == "post_anchor":
        assert got["post_anchor"] == want["post_anchor"] and got["post_anchor_count"] == len(want["post_anchor"])
        assert got["anchor_kind"] == want["anchor_kind"] and got["anchor_at"] == _iso(want["anchor_at"])
        assert got["reported_null"] == want["reported_null"] and got["undated_reported_null"] == want["undated_reported_null"]
    elif branch == "no_baseline":
        assert want["post_anchor"] == [] and want["anchor_kind"] is None
        assert got["floor_candidates"] == want["floor_candidates"] and got["checked"] == want["checked"]
    elif branch == "clean":
        assert want["post_anchor"] == [] and want["anchor_kind"] is not None
        assert got["checked"] == want["checked"]
        assert got["reported_null"] == want["reported_null"] and got["undated_reported_null"] == want["undated_reported_null"]
    elif branch == "quiet":
        assert want["post_anchor"] == [] and want["checked"] == 0 and want["anchor_kind"] is None
        assert got["reported_null"] == want["reported_null"] and got["undated_reported_null"] == want["undated_reported_null"]
    else:  # pragma: no cover - a scenario this table never holds
        raise AssertionError(f"{scenario['id']}: the row skipped")


def test_every_branch_of_the_fence_row_is_held_to_a_scenario(monkeypatch, clock) -> None:
    """The branches the table reaches: the row's four outcomes, none left to a lucky pass."""
    seen: set[str] = set()
    for scenario in _scenarios("fence"):
        _wire(monkeypatch, scenario)
        seen.add(_fence_view(health._stale_fence_rows_over(_walk(scenario), _Catalog(scenario)))[0])
    assert seen == {"post_anchor", "no_baseline", "clean", "quiet"}


# ── failed documents with hidden chunks ─────────────────────────────────────


def _hidden_view(rows: list[health.HealthResult]) -> dict:
    d = rows[0].detail
    if m := re.match(r"(\d+) of (\d+) failed document\(s\) have chunks stored with no manifest owner", d):
        return {"failed": int(m.group(2)), "count": int(m.group(1))}
    if m := re.match(r"none \((\d+) failed document\(s\), none with stored chunks hidden\)", d):
        return {"failed": int(m.group(1)), "count": 0}
    if d.startswith("not applicable (no failed index runs)"):
        return {"failed": 0, "count": 0}
    raise AssertionError(d)


@pytest.mark.parametrize("scenario", _scenarios("failed_hidden"), ids=lambda s: s["id"])
def test_the_failed_runs_row_counts_what_the_tables_failed_hidden_bucket_counts(scenario: dict, monkeypatch) -> None:
    _wire(monkeypatch, scenario)
    want = scenario["expect"]["failed_hidden"]
    assert _hidden_view(health._failed_hidden_rows_over(_walk(scenario), _Catalog(scenario))) == {"failed": want["failed"], "count": want["count"]}


def test_the_heal_dry_run_itself_rebuilds_exactly_the_tables_hidden_count(monkeypatch) -> None:
    """The row's count is the heal's ``reconciled``; this runs ``heal_manifest_gaps`` directly, so the oracle the engine bucket
    is held to is the heal and not only the row's wording of it (the shared walk the row used to read is going away)."""
    from nexus.catalog.manifest_heal import heal_manifest_gaps

    for scenario in _scenarios("failed_hidden"):
        _wire(monkeypatch, scenario)
        failed = [e for e in _walk(scenario) if e.index_state == "failed"]
        result = heal_manifest_gaps(failed, _Catalog(scenario), lambda s=scenario: _T3(s), None, dry_run=True)
        assert result.reconciled == scenario["expect"]["failed_hidden"]["count"], scenario["id"]
