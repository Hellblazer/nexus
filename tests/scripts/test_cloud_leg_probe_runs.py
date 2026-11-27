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
    """The registry: rows by label, a live-tuple rule (one live row per tuple), nameless registration that adopts or mints.

    The rename follows the engine. Before gate 3 it is the name path: the old row is kept as a tombstone with
    superseded_by naming the new label, a new row is born, and a rename back revives the tombstone (CatalogHandler.
    handleCollectionRename: the tombstone must be empty and name the old label). From gate 3 (``latched``) it is the one-row
    rename: the row is relabelled in place and no tombstone is left. Every request is logged as (status, method, route)."""

    def __init__(self, home: str = "knowledge__home__m__v1", *, latched: bool = False, taps: list | None = None) -> None:
        self.rows: dict[str, dict] = {home: {"name": home, "content_type": "knowledge", "owner_id": "1.1", "embedding_model": "m",
                                             "model_version": "v1", "superseded_by": ""}}
        self.home, self.latched, self.minted = home, latched, 0
        self.calls: list[tuple] = []
        self.taps: list = [] if taps is None else taps

    def _tap(self, status: int, method: str, route: str) -> None:
        self.taps.append([status, method, "/v1/catalog" + route])

    def get_collection(self, name):
        row = self.rows.get(name)
        self._tap(200 if row else 404, "GET", "/collections/get")
        return row

    def _live_on(self, owner):
        return [r for r in self.rows.values() if r["owner_id"] == owner and not r["superseded_by"]]

    def register_collection(self, name, *, content_type, owner_id, embedding_model, model_version):
        self.calls.append(("register", name, owner_id))
        held = self._live_on(owner_id)
        if name is None:
            if held:
                self._tap(200, "POST", "/collections/upsert")
                return SimpleNamespace(label=held[0]["name"])
            self.minted += 1
            name = f"c{self.minted}"
        elif held and held[0]["name"] != name:
            self._tap(409, "POST", "/collections/upsert")
            raise _status_error(409, {"error": f"collections '{held[0]['name']}' and '{name}' are both live for the tuple"})
        self._tap(200, "POST", "/collections/upsert")
        self.rows.setdefault(name, {"name": name, "content_type": content_type, "owner_id": owner_id,
                                    "embedding_model": embedding_model, "model_version": model_version, "superseded_by": ""})
        return SimpleNamespace(label=name)

    def _post(self, path, body, *, idempotent=True):
        assert path == "/collections/rename" and idempotent is False
        assert set(body) == {"old_name", "new_name"}, "the registry rename must send no content type or owner: that would move the tuple"
        old, new = body["old_name"], body["new_name"]
        self.calls.append(("rename", old, new))
        src, target = self.rows.get(old), self.rows.get(new)
        if src is None or src["superseded_by"] or (target and not target["superseded_by"]):
            self._tap(409 if src else 404, "POST", "/collections/rename")
            raise _status_error(409 if src else 404, {"error": "rename refused"})
        if self.latched:
            self.rows.pop(old)
            self.rows[new] = {**src, "name": new}
        else:
            if target is not None and target["superseded_by"] != old:
                self._tap(409, "POST", "/collections/rename")
                raise _status_error(409, {"error": "the tombstone names another collection"})
            self.rows[new] = {**src, "name": new, "superseded_by": ""}   # a new row, or the revived tombstone
            self.rows[old] = {**src, "superseded_by": new}               # the old label is retired in favour of the new
        self._tap(200, "POST", "/collections/rename")


