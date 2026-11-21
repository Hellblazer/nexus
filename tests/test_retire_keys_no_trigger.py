# SPDX-License-Identifier: AGPL-3.0-or-later
"""The retire-keys routes have no automatic trigger (nexus-6pfgb.43): a census of every file that names them.

Moved out of ``tests/test_retire_keys.py`` (its size ceiling) when the scan learned to read only what a commit could
carry."""
from __future__ import annotations

from pathlib import Path

import pytest


# ── no automatic trigger ─────────────────────────────────────────────────────


@pytest.mark.census
class TestNoAutomaticTrigger:
    """Slice C of nexus-6pfgb.43 (the upgrade-ladder rung that sweeps every install) shipped after the 2026-11-08 cloud
    dry runs were read. The only callers of the routes are the verb and that one rung (``retire-keys-sweep``), which runs
    the dry run and the apply at ``nx upgrade`` and never adds or validates a constraint (those stay the verb's, behind
    the Step 2c engine cut): no hook, doctor row, daemon or other rung calls them, and the engine mounts them for a
    request to reach and runs them from no schedule."""

    #: Where the routes, the client methods and the verb may be named: the client (defines the three methods), the
    #: verb, the group that registers it, the key table's docstring, the engine classes that implement and mount
    #: the routes, and the tests of all of them. Anything else in the repository that names one is a caller.
    NAMING_ALLOWED = {
        "src/nexus/db/http_vector_client.py",
        "src/nexus/commands/t3_cmds/retire_keys.py",
        "src/nexus/commands/t3.py",
        "src/nexus/tables/chunk_metadata_keys.py",
        "service/src/main/java/dev/nexus/service/NexusService.java",
        "service/src/main/java/dev/nexus/service/vectors/ChunkKeyPlan.java",
        "service/src/main/java/dev/nexus/service/vectors/ChunkKeySweep.java",
        "service/src/main/java/dev/nexus/service/http/ChunkKeysAdminHandler.java",
        "tests/test_retire_keys.py",
        "tests/test_retire_keys_no_trigger.py",
        "service/src/main/java/dev/nexus/service/http/ChunkKeysSweepJobs.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepAsyncIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/http/ChunkKeysSweepJobsTest.java",
        # reads the engine's null_span_rows through the dry run (nothing is written) to compare it with the span-fill rung's summary
        "tests/upgrade/test_legacy_rows_walk_substrate.py",
        "service/src/test/java/dev/nexus/service/ChunkKeysTestBase.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepRootTargetIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/ChunkConstraintRoutesIntegrationTest.java",
        # RDR-229 P2.5: adds the allow-list CHECK through the route to show an imported plain-named quarantine row is held to it
        "service/src/test/java/dev/nexus/service/ImportedPlainNamedQuarantineAllowListTest.java",
        "service/src/test/java/dev/nexus/service/db/ManifestWriteRetryGateTest.java",
        "service/src/test/java/dev/nexus/service/db/TenantKeyedStateTables.java",
        "service/src/test/java/dev/nexus/service/db/ChunksWriterLastWrittenAtScanTest.java",
        "service/src/test/java/dev/nexus/service/EgressGuardCensus.java",
        # Slice C (nexus-6pfgb.43): the rung, its registration, and the tests that name it. What it may call is pinned
        # by test_only_the_verb_and_the_rung_call_the_client_and_the_rung_never_touches_the_constraints below.
        "src/nexus/upgrade_ladder/rungs/retire_keys_sweep.py",
        "src/nexus/upgrade_ladder/registry.py",
        # the rung's measurement note and the `Retired chunk keys` doctor row read it (no route is called there), and
        # health.py wires the row; the rdr192 rung names the sweep's completion record to refuse after it
        "src/nexus/upgrade_ladder/retire_keys_state.py",
        "src/nexus/health.py",
        "src/nexus/upgrade_ladder/rungs/rdr192_manifest_backfill.py",
        "tests/upgrade/test_retire_keys_state.py",
        "tests/upgrade/test_rdr192_manifest_backfill_rung.py",
        "tests/upgrade/test_retire_keys_sweep_rung.py",
        "tests/integration/test_retire_keys_sweep_rung_engine.py",
        "tests/upgrade/test_ladder_registry.py",
        "tests/test_doctor_ladder_row_production_registry.py",
    }
    #: Tables that list repository paths with a count beside each (the file-size, context-budget and scrub ratchets,
    #: nexus-3ceda.1/.12/.7): one names tests/test_retire_keys.py as a path, which calls nothing. Each is checked to
    #: hold only such entries, so no caller can hide in one.
    PATH_LISTINGS = {
        "scripts/file-size-exceptions.txt",
        "scripts/folder-shape-exceptions.txt",
        "tests/fixtures/context-budgets.txt",
        "tests/fixtures/scrub-allowlist.txt",
    }
    NAMES = ("chunk_keys_sweep", "chunk_keys_dry_run", "chunk_keys_add_constraints", "chunk_keys_validate_constraints",
             "/v1/admin/chunk-keys", "admin/chunk-keys", "retire-keys", "retire_keys")
    #: Not scanned: version control, environments and build output, and prose (docs, the site, any *.md) that only
    #: describes the verb. Everything else is: src, scripts, conexus, .github, deploy, hooks, tests, the engine.
    SKIP_DIRS = {".git", ".venv", "node_modules", "target", "__pycache__", "docs", "web", ".mypy_cache",
                 ".ruff_cache", ".pytest_cache", ".beads", ".claude", "build", "dist"}
    #: Directories the brief names; each that exists must have been scanned (a gate that scanned none is vacuous).
    NAMED_ROOTS = ("src", "scripts", "conexus", ".github", "deploy")

    @classmethod
    def _files(cls, root: Path) -> list[Path]:
        """The files git tracks, plus untracked files no ignore rule covers: what a commit could carry. A run's own
        output (a status file under the gitignored ``output/``) names the verb in its argv and is not the repository.
        Without git (no work tree), the walk with SKIP_DIRS."""
        import os  # noqa: PLC0415 — only this scan walks the tree
        import subprocess  # noqa: PLC0415 — only this scan lists the tree

        try:
            listed = subprocess.run(  # noqa: S603, S607 — fixed argv, the repo's own git
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True, timeout=60,
            ).stdout.decode("utf-8", "surrogateescape")
        except (OSError, subprocess.SubprocessError):
            out: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in cls.SKIP_DIRS]
                out.extend(Path(dirpath) / f for f in filenames)
            return out
        return [
            root / rel for rel in listed.split("\0")
            if rel and not set(Path(rel).parts[:-1]) & cls.SKIP_DIRS and (root / rel).is_file()
        ]

    def _scan(self) -> tuple[dict[str, set[str]], dict[str, int]]:
        root = Path(__file__).resolve().parent.parent
        seen: dict[str, set[str]] = {}
        scanned: dict[str, int] = {}
        for path in self._files(root):
            if path.suffix == ".md":
                continue
            try:
                if path.stat().st_size > 3_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in self.PATH_LISTINGS:
                continue
            top = rel.split("/", 1)[0]
            scanned[top] = scanned.get(top, 0) + 1
            hits = {n for n in self.NAMES if n in text}
            if hits:
                seen[rel] = hits
        return seen, scanned

    def test_the_scan_reads_what_a_commit_could_carry_not_ignored_output(self, tmp_path: Path) -> None:
        """A run's status file under a gitignored output/ named the verb and turned this gate red (RDR-236)."""
        import subprocess  # noqa: PLC0415 — this pin builds a throwaway repo

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
        (tmp_path / ".gitignore").write_text("output/\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)  # noqa: S603, S607
        (tmp_path / "src" / "new.py").write_text("y = 2\n", encoding="utf-8")  # untracked, not ignored: counts
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "status.txt").write_text("nx t3 retire-keys --apply\n", encoding="utf-8")
        rels = {p.relative_to(tmp_path).as_posix() for p in self._files(tmp_path)}
        assert rels == {".gitignore", "src/tracked.py", "src/new.py"}

    def test_nothing_in_the_repository_names_the_routes_but_the_verb_the_engine_and_their_tests(self) -> None:
        seen, scanned = self._scan()
        for top in self.NAMED_ROOTS:
            if (Path(__file__).resolve().parent.parent / top).is_dir():
                assert scanned.get(top, 0) > 0, f"the scan looked at nothing under {top}/"
        assert sum(scanned.values()) > 1000, "the scan examined too few files to mean anything"
        assert set(seen) <= self.NAMING_ALLOWED, \
            f"a file other than the verb, the engine and their tests names the routes: {sorted(set(seen) - self.NAMING_ALLOWED)}"
        assert {"src/nexus/db/http_vector_client.py", "src/nexus/commands/t3_cmds/retire_keys.py"} <= set(seen), \
            "the scan cannot see the files that define the routes' client: it is not looking where it says"

    def test_only_the_verb_and_the_rung_call_the_client_and_the_rung_never_touches_the_constraints(self) -> None:
        """Within ``src/``: the sweep's dry run and apply are called by the verb and by the ``retire-keys-sweep`` rung and
        by nothing else; the two table-wide constraint calls by the verb alone. The rung adds and validates no CHECK
        (RDR-236 Step 2c is a later engine cut, and a constraint is operator-only)."""
        root = Path(__file__).resolve().parent.parent / "src" / "nexus"
        defining = "src/nexus/db/http_vector_client.py"
        verb = "src/nexus/commands/t3_cmds/retire_keys.py"
        rung = "src/nexus/upgrade_ladder/rungs/retire_keys_sweep.py"
        callers: dict[str, set[str]] = {}
        examined = 0
        for path in root.rglob("*.py"):
            examined += 1
            rel = path.relative_to(root.parent.parent).as_posix()
            text = path.read_text(encoding="utf-8")
            for name in ("chunk_keys_sweep", "chunk_keys_dry_run", "chunk_keys_apply", "chunk_keys_add_constraints",
                         "chunk_keys_validate_constraints"):
                if name in text and rel != defining:
                    callers.setdefault(name, set()).add(rel)
        assert examined > 300, "the scan examined too few files to mean anything"
        assert callers["chunk_keys_dry_run"] == {verb, rung}
        assert callers["chunk_keys_apply"] == {verb, rung}
        assert callers["chunk_keys_sweep"] == {verb}
        assert callers["chunk_keys_add_constraints"] == {verb}
        assert callers["chunk_keys_validate_constraints"] == {verb}

    def test_the_path_listings_hold_only_path_entries(self) -> None:
        import re  # noqa: PLC0415

        entry = re.compile(r"^(?:(?:dir|family|user) )?[A-Za-z0-9._/@+-]+(?: \d+)?$")
        root = Path(__file__).resolve().parent.parent
        checked = 0
        for rel in sorted(self.PATH_LISTINGS):
            path = root / rel
            if not path.is_file():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = line.split("#", 1)[0].strip()
                if line:
                    assert entry.match(line), f"{rel}:{n} is not a path entry, so the exemption does not hold: {line!r}"
                    checked += 1
        assert checked > 0, "no path listing was read"

    def test_the_engines_main_sources_name_the_classes_only_where_they_are_built_and_bound(self) -> None:
        root = Path(__file__).resolve().parent.parent / "service" / "src" / "main" / "java"
        callers = {
            p.name for p in root.rglob("*.java")
            if any(tok in p.read_text(encoding="utf-8")
                   for tok in ("ChunkKeySweep", "ChunkConstraintAdmin", "ChunkKeysAdminHandler"))
        }
        # The classes themselves, the handler that mounts them, and the two places that build and bind them.
        # CatalogTtlCheckHandler (nexus-6pfgb.44) and ManifestCheckHandler (nexus-6pfgb.42) call the handler's
        # static undercount comparison and are reached by no automatic caller either
        # (tests/test_catalog_ttl_check_no_trigger.py, tests/test_manifest_check_no_trigger.py).
        assert callers == {"ChunkKeySweep.java", "ChunkConstraintAdmin.java", "ChunkKeysAdminHandler.java",
                           "NexusService.java", "Main.java", "CatalogTtlCheckHandler.java",
                           "ManifestCheckHandler.java"}

    def test_nexus_service_and_main_never_invoke_the_operations_or_schedule_them(self) -> None:
        """Not a file whitelist: every line of NexusService and Main that names the sweep, the admin or the handler
        (and the two lines either side) is read, and none may call an operation on them or sit beside a scheduler."""
        import re  # noqa: PLC0415 — only this check needs a regex

        root = Path(__file__).resolve().parent.parent / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
        mention = re.compile(r"\b(ChunkKeySweep|ChunkConstraintAdmin|ChunkKeysAdminHandler|chunkConstraintAdmin)\b")
        operations = (".dryRun(", ".apply(", ".probe(", ".collectionsOf(", ".validate(", ".add(", ".states(",
                      ".preconditions(", ".preconditionReport(", ".run(", ".submit(", ".execute(")
        forbidden_words = ("schedul", "Executor", "Runnable", "startup", "Timer", "Thread")
        examined = 0
        for name in ("NexusService.java", "Main.java"):
            lines = (root / name).read_text(encoding="utf-8").splitlines()
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
                    assert not re.search(r"\bchunkConstraintAdmin\s*\.", lines[j]), \
                        f"{name}:{j + 1} calls on the admin: {stripped}"
        assert examined >= 8, "the check read too few lines to mean anything"
