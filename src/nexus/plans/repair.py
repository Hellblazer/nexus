# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 §A8 / nexus-rv7x6: plan-library content-repair helpers.

Six legacy migrations carried per-row content backfills in
``src/nexus/db/migrations.py``. Per RDR-120 the substrate runs DDL
only; content backfill is consumer-driven via these helpers, dispatched
from ``nx plan repair`` subcommands.

Each ``repair_*`` function:

  * Takes an open sqlite3.Connection to ``memory.db``.
  * Is idempotent (re-runs do not corrupt or double-apply).
  * Returns a per-call diagnostic dict the CLI surfaces.
  * Tolerates missing tables / columns (returns immediately with a
    "skipped" reason so the CLI can report cleanly).

The migration functions in ``nexus.db.migrations`` retain the
schema portion (ALTER TABLE / FTS rebuild) for the cases where DDL
is the substrate's job; bodies that only mutate rows are no-ops now.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def _plans_table_exists(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone())


def _plan_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}


# ── 1. scope_tags (RDR-091) ────────────────────────────────────────────────


def repair_scope_tags(conn: sqlite3.Connection) -> dict[str, Any]:
    """Backfill empty ``plans.scope_tags`` rows, then rewash any row
    whose value still contains the literal ``'all'`` sentinel.

    Combines the substantive bodies of the former
    ``_add_plan_scope_tags`` (4.8.0) and
    ``_rewash_plan_scope_tags_all_sentinel`` (4.8.1) migrations.
    Idempotent on three axes: empty rows get inferred values once;
    pre-populated values are not stomped; ``'all'`` sentinel rows
    are re-inferred on every call until they are clean.
    """
    if not _plans_table_exists(conn):
        return {"skipped": "no plans table"}
    if "scope_tags" not in _plan_columns(conn):
        return {"skipped": "scope_tags column missing"}

    from nexus.plans.scope import (  # noqa: PLC0415
        _SCOPE_AGNOSTIC_SENTINELS,
        _infer_scope_tags,
        _normalize_scope_string,
    )

    backfilled = 0
    rewashed = 0

    rows = conn.execute(
        "SELECT id, plan_json, project FROM plans WHERE scope_tags = ''"
    ).fetchall()
    for row_id, plan_json, project in rows:
        inferred = _infer_scope_tags(plan_json or "")
        if not inferred and project:
            # #1069 project-column fallback: corpus:all plans infer '' from
            # their plan_json; recover from the populated project column using
            # the same normalization / sentinel-drop as save_plan.
            candidate = _normalize_scope_string((project or "").strip())
            if candidate and candidate not in _SCOPE_AGNOSTIC_SENTINELS:
                inferred = candidate
        if inferred:
            conn.execute(
                "UPDATE plans SET scope_tags = ? WHERE id = ? AND scope_tags = ''",
                (inferred, row_id),
            )
            backfilled += 1

    sentinel_rows = conn.execute(
        """
        SELECT id, plan_json FROM plans
        WHERE scope_tags = 'all'
           OR scope_tags LIKE 'all,%'
           OR scope_tags LIKE '%,all'
           OR scope_tags LIKE '%,all,%'
        """
    ).fetchall()
    for row_id, plan_json in sentinel_rows:
        inferred = _infer_scope_tags(plan_json or "")
        conn.execute(
            "UPDATE plans SET scope_tags = ? WHERE id = ?",
            (inferred, row_id),
        )
        rewashed += 1

    if backfilled or rewashed:
        conn.commit()
    return {"backfilled": backfilled, "rewashed": rewashed}


# ── 2. dimensions (RDR-092 Phase 0d.1) ─────────────────────────────────────


