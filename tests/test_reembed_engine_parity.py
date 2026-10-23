# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 6 Step 3 (nexus-cnxjk): the client's copies of the engine's re-embed constants, read out of
the Java source so neither side can move alone.

* ``reembed_job.LOCK_STATES`` is ``ReembedJobRepository.LOCK_STATES`` (the states that hold a source's lock);
* the body keys ``HttpVectorClient`` sends to the plan, start and resume routes are exactly the field sets
  ``ReembedHandler`` accepts (it refuses an unknown field with a 400, so an extra key breaks the verb and a
  missing one silently drops a flag).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from nexus import reembed_job
from nexus.db.http_vector_client import HttpVectorClient

_SERVICE = Path(__file__).resolve().parents[1] / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
_HANDLER = (_SERVICE / "http" / "ReembedHandler.java").read_text(encoding="utf-8")
_REPOSITORY = (_SERVICE / "db" / "ReembedJobRepository.java").read_text(encoding="utf-8")


def _java_string_set(source: str, name: str) -> set[str]:
    m = re.search(rf"{name}\s*=\s*Set\.of\(([^;]*?)\);", source, re.S)
    assert m, f"{name} not found: the Java declaration moved, update this reader"
    values = re.findall(r'"([^"]*)"', m.group(1))
    return set(values)


def test_lock_states_match_the_engines():
    constants = dict(re.findall(r'public static final String (\w+) = "([^"]+)";', _REPOSITORY))
    m = re.search(r"LOCK_STATES\s*=\s*Set\.of\(([^;]*?)\);", _REPOSITORY, re.S)
    assert m, "ReembedJobRepository.LOCK_STATES not found"
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    engine = {constants[n] for n in names}
    assert len(engine) == 5, "non-vacuity: the five lock states were read"
    assert set(reembed_job.LOCK_STATES) == engine


def test_the_java_field_sets_are_read_non_vacuously():
    assert _java_string_set(_HANDLER, "START_FIELDS") >= {"source_collection", "token_cap"}
    assert _java_string_set(_HANDLER, "PLAN_FIELDS") == {"source_collection", "target_collection"}
    assert _java_string_set(_HANDLER, "RESUME_FIELDS") == {"token_cap"}
    assert re.search(r"NO_FIELDS\s*=\s*Set\.of\(\)", _HANDLER)


@pytest.fixture
def bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    sent: dict[str, dict[str, Any]] = {}

    def fake_post(path: str, body: dict, *, tenant: str = "default", timeout: float = 120) -> Any:
        sent[path.rsplit("/", 1)[-1]] = body
        return {}

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    return sent


def test_the_clients_request_bodies_use_exactly_the_engines_fields(bodies):
    client = HttpVectorClient()
    client.reembed_plan("s", target_collection="t")
    client.reembed_start("s", target_collection="t", token_cap=5, keep_source=True, force_delete=True)
    client.reembed_resume("11111111-2222-3333-4444-555555555555", token_cap=9)
    client.reembed_pause("11111111-2222-3333-4444-555555555555")
    client.reembed_abort("11111111-2222-3333-4444-555555555555")

    assert set(bodies["plan"]) == _java_string_set(_HANDLER, "PLAN_FIELDS")
    assert set(bodies["jobs"]) == _java_string_set(_HANDLER, "START_FIELDS")
    assert set(bodies["resume"]) == _java_string_set(_HANDLER, "RESUME_FIELDS")
    assert bodies["pause"] == {} and bodies["abort"] == {}, "NO_FIELDS: these routes take no body field"


def test_the_default_token_cap_is_the_same_on_both_sides():
    m = re.search(r"public static final long DEFAULT_TOKEN_CAP\s*=\s*([0-9_]+)L;", _HANDLER)
    assert m, "ReembedHandler.DEFAULT_TOKEN_CAP not found: the Java declaration moved, update this reader"
    engine = int(m.group(1).replace("_", ""))
    assert engine >= 1_000_000, "non-vacuity: a real cap was read, not a placeholder"
    assert reembed_job.DEFAULT_TOKEN_CAP == engine
