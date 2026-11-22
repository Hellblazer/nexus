# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): an index run registers its collections before the catalog hook, in either topology.

The rule is "a catalog write needs a registry row": the engine answers a document under an unregistered collection
with a 422 (``unregistered_collection``), so ``_run_index`` registers the code / docs / rdr collections it files under
before it calls ``_catalog_hook``, whichever backend holds the vectors. Until P3.1 the registration ran only when the
vectors were on the engine, so a client-embedding T3 double (the ``NX_STORAGE_BACKEND_VECTORS`` opt-out) filed
documents under collections nobody had registered.

Needs no engine: the registrar and the hook are fakes (``NX_TEST_T2_SUBSTRATE=none`` runs it).
"""
from __future__ import annotations

import pytest

from nexus.collection_errors import SupersededCollectionWriteError
from nexus.corpus import EmbeddingProfileMismatchError
from tests.test_indexer import _chunk, _mock_db, _patches, _reg, _reset_t3_singleton_per_test, _voyage  # noqa: F401 — the autouse reset is imported by name to apply here

pytestmark = pytest.mark.usefixtures("cloud_mode")

#: The two topologies: the engine holds the vectors (the default), and the client-embedding double (the opt-out).
_TOPOLOGIES = [pytest.param("service", id="vectors-on-engine"), pytest.param("local", id="vectors-on-a-double")]


def _run_once(tmp_path, monkeypatch, backend: str, *, register, files=("main.py",)):
    """One ``_run_index`` over a tiny repo with *register* as the registrar and a hook that records its call."""
    from nexus.indexer import _run_index  # noqa: PLC0415 — deferred so the monkeypatches below land first

    # Local mode is the only posture that survives the opt-out, and it is harmless with the engine as backend.
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", backend)
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in files:
        (repo / name).write_text("x = 1\n" if name.endswith(".py") else "# T\n\nbody\n")

    events: list[tuple[str, str]] = []

    def _fake_register(name, *, registrar=None, kwargs=None):
        events.append(("register", name))
        return register(name, kwargs)

    def _fake_hook(*args, **kwargs):
        events.append(("hook", ""))
        return {}

    db, _col = _mock_db()
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk()]},
        "voyageai.Client": {"return_value": _voyage(1)},
        "nexus.corpus.ensure_collection_registered": {"side_effect": _fake_register},
        "nexus.indexer._catalog_hook": {"side_effect": _fake_hook},
    }):
        _run_index(repo, _reg())
    return events


@pytest.mark.parametrize("backend", _TOPOLOGIES)
def test_the_run_registers_its_collection_before_the_hook_in_either_topology(tmp_path, monkeypatch, backend):
    events = _run_once(tmp_path, monkeypatch, backend, register=lambda name, kwargs: None)

    registered = [name for kind, name in events if kind == "register"]
    assert registered, f"no collection was registered with {backend!r} as the vector backend: {events!r}"
    assert all(name.startswith("code__") for name in registered), (
        f"only the code collection has files, so only it registers: {registered!r}")
    kinds = [kind for kind, _name in events]
    assert "hook" in kinds, f"the catalog hook never ran: {events!r}"
    assert kinds.index("hook") > max(i for i, k in enumerate(kinds) if k == "register"), (
        f"the hook ran before a collection was registered: {events!r}")


@pytest.mark.parametrize("backend", _TOPOLOGIES)
def test_a_content_type_with_no_files_registers_nothing(tmp_path, monkeypatch, backend):
    events = _run_once(tmp_path, monkeypatch, backend, register=lambda name, kwargs: None)

    assert not [n for kind, n in events if kind == "register" and not n.startswith("code__")], (
        f"a deferred (docs / rdr) collection was registered when no such file exists: {events!r}")


@pytest.mark.parametrize("backend", _TOPOLOGIES)
def test_an_unreachable_registry_stops_the_run_in_either_topology(tmp_path, monkeypatch, backend):
    """linda 2026-11-22: no lenient path for the test-only double topology; the hook never runs on a run whose
    collections did not register."""

    def _unreachable(name, kwargs):
        raise RuntimeError("catalog unreachable")

    with pytest.raises(RuntimeError, match="catalog unreachable"):
        _run_once(tmp_path, monkeypatch, backend, register=_unreachable)


@pytest.mark.parametrize("backend", _TOPOLOGIES)
def test_a_profile_refusal_stops_the_run_in_either_topology(tmp_path, monkeypatch, backend):
    """The profile check is untouched (``ensure_collection_registered`` raises it) and nothing downstream absorbs it."""

    def _refused(name, kwargs):
        raise EmbeddingProfileMismatchError("code", "minilm-l6-v2-384", "bge-base-en-v15-768")

    with pytest.raises(EmbeddingProfileMismatchError):
        _run_once(tmp_path, monkeypatch, backend, register=_refused)


@pytest.mark.parametrize("backend", _TOPOLOGIES)
def test_a_retired_name_stops_the_run_in_either_topology(tmp_path, monkeypatch, backend):
    def _retired(name, kwargs):
        raise SupersededCollectionWriteError(name, "code__successor__bge-base-en-v15-768__v1")

    with pytest.raises(SupersededCollectionWriteError):
        _run_once(tmp_path, monkeypatch, backend, register=_retired)
