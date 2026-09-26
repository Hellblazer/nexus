# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wauo1.37 review: the hook-surface-shakeout probe asserts its own
verdict, and its --mcp-config override refuses a token it cannot splice
into JSON safely. Fake tokens only."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parents[1] / "e2e" / "hook-surface-shakeout"
SCRIPT = HERE / "shakeout_in_container.sh"


def _census():
    spec = importlib.util.spec_from_file_location("hook_census", HERE / "hook_census.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stdin(tmp_path: pathlib.Path, names: list[str]) -> pathlib.Path:
    p = tmp_path / "mcp-stdin.jsonl"
    p.write_text("\n".join(json.dumps({"method": "tools/call", "params": {"name": n}}) for n in names) + "\n")
    return p


def test_probe_passes_only_when_all_six_hooks_reached_the_server(tmp_path) -> None:
    h = _census()
    assert h.probe_main(_stdin(tmp_path, list(h.PROBE_HOOKS))) == 0
    assert h.probe_main(_stdin(tmp_path, list(h.PROBE_HOOKS[:-1]))) == 1
    assert h.probe_main(tmp_path / "absent.jsonl") == 1


def test_probe_mode_exits_with_the_census_verdict() -> None:
    text = SCRIPT.read_text()
    block = text[text.index('if [ -n "${SHAKEOUT_HOOK_PROBE:-}" ]; then'):]
    block = block[:block.index("\nfi\n")]
    assert 'hook_census.py" --probe "$RUN/mcp-stdin.jsonl"' in block
    assert 'exit "$PROBE_RC"' in block and "exit 0" not in block


def _guard() -> str:
    line = next(l for l in SCRIPT.read_text().splitlines() if l.strip().startswith('TOKEN_GUARD="[['))
    out = subprocess.run(["bash", "-c", line.strip() + '\nprintf %s "$TOKEN_GUARD"'],
                         capture_output=True, text=True, check=True).stdout
    assert "${CLAUDE_CODE_OAUTH_TOKEN}" in out, "the guard must reference the variable, never a value"
    return out


def test_override_guard_admits_base64url_and_refuses_anything_else() -> None:
    guard = _guard()
    for token, expected in (("sk-ant-oat01-FAKE_abc-123", 0), ('sk-ant-oat01-FA"KE', 1), ("sk-ant-oat01-FA\\KE", 1)):
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
        rc = subprocess.run(["bash", "-c", guard + "true"], env=env, capture_output=True, text=True).returncode
        assert rc == expected, (token, rc)