_VERB_STEMS: dict[str, str] = {
    "find": "research", "search": "research", "list": "research",
    "get": "research", "show": "research", "enumerate": "research",
    "fetch": "research", "retrieve": "research",
    "analyze": "analyze", "analyse": "analyze",
    "compare": "analyze", "contrast": "analyze",
    "rank": "analyze", "synthesize": "analyze",
    "summarize": "analyze", "summarise": "analyze",
    "review": "review", "audit": "review",
    "evaluate": "review", "critique": "review",
    "assess": "review",
    "debug": "debug", "trace": "debug",
    "investigate": "debug", "fix": "debug",
    "troubleshoot": "debug",
    "document": "document", "describe": "document",
    "explain": "document",
}

_WH_FALLBACK: dict[str, str] = {
    "how": "research", "what": "research",
    "why": "review",
    "when": "research", "where": "research", "who": "research",
    "which": "research",
}

_NAME_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "at", "by", "with", "from", "about",
    "and", "or", "but", "so", "as",
    "how", "what", "why", "when", "where", "who", "which",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "this", "that", "these", "those",
    "i", "we", "you", "they", "it", "he", "she",
})


def _infer_plan_verb_from_query(query: str) -> tuple[str, bool]:
    import re
    tokens = re.findall(r"[a-z][a-z-]+", (query or "").lower())
    for token in tokens:
        if token in _VERB_STEMS:
            return _VERB_STEMS[token], True
    for token in tokens:
        if token in _WH_FALLBACK:
            return _WH_FALLBACK[token], False
    return "research", False


def _derive_plan_name_from_query(query: str, *, max_words: int = 5) -> str:
    import re
    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_]*", (query or "").lower())
    content = [t for t in tokens if t not in _NAME_STOP_WORDS]
    take = content[:max_words] if content else tokens[:max_words]
    return "-".join(take) or "backfilled-plan"


def repair_dimensions(conn: sqlite3.Connection) -> dict[str, Any]:
    """Backfill verb / name / dimensions on NULL-dimension plan rows.

    Body of the former ``_backfill_plan_dimensions`` (4.9.12) migration.
    Touches only rows where ``dimensions IS NULL``; authored rows
    (shipped YAML seeds, already-dimensional grown plans, previously-
    backfilled rows) are left alone. Collision-resolved via a
    deterministic row-id suffix when two NULL-dimension rows in the
    same project would collapse to the same canonical dimensions JSON.
    """
    if not _plans_table_exists(conn):
        return {"skipped": "no plans table"}
    if "dimensions" not in _plan_columns(conn):
        return {"skipped": "dimensions column missing"}

    rows = conn.execute(
        "SELECT id, query, tags FROM plans WHERE dimensions IS NULL"
    ).fetchall()
    if not rows:
        return {"backfilled": 0, "low_conf": 0, "collisions": 0, "total_rows": 0}

    from nexus.plans.schema import canonical_dimensions_json

    backfilled = 0
    low_conf = 0
    collisions = 0
    claimed: set[tuple[str, str]] = set()
    for row_id, query, tags in rows:
        verb, confident = _infer_plan_verb_from_query(query or "")
        base_name = _derive_plan_name_from_query(query or "")
        scope = "personal" if (tags or "").find("grown") >= 0 else "global"
        project = ""

        def _dims(name_value: str) -> str:
            return canonical_dimensions_json({
                "scope": scope,
                "strategy": name_value,
                "verb": verb,
            })

        name = base_name
        dims_json = _dims(name)
        key = (project, dims_json)
        db_hit = conn.execute(
            "SELECT 1 FROM plans "
            "WHERE project = ? AND dimensions = ? AND id != ? LIMIT 1",
            (project, dims_json, row_id),
        ).fetchone()
        if db_hit or key in claimed:
            name = f"{base_name}-{row_id}"
            dims_json = _dims(name)
            key = (project, dims_json)
            collisions += 1
        claimed.add(key)

        tag_flag = "backfill" if confident else "backfill-low-conf"
        existing_tags = [t for t in (tags or "").split(",") if t]
        if tag_flag not in existing_tags:
            existing_tags.append(tag_flag)
        new_tags = ",".join(existing_tags)

        conn.execute(
            "UPDATE plans SET verb = ?, scope = ?, name = ?, "
            "dimensions = ?, tags = ? WHERE id = ?",
            (verb, scope, name, dims_json, new_tags, row_id),
        )
        if confident:
            backfilled += 1
        else:
            low_conf += 1

    conn.commit()
    return {
        "backfilled": backfilled, "low_conf": low_conf,
        "collisions": collisions, "total_rows": len(rows),
    }


