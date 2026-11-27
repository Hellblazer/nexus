# SPDX-License-Identifier: AGPL-3.0-or-later
"""The telemetry-rename and persist-split cloud-leg probes, executed against fake clients (no engine, no cloud).

What matters about both probes is that they answer the same in every engine regime, because the leg fails a status the cloud
returns and the local engine does not (``_judge``). The fakes model the two regimes the engine has for a per-store rename:
before gate 3 latches it is the name path (200), and once it has latched it is refused 409 rename_not_in_registry unless the
registry already moved the label (RenameGuard.requireRegistryRenamed).
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

import httpx
import nexus.commands.catalog as catalog_cmds
import nexus.corpus as corpus
import nexus.db as db
import nexus.db.http_engine_status as engine_status
import nexus.db.t2.http_taxonomy_store as taxonomy
import nexus.db.t2.http_telemetry_store as telemetry
import pytest

from tests.scripts.test_cloud_leg import _res, cl


def _status_error(code: int, body: dict) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://engine/v1/x")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, json=body, request=req))


class FakeCatalog:
    """The registry: rows by label, a live-tuple rule (one live row per tuple), nameless registration that adopts or mints."""

    def __init__(self, home: str = "knowledge__home__m__v1") -> None:
        self.rows: dict[str, dict] = {home: {"name": home, "content_type": "knowledge", "owner_id": "1.1", "embedding_model": "m",
                                             "model_version": "v1", "superseded_by": ""}}
        self.home = home
        self.minted = 0
        self.calls: list[tuple] = []

    def get_collection(self, name):
        return self.rows.get(name)

    def _live_on(self, owner):
        return [r for r in self.rows.values() if r["owner_id"] == owner and not r["superseded_by"]]

    def register_collection(self, name, *, content_type, owner_id, embedding_model, model_version):
        self.calls.append(("register", name, owner_id))
        held = self._live_on(owner_id)
        if name is None:
            if held:
                return SimpleNamespace(label=held[0]["name"])
            self.minted += 1
            name = f"c{self.minted}"
        elif held and held[0]["name"] != name:
            raise _status_error(409, {"error": f"collections '{held[0]['name']}' and '{name}' are both live for the tuple"})
        self.rows.setdefault(name, {"name": name, "content_type": content_type, "owner_id": owner_id,
                                    "embedding_model": embedding_model, "model_version": model_version, "superseded_by": ""})
        return SimpleNamespace(label=name)

    def _post(self, path, body, *, idempotent=True):
        assert path == "/collections/rename" and idempotent is False
        assert set(body) == {"old_name", "new_name"}, "the registry rename must send no content type or owner: that would move the tuple"
        self.calls.append(("rename", body["old_name"], body["new_name"]))
        old, new = body["old_name"], body["new_name"]
        row = self.rows.pop(old)
        row["name"] = new
        self.rows[new] = row


class FakeTelemetry:
    def __init__(self, catalog: FakeCatalog, latched: bool, fail_legal: int | None = None) -> None:
        self.catalog, self.latched, self.fail_legal = catalog, latched, fail_legal
        self.calls: list[tuple[str, str]] = []

    def query_collection_stats(self, *_a, **_k):
        return {"row_count": 0}

    def list_hook_failures(self, **_k):
        return {"total": 0, "rows": []}

    def rename_collection(self, *, old, new):
        self.calls.append((old, new))
        if new == "c42":   # the label rule runs before the rename guard: 400 in every gate state, a body with `error` and no `reason`
            raise _status_error(400, {"error": "collection label 'c42' is of the minted form (c followed by digits)"})
        if self.fail_legal:
            raise _status_error(self.fail_legal, {"error": "boom", "reason": "rename_refused"})
        if self.latched:
            live = [n for n, r in self.catalog.rows.items() if not r["superseded_by"]]
            if new not in live or old in live:
                raise _status_error(409, {"error": "rename the collection in the registry first", "reason": "rename_not_in_registry"})
        return {"search_telemetry": 0, "hook_failures": 0}


def _run(code: str, monkeypatch, **fakes):
    """exec a probe snippet with the client modules patched; returns (stdout, SystemExit-or-None)."""
    cat = fakes["catalog"]
    monkeypatch.setattr(catalog_cmds, "_get_catalog", lambda: cat)
    monkeypatch.setattr(corpus, "t3_collection_name", lambda *_a, **_k: cat.home)
    monkeypatch.setattr(db, "make_t3", lambda: object())
    if "telemetry" in fakes:
        monkeypatch.setattr(telemetry, "HttpTelemetryStore", lambda: fakes["telemetry"])
    if "taxonomy" in fakes:
        monkeypatch.setattr(taxonomy, "HttpTaxonomyStore", lambda: fakes["taxonomy"])
    latch = fakes.get("status", "absent")
    monkeypatch.setattr(engine_status, "fetch_engine_status",
                        lambda timeout=0: None if latch == "absent" else {"rdr229_completion": {"gate3_latched": latch}})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            exec(compile(code.replace("@label@", "cloudleg-r1"), "probe", "exec"), {"__name__": "probe"})  # noqa: S102 - the probe is the unit under test
        except SystemExit as exc:
            return out.getvalue(), exc
    return out.getvalue(), None


# ── telemetry rename ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("latched", [False, True])
def test_the_legal_rename_answers_200_with_zero_counts_in_both_regimes(monkeypatch, latched) -> None:
    cat = FakeCatalog()
    tel = FakeTelemetry(cat, latched)
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=tel, status=latched)
    assert err is None, (err, out)
    assert "minted target refused 400" in out  # the line the step waits for
    assert f"result: legal rename answered 200 {{'search_telemetry': 0, 'hook_failures': 0}} gate3_latched={latched}" in out
    # order: the minted target first, then the registry rename, then the same pair in the history tables
    assert tel.calls == [("cloudleg-tel-probe-a", "c42"), ("cloudleg-tel-probe-a", "cloudleg-tel-probe-b")]
    assert [c for c in cat.calls if c[0] == "rename"] == [("rename", "cloudleg-tel-probe-a", "cloudleg-tel-probe-b"),
                                                          ("rename", "cloudleg-tel-probe-b", "cloudleg-tel-probe-a")]
    # renamed back: the next run starts the same
    assert cat.rows["cloudleg-tel-probe-a"]["owner_id"] == "1.9997" and "cloudleg-tel-probe-b" not in cat.rows


def test_the_two_regimes_leave_the_same_taps_so_the_comparator_passes_the_step() -> None:
    """The slots differ only in the latch state the probe prints; every status is the same, so _judge passes."""
    taps = [[200, "GET", "/v1/status"], [400, "POST", "/v1/telemetry/rename_collection"],
            [200, "POST", "/v1/catalog/collections/rename"], [200, "POST", "/v1/telemetry/rename_collection"]]
    local = _res("route-telemetry-rename", "local", taps=taps) | {"result": "legal rename answered 200 gate3_latched=False"}
    cloud = _res("route-telemetry-rename", "cloud", taps=taps) | {"result": "legal rename answered 200 gate3_latched=True"}
    assert cl.judge_step(local, cloud)[0] == "PASS"
    # the shape the old probe produced on a latched cloud: a cloud-only 409 that the comparator (rightly) fails
    old_cloud = _res("route-telemetry-rename", "cloud", exit=1, taps=[[400, "POST", "/v1/telemetry/rename_collection"],
                                                                      [409, "POST", "/v1/telemetry/rename_collection"]])
    old_local = _res("route-telemetry-rename", "local", taps=[[400, "POST", "/v1/telemetry/rename_collection"],
                                                              [200, "POST", "/v1/telemetry/rename_collection"]])
    verdict, why = cl.judge_step(old_local, old_cloud)
    assert verdict == "FAIL" and "cloud-only 409" in why


def test_a_refused_legal_rename_fails_the_probe_and_still_renames_the_collection_back(monkeypatch) -> None:
    cat = FakeCatalog()
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, True, fail_legal=409), status=True)
    assert isinstance(err, SystemExit) and "409" in str(err.code)
    assert "result: legal rename answered 409 reason rename_refused gate3_latched=True" in out
    assert "cloudleg-tel-probe-a" in cat.rows and "cloudleg-tel-probe-b" not in cat.rows


def test_an_unreadable_latch_state_fails_the_probe_before_it_touches_the_registry(monkeypatch) -> None:
    cat = FakeCatalog()
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, False), status="absent")
    assert isinstance(err, SystemExit) and "latch state is unreadable" in str(err.code)
    assert not [c for c in cat.calls if c[0] in ("rename", "register")]


def test_the_probes_collection_is_registered_once_and_reused_by_a_second_run(monkeypatch) -> None:
    cat = FakeCatalog()
    for _ in range(2):
        out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, False), status=False)
        assert err is None, (err, out)
    assert [c for c in cat.calls if c[0] == "register"] == [("register", "cloudleg-tel-probe-a", "1.9997")]


def test_a_run_that_stopped_between_the_rename_and_its_undo_is_recovered(monkeypatch) -> None:
    cat = FakeCatalog()
    cat.rows["cloudleg-tel-probe-b"] = {"name": "cloudleg-tel-probe-b", "content_type": "knowledge", "owner_id": "1.9997",
                                        "embedding_model": "m", "model_version": "v1", "superseded_by": ""}
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, True), status=True)
    assert err is None, (err, out)
    assert not [c for c in cat.calls if c[0] == "register"]   # no second live row on the tuple
    assert "cloudleg-tel-probe-a" in cat.rows and "cloudleg-tel-probe-b" not in cat.rows


def test_the_minted_target_refusal_prints_the_bodys_error_when_it_has_no_reason(monkeypatch) -> None:
    cat = FakeCatalog()
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, False), status=False)
    assert err is None
    assert "result: minted target answered 400 error 'collection label 'c42' is of the minted form" in out.replace('"', "'")


# ── taxonomy persist_split ───────────────────────────────────────────────────

class FakeTaxonomy:
    def __init__(self) -> None:
        self.topics: dict[int, str] = {}
        self.filed_under: list[str] = []

    def persist_discovered_topics(self, collection, specs):
        self.filed_under.append(collection)
        ids = [len(self.topics) + i + 1 for i, _ in enumerate(specs)]
        for i in ids:
            self.topics[i] = collection
        return ids

    def persist_split(self, req):
        if self.topics[req["topic_id"]] != req["collection_name"]:
            raise _status_error(400, {"error": "topic is not a topic of the named collection"})
        raise AssertionError("the probe's split must be refused")

    def delete_topic(self, topic_id):
        self.topics.pop(topic_id)


def test_persist_split_files_its_topic_under_the_label_the_registration_answers_and_reruns_reuse_the_row(monkeypatch) -> None:
    cat = FakeCatalog()
    tax = FakeTaxonomy()
    for run in (1, 2):
        out, err = _run(cl._routes.PERSIST_SPLIT_REFUSAL, monkeypatch, catalog=cat, taxonomy=tax)
        assert err is None, (err, out)
        assert "persist_split refused 400" in out  # the line the step waits for
        assert "result: persist_split answered 400 error 'topic is not a topic of the named collection'" in out
    registrations = [c for c in cat.calls if c[0] == "register"]
    assert registrations == [("register", None, "1.9998")] * 2      # nameless, on the dedicated tuple
    other_rows = [r for r in cat.rows.values() if r["owner_id"] == "1.9998"]
    assert len(other_rows) == 1 and other_rows[0]["name"] == "c1"   # the engine minted once; run 2 adopted it
    assert tax.filed_under == ["c1", "c1"]                          # the label the registration answered, never a per-run name
    assert not tax.topics                                           # its topic is removed again


def test_two_slots_that_differ_in_whether_the_row_was_minted_or_reused_judge_the_same(monkeypatch) -> None:
    taps = [[200, "POST", "/v1/catalog/collections/upsert"], [400, "POST", "/v1/taxonomy/topics/persist_split"]]
    local = _res("route-taxonomy-persist-split", "local", taps=taps) | {"result": "persist_split answered 400 error 'x'"}
    cloud = _res("route-taxonomy-persist-split", "cloud", taps=taps) | {"result": "persist_split answered 400 error 'x'"}
    assert cl.judge_step(local, cloud)[0] == "PASS"

