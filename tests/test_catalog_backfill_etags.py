# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0ne1m critique (significant #2): ``nx catalog backfill-etags``
captures the ETag in bulk for ``https://`` references that predate ETag
capture (or whose index-time HEAD failed), so they don't read 'unknown'
forever under ``nx doctor --check-references``.

Same fake-reader/-writer pattern as ``test_catalog_backfill_source_uri.py``
(monkeypatching ``nexus.commands.catalog._get_catalog``/
``_get_catalog_writer`` directly) -- no real engine needed for the
COMMAND'S OWN candidate-selection/counting logic; the real jsonb-merge
behavior is proven separately by the engine-substrate test in
test_doctor_references.py.
"""
from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from nexus.aspect_readers import HTTPS_ETAG_META_KEY
from nexus.cli import main


class _FakeDoc:
    def __init__(
        self, tumbler: str, source_uri: str, *, meta: dict | None = None, alias_of: str = "",
    ) -> None:
        self.tumbler = tumbler
        self.source_uri = source_uri
        self.meta = meta or {}
        self.alias_of = alias_of


class _FakeReader:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    def all_documents(self, limit: int = 0) -> list[_FakeDoc]:
        return self._docs

    def by_owner(self, owner: Any) -> list[_FakeDoc]:
        prefix = f"{owner}."
        return [d for d in self._docs if str(d.tumbler).startswith(prefix)]


class _FakeWriter:
    def __init__(self, fail_tumblers: set[str] | None = None) -> None:
        self.update_calls: list[tuple[str, dict]] = []
        self.closed = False
        self._fail = fail_tumblers or set()

    def update(self, tumbler: str, **fields: Any) -> None:
        if tumbler in self._fail:
            raise RuntimeError("engine unreachable")
        self.update_calls.append((tumbler, fields))

    def close(self) -> None:
        self.closed = True


def _invoke(monkeypatch, docs, args, *, writer=None, capture=None):
    from nexus.commands import catalog as _cat_cmd

    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _FakeReader(docs))
    if writer is not None:
        monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: writer)
    if capture is not None:
        monkeypatch.setattr("nexus.aspect_readers.capture_https_etag", capture)
    return CliRunner().invoke(main, ["catalog", "backfill-etags", *args])


def _docs_mixed() -> list[_FakeDoc]:
    return [
        _FakeDoc("1.1.1", "https://example.com/a"),  # candidate
        _FakeDoc("1.1.2", "https://example.com/b", meta={HTTPS_ETAG_META_KEY: '"already"'}),  # excluded
        _FakeDoc("1.1.3", "file:///abs/c.md"),  # excluded (scheme)
        _FakeDoc("1.1.4", "https://example.com/d", alias_of="1.1.2"),  # excluded (alias)
        _FakeDoc("1.2.1", "https://example.com/e"),  # candidate, different owner
    ]


def test_dry_run_lists_candidates_and_makes_no_network_call(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("capture_https_etag must not be called under --dry-run")

    result = _invoke(monkeypatch, _docs_mixed(), ["--dry-run"], capture=_boom)

    assert result.exit_code == 0, result.output
    assert "missing an ETag: 2" in result.output
    assert "1.1.1" in result.output
    assert "1.2.1" in result.output
    assert "1.1.2" not in result.output
    assert "Dry-run" in result.output


def test_apply_records_etag_only_for_missing_candidates(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_capture(source_uri: str, *, http_client=None) -> str:
        calls.append(source_uri)
        return '"captured"'

    writer = _FakeWriter()
    result = _invoke(monkeypatch, _docs_mixed(), [], writer=writer, capture=_fake_capture)

    assert result.exit_code == 0, result.output
    assert sorted(calls) == ["https://example.com/a", "https://example.com/e"]
    assert writer.update_calls == [
        ("1.1.1", {"meta": {HTTPS_ETAG_META_KEY: '"captured"'}}),
        ("1.2.1", {"meta": {HTTPS_ETAG_META_KEY: '"captured"'}}),
    ]
    assert writer.closed
    assert "Recorded 2 ETag(s)" in result.output


def test_owner_filter_restricts_candidates(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_capture(source_uri: str, *, http_client=None) -> str:
        calls.append(source_uri)
        return '"captured"'

    writer = _FakeWriter()
    result = _invoke(
        monkeypatch, _docs_mixed(), ["--owner", "1.1"], writer=writer, capture=_fake_capture,
    )

    assert result.exit_code == 0, result.output
    assert calls == ["https://example.com/a"]
    assert writer.update_calls == [("1.1.1", {"meta": {HTTPS_ETAG_META_KEY: '"captured"'}})]


def test_no_etag_in_response_is_counted_not_recorded(monkeypatch) -> None:
    writer = _FakeWriter()
    result = _invoke(monkeypatch, _docs_mixed(), [], writer=writer, capture=lambda *a, **kw: "")

    assert result.exit_code == 0, result.output
    assert writer.update_calls == []
    assert "Recorded 0 ETag(s); 2 had none to capture" in result.output


def test_write_failure_is_reported_and_exits_nonzero(monkeypatch) -> None:
    writer = _FakeWriter(fail_tumblers={"1.1.1"})
    result = _invoke(
        monkeypatch, _docs_mixed(), [], writer=writer, capture=lambda *a, **kw: '"captured"',
    )

    assert result.exit_code == 1, result.output
    assert "1 write failure" in result.output
    # The other candidate still gets recorded -- one failure never aborts the rest.
    assert writer.update_calls == [("1.2.1", {"meta": {HTTPS_ETAG_META_KEY: '"captured"'}})]


def test_limit_bounds_candidates_processed(monkeypatch) -> None:
    docs = [_FakeDoc(f"1.1.{i}", f"https://example.com/{i}") for i in range(5)]
    calls: list[str] = []

    def _fake_capture(source_uri: str, *, http_client=None) -> str:
        calls.append(source_uri)
        return '"captured"'

    writer = _FakeWriter()
    result = _invoke(monkeypatch, docs, ["--limit", "2"], writer=writer, capture=_fake_capture)

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "missing an ETag: 5" in result.output
    assert "processes up to --limit 2: 2" in result.output


def test_idempotent_when_nothing_is_missing(monkeypatch) -> None:
    docs = [_FakeDoc("1.1.1", "https://example.com/a", meta={HTTPS_ETAG_META_KEY: '"already"'})]

    def _boom(*_a, **_kw):
        raise AssertionError("nothing should be captured when every candidate already has an ETag")

    result = _invoke(monkeypatch, docs, [], capture=_boom)

    assert result.exit_code == 0, result.output
    assert "missing an ETag: 0" in result.output
    assert "Nothing to backfill" in result.output