# ── 3. match_text (RDR-092 Phase 3.1) ──────────────────────────────────────


def repair_match_text(conn: sqlite3.Connection) -> dict[str, Any]:
    """Populate ``plans.match_text`` and refresh ``plans_fts`` for all
    rows whose ``match_text`` is empty.

    The DDL parts of the former ``_add_plan_match_text_column`` (4.9.13)
    migration (column add, FTS drop+recreate, trigger setup, FTS
    rebuild) remain in the substrate because they are schema-level.
    Only the per-row UPDATE that synthesises ``match_text`` from
    ``query`` / ``verb`` / ``name`` / ``scope`` moves to this verb;
    the AFTER UPDATE trigger then refreshes ``plans_fts`` per row.

    Idempotent: only touches rows where ``match_text`` is empty.
    """
    if not _plans_table_exists(conn):
        return {"skipped": "no plans table"}
    if "match_text" not in _plan_columns(conn):
        return {"skipped": "match_text column missing"}

    from nexus.db.t2.plan_library import _synthesize_match_text

    rows = conn.execute(
        "SELECT id, query, verb, name, scope FROM plans WHERE match_text = ''"
    ).fetchall()
    backfilled = 0
    for row_id, query, verb, name, scope in rows:
        synthesised = _synthesize_match_text(
            description=query, verb=verb, name=name, scope=scope,
        )
        conn.execute(
            "UPDATE plans SET match_text = ? WHERE id = ?",
            (synthesised, row_id),
        )
        backfilled += 1
    if backfilled:
        conn.commit()
    return {"backfilled": backfilled}


# ── 4. retire legacy operation-shape plans (RDR-092 Phase 0a) ──────────────


def repair_retire_legacy(conn: sqlite3.Connection) -> dict[str, Any]:
    """Delete plan rows whose ``plan_json`` uses the pre-RDR-078
    ``operation`` shape.

    Body of the former ``_retire_legacy_operation_shape_plans`` (4.10.1)
    migration. Legacy shape: a step dict has ``operation`` key AND no
    step has ``tool`` key. Idempotent: after the first --apply, no
    legacy-shape rows remain.
    """
    candidates = conn.execute(
        "SELECT id, plan_json FROM plans WHERE plan_json LIKE '%\"operation\"%'"
    ).fetchall()
    if not candidates:
        return {"deleted": 0, "ids": []}

    legacy_ids: list[int] = []
    for row_id, plan_json_text in candidates:
        try:
            parsed = json.loads(plan_json_text or "{}")
        except json.JSONDecodeError:
            continue
        steps = parsed.get("steps") if isinstance(parsed, dict) else None
        if not isinstance(steps, list) or not steps:
            continue
        has_operation = any(
            isinstance(s, dict) and "operation" in s for s in steps
        )
        has_tool = any(
            isinstance(s, dict) and "tool" in s for s in steps
        )
        if has_operation and not has_tool:
            legacy_ids.append(int(row_id))

    if not legacy_ids:
        return {"deleted": 0, "ids": []}

    placeholders = ",".join("?" * len(legacy_ids))
    conn.execute(
        f"DELETE FROM plans WHERE id IN ({placeholders})",
        legacy_ids,
    )
    conn.commit()
    return {"deleted": len(legacy_ids), "ids": legacy_ids}


# ── 5. builtin bindings (RDR-091 / nexus-80tk follow-up) ───────────────────


