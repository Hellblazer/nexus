# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-zw44w: the installed python-tuf survives a refused symlink.

sigstore verifies engine and PG bundle signatures through python-tuf, whose
Updater re-points root.json at root_history/<n>.root.json on every start. An
unprivileged Windows account may not create symlinks (WinError 1314); tuf
7.0.0 then raised and verification failed closed (the win-release runner,
2026-10-06). tuf 7.0.1 falls back to a hard link (python-tuf #2980, PR #2981),
and pyproject.toml floors tuf there. This drives the installed tuf's own
method with the symlink refused, on every host, so a downgrade fails here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from tuf.ngclient import updater as tuf_updater


class _PrivilegeNotHeld(OSError):
    winerror = 1314


def test_root_json_is_written_when_the_symlink_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "root_history").mkdir()
    (tmp_path / "root_history" / "15.root.json").write_text('{"signed": {"version": 15}}', encoding="utf-8")
    (tmp_path / "root.json").write_text('{"signed": {"version": 14}}', encoding="utf-8")

    def refuse(src: str, dst: str, *a: object, **k: object) -> None:
        raise _PrivilegeNotHeld(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(tuf_updater.os, "symlink", refuse)
    fake = SimpleNamespace(_dir=str(tmp_path), _trusted_set=SimpleNamespace(root=SimpleNamespace(version=15)))

    tuf_updater.Updater._update_root_symlink(fake)  # type: ignore[arg-type]

    root = tmp_path / "root.json"
    assert root.read_text(encoding="utf-8") == '{"signed": {"version": 15}}'
    assert not root.is_symlink()


def test_any_other_symlink_error_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "root_history").mkdir()
    (tmp_path / "root_history" / "3.root.json").write_text("{}", encoding="utf-8")

    def broken(src: str, dst: str, *a: object, **k: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(tuf_updater.os, "symlink", broken)
    fake = SimpleNamespace(_dir=str(tmp_path), _trusted_set=SimpleNamespace(root=SimpleNamespace(version=3)))
    with pytest.raises(OSError):
        tuf_updater.Updater._update_root_symlink(fake)  # type: ignore[arg-type]
