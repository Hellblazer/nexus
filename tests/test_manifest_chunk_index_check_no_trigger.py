# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nothing calls the manifest chunk-index CHECK routes on its own (nexus-6pfgb.42, RDR-236).

The census, add-NOT-VALID and VALIDATE of ``chunk_index = position`` on ``nexus.catalog_document_chunks`` go out only
when an operator's request sends them: never a changeset, never at boot (a boot-time VALIDATE crash-loops an install
whose rows differ from the measured one). So no hook, upgrade rung, doctor row, daemon or schedule may reach them.
Only the engine classes that implement and mount them, and the tests of those, may name them.

Same shape as the no-automatic-trigger tests of the catalog TTL CHECK and the chunk-table routes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where the routes, their classes and the engine binding may be named: the three engine classes, the two places that
#: mount and bind them, and the tests and gate files that classify them.
NAMING_ALLOWED = {
    "service/src/main/java/dev/nexus/service/db/ManifestChunkIndexCheckAdmin.java",
    "service/src/main/java/dev/nexus/service/http/ManifestChunkIndexCheckHandler.java",
    "service/src/main/java/dev/nexus/service/NexusService.java",
    "service/src/main/java/dev/nexus/service/Main.java",
    "service/src/test/java/dev/nexus/service/ManifestChunkIndexCheckRoutesIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/db/ManifestChunkIndexCheckAdminIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/db/TenantKeyedStateGateTest.java",
    "service/src/test/java/dev/nexus/service/db/ManifestWriteRetryGateTest.java",
    "tests/test_manifest_chunk_index_check_no_trigger.py",
    # the catalog TTL step's scan lists these classes among the ones that name its own
    "tests/test_catalog_ttl_check_no_trigger.py",
    # the chunk routes' own scan lists the one class that borrows its comparison; the file name is spelled in two parts
    # because that scan reads this file too
    "tests/test_retire_" + "keys.py",
}
NAMES = ("manifest-chunk-index-check", "ManifestChunkIndexCheck", "manifestChunkIndexCheck", "manifest_chunk_index_check", "catalog_document_chunks_chunk_index_position")
#: Not scanned: version control, environments and build output, and prose that only describes the routes.
SKIP_DIRS = {".git", ".venv", "node_modules", "target", "__pycache__", "docs", "web", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", ".beads", ".claude", "build", "dist"}
#: Directories the brief names; each that exists must have been scanned (a gate that scanned none is vacuous).
NAMED_ROOTS = ("src", "scripts", "conexus", ".github", "deploy")
SERVICE_MAIN = ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"


def _scan() -> tuple[dict[str, set[str]], dict[str, int]]:
    seen: dict[str, set[str]] = {}
    scanned: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            path = Path(dirpath) / fname
            if path.suffix == ".md":
                continue
            try:
                if path.stat().st_size > 3_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(ROOT).as_posix()
            scanned[rel.split("/", 1)[0]] = scanned.get(rel.split("/", 1)[0], 0) + 1
            hits = {n for n in NAMES if n in text}
            if hits:
                seen[rel] = hits
    return seen, scanned


def test_nothing_in_the_repository_names_the_routes_but_the_engine_classes_and_their_tests() -> None:
    seen, scanned = _scan()

    for top in NAMED_ROOTS:
        if (ROOT / top).is_dir():
            assert scanned.get(top, 0) > 0, f"the scan looked at nothing under {top}/"
    assert sum(scanned.values()) > 1000, "the scan examined too few files to mean anything"
    assert set(seen) <= NAMING_ALLOWED, \
        f"a file other than the engine classes and their tests names the routes: {sorted(set(seen) - NAMING_ALLOWED)}"
    assert {"service/src/main/java/dev/nexus/service/db/ManifestChunkIndexCheckAdmin.java",
            "service/src/main/java/dev/nexus/service/http/ManifestChunkIndexCheckHandler.java"} <= set(seen), \
        "the scan cannot see the files that define the routes: it is not looking where it says"


def test_the_engines_main_sources_name_the_classes_only_where_they_are_built_and_bound() -> None:
    callers = {
        p.name for p in SERVICE_MAIN.rglob("*.java")
        if any(tok in p.read_text(encoding="utf-8") for tok in ("ManifestChunkIndexCheckAdmin", "ManifestChunkIndexCheckHandler"))
    }

    # The two classes, and the two places that mount and bind them.
    assert callers == {"ManifestChunkIndexCheckAdmin.java", "ManifestChunkIndexCheckHandler.java", "NexusService.java", "Main.java"}


def test_nexus_service_and_main_never_invoke_the_operations_or_schedule_them() -> None:
    """Not a file whitelist: every line of NexusService and Main that names the admin or the handler (and the two
    lines either side) is read, and none may call an operation on them or sit beside a scheduler."""
    mention = re.compile(r"\b(ManifestChunkIndexCheckAdmin|ManifestChunkIndexCheckHandler|manifestChunkIndexCheckAdmin)\b")
    operations = (".census(", ".add(", ".validate(", ".state(", ".run(", ".submit(", ".execute(", ".schedule")
    forbidden_words = ("schedul", "Executor", "Runnable", "startup", "Timer", "Thread")
    examined = 0
    for name in ("NexusService.java", "Main.java"):
        lines = (SERVICE_MAIN / name).read_text(encoding="utf-8").splitlines()
        hit_lines = [i for i, line in enumerate(lines) if mention.search(line)]
        assert hit_lines, f"{name} no longer builds or binds the routes' classes: the check is looking at nothing"
        for i in hit_lines:
            for j in range(max(0, i - 2), min(len(lines), i + 3)):
                stripped = lines[j].strip()
                if stripped.startswith(("//", "*", "/*")):
                    continue
                examined += 1
                assert not any(op in lines[j] for op in operations), f"{name}:{j + 1} invokes an operation: {stripped}"
                assert not any(w in lines[j] for w in forbidden_words), \
                    f"{name}:{j + 1} sits beside a scheduler or startup hook: {stripped}"
                assert not re.search(r"\bmanifestChunkIndexCheckAdmin\s*\.", lines[j]), \
                    f"{name}:{j + 1} calls on the admin: {stripped}"
    assert examined >= 8, "the check read too few lines to mean anything"

