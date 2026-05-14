"""Tests for nexus.tuplespace.registry — YAML loader + JSON Schema validation.

RDR-110 P1.1 (nexus-rugy). The registry loads ``nx/tuplespace/builtin/*.yml``,
validates each against an inline JSON Schema for the registry format,
and exposes ``get_schema_for(subspace)`` with single-segment
parameterised matching (``tasks/<project>`` matches ``tasks/nexus``).

This bead has NO SQLite, NO daemon — pure YAML + JSON Schema (RDR-110
§Implementation Plan Step 1, §Storage-boundary in the bead).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


_VALID_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:    { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:  { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  assignee:  { type: string, required: false }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.55
  margin: 0.08
  default_lease_seconds: 600
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 7776000
"""


_VALID_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder:   { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 30
read:
  default_floor: 0.0
  default_n: 1
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    """Build a synthetic ``nx/tuplespace/builtin/`` directory."""
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_VALID_TASKS_YAML)
    (d / "locks.yml").write_text(_VALID_LOCKS_YAML)
    return d


# -- Happy-path loading ------------------------------------------------------


def test_registry_loads_valid_yaml_files(builtin_dir):
    from nexus.tuplespace.registry import Registry

    reg = Registry.load(builtin_dir)

    names = {s.name for s in reg.schemas()}
    assert names == {"tasks/<project>", "locks/<resource>"}


def test_subspace_schema_fields_are_populated(builtin_dir):
    from nexus.tuplespace.registry import Registry

    reg = Registry.load(builtin_dir)
    schema = reg.get_schema_for("tasks/nexus")

    assert schema.name == "tasks/<project>"
    assert schema.tier == "project"
    assert schema.content_type == "text"
    assert schema.embed_from == "content"
    assert schema.tiers == ["project"]
    assert schema.retention_seconds == 7776000
    assert schema.dimensions["status"]["type"] == "enum"
    assert schema.dimensions["status"]["required"] is True
    assert schema.take["mode"] == "semantic"
    assert schema.take["floor"] == 0.55


# -- Parameterised matching --------------------------------------------------


def test_parameterised_template_matches_concrete_subspace(builtin_dir):
    from nexus.tuplespace.registry import Registry

    reg = Registry.load(builtin_dir)

    assert reg.get_schema_for("tasks/nexus").name == "tasks/<project>"
    assert reg.get_schema_for("tasks/another-project").name == "tasks/<project>"
    assert reg.get_schema_for("locks/db-write").name == "locks/<resource>"


def test_parameterised_param_matches_single_segment_only(builtin_dir):
    """``tasks/<project>`` MUST NOT match ``tasks/a/b`` — params are
    single-segment by RDR-110 §Step 1.
    """
    from nexus.tuplespace.registry import Registry, UnknownSubspaceError

    reg = Registry.load(builtin_dir)

    with pytest.raises(UnknownSubspaceError):
        reg.get_schema_for("tasks/a/b")


def test_unknown_subspace_raises(builtin_dir):
    from nexus.tuplespace.registry import Registry, UnknownSubspaceError

    reg = Registry.load(builtin_dir)

    with pytest.raises(UnknownSubspaceError, match="mailbox/anyone"):
        reg.get_schema_for("mailbox/anyone")