class FakeTelemetry:
    def __init__(self, catalog: FakeCatalog, latched: bool, fail_legal: int | None = None) -> None:
        self.catalog, self.latched, self.fail_legal = catalog, latched, fail_legal
        self.calls: list[tuple[str, str]] = []

    def _tap(self, status: int, method: str, route: str) -> None:
        self.catalog.taps.append([status, method, "/v1/telemetry" + route])

    def query_collection_stats(self, *_a, **_k):
        self._tap(200, "GET", "/search/stats")
        return {"row_count": 0}

    def list_hook_failures(self, **_k):
        self._tap(200, "GET", "/hook_failures/list")
        return {"total": 0, "rows": []}

    def rename_collection(self, *, old, new):
        self.calls.append((old, new))
        if new == "c42":   # the label rule runs before the rename guard: 400 in every gate state, a body with `error` and no `reason`
            self._tap(400, "POST", "/rename_collection")
            raise _status_error(400, {"error": "collection label 'c42' is of the minted form (c followed by digits)"})
        if self.fail_legal:
            self._tap(self.fail_legal, "POST", "/rename_collection")
            raise _status_error(self.fail_legal, {"error": "boom", "reason": "rename_refused"})
        if self.latched:
            live = [n for n, r in self.catalog.rows.items() if not r["superseded_by"]]
            if new not in live or old in live:
                self._tap(409, "POST", "/rename_collection")
                raise _status_error(409, {"error": "rename the collection in the registry first", "reason": "rename_not_in_registry"})
        self._tap(200, "POST", "/rename_collection")
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
    fetches = fakes.setdefault("fetches", [])

    def fetch(timeout=0):
        fetches.append(timeout)
        cat.taps.append([200 if latch != "absent" else 0, "GET", "/v1/status"])
        return None if latch == "absent" else {"rdr229_completion": {"gate3_latched": latch}}
    monkeypatch.setattr(engine_status, "fetch_engine_status", fetch)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            exec(compile(code.replace("@label@", "cloudleg-r1"), "probe", "exec"), {"__name__": "probe"})  # noqa: S102 - the probe is the unit under test
        except SystemExit as exc:
            return out.getvalue(), exc
    return out.getvalue(), None


# ── telemetry rename ─────────────────────────────────────────────────────────

A, B = "cloudleg-tel-probe-a", "cloudleg-tel-probe-b"


def _live(cat: FakeCatalog, name: str) -> bool:
    return name in cat.rows and not cat.rows[name]["superseded_by"]


def _run_telemetry(monkeypatch, *, latched, cat=None, code=None, **kw):
    cat = cat or FakeCatalog(latched=latched)
    tel = FakeTelemetry(cat, latched, **kw)
    out, err = _run(code or cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=tel, status=latched)
    return out, err, cat, tel


@pytest.mark.parametrize("latched", [False, True])
def test_the_legal_rename_answers_200_with_zero_counts_in_both_regimes(monkeypatch, latched) -> None:
    out, err, cat, tel = _run_telemetry(monkeypatch, latched=latched)
    assert err is None, (err, out)
    assert "minted target refused 400" in out  # the line the step waits for
    assert out.splitlines()[1] == f"result: gate3_latched={latched}"   # the latch state leads the recorded result
    assert "result: legal rename answered 200 {'search_telemetry': 0, 'hook_failures': 0}" in out
    # order: the minted target first, then the registry rename, then the same pair in the history tables
    assert tel.calls == [(A, "c42"), (A, B)]
    assert [c for c in cat.calls if c[0] == "rename"] == [("rename", A, B), ("rename", B, A)]
    # renamed back: the next run starts the same (the label B is a tombstone before gate 3, absent from it)
    assert _live(cat, A) and cat.rows[A]["owner_id"] == "1.9997" and not _live(cat, B)


def test_the_name_path_rename_retires_the_old_label_and_a_rename_back_revives_it(monkeypatch) -> None:
    out, err, cat, _tel = _run_telemetry(monkeypatch, latched=False)
    assert err is None, out
    assert cat.rows[B]["superseded_by"] == A and cat.rows[A]["superseded_by"] == ""   # B retired in favour of A, A revived
    out, err, cat, _tel = _run_telemetry(monkeypatch, latched=False, cat=cat)           # the next run revives B, then A again
    assert err is None, out
    assert [c for c in cat.calls if c[0] == "register"] == [("register", A, "1.9997")]


