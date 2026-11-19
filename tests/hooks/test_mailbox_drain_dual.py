# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: a new client's drain hook reads BOTH ``mailbox/<session id>`` and ``mailbox/<identity>``.

New-client senders mail the identity, old-client senders mail the session, so a hook that drains only one loses the
other half of a mixed fleet silently. Both copies of the hook (the plugin script and the wheel port) run their real
``_drain_all`` here, over a token file on disk; only the engine call (``_drain_address``) is a recorder.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"
SID = "11111111-2222-4333-8444-555555555555"


def _plugin() -> Any:
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("_plugin_mailbox_drain_dual", SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return mod


def _wheel() -> Any:
    import nexus.hooks.mailbox_drain as mod  # noqa: PLC0415

    return mod


def _run(mod: Any, flavour: str, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> list[str]:
    drained: list[str] = []
    monkeypatch.setattr(mod, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(mod, "_resolve_endpoint", lambda _cd: ("http://127.0.0.1:1", "bearer", True))
    monkeypatch.setattr(mod, "_prune_stale_cleared_records", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_drain_cleared_record", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_drain_address", lambda _b, _t, address, **_k: drained.append(address))
    payload = {"session_id": SID}
    if flavour == "plugin":
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        mod._drain_all()
    else:
        mod._drain_all(payload, mod._Out())
    return drained


def _bind(config_dir: Path, identity: str = "i12") -> None:
    directory = config_dir / "identity"
    directory.mkdir(parents=True)
    (directory / "1.1.token").write_text(
        json.dumps({"identity": identity, "token": "t", "generation": 1, "pid": 1, "started_at": 1,
                    "session_id": SID, "session_ids": [SID]}), encoding="utf-8")


@pytest.fixture(params=["plugin", "wheel"])
def hook(request: pytest.FixtureRequest) -> tuple[Any, str]:
    return (_plugin() if request.param == "plugin" else _wheel()), request.param


def test_a_bound_session_drains_its_session_mailbox_and_its_identity_mailbox(
    hook: tuple[Any, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, flavour = hook
    _bind(tmp_path)
    assert _run(mod, flavour, monkeypatch, tmp_path) == [SID, "i12"]


def test_a_session_with_no_identity_drains_only_its_session_mailbox(
    hook: tuple[Any, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, flavour = hook
    assert _run(mod, flavour, monkeypatch, tmp_path) == [SID]
