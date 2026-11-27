# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud leg's route probes send the requests they name (nexus-clfse.1).

Pins for the four probe defects the 8.5.6 post-deploy leg r2611250807567b5a found: the rename-refusal probes went through
the registering store wrappers and never sent the route, the by-id probes used a 19-digit id, and the persist-split probe
put its second collection on the home row's live tuple. Kept apart from test_cloud_leg.py, which is at its size cap.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import types
from pathlib import Path

import httpx

LEG_PY = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "lib" / "cloud_leg.py"

if "cloud_leg" in sys.modules:
    cl = sys.modules["cloud_leg"]
else:
    _spec = importlib.util.spec_from_file_location("cloud_leg", LEG_PY)
    cl = importlib.util.module_from_spec(_spec)
    sys.modules["cloud_leg"] = cl
    _spec.loader.exec_module(cl)


def test_rename_refusal_probes_post_the_route_directly() -> None:
    """The store's rename_collection registers the target first and the engine refuses a minted label there (400), so the
    rename route was never sent and the probe passed on the registration's refusal."""
    routes = cl._routes
    chash = routes.CHASH_RENAME_REFUSAL
    tax = routes.TAXONOMY_RENAME_REFUSAL
    assert 'store._post("/v1/chash/rename_collection"' in chash and "store.rename_collection(" not in chash
    assert 'store._post("/rename_collection"' in tax and "store.rename_collection(" not in tax
    for code in (chash, tax):
        assert "exc.response.url.path.endswith(" in code  # the refusal must come from the rename route, not the registration


def test_retiring_by_id_probe_id_fits_the_engines_18_digit_cap() -> None:
    """The engine answers 400 for a collection_id over 18 digits; the probe wants 404 unknown_collection_id."""
    m = re.search(r"probe_id = ([\d_]+)", cl._routes._RETIRING_BY_ID)
    assert m, "the probe names no probe_id"
    digits = m.group(1).replace("_", "")
    assert len(digits) <= 18 and int(digits) > 0, digits


def test_persist_split_probe_registers_other_on_a_distinct_tuple() -> None:
    """A second live collection on the home row's tuple is refused 409, so `other` must differ from the home row."""
    call = _register_other_call()
    assert 'owner_id=row["owner_id"]' not in call
    assert re.search(r'owner_id="1\.\d+"', call), call
    assert 'model_version=row["model_version"]' in call, "a different model_version would capture the store steps' newest-version resolution"


def _register_other_call() -> str:
    code = cl._routes.PERSIST_SPLIT_REFUSAL
    call = code[code.index("cat.register_collection("):]
    return call[: call.index(".label\n")]


def test_persist_split_probe_registers_other_without_a_name_and_uses_the_answered_label() -> None:
    """A named `other` stayed live on the persistent tenant, so every later run's differently named registration hit
    409 LiveTupleConflict. Nameless, the engine adopts the tuple's live row or mints one, and the probe uses its label."""
    code = cl._routes.PERSIST_SPLIT_REFUSAL
    assert "cat.register_collection(None," in code
    assert "@label@-split-other" not in code
    assert _register_other_call().startswith("cat.register_collection(None,")
    assert "other = cat.register_collection(None," in code and ".label" in code
    assert "persist_discovered_topics(other," in code


def test_persist_split_probe_prints_the_refusals_reason() -> None:
    code = cl._routes.PERSIST_SPLIT_REFUSAL
    assert "def _reason_of(exc)" in code
    assert 'print("result: persist_split answered", exc.response.status_code, "reason", _reason_of(exc))' in code
    assert "persist_split refused 400" in code  # the line the step's expect_stdout waits for


def test_telemetry_rename_probe_sends_the_minted_target_before_the_legal_pair() -> None:
    """The label rule runs before the rename guard, so the minted target is 400 in every gate state; the legal pair is
    200 with zero counts before gate 3 latches and 409 rename_not_in_registry once it has."""
    code = cl._routes.TELEMETRY_RENAME
    assert code.index('new="c42"') < code.index('new=source + "-renamed"')
    assert "minted target refused 400" in code  # the line the step's expect_stdout waits for
    assert 'reason == "rename_not_in_registry"' in code and "exc.response.status_code == 409" in code
    assert "gate3_latched" in code and "rdr229_completion" in code
    assert 'print("result: legal rename answered"' in code and "_reason_of(exc)" in code
    assert "moved no rows" not in code and "legal rename then moves zero" not in code