def _tap_lists(monkeypatch, *, local_latched, cloud_latched, cloud_runs=1, code=None):
    """The taps one probe run logs on a fresh engine in the first regime and, in the second, on an engine that has already
    carried `cloud_runs - 1` earlier runs; each is the record the leg would judge."""
    out, err, cat, _tel = _run_telemetry(monkeypatch, latched=local_latched, code=code)
    local = _res("route-telemetry-rename", "local", exit=1 if err else 0, taps=cat.taps)
    cloud_cat = FakeCatalog(latched=cloud_latched)
    for run in range(cloud_runs):
        cloud_cat.taps.clear()
        out, err, _c, _t = _run_telemetry(monkeypatch, latched=cloud_latched, cat=cloud_cat, code=code)
        assert err is None or code is not None, out
    return local, _res("route-telemetry-rename", "cloud", exit=1 if err else 0, taps=cloud_cat.taps)


def test_the_logged_taps_of_the_two_regimes_pass_the_comparator(monkeypatch) -> None:
    """A fresh unlatched local engine against a latched cloud, on its first run and on a later one that finds the probe's
    collection already registered: the same bad statuses (the 400 the probe expects), so judge_step passes."""
    for runs in (1, 2):
        local, cloud = _tap_lists(monkeypatch, local_latched=False, cloud_latched=True, cloud_runs=runs)
        bad = sorted({t[0] for t in cloud["taps"] if t[0] >= 400})
        assert bad == ([400, 404] if runs == 1 else [400]), cloud["taps"]   # the minted target; a first run also meets its lookups' 404s
        assert cl.judge_step(local, cloud)[0] == "PASS", (cl.judge_step(local, cloud), local["taps"], cloud["taps"])


def test_a_probe_that_skips_the_registry_rename_fails_the_comparator_on_a_latched_cloud(monkeypatch) -> None:
    """The control: without the registry rename the latched engine answers the legal pair 409, a cloud-only status."""
    code = cl._routes.TELEMETRY_RENAME.replace("rename_in_registry(a, b)\ntry:", "try:")
    assert code != cl._routes.TELEMETRY_RENAME
    local, cloud = _tap_lists(monkeypatch, local_latched=False, cloud_latched=True, code=code)
    verdict, why = cl.judge_step(local, cloud)
    assert verdict == "FAIL" and "cloud-only 409" in why


def test_a_refused_legal_rename_fails_the_probe_and_still_renames_the_collection_back(monkeypatch) -> None:
    out, err, cat, _tel = _run_telemetry(monkeypatch, latched=True, fail_legal=409)
    assert isinstance(err, SystemExit) and "409" in str(err.code)
    assert "result: legal rename answered 409 reason rename_refused" in out
    assert _live(cat, A) and not _live(cat, B)


def test_an_unreadable_latch_state_is_retried_once_then_fails_the_probe_before_it_touches_the_registry(monkeypatch) -> None:
    cat = FakeCatalog()
    fetches: list = []
    out, err = _run(cl._routes.TELEMETRY_RENAME, monkeypatch, catalog=cat, telemetry=FakeTelemetry(cat, False), status="absent", fetches=fetches)
    assert isinstance(err, SystemExit) and "latch state is unreadable" in str(err.code)
    assert len(fetches) == 2   # one retry
    assert not [c for c in cat.calls if c[0] in ("rename", "register")] and not [t for t in cat.taps if "rename" in t[2]]


def test_a_latch_state_read_on_the_retry_is_used(monkeypatch) -> None:
    cat = FakeCatalog(latched=True)
    answers = iter([None, {"rdr229_completion": {"gate3_latched": True}}])
    out = io.StringIO()
    monkeypatch.setattr(engine_status, "fetch_engine_status", lambda timeout=0: next(answers))
    monkeypatch.setattr(catalog_cmds, "_get_catalog", lambda: cat)
    monkeypatch.setattr(corpus, "t3_collection_name", lambda *_a, **_k: cat.home)
    monkeypatch.setattr(db, "make_t3", lambda: object())
    monkeypatch.setattr(telemetry, "HttpTelemetryStore", lambda: FakeTelemetry(cat, True))
    with contextlib.redirect_stdout(out):
        exec(compile(cl._routes.TELEMETRY_RENAME, "probe", "exec"), {"__name__": "probe"})  # noqa: S102 - the probe is the unit under test
    assert "result: gate3_latched=True" in out.getvalue()


