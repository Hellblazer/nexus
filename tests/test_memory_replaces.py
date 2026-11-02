# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Client and MCP half of ``memory_put(replaces=...)`` (RDR-238 P1 S2, nexus-qo7ie.7).

The engine half (S1, nexus-qo7ie.6) deletes the named row and writes the new one in one
transaction and echoes ``replaced_title`` / ``replaced_content``. This file pins what the
client adds:

* the lineage line ``replaces: <old title>`` as the content's last line, appended once;
* K1: ``_verify_t2_write_landed`` compares the re-read row with the content actually SENT
  (lineage line included), which the store hands back on a result object, so a replacing
  put does not report a guaranteed "mismatch";
* K5: a replacing put posts with ``idempotent=False``; a lost response raises an error that
  says the outcome is unknown and how to check, instead of a retry that is refused for a
  replace that already landed;
* a response with no echo (an engine without ``replaces``) raises: the new row WAS written,
  the old row is still alive;
* the facade purges taxonomy assignments for the replaced title, as ``nx memory delete`` does.

Cases 1 to 3 run against the self-provisioned local engine (``t2_tenant``). Cases 4 and 5
and the unit pins need no engine: they stub ``_post`` or the T2 context.
"""
from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

import nexus.mcp.core as core
from nexus.db.t2 import T2Database
from nexus.db.t2.http_memory_store import (
    HttpMemoryStore,
    ReplacePutResult,
    ReplacesEchoMissingError,
    ReplacesOutcomeUnknownError,
    with_lineage_line,
)


@pytest.fixture(autouse=True)
def _no_tier_write_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_record_tier_write`` is best effort and reaches the store; not under test here."""
    monkeypatch.setattr(core, "_record_tier_write", lambda **kw: None)


def _stub_store() -> HttpMemoryStore:
    """A real client whose wire is replaced per test; nothing connects."""
    return HttpMemoryStore(base_url="http://127.0.0.1:9", _token="stub-token")


# ── the lineage line ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("body", "body\nreplaces: A"),
        ("body\n", "body\nreplaces: A"),
        ("a\nb", "a\nb\nreplaces: A"),
        ("", "replaces: A"),
        # already ends with the line: sent as given, trailing newline included
        ("body\nreplaces: A", "body\nreplaces: A"),
        ("body\nreplaces: A\n", "body\nreplaces: A\n"),
        ("replaces: A", "replaces: A"),
        # a different target, or the line only as the tail of a longer line, still gets its own line
        ("body\nreplaces: B", "body\nreplaces: B\nreplaces: A"),
        ("see also replaces: A", "see also replaces: A\nreplaces: A"),
        # the line in the middle is not "ends with"
        ("replaces: A\nmore", "replaces: A\nmore\nreplaces: A"),
    ],
)
def test_lineage_line_is_appended_once_and_deterministically(content: str, expected: str) -> None:
    once = with_lineage_line(content, "A")
    assert once == expected
    assert with_lineage_line(once, "A") == once, "applying it twice must not add a second line"


# ── K5 and the echo, no engine (cases 4 and 5) ──────────────────────────────────

def test_case4_no_echo_raises_and_says_the_new_row_was_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """An engine without ``replaces`` ignores the field, writes the new row and answers {"id"}."""
    store = _stub_store()
    monkeypatch.setattr(store, "_post", lambda path, payload, **kw: {"id": 11})
    with pytest.raises(ReplacesEchoMissingError) as err:
        store.put_replacing("p", "B", "content", replaces="A")
    text = str(err.value)
    assert "lacks replaces" in text
    assert "WAS written" in text
    assert "still alive" in text
    assert "p/B" in text and "p/A" in text, text


@pytest.mark.parametrize("partial", [{"replaced_title": "A"}, {"replaced_content": "x"}])
def test_a_half_echo_is_no_echo(monkeypatch: pytest.MonkeyPatch, partial: dict) -> None:
    store = _stub_store()
    monkeypatch.setattr(store, "_post", lambda path, payload, **kw: {"id": 11, **partial})
    with pytest.raises(ReplacesEchoMissingError):
        store.put_replacing("p", "B", "content", replaces="A")


