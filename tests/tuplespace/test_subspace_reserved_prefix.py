# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reserved-prefix gate tests for subspace_add -- RDR-112 nexus-me9y.

The third-party admission path (``RegistryStore.add`` / ``subspace_add``
admin RPC) must reject any subspace name whose prefix matches a builtin
or daemon-internal namespace. Builtins enter via ``seed_from_builtin_dir``
instead.

This file exercises every reserved prefix listed in
``nexus.daemon.subspace_registry._RESERVED_PREFIXES`` plus a handful of
non-reserved names that must continue to succeed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.daemon.subspace_registry import (
    RegistryStore,
    ReservedPrefixError,
    _RESERVED_PREFIXES,
)


_YAML_TEMPLATE = """\
name: {name}
tier: project
content_type: text
embed_from: content
dimensions:
  actor: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.50
  margin: 0.05
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""


# The 10 RDR-defined reserved prefixes (RDR-110 + RDR-111 + RDR-112
# nexus-0xaq) plus the two daemon-internal prefixes (tuples/, daemon/).
_RESERVED_SAMPLE_NAMES = [
    "tasks/myproject",
    "mailbox/alice/primary",
    "locks/db-write",
    "events/lifecycle",
    "barriers/release-1.0",
    "hook_events/tool_call_completed",
    "hook_events/foo",
    "layout_state/cockpit",
    "connection_manifest/peer-a",
    "bindings/default",
    "bindings/x",
    "derived/default",
    "tuples/raw",
    "daemon/lifecycle",
]


def _make_store(tmp_path: Path) -> RegistryStore:
    return RegistryStore(tuples_db_path=tmp_path / "tuples.db")


def _yaml_for(name: str) -> str:
    # The minimal template uses Python str.format placeholders; the only
    # actual brace expansion we want is for the {name} field. The
    # dimensions block uses curly braces literally, so we build the YAML
    # without going through .format() to avoid KeyError on '{type: ...}'.
    return _YAML_TEMPLATE.replace("{name}", name)


@pytest.mark.parametrize("name", _RESERVED_SAMPLE_NAMES)
def test_subspace_add_rejects_reserved_prefix(tmp_path: Path, name: str) -> None:
    """Every reserved-prefix sample name must raise ReservedPrefixError
    when submitted through the third-party ``add`` path."""
    store = _make_store(tmp_path)
    with pytest.raises(ReservedPrefixError) as exc_info:
        store.add(_yaml_for(name))

    msg = str(exc_info.value)
    assert name in msg, (
        f"Error message must name the offending subspace {name!r}: {msg!r}"
    )
    # The message must also identify which reserved prefix matched.
    matched_prefix = next(p for p in _RESERVED_PREFIXES if name.startswith(p))
    assert matched_prefix in msg, (
        f"Error message must name the reserved prefix {matched_prefix!r}: {msg!r}"
    )


def test_reserved_prefixes_includes_all_ten_builtin_namespaces() -> None:
    """nexus-me9y acceptance criterion 2: the reserved-prefix tuple must
    list every canonical builtin namespace from RDR-110 / RDR-111 /
    RDR-112 nexus-0xaq, plus the two daemon-internal prefixes."""
    required = {
        "tasks/",
        "mailbox/",
        "locks/",
        "events/",
        "barriers/",
        "hook_events/",
        "layout_state/",
        "connection_manifest/",
        "bindings/",
        "derived/",
    }
    missing = required - set(_RESERVED_PREFIXES)
    assert not missing, f"Reserved prefixes missing: {missing}"

    # Daemon-internals must also remain present.
    assert "tuples/" in _RESERVED_PREFIXES
    assert "daemon/" in _RESERVED_PREFIXES


@pytest.mark.parametrize(
    "name",
    [
        "myapp/whatever",
        "third_party/queue",
        "telemetry/heartbeat",
        "custom/foo/bar",
    ],
)
def test_subspace_add_accepts_non_reserved_names(tmp_path: Path, name: str) -> None:
    """Names that do not match a reserved prefix register normally."""
    store = _make_store(tmp_path)
    result = store.add(_yaml_for(name))
    assert result["name"] == name
    assert len(result["digest"]) == 64  # sha256 hex
