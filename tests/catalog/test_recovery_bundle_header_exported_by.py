# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P3.9 follow-ups ruled for the P3.3 cut (linda, 2026-11-22, nexus-p2m1x.38 notes).

G2: the format-3 header names what wrote it (``exported_by``: the client version, and the engine's
release version when the reader can tell it), so a bad bundle found later says where it came from.
Readers ignore the field.

The future-format refusal: a bundle newer than this client reads is a typed error naming both
versions, and the CLI turns it into one message and exit 1, never a traceback.
"""

from __future__ import annotations

import json
from importlib.metadata import version as dist_version

import pytest
from click.testing import CliRunner

from nexus.catalog.recovery_bundle import (
    MAX_SUPPORTED_RECOVERY_FORMAT_VERSION,
    ExportSummary,
    exported_by,
    read_bundle,
    write_bundle,
)
from nexus.cli import main
from nexus.errors import RecoveryBundleFormatTooNewError


def _header(path):
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_the_header_names_the_client_and_engine_that_wrote_it(tmp_path):
    bundle = tmp_path / "b.jsonl"
    write_bundle(bundle, [], [], ExportSummary(), exported_by={"client": "9.9.9", "engine": "0.1.999"})
    assert _header(bundle)["exported_by"] == {"client": "9.9.9", "engine": "0.1.999"}


class _EngineReader:
    def __init__(self, release: str | Exception) -> None:
        self._release = release

    def engine_release_version(self) -> str:
        if isinstance(self._release, Exception):
            raise self._release
        return self._release


@pytest.mark.parametrize(
    ("reader", "engine"),
    [
        (_EngineReader("0.1.200"), "0.1.200"),
        (_EngineReader(""), ""),  # a tree-built engine reports no release
        (_EngineReader(ConnectionError("down")), ""),  # unreachable: the export does not fail on it
        (object(), ""),  # a local catalog has no engine to ask
    ],
)
def test_exported_by_takes_the_installed_client_and_whatever_engine_version_the_reader_gives(reader, engine):
    assert exported_by(reader) == {"client": dist_version("conexus"), "engine": engine}


def test_a_reader_ignores_exported_by_and_any_other_unknown_header_key(tmp_path):
    bundle = tmp_path / "b.jsonl"
    bundle.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": MAX_SUPPORTED_RECOVERY_FORMAT_VERSION,
                                  "exported_by": {"client": "1", "engine": ""}, "a_key_from_the_future": [1, 2]}) + "\n",
                      encoding="utf-8")
    header, records = read_bundle(bundle)
    assert header["format_version"] == MAX_SUPPORTED_RECOVERY_FORMAT_VERSION and records == []


def test_write_bundle_without_exported_by_still_records_the_client(tmp_path):
    bundle = tmp_path / "b.jsonl"
    write_bundle(bundle, [], [], ExportSummary())
    assert _header(bundle)["exported_by"] == {"client": dist_version("conexus"), "engine": ""}


def test_the_cli_turns_a_malformed_header_into_one_message(tmp_path, monkeypatch):
    bundle = tmp_path / "bad.jsonl"
    bundle.write_text("{not json\n", encoding="utf-8")
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: pytest.fail("connected before the header check"))
    result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    assert result.exit_code == 1, result.output
    assert "is not a readable recovery bundle" in result.output and "Traceback" not in result.output


def _future_bundle(tmp_path):
    bundle = tmp_path / "future.jsonl"
    bundle.write_text(
        json.dumps({"format": "nexus-recovery-bundle", "format_version": MAX_SUPPORTED_RECOVERY_FORMAT_VERSION + 1})
        + "\n",
        encoding="utf-8",
    )
    return bundle


def test_a_future_format_is_a_typed_error_naming_both_versions(tmp_path):
    bundle = _future_bundle(tmp_path)
    with pytest.raises(RecoveryBundleFormatTooNewError) as info:
        read_bundle(bundle)
    err = info.value
    assert err.bundle_version == MAX_SUPPORTED_RECOVERY_FORMAT_VERSION + 1
    assert err.max_supported == MAX_SUPPORTED_RECOVERY_FORMAT_VERSION
    text = str(err)
    assert str(MAX_SUPPORTED_RECOVERY_FORMAT_VERSION + 1) in text and str(MAX_SUPPORTED_RECOVERY_FORMAT_VERSION) in text
    assert str(bundle) in text


def test_the_cli_refuses_a_future_format_with_one_message_and_no_traceback(tmp_path, monkeypatch):
    bundle = _future_bundle(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: touched.append("reader") or object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: touched.append("writer") or object())
    monkeypatch.setattr("nexus.db.make_t3", lambda: touched.append("t3") or object())
    result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.output
    assert f"format_version {MAX_SUPPORTED_RECOVERY_FORMAT_VERSION + 1}" in result.output
    assert touched == [], "the header is refused before the command connects to anything"
