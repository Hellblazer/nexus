# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nothing calls the manifest CHECK routes on its own (nexus-6pfgb.42, RDR-236).

The census, add-NOT-VALID and VALIDATE of the two manifest CHECKs on ``nexus.catalog_document_chunks``
(``chunk_index = position``, and ``labels_written OR the three section labels are NULL``) go out only when an operator's
request sends them: never a changeset, never at boot (a boot-time VALIDATE crash-loops an install whose rows differ from
the measured one). So no hook, upgrade rung, doctor row, daemon or schedule may reach them. Only the engine classes that
implement and mount them, and the tests of those, may name them.

Same shape as the no-automatic-trigger tests of the catalog TTL CHECK and the chunk-table routes. One scan covers both
route families: ``/v1/admin/manifest-chunk-index-check`` and ``/v1/admin/manifest-labels-check``.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where the routes, their classes and the engine binding may be named: the engine classes, the two places that mount
#: and bind them, and the tests and gate files that classify them.
NAMING_ALLOWED = {
    "service/src/main/java/dev/nexus/service/db/ManifestCheck.java",
    "service/src/main/java/dev/nexus/service/db/ManifestCheckAdmin.java",
    "service/src/main/java/dev/nexus/service/http/ManifestCheckHandler.java",
    "service/src/main/java/dev/nexus/service/ManifestCheckWiring.java",
    "service/src/main/java/dev/nexus/service/NexusService.java",
    "service/src/main/java/dev/nexus/service/Main.java",
    # read the enum's constants only: the key sweep counts with the labels predicate, the 409 body names its remedy
    # (test_the_sweep_and_the_409_body_use_only_the_labels_constants_never_the_admin_or_the_routes)
    "service/src/main/java/dev/nexus/service/vectors/ChunkKeySweep.java",
    "service/src/main/java/dev/nexus/service/http/HttpUtil.java",
    "service/src/test/java/dev/nexus/service/ManifestCheckRoutesIntegrationBase.java",
    "service/src/test/java/dev/nexus/service/ManifestChunkIndexCheckRoutesIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/ManifestLabelsCheckRoutesIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/db/ManifestCheckAdminIntegrationBase.java",
    "service/src/test/java/dev/nexus/service/db/ManifestChunkIndexCheckAdminIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/db/ManifestLabelsCheckAdminIntegrationTest.java",
    "service/src/test/java/dev/nexus/service/db/ManifestCheckLiteralsTest.java",
    "service/src/test/java/dev/nexus/service/ManifestLabelsCheckWritersTest.java",
    "service/src/test/java/dev/nexus/service/db/TenantKeyedStateTables.java",
    "service/src/test/java/dev/nexus/service/db/ManifestWriteRetryGateTest.java",
    "tests/test_manifest_check_no_trigger.py",
    # the per-leaf FK census (nexus-p2m1x.21) lists every engine file that holds an ADD or VALIDATE CONSTRAINT with its exact counts, these classes
    # among them (the boot-time admin validators, kept at their 30 minute bound); it is a test of the engine classes and runs nothing
    "service/src/test/java/dev/nexus/service/datamove/Rdr229ValidationBoundsCensusIntegrationTest.java",
    # the catalog TTL step's scan lists these classes among the ones that name its own
    "tests/test_catalog_ttl_check_no_trigger.py",
    # the chunk routes' own scan lists the one class that borrows its comparison; the file name is spelled in two parts
    # because that scan reads this file too
    "tests/test_retire_" + "keys_no_trigger.py",
}
NAMES = (
    "manifest-chunk-index-check", "manifest-labels-check", "ManifestCheck", "manifestCheck", "ManifestChunkIndexCheck",
    "ManifestLabelsCheck", "manifest_chunk_index_check", "manifest_labels_check",
    "catalog_document_chunks_chunk_index_position", "catalog_document_chunks_labels_written",
)
#: Not scanned: version control, environments and build output, and prose that only describes the routes.
#: RDR-258 red-run report data (nexus-2jomg.1): a plain-text CI-log extract committed there would quote route names without calling them.
SKIP_DIRS = {".git", ".venv", "node_modules", "target", "__pycache__", "docs", "web", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", ".beads", ".claude", "build", "dist", "red-run-data"}
#: Directories the brief names; each that exists must have been scanned (a gate that scanned none is vacuous).
NAMED_ROOTS = ("src", "scripts", "conexus", ".github", "deploy")
SERVICE_MAIN = ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"


def _files(root: Path) -> list[Path]:
    """The files to scan: only what git tracks, so a run's own output (a --status file such as ``aff.json`` that a lab
    run writes into the repo root, ignored or not) is not read. An untracked, unstaged new file is deliberately not
    checked here: it is caught once staged, by the push gate's tip worktree, or by CI. Without git (no work tree, or
    git failing) the fallback walk with SKIP_DIRS still reads untracked files. The same listing as the chunk routes'
    and the catalog TTL step's no-trigger scans."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            capture_output=True, check=True, timeout=60,
        ).stdout.decode("utf-8", "surrogateescape")
    except (OSError, subprocess.SubprocessError):
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            out.extend(Path(dirpath) / f for f in filenames)
        return out
    return [
        root / rel for rel in listed.split("\0")
        if rel and not set(Path(rel).parts[:-1]) & SKIP_DIRS and (root / rel).is_file()
    ]


def _scan(root: Path = ROOT) -> tuple[dict[str, set[str]], dict[str, int]]:
    seen: dict[str, set[str]] = {}
    scanned: dict[str, int] = {}
    for path in _files(root):
        if path.suffix == ".md":
            continue
        try:
            if path.stat().st_size > 3_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        scanned[rel.split("/", 1)[0]] = scanned.get(rel.split("/", 1)[0], 0) + 1
        hits = {n for n in NAMES if n in text}
        if hits:
            seen[rel] = hits
    return seen, scanned


def test_the_scan_lists_tracked_files_only_never_untracked_or_ignored_ones(tmp_path: Path) -> None:
    """A staged file is scanned. An untracked file that names the route is not, whether or not an ignore rule covers
    it: a lab run's --status file (``aff.json``) or selector list is a run's output, not the repository."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / ".gitignore").write_text("output/\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tracked.py").write_text("ROUTE = 'manifest-labels-" + "check'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "src/tracked.py"], check=True, timeout=60)
    (tmp_path / "aff.json").write_text('{"route": "manifest-labels-' + 'check"}\n', encoding="utf-8")  # untracked, not ignored
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "affected.txt").write_text("manifest-labels-" + "check\n", encoding="utf-8")  # ignored
    rels = {p.relative_to(tmp_path).as_posix() for p in _files(tmp_path)}
    assert rels == {".gitignore", "src/tracked.py"}
    seen, _ = _scan(tmp_path)
    assert seen == {"src/tracked.py": {"manifest-labels-check"}}