def test_multi_param_template_matches(tmp_path):
    """Templates with two single-segment params resolve correctly."""
    from nexus.tuplespace.registry import Registry

    yml = yaml.safe_dump(
        {
            "name": "mailbox/<agent>/<inbox>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {"sender": {"type": "string", "required": True}},
            "take": {"enabled": True, "mode": "semantic"},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "mailbox.yml").write_text(yml)

    reg = Registry.load(d)
    schema = reg.get_schema_for("mailbox/alice/primary")
    assert schema.name == "mailbox/<agent>/<inbox>"


def test_dashed_param_name_raises_at_load(tmp_path):
    """Param identifiers must match Python named-group syntax (no dashes).

    ``mailbox/<agent-name>`` would compile to an invalid named group at
    runtime. The loader rejects this at load time so a YAML author sees
    the failure immediately rather than discovering it on first ``take``
    via an opaque ``UnknownSubspaceError``.
    """
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    yml = yaml.safe_dump(
        {
            "name": "mailbox/<agent-name>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {},
            "take": {"enabled": True, "mode": "semantic"},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "bad.yml").write_text(yml)

    with pytest.raises(RegistryLoadError, match="agent-name"):
        Registry.load(d)


def test_empty_angle_brackets_raise_at_load(tmp_path):
    """``mailbox/<>`` is the dashed-param sibling: empty identifier.

    Round-2 critic catch: the original ``_ANGLE_TOKEN`` regex used
    ``[^>]+`` (one-or-more), so empty brackets produced zero captures
    and slipped through the bad-param filter. The widened ``[^>]*``
    regex captures the empty string, which ``_PARAM_PATTERN`` then
    rejects.
    """
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    yml = yaml.safe_dump(
        {
            "name": "mailbox/<>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {},
            "take": {"enabled": True, "mode": "semantic"},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "bad.yml").write_text(yml)

    with pytest.raises(RegistryLoadError, match="invalid param"):
        Registry.load(d)


def test_empty_segment_rejected_in_multi_param_match(tmp_path):
    """``mailbox/<agent>/<inbox>`` must not match ``mailbox//primary``.

    The ``[^/]+`` quantifier rejects empty segments; pinning this here
    guards against a future refactor of ``_compile_template`` that
    switched the quantifier to ``*``.
    """
    from nexus.tuplespace.registry import Registry, UnknownSubspaceError

    yml = yaml.safe_dump(
        {
            "name": "mailbox/<agent>/<inbox>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {"sender": {"type": "string", "required": True}},
            "take": {"enabled": True, "mode": "semantic"},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "mailbox.yml").write_text(yml)

    reg = Registry.load(d)
    with pytest.raises(UnknownSubspaceError):
        reg.get_schema_for("mailbox//primary")
    with pytest.raises(UnknownSubspaceError):
        reg.get_schema_for("mailbox/alice/")


def test_empty_subspace_raises(builtin_dir):
    from nexus.tuplespace.registry import Registry, UnknownSubspaceError

    reg = Registry.load(builtin_dir)

    with pytest.raises(UnknownSubspaceError):
        reg.get_schema_for("")


# -- Validation errors -------------------------------------------------------


def test_malformed_yaml_raises_at_load(tmp_path):
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "bad.yml").write_text("name: tasks/<x>\n: : bad\n")

    with pytest.raises(RegistryLoadError, match="bad.yml"):
        Registry.load(d)


def test_schema_violation_yaml_raises_at_load(tmp_path):
    """Missing required top-level field (e.g. ``tier``) fails validation."""
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "incomplete.yml").write_text("name: tasks/<project>\n")

    with pytest.raises(RegistryLoadError, match="incomplete.yml"):
        Registry.load(d)


def test_invalid_take_mode_raises(tmp_path):
    """``take.mode`` must be 'semantic' or 'exact' per the JSON schema."""
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    bad_yaml = yaml.safe_dump(
        {
            "name": "things/<x>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {},
            "take": {"enabled": True, "mode": "lol-fuzzy"},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "bad-mode.yml").write_text(bad_yaml)

    with pytest.raises(RegistryLoadError, match="bad-mode.yml"):
        Registry.load(d)


def test_exact_mode_without_match_keys_raises(tmp_path):
    """``take.mode=exact`` requires non-empty ``match_keys`` (RDR-110 §C2)."""
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    bad_yaml = yaml.safe_dump(
        {
            "name": "things/<x>",
            "tier": "project",
            "content_type": "text",
            "embed_from": "content",
            "dimensions": {"x": {"type": "string", "required": True}},
            "take": {"enabled": True, "mode": "exact", "match_keys": []},
            "read": {"default_floor": 0.4, "default_n": 5},
            "tiers": ["project"],
            "retention_seconds": 3600,
        }
    )
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "exact-no-keys.yml").write_text(bad_yaml)

    with pytest.raises(RegistryLoadError, match="exact-no-keys.yml"):
        Registry.load(d)


def test_duplicate_subspace_names_raise(tmp_path):
    """Two YAML files declaring the same subspace name must fail loudly."""
    from nexus.tuplespace.registry import Registry, RegistryLoadError

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "a.yml").write_text(_VALID_TASKS_YAML)
    (d / "b.yml").write_text(_VALID_TASKS_YAML)

    with pytest.raises(RegistryLoadError, match="duplicate"):
        Registry.load(d)


# -- Production builtin dir --------------------------------------------------


def test_real_builtin_dir_loads_cleanly():
    """The ``nx/tuplespace/builtin/`` shipped with the repo must load."""
    from nexus.tuplespace.registry import Registry, default_builtin_dir

    reg = Registry.load(default_builtin_dir())
    assert len(list(reg.schemas())) >= 1