def test_the_probes_collection_is_registered_once_and_reused_by_a_second_run(monkeypatch) -> None:
    cat = FakeCatalog()
    for _ in range(2):
        out, err, cat, _tel = _run_telemetry(monkeypatch, latched=False, cat=cat)
        assert err is None, (err, out)
    assert [c for c in cat.calls if c[0] == "register"] == [("register", A, "1.9997")]


@pytest.mark.parametrize("latched", [False, True])
def test_a_run_that_stopped_between_the_rename_and_its_undo_is_recovered(monkeypatch, latched) -> None:
    """The collection is left labelled B. Before gate 3 that is the name path's state: B live, A a tombstone naming B."""
    cat = FakeCatalog(latched=latched)
    row = {"name": B, "content_type": "knowledge", "owner_id": "1.9997", "embedding_model": "m", "model_version": "v1", "superseded_by": ""}
    cat.rows[B] = row
    if not latched:
        cat.rows[A] = {**row, "name": A, "superseded_by": B}
    out, err, cat, _tel = _run_telemetry(monkeypatch, latched=latched, cat=cat)
    assert err is None, (err, out)
    assert not [c for c in cat.calls if c[0] == "register"]   # no second live row on the tuple
    assert _live(cat, A) and not _live(cat, B)


def test_the_minted_target_refusal_prints_the_bodys_error_when_it_has_no_reason(monkeypatch) -> None:
    out, err, _cat, _tel = _run_telemetry(monkeypatch, latched=False)
    assert err is None
    assert "result: minted target answered 400 error 'collection label 'c42' is of the minted form" in out.replace('"', "'")


def test_the_recorded_result_keeps_the_latch_state_whatever_the_reasons_say(monkeypatch) -> None:
    out, err, _cat, _tel = _run_telemetry(monkeypatch, latched=True, fail_legal=409)
    assert isinstance(err, SystemExit)
    assert cl._result.result_line(out, lambda t: t).startswith("gate3_latched=True | minted target answered 400")


# ── taxonomy persist_split ───────────────────────────────────────────────────

class FakeTaxonomy:
    def __init__(self, taps: list | None = None) -> None:
        self.topics: dict[int, str] = {}
        self.filed_under: list[str] = []
        self.taps: list = [] if taps is None else taps

    def persist_discovered_topics(self, collection, specs):
        self.taps.append([200, "POST", "/v1/taxonomy/topics/persist_discovered"])
        self.filed_under.append(collection)
        ids = [len(self.topics) + i + 1 for i, _ in enumerate(specs)]
        for i in ids:
            self.topics[i] = collection
        return ids

    def persist_split(self, req):
        if self.topics[req["topic_id"]] != req["collection_name"]:
            self.taps.append([400, "POST", "/v1/taxonomy/topics/persist_split"])
            raise _status_error(400, {"error": "topic is not a topic of the named collection"})
        raise AssertionError("the probe's split must be refused")

    def delete_topic(self, topic_id):
        self.taps.append([200, "POST", "/v1/taxonomy/topics/delete"])
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


def test_a_fresh_engine_and_a_tenant_that_already_holds_the_row_log_taps_the_comparator_passes(monkeypatch) -> None:
    """Local mints the row on a fresh engine; the cloud slot, on a later run, adopts the row the first run minted. The logged
    taps carry the same bad status (the 400 the probe expects)."""
    local_cat = FakeCatalog()
    _run(cl._routes.PERSIST_SPLIT_REFUSAL, monkeypatch, catalog=local_cat, taxonomy=FakeTaxonomy(local_cat.taps))
    cloud_cat = FakeCatalog(latched=True)
    for _ in range(2):
        cloud_cat.taps.clear()
        out, err = _run(cl._routes.PERSIST_SPLIT_REFUSAL, monkeypatch, catalog=cloud_cat, taxonomy=FakeTaxonomy(cloud_cat.taps))
        assert err is None, out
    local = _res("route-taxonomy-persist-split", "local", taps=local_cat.taps)
    cloud = _res("route-taxonomy-persist-split", "cloud", taps=cloud_cat.taps)
    assert [t[0] for t in cloud["taps"] if t[0] >= 400] == [400] and cl.judge_step(local, cloud)[0] == "PASS"