def _run_telemetry_probe(monkeypatch, *, latched, legal_answer):
    """Run TELEMETRY_RENAME against a fake store/status; legal_answer is a counts dict (200) or (status, body) for a refusal."""
    calls: list[tuple[str, str]] = []

    class FakeStore:
        def query_collection_stats(self, *_a, **_k):
            return {"row_count": 0}

        def list_hook_failures(self, **_k):
            return {"total": 0, "rows": []}

        def rename_collection(self, *, old, new):
            calls.append((old, new))
            if new == "c42":
                raise _status_error(400, {"error": "minted", "reason": "label_minted_form"})
            if isinstance(legal_answer, dict):
                return legal_answer
            raise _status_error(*legal_answer)

    def _status_error(code, body):
        req = httpx.Request("POST", "http://engine/v1/telemetry/rename_collection")
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, json=body, request=req))

    mod = types.ModuleType("nexus.db.t2.http_telemetry_store")
    mod.HttpTelemetryStore = FakeStore
    status = types.ModuleType("nexus.db.http_engine_status")
    status.fetch_engine_status = lambda timeout=0: (
        None if latched == "absent" else {"rdr229_completion": {"gate3_latched": latched}})
    monkeypatch.setitem(sys.modules, "nexus.db.t2.http_telemetry_store", mod)
    monkeypatch.setitem(sys.modules, "nexus.db.http_engine_status", status)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            exec(compile(cl._routes.TELEMETRY_RENAME.replace("@label@", "cloudleg-r1"), "probe", "exec"), {"__name__": "probe"})  # noqa: S102 - the probe is the unit under test
        except SystemExit as exc:
            return out.getvalue(), exc, calls
        except AssertionError as exc:
            return out.getvalue(), exc, calls
    return out.getvalue(), None, calls


def test_telemetry_rename_probe_passes_a_pre_latch_200_with_zero_counts(monkeypatch) -> None:
    out, err, calls = _run_telemetry_probe(monkeypatch, latched=False, legal_answer={"search_telemetry": 0, "hook_failures": 0})
    assert err is None, err
    assert calls[0][1] == "c42" and calls[1][1].endswith("-renamed")
    assert "minted target refused 400" in out and "result: legal rename answered 200" in out and "gate3_latched=False" in out


def test_telemetry_rename_probe_passes_a_latched_409_and_prints_its_reason(monkeypatch) -> None:
    out, err, _ = _run_telemetry_probe(monkeypatch, latched=True, legal_answer=(409, {"error": "e", "reason": "rename_not_in_registry"}))
    assert err is None, err
    assert "result: legal rename answered 409 reason rename_not_in_registry gate3_latched=True" in out


def test_telemetry_rename_probe_fails_a_409_with_another_reason_or_a_latched_200(monkeypatch) -> None:
    out, err, _ = _run_telemetry_probe(monkeypatch, latched=True, legal_answer=(409, {"error": "e", "reason": "rename_refused"}))
    assert isinstance(err, AssertionError) and "rename_refused" in str(err)
    out, err, _ = _run_telemetry_probe(monkeypatch, latched=True, legal_answer={"search_telemetry": 0, "hook_failures": 0})
    assert isinstance(err, AssertionError) and "latched" in str(err)
    out, err, _ = _run_telemetry_probe(monkeypatch, latched="absent", legal_answer={"search_telemetry": 3, "hook_failures": 0})
    assert isinstance(err, AssertionError) and "moved rows" in str(err)


def test_the_probes_mark_their_answers_for_the_step_record() -> None:
    """The step record keeps only `result: ` lines (cloud_leg_result), so each probe prints its reason on one."""
    for code in (cl._routes.TELEMETRY_RENAME, cl._routes.PERSIST_SPLIT_REFUSAL):
        assert 'print("result: ' in code and "_reason_of(exc)" in code