def test_nothing_in_the_repository_names_the_routes_but_the_engine_classes_and_their_tests() -> None:
    seen, scanned = _scan()

    for top in NAMED_ROOTS:
        if (ROOT / top).is_dir():
            assert scanned.get(top, 0) > 0, f"the scan looked at nothing under {top}/"
    assert sum(scanned.values()) > 1000, "the scan examined too few files to mean anything"
    assert set(seen) <= NAMING_ALLOWED, \
        f"a file other than the engine classes and their tests names the routes: {sorted(set(seen) - NAMING_ALLOWED)}"
    assert {"service/src/main/java/dev/nexus/service/db/ManifestCheck.java",
            "service/src/main/java/dev/nexus/service/db/ManifestCheckAdmin.java",
            "service/src/main/java/dev/nexus/service/http/ManifestCheckHandler.java"} <= set(seen), \
        "the scan cannot see the files that define the routes: it is not looking where it says"
    # both route families are named where they are defined: a rename of one would leave the other unscanned
    defined = (ROOT / "service/src/main/java/dev/nexus/service/db/ManifestCheck.java").read_text(encoding="utf-8")
    assert "manifest-chunk-index-check" in defined and "manifest-labels-check" in defined


CLASS_TOKENS = ("ManifestCheck", "manifestChecks")


def test_the_engines_main_sources_name_the_classes_only_where_they_are_built_and_bound() -> None:
    callers = {
        p.name for p in SERVICE_MAIN.rglob("*.java")
        if any(tok in p.read_text(encoding="utf-8") for tok in CLASS_TOKENS)
    }

    # The classes (the enum, the admin, the handler, the wiring), and the two places that mount and bind them.
    assert {"ManifestCheckAdmin.java", "ManifestCheckHandler.java", "ManifestCheckWiring.java", "NexusService.java",
            "Main.java"} <= callers
    assert callers <= {"ManifestCheck.java", "ManifestCheckAdmin.java", "ManifestCheckHandler.java",
                       "ManifestCheckWiring.java", "NexusService.java", "Main.java",
                       "ChunkKeySweep.java", "HttpUtil.java"}, \
        f"a main source other than the classes and their two places names the steps: {sorted(callers)}"


#: The two main sources outside the classes that read the labels constants: the key sweep's census and the 409 remedy.
CONSTANT_READERS = ("vectors/ChunkKeySweep.java", "http/HttpUtil.java")
ALLOWED_USE = re.compile(r"ManifestCheck\.LABELS\.(violation|constraint)\(\)|dev\.nexus\.service\.db\.ManifestCheck;?$")


def test_the_sweep_and_the_409_body_use_only_the_labels_constants_never_the_admin_or_the_routes() -> None:
    """They may name the enum only to read the labels predicate or the constraint name: no admin, handler, wiring,
    route path, census, add or validate. Every code line that names the enum is matched, so a new use fails here."""
    mention = re.compile(r"ManifestCheck\w*")
    named = 0
    for rel in CONSTANT_READERS:
        for i, line in enumerate((SERVICE_MAIN / rel).read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")) or not mention.search(stripped):
                continue
            named += 1
            assert ALLOWED_USE.search(stripped), f"{rel}:{i} uses the manifest check beyond its constants: {stripped}"
            assert "manifest-labels-check" not in stripped and "manifest-chunk-index-check" not in stripped
    assert named >= 3, "the readers no longer name the labels constants: the check is looking at nothing"


def test_nexus_service_and_main_never_invoke_the_operations_or_schedule_them() -> None:
    """Not a file whitelist: every line of NexusService, Main and the wiring that names the steps (and the two lines
    either side) is read, and none may call an operation on them or sit beside a scheduler."""
    mention = re.compile(r"\b(ManifestCheck\w*|manifestChecks)\b")
    operations = (".census(", ".add(", ".validate(", ".state(", ".run(", ".submit(", ".execute(", ".schedule")
    forbidden_words = ("schedul", "Executor", "Runnable", "startup", "Timer", "Thread")
    examined = 0
    for name in ("NexusService.java", "Main.java", "ManifestCheckWiring.java"):
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
                assert not re.search(r"\bmanifestChecks\s*\(\s*\)\s*\.\s*(census|add|validate|state)\b", lines[j]), \
                    f"{name}:{j + 1} calls an operation through the wiring: {stripped}"
    assert examined >= 12, "the check read too few lines to mean anything"