def test_case5_a_replacing_put_is_posted_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict, dict]] = []

    def _post(path, payload, **kw):
        calls.append((path, payload, kw))
        return {"id": 12, "replaced_title": "A", "replaced_content": "old A"}

    store = _stub_store()
    monkeypatch.setattr(store, "_post", _post)
    result = store.put_replacing("p", "B", "content", replaces="A")

    [(path, payload, kw)] = calls
    assert path == "/v1/memory/put"
    assert kw.get("idempotent") is False, "a retried replace is refused for a replace that landed"
    assert payload["replaces"] == "A"
    assert payload["content"] == "content\nreplaces: A"
    assert result == ReplacePutResult(
        id=12, sent_content="content\nreplaces: A",
        replaced_title="A", replaced_content="old A",
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_case5_a_lost_response_says_the_outcome_is_unknown(
    monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    def _post(path, payload, **kw):
        raise failure

    store = _stub_store()
    monkeypatch.setattr(store, "_post", _post)
    with pytest.raises(ReplacesOutcomeUnknownError) as err:
        store.put_replacing("p", "B", "content", replaces="A")
    text = str(err.value)
    assert "unknown" in text
    assert "memory_get" in text
    assert "p/B" in text and "p/A" in text, text
    assert err.value.__cause__ is failure


def test_case5_a_gateway_status_is_also_an_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://x/v1/memory/put")
    failure = httpx.HTTPStatusError(
        "HTTP 504", request=request, response=httpx.Response(504, request=request),
    )

    def _post(path, payload, **kw):
        raise failure

    store = _stub_store()
    monkeypatch.setattr(store, "_post", _post)
    with pytest.raises(ReplacesOutcomeUnknownError):
        store.put_replacing("p", "B", "content", replaces="A")


def test_an_engine_refusal_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 is a definitive refusal (nothing written), not an unknown outcome."""
    request = httpx.Request("POST", "http://x/v1/memory/put")
    refusal = httpx.HTTPStatusError(
        "HttpMemoryStore./v1/memory/put failed: HTTP 409: replaces target p/A not found",
        request=request, response=httpx.Response(409, request=request),
    )

    def _post(path, payload, **kw):
        raise refusal

    store = _stub_store()
    monkeypatch.setattr(store, "_post", _post)
    with pytest.raises(httpx.HTTPStatusError) as err:
        store.put_replacing("p", "B", "content", replaces="A")
    assert err.value is refusal


def test_a_plain_put_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[dict, dict]] = []

    def _post(path, payload, **kw):
        calls.append((payload, kw))
        return {"id": 5}

    store = _stub_store()
    monkeypatch.setattr(store, "_post", _post)
    assert store.put("p", "B", "content") == 5
    [(payload, kw)] = calls
    assert "replaces" not in payload, "an old engine must see the old wire"
    assert payload["content"] == "content"
    assert kw.get("idempotent", True) is True


def test_a_blank_replaces_is_refused_before_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _stub_store()
    monkeypatch.setattr(store, "_post", lambda *a, **k: pytest.fail("no request for a blank target"))
    with pytest.raises(ValueError):
        store.put_replacing("p", "B", "content", replaces="")


# ── the MCP tool, no engine ──────────────────────────────────────────────────────

class _FakeDb:
    """T2 whose write and read-back are controlled; records which put was used."""

    def __init__(self, *, stored: dict | None, result: ReplacePutResult | Exception | None = None) -> None:
        self.stored = stored
        self.result = result
        self.plain_puts: list[dict] = []
        self.replacing_puts: list[dict] = []

    def put(self, **kw):
        self.plain_puts.append(kw)
        return 42

    def put_replacing(self, **kw):
        self.replacing_puts.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def resolve_title(self, *, project, title):
        return self.stored, []


def _use(monkeypatch: pytest.MonkeyPatch, db: _FakeDb) -> None:
    @contextmanager
    def _ctx():
        yield db

    monkeypatch.setattr(core, "_t2_ctx", _ctx)


def _landed(content: str) -> dict:
    return {"id": 42, "project": "p", "title": "B", "content": content, "tags": "", "timestamp": ""}


def test_mcp_verifies_against_the_content_sent_and_returns_the_deleted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = "new body\nreplaces: A"
    db = _FakeDb(
        stored=_landed(sent),
        result=ReplacePutResult(id=42, sent_content=sent, replaced_title="A", replaced_content="old A body"),
    )
    _use(monkeypatch, db)
    out = core.memory_put(content="new body", project="p", title="B", replaces="A")
    # verified (a raw-argument comparison would report a mismatch), and says what was removed
    assert out.startswith("Stored: [42] p/B"), out
    assert "A" in out and "old A body" in out, out
    assert db.plain_puts == []
    [call] = db.replacing_puts
    assert call["replaces"] == "A" and call["content"] == "new body"


@pytest.mark.parametrize(
    ("stored", "verdict"),
    [(_landed("something else"), "mismatch"), (None, "missing")],
)
def test_mcp_reports_a_failed_read_back_and_still_hands_over_the_deleted_content(
    monkeypatch: pytest.MonkeyPatch, stored: dict | None, verdict: str,
) -> None:
    """The delete is committed once put_replacing returns; the echo is the only record of it."""
    db = _FakeDb(
        stored=stored,
        result=ReplacePutResult(
            id=42, sent_content="new\nreplaces: A", replaced_title="A",
            replaced_content="the deleted body zebrafinch",
        ),
    )
    _use(monkeypatch, db)
    out = core.memory_put(content="new", project="p", title="B", replaces="A")
    assert out.startswith("Error:"), out
    assert f"({verdict})" in out
    assert "Replaced (deleted): p/A" in out and "the deleted body zebrafinch" in out, out


def test_mcp_a_raising_tier_write_record_still_hands_over_the_deleted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception AFTER the replace landed, outside the verifier, loses the echo too without the note."""
    db = _FakeDb(
        stored=_landed("new\nreplaces: A"),
        result=ReplacePutResult(
            id=42, sent_content="new\nreplaces: A", replaced_title="A",
            replaced_content="the deleted body zebrafinch",
        ),
    )
    _use(monkeypatch, db)

    def _boom(**kw):
        raise OSError("telemetry store down")

    monkeypatch.setattr(core, "_record_tier_write", _boom)
    out = core.memory_put(content="new", project="p", title="B", replaces="A")
    assert out.startswith("Error:"), out
    assert "telemetry store down" in out
    assert "the deleted body zebrafinch" in out, out


def test_mcp_a_refusal_has_no_deleted_content_to_hand_over(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _FakeDb(stored=None, result=RuntimeError("HTTP 409: replaces target p/A not found")))
    out = core.memory_put(content="new", project="p", title="B", replaces="A")
    assert "Deleted content" not in out, out


def test_mcp_says_the_taxonomy_purge_failed_when_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = "new\nreplaces: A"
    for purge_failed, expect in ((True, True), (False, False)):
        db = _FakeDb(
            stored=_landed(sent),
            result=ReplacePutResult(
                id=42, sent_content=sent, replaced_title="A", replaced_content="old A body",
                purge_failed=purge_failed,
            ),
        )
        _use(monkeypatch, db)
        out = core.memory_put(content="new", project="p", title="B", replaces="A")
        assert out.startswith("Stored: [42] p/B"), out
        assert "old A body" in out
        assert ("NOT purged" in out) is expect, out


@pytest.mark.parametrize(
    ("failure", "needles"),
    [
        (ReplacesEchoMissingError("p", "B", "A"), ["lacks replaces", "WAS written", "still alive"]),
        (ReplacesOutcomeUnknownError("p", "B", "A"),
         ["unknown", "memory_get", "the replace landed", "cannot be recovered from the engine"]),
        (RuntimeError("HttpMemoryStore./v1/memory/put failed: HTTP 409: replaces target p/A is quarantined"),
         ["409", "p/A", "quarantined"]),
    ],
)
def test_mcp_failures_come_back_as_error_strings(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, needles: list[str],
) -> None:
    _use(monkeypatch, _FakeDb(stored=None, result=failure))
    out = core.memory_put(content="new", project="p", title="B", replaces="A")
    assert out.startswith("Error:"), out
    for needle in needles:
        assert needle in out, (needle, out)


def test_mcp_without_replaces_takes_the_plain_path(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(stored=_landed("plain"))
    _use(monkeypatch, db)
    out = core.memory_put(content="plain", project="p", title="B")
    assert out.startswith("Stored: [42] p/B"), out
    assert db.replacing_puts == []
    [call] = db.plain_puts
    assert "replaces" not in call


# ── cases 1 to 3 against the local engine ───────────────────────────────────────

def _find_rows(db: T2Database, project: str, title: str) -> list[dict]:
    return [r for r in db.memory.get_all(project) if r["title"] == title]


@pytest.mark.t2_tenant
def test_case1_replace_removes_the_old_row_and_the_new_row_verifies(db: T2Database) -> None:
    old = "zebrafinch ledger of the quarterly migration"
    db.put(project="rp", title="A", content=old)
    out = core.memory_put(content="fresh account of the migration", project="rp", title="B", replaces="A")

    assert out.startswith("Stored: ["), out
    assert old in out, "the deleted content must reach the writer so it can be re-put"

    assert db.get(project="rp", title="A") is None
    assert _find_rows(db, "rp", "A") == []
    assert [r["title"] for r in db.search("zebrafinch", project="rp")] == []

    landed = db.get(project="rp", title="B")
    assert landed is not None
    assert landed["content"] == "fresh account of the migration\nreplaces: A"
    assert core._verify_t2_write_landed(
        project="rp", title="B", content=landed["content"], row_id=landed["id"],
    ) == ("verified", "")


def _spy_on_purge(monkeypatch: pytest.MonkeyPatch, db: T2Database) -> list[tuple[str, str]]:
    """Record taxonomy purges; the real purge still runs against the engine."""
    purged: list[tuple[str, str]] = []
    real = db.taxonomy.purge_assignments_for_doc

    def _spy(project, title):
        purged.append((project, title))
        return real(project=project, title=title)

    monkeypatch.setattr(db.taxonomy, "purge_assignments_for_doc", _spy)
    return purged


@pytest.mark.t2_tenant
def test_case1_store_result_carries_the_echo_and_the_sent_content(
    db: T2Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Also: the facade purges assignments for the replaced title, and only that one."""
    purged = _spy_on_purge(monkeypatch, db)
    db.put(project="rp", title="A", content="old A")
    result = db.put_replacing(project="rp", title="B", content="new B", replaces="A")
    assert purged == [("rp", "A")]
    assert result.purge_failed is False
    assert result.replaced_title == "A"
    assert result.replaced_content == "old A"
    assert result.sent_content == "new B\nreplaces: A"
    assert db.get(project="rp", title="B")["id"] == result.id


@pytest.mark.t2_tenant
def test_case1_a_chain_leaves_only_the_last_row(db: T2Database) -> None:
    db.put(project="rp", title="A", content="a")
    db.put_replacing(project="rp", title="B", content="b", replaces="A")
    db.put_replacing(project="rp", title="C", content="c", replaces="B")
    assert [r["title"] for r in db.memory.get_all("rp")] == ["C"]
    assert db.get(project="rp", title="C")["content"] == "c\nreplaces: B"


@pytest.mark.t2_tenant
def test_case2_content_that_already_ends_with_the_line_gets_no_second_one(db: T2Database) -> None:
    db.put(project="rp", title="A", content="old A")
    out = core.memory_put(content="new B\nreplaces: A", project="rp", title="B", replaces="A")
    assert out.startswith("Stored: ["), out
    assert db.get(project="rp", title="B")["content"] == "new B\nreplaces: A"


@pytest.mark.t2_tenant
def test_case3_a_target_that_exists_only_in_another_project_is_refused(
    db: T2Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged = _spy_on_purge(monkeypatch, db)
    db.put(project="other", title="A", content="other A")
    out = core.memory_put(content="new B", project="rp", title="B", replaces="A")
    assert purged == [], "a refused put purges nothing"

    assert out.startswith("Error:"), out
    assert "409" in out and "A" in out, out
    assert db.get(project="rp", title="B") is None, "a refused replace writes nothing"
    assert db.get(project="other", title="A")["content"] == "other A"


@pytest.mark.t2_tenant
def test_replacing_a_row_with_itself_is_refused_and_leaves_it(db: T2Database) -> None:
    db.put(project="rp", title="A", content="keep me")
    out = core.memory_put(content="new", project="rp", title="A", replaces="A")
    assert out.startswith("Error:"), out
    assert db.get(project="rp", title="A")["content"] == "keep me"


@pytest.mark.t2_tenant
def test_a_failed_purge_does_not_hide_a_replace_that_landed(
    db: T2Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(project, title):
        raise ConnectionError("taxonomy store down")

    monkeypatch.setattr(db.taxonomy, "purge_assignments_for_doc", _boom)
    db.put(project="rp", title="A", content="old A")
    result = db.put_replacing(project="rp", title="B", content="new B", replaces="A")
    assert result.replaced_content == "old A"
    assert result.purge_failed is True, "the writer must be told the assignments were not purged"
    assert db.get(project="rp", title="A") is None