def repair_builtin_bindings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Patch ``required_bindings`` / ``optional_bindings`` into existing
    builtin plan rows whose stored ``plan_json`` predates the seed
    loader's binding-merge fix (4.10.1).

    Body of the former ``_backfill_builtin_bindings`` (4.10.2)
    migration. For each builtin row whose ``plan_json`` lacks a
    ``required_bindings`` key, resolves the shipping YAML by
    ``(verb, scope, strategy)`` and patches the lists in. Silent
    no-op when the shipping YAMLs are unreachable (exotic install
    layouts); ``nx catalog setup`` is the escalation path.
    """
    if not _plans_table_exists(conn):
        return {"skipped": "no plans table"}

    try:
        import yaml as _yaml
        from importlib.resources import as_file, files
    except ModuleNotFoundError:
        return {"skipped": "PyYAML missing"}

    rows = conn.execute(
        "SELECT id, plan_json, dimensions FROM plans "
        "WHERE tags LIKE '%builtin%' "
        "AND plan_json NOT LIKE '%required_bindings%'"
    ).fetchall()
    if not rows:
        return {"backfilled": 0}

    yaml_dir = None
    try:
        resource = files("nexus") / "_resources" / "plans" / "builtin"
        with as_file(resource) as resolved:
            from pathlib import Path as _Path
            if _Path(resolved).is_dir():
                yaml_dir = _Path(resolved)
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    if yaml_dir is None:
        from pathlib import Path as _Path
        repo_candidate = _Path(__file__).resolve().parents[3] / "conexus" / "plans" / "builtin"
        if repo_candidate.is_dir():
            yaml_dir = repo_candidate
    if yaml_dir is None:
        return {"skipped": "shipping YAMLs unreachable"}

    bindings_index: dict[tuple[str, str, str], tuple[list, list]] = {}
    for entry in yaml_dir.iterdir():
        if not entry.is_file() or entry.suffix not in (".yml", ".yaml"):
            continue
        try:
            template = _yaml.safe_load(entry.read_text()) or {}
        except Exception:
            continue
        dims = template.get("dimensions") or {}
        key = (
            str(dims.get("verb", "")),
            str(dims.get("scope", "")),
            str(dims.get("strategy", "")),
        )
        required = list(template.get("required_bindings") or [])
        optional = list(template.get("optional_bindings") or [])
        if required or optional:
            bindings_index[key] = (required, optional)

    if not bindings_index:
        return {"backfilled": 0}

    backfilled = 0
    for row_id, plan_json_text, dims_text in rows:
        try:
            parsed = json.loads(plan_json_text or "{}")
            dims = json.loads(dims_text or "{}")
        except json.JSONDecodeError:
            continue
        key = (
            str(dims.get("verb", "")),
            str(dims.get("scope", "")),
            str(dims.get("strategy", "")),
        )
        bindings = bindings_index.get(key)
        if not bindings:
            continue
        required, optional = bindings
        if required:
            parsed["required_bindings"] = required
        if optional:
            parsed["optional_bindings"] = optional
        conn.execute(
            "UPDATE plans SET plan_json = ? WHERE id = ?",
            (json.dumps(parsed), row_id),
        )
        backfilled += 1

    if backfilled:
        conn.commit()
    return {"backfilled": backfilled}


# ── Bundle ─────────────────────────────────────────────────────────────────


def repair_all(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Run every repair pass in dependency order. Order matters:

      1. scope_tags        (independent)
      2. dimensions        (writes verb/name/scope used by match_text)
      3. match_text        (reads verb/name/scope from step 2)
      4. retire-legacy     (deletes pre-RDR-078 rows)
      5. builtin-bindings  (patches surviving builtin rows)
    """
    return {
        "scope_tags": repair_scope_tags(conn),
        "dimensions": repair_dimensions(conn),
        "match_text": repair_match_text(conn),
        "retire_legacy": repair_retire_legacy(conn),
        "builtin_bindings": repair_builtin_bindings(conn),
    }
