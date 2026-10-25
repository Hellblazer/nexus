# SPDX-License-Identifier: AGPL-3.0-or-later
"""``read_stdin_utf8``: every CLI verb that reads stdin as text reads it the same way.

Python decodes a piped stdin with the locale encoding, cp1252 on Windows (7.86.1
shakeout), and reading ``sys.stdin.buffer`` skips text-mode newline translation, so the
helper decodes UTF-8 itself and folds CRLF / CR to LF. These tests drive each sibling
(``memory put -``, ``scratch put -``, ``config set --stdin``) through a cp1252 CliRunner.
They hit the command seam with doubles, so they need no engine.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
import yaml
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands._helpers import read_stdin_utf8

_SECRET = "vk-secret-value-123456"

_CASES = [
    pytest.param("﻿café", "café", id="bom-and-accent"),
    pytest.param("one\r\ntwo\r\n", "one\ntwo\n", id="crlf"),
    pytest.param("﻿über\r\n中文", "über\n中文", id="bom-nonascii-crlf"),
]


def _runner() -> CliRunner:
    return CliRunner(charset="cp1252")


class TestHelper:
    @pytest.mark.parametrize(("raw", "expected"), _CASES)
    def test_decodes_utf8_and_normalises_newlines(self, monkeypatch, raw, expected) -> None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="cp1252"))
        assert read_stdin_utf8() == expected

    def test_lone_cr_becomes_lf(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"a\rb"), encoding="cp1252"))
        assert read_stdin_utf8() == "a\nb"

    def test_text_only_stdin_falls_back_and_drops_a_bom(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("﻿plain"))
        assert read_stdin_utf8() == "plain"

    def test_non_utf8_names_the_remedy_and_not_the_input(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"s3cr3t\xe9"), encoding="cp1252"))
        with pytest.raises(click.ClickException) as ei:
            read_stdin_utf8()
        msg = ei.value.format_message()
        assert "not valid UTF-8" in msg and "pass the file path instead" in msg
        assert "OutputEncoding" in msg and "s3cr3t" not in msg


class TestMemoryPut:
    @pytest.mark.parametrize(("raw", "expected"), _CASES)
    def test_stdin_content_is_utf8_lf(self, monkeypatch, raw, expected) -> None:
        db = MagicMock()
        db.memory.put.return_value = 7

        @contextlib.contextmanager
        def fake_handle():
            yield db

        monkeypatch.setattr("nexus.commands.memory.t2_handle", fake_handle)
        result = _runner().invoke(main, ["memory", "put", "-", "-p", "proj", "-t", "title"], input=raw.encode("utf-8"))
        assert result.exit_code == 0, result.output
        assert db.memory.put.call_args.kwargs["content"] == expected

    def test_non_utf8_stdin_is_a_clean_error(self) -> None:
        result = _runner().invoke(main, ["memory", "put", "-", "-p", "proj", "-t", "title"], input=b"caf\xe9")
        assert result.exit_code != 0 and "not valid UTF-8" in result.output


class TestScratchPut:
    @pytest.mark.parametrize(("raw", "expected"), _CASES)
    def test_stdin_content_is_utf8_lf(self, monkeypatch, raw, expected) -> None:
        t1 = MagicMock()
        t1.put.return_value = "abc123"
        monkeypatch.setattr("nexus.commands.scratch._t1", lambda: t1)
        result = _runner().invoke(main, ["scratch", "put", "-"], input=raw.encode("utf-8"))
        assert result.exit_code == 0, result.output
        assert t1.put.call_args.kwargs["content"] == expected

    def test_non_utf8_stdin_is_a_clean_error(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.commands.scratch._t1", lambda: MagicMock())
        result = _runner().invoke(main, ["scratch", "put", "-"], input=b"caf\xe9")
        assert result.exit_code != 0 and "not valid UTF-8" in result.output


class TestConfigSetStdin:
    # The default (UTF-8) runner: ``config set`` echoes "->" as U+2192, which a cp1252
    # CliRunner cannot encode on stdout (a separate, pre-existing output problem). The
    # cp1252 stdin decode itself is pinned by TestHelper.
    @pytest.fixture
    def fake_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("HOME", str(tmp_path))
        for key in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "MXBAI_API_KEY", "NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        return tmp_path

    @staticmethod
    def _stored(home: Path) -> str:
        return yaml.safe_load((home / ".config" / "nexus" / "config.yml").read_text())["credentials"]["voyage_api_key"]

    @pytest.mark.parametrize("raw", ["﻿" + _SECRET + "\r\n", _SECRET + "\r\n", _SECRET + "\n"],
                             ids=["bom-crlf", "crlf", "lf"])
    def test_secret_is_stored_without_bom_or_line_ending(self, fake_home, raw) -> None:
        result = CliRunner().invoke(main, ["config", "set", "voyage_api_key", "--stdin"], input=raw.encode("utf-8"))
        assert result.exit_code == 0, result.output
        assert self._stored(fake_home) == _SECRET
        assert _SECRET not in result.output

    def test_non_utf8_secret_input_fails_clean_and_never_echoes(self, fake_home) -> None:
        result = CliRunner().invoke(main, ["config", "set", "voyage_api_key", "--stdin"], input=b"vk-secret\xe9\n")
        assert result.exit_code != 0 and "not valid UTF-8" in result.output
        assert "vk-secret" not in result.output
        assert not (fake_home / ".config" / "nexus" / "config.yml").exists()
