# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P4-S1 (Gap 4): a second live clone with the same folder name gets its own owner.

CLI through ``nx index repo`` (the run's own indexing stubbed out: what is under test is the owner the command
registers) against ``_OwnerEngine``, a mock engine that answers the owner routes the way the real one does: an upsert
without a prefix resolves ``(name, owner_type)`` FIRST and ``repo_hash`` second, converges on the row it finds and
overwrites that row's hash and root (``OwnerConvergenceByNameCharacterizationTest``; the engine's matching is unchanged
by this RDR, so the client has to stop sending the name that takes an owner over). The same behaviour against the real
engine is pinned in ``tests/integration/test_second_clone_real_engine.py``.

The two direct ``register_owner`` sites (``indexer._catalog_hook`` and ``nx catalog backfill``) are driven with the hash
and name their callers pass, because tests and ``backfill`` give an explicit hash the rule must not recompute."""
from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands.catalog import _backfill_repos
from nexus.indexer import _catalog_hook
from nexus.repo_identity import _repo_identity_with_main


class _OwnerEngine:
    """Owners by tumbler prefix. Unknown routes answer 404, so a write this test did not expect shows as an error."""

    def __init__(self) -> None:
        self.owners: dict[str, dict] = {}
        self.upserts: list[dict] = []

    def _resolve(self, body: dict) -> str | None:
        by_name = next((p for p, o in self.owners.items()
                        if o["name"] == body["name"] and o["owner_type"] == body["owner_type"]), None)
        by_hash = next((p for p, o in self.owners.items()
                        if body.get("repo_hash") and o["repo_hash"] == body["repo_hash"]), None)
        return by_name or by_hash

    def handle(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        key = (request.method, request.url.path)
        if key == ("GET", "/version"):
            return httpx.Response(200, json={"release_version": "0.1.200"})
        if key == ("GET", "/v1/catalog/owners/by_repo"):
            hit = next((o for o in self.owners.values() if o["repo_hash"] == query.get("repo_hash")), None)
            return httpx.Response(200, json=hit) if hit else httpx.Response(404, json={})
        if key == ("GET", "/v1/catalog/owners/by_name"):
            return httpx.Response(200, json={"owners": [o for o in self.owners.values() if o["name"] == query["name"]]})
        if key == ("GET", "/v1/catalog/owners/show"):
            hit = self.owners.get(query.get("tumbler_prefix", ""))
            return httpx.Response(200, json=hit) if hit else httpx.Response(404, json={})
        if key == ("POST", "/v1/catalog/owners/upsert"):
            self.upserts.append(body)
            prefix = self._resolve(body) or f"1.{len(self.owners) + 1}"
            row = self.owners.get(prefix, {"tumbler_prefix": prefix})
            row.update({k: body.get(k, row.get(k, "")) for k in ("name", "owner_type", "repo_hash", "repo_root")})
            self.owners[prefix] = row
            return httpx.Response(200, json={"tumbler_prefix": prefix})
        return httpx.Response(404, json={"error": request.url.path})

    def client(self) -> HttpCatalogClient:
        cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
        cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(self.handle))
        return cat

    def snapshot(self) -> dict[str, tuple[str, str, str]]:
        return {p: (o["name"], o["repo_hash"], o["repo_root"]) for p, o in self.owners.items()}


def _clone(base: Path, where: str) -> Path:
    path = base / where / "proj"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _identity(repo: Path) -> tuple[str, str]:
    _name, repo_hash, main_repo = _repo_identity_with_main(repo)
    return repo_hash, str(main_repo)


def _index(engine: _OwnerEngine, repo: Path):
    """``nx index repo <repo>`` with the registry fresh (so the command registers the owner) and the run's own work
    stubbed. The taxonomy-deferral probe the command makes after registering would reach the engine's ``/version``
    through the vector client, which a tenant-free test has none of, so its two reads are stubbed too."""
    added: set[str] = set()
    reg = MagicMock()
    reg.get.side_effect = lambda p: {"collection": "code__proj"} if str(p) in added else None
    reg.add.side_effect = lambda p, cat=None: added.add(str(p))
    cat = engine.client()
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", return_value={}), \
            patch("nexus.mcp_infra.taxonomy_failure_marker_path", return_value=repo.parent.parent / "taxonomy-failed"), \
            patch("nexus.mcp_infra.engine_process_uptime_seconds", return_value=None), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=cat):
        return CliRunner().invoke(main, ["index", "repo", str(repo)], env={"NX_NO_TELEMETRY": "1"})


def test_two_live_clones_get_two_owners_and_rerun_changes_neither_root(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    hash_a, root_a = _identity(first)
    hash_b, root_b = _identity(second)
    assert hash_a != hash_b
    engine = _OwnerEngine()

    for repo in (first, second):
        result = _index(engine, repo)
        assert result.exit_code == 0, result.output

    assert engine.snapshot() == {
        "1.1": ("proj", hash_a, root_a),
        "1.2": (f"proj-{hash_b}", hash_b, root_b),
    }
    before = engine.snapshot()
    for repo in (first, second, first, second):  # a run from either clone changes neither owner
        result = _index(engine, repo)
        assert result.exit_code == 0, result.output
        assert engine.snapshot() == before


def test_the_first_clone_registers_under_its_plain_name(tmp_path):
    first = _clone(tmp_path, "work")
    engine = _OwnerEngine()
    assert _index(engine, first).exit_code == 0
    assert [o["name"] for o in engine.owners.values()] == ["proj"]


def test_symlinked_path_to_same_clone_is_not_split_root_and_hash_unchanged(tmp_path):
    clone = _clone(tmp_path, "work")
    link = tmp_path / "link"
    link.symlink_to(clone.parent, target_is_directory=True)
    hash_a, root_a = _identity(clone)
    engine = _OwnerEngine()
    assert _index(engine, clone).exit_code == 0

    result = _index(engine, link / "proj")
    assert result.exit_code == 0, result.output
    assert engine.snapshot() == {"1.1": ("proj", hash_a, root_a)}

    # An owner recorded through the link under another hash (a registration older than the identity rules) and a run from
    # the real path: one clone reached two ways is not two clones.
    stale = _OwnerEngine()
    stale.owners["1.1"] = {"tumbler_prefix": "1.1", "name": "proj", "owner_type": "repo", "repo_hash": "0ldhash0",
                           "repo_root": str(link / "proj")}
    assert _index(stale, clone).exit_code == 0
    assert sorted(o["name"] for o in stale.owners.values()) == ["proj"]


def test_gone_root_registers_as_today(tmp_path):
    second = _clone(tmp_path, "scratch")
    hash_b, root_b = _identity(second)
    engine = _OwnerEngine()
    engine.owners["1.1"] = {"tumbler_prefix": "1.1", "name": "proj", "owner_type": "repo", "repo_hash": "0ldhash0",
                            "repo_root": str(tmp_path / "work-gone" / "proj")}

    result = _index(engine, second)
    assert result.exit_code == 0, result.output
    # the plain name: the engine converges on the owner whose root is gone (the Gap 3 hint picks that case up)
    assert engine.snapshot() == {"1.1": ("proj", hash_b, root_b)}


def test_direct_register_owner_sites_pass_their_explicit_hash(tmp_path, monkeypatch):
    """``_catalog_hook`` and ``nx catalog backfill`` register with a hash and name their callers hand them (tests pass a
    fixed hash; backfill hashes the registry path with no main-repository resolution). The rule gets those arguments
    and the register call carries the same hash, with the split name when another clone is live."""
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    engine = _OwnerEngine()
    engine.owners["1.1"] = {"tumbler_prefix": "1.1", "name": "proj", "owner_type": "repo", "repo_hash": "feedbeef",
                            "repo_root": str(first)}

    # indexer._catalog_hook: an explicit hash that is NOT the path-derived one
    cat = engine.client()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **kw: cat)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda **kw: cat)
    # the mock engine has no reconcile route, so the hook fails after the owner and audits the failure into T2; a
    # tenant-free test has no T2
    monkeypatch.setattr("nexus.indexer.record_catalog_hook_failure", lambda **kw: None)
    try:
        _catalog_hook(repo=second, repo_name="proj", repo_hash="cafe0001", head_hash="abc", indexed_files=[])
    except Exception:  # noqa: BLE001 — the hook's work after the owner is not under test; the owner row is
        pass
    registered = [u for u in engine.upserts if u["repo_hash"] == "cafe0001"]
    assert len(registered) == 1, engine.upserts
    assert registered[0]["name"] == "proj-cafe0001" and registered[0]["repo_root"] == str(second)
    assert engine.owners["1.1"]["repo_hash"] == "feedbeef" and engine.owners["1.1"]["repo_root"] == str(first)

    # nx catalog backfill: the hash is the sha256 of the registry path, 8 hex
    registry = MagicMock()
    third = _clone(tmp_path, "archive")
    registry.all_info.return_value = {str(third): {"name": "proj", "status": "ready"}}
    _backfill_repos(engine.client(), registry, False)
    path_hash = sha256(str(third).encode()).hexdigest()[:8]
    by_hash = {o["repo_hash"]: o for o in engine.owners.values()}
    assert by_hash[path_hash]["name"] == f"proj-{path_hash}" and by_hash[path_hash]["repo_root"] == str(third)
    assert by_hash["feedbeef"]["name"] == "proj"


def test_an_owner_already_shared_by_two_clones_gives_the_clone_it_no_longer_carries_its_own_owner(tmp_path):
    """An owner two clones registered before this rule holds ONE hash and root (the later write won). The clone whose
    hash it no longer carries misses ``owner_for_repo``, sees the other clone live under the plain name, and registers
    ``proj-<its hash>``: a new owner, so a new collection and one full re-registration. The shared owner is untouched."""
    clone_a, clone_b = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    hash_a, root_a = _identity(clone_a)
    hash_b, root_b = _identity(clone_b)
    engine = _OwnerEngine()
    engine.owners["1.1"] = {"tumbler_prefix": "1.1", "name": "proj", "owner_type": "repo", "repo_hash": hash_b,
                            "repo_root": root_b}

    result = _index(engine, clone_a)
    assert result.exit_code == 0, result.output
    assert engine.snapshot() == {"1.1": ("proj", hash_b, root_b), "1.2": (f"proj-{hash_a}", hash_a, root_a)}


class _FailingRoute(_OwnerEngine):
    """The engine answers 500 on one owner route."""

    def __init__(self, route: str) -> None:
        super().__init__()
        self.route = route

    def handle(self, request):
        if request.url.path == self.route:
            return httpx.Response(500, json={"error": "boom"})
        return super().handle(request)


def test_a_failed_by_name_lookup_registers_nothing(tmp_path):
    """The command's pre-registration is best effort (the indexer registers the owner regardless), so the exit code
    says nothing about the rule. What the rule owes is that an engine error from its lookup is not read as "no owner
    of that name": no upsert goes out. A rule that swallowed the error and returned the plain name would upsert it."""
    engine = _FailingRoute("/v1/catalog/owners/by_name")
    result = _index(engine, _clone(tmp_path, "scratch"))
    assert result.exit_code == 0, result.output
    assert engine.upserts == [] and engine.owners == {}


def test_a_failed_owner_show_registers_nothing(tmp_path):
    """``get_owner_by_prefix`` maps only a 404 to "no such owner"; a 500 from ``/owners/show`` raises through the
    rule, so a same-name owner whose row cannot be read is not read as absent (a plain-name upsert would take it)."""
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    hash_a, root_a = _identity(first)
    engine = _FailingRoute("/v1/catalog/owners/show")
    engine.owners["1.1"] = {"tumbler_prefix": "1.1", "name": "proj", "owner_type": "repo", "repo_hash": hash_a,
                            "repo_root": root_a}
    result = _index(engine, second)
    assert result.exit_code == 0, result.output
    assert engine.upserts == [] and engine.snapshot() == {"1.1": ("proj", hash_a, root_a)}
