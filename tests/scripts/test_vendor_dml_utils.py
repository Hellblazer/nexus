# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/vendor_dml_utils.py: --update and --check against tmp repositories (nexus-f3yqd.2.10).

The library clones here are throwaway git repositories, so nothing reads or writes the real
``~/git/ddl_utils`` or ``~/git/dml_utils``. One test points ``check`` at the checked-in vendor
tree and refuses an empty one (the non-vacuity leg).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import vendor_dml_utils as v

REPO_ROOT = Path(__file__).resolve().parents[2]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return out.stdout.strip()


def _lib_files(name: str, tag: str) -> dict[str, bytes]:
    base = f"{name}/src/main/resources/db/changelog"
    return {
        "LICENSE": f"MIT License {tag}\n".encode(),
        "README.md": b"not vendored\n",
        f"{name}/src/test/java/T.java": b"not vendored\n",
        f"{base}/db.changelog-master.xml": f"<master {tag}/>\n".encode(),
        f"{base}/changes/changes.xml": b"<changes/>\n",
        f"{base}/changes/sql_changes/001-a.sql": b"create schema a;\r\nselect 1;\n",
        f"{base}/changes/procedures/x/.gitkeep": b"",
    }


def _commit(repo: Path, files: dict[str, bytes], message: str, remove: tuple[str, ...] = ()) -> str:
    for rel in remove:
        (repo / rel).unlink()
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clones(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    """name -> (clone path, commit sha), one commit each."""
    out: dict[str, tuple[Path, str]] = {}
    for name in v.LIBRARIES:
        repo = tmp_path / "clones" / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        out[name] = (repo, _commit(repo, _lib_files(name, "one"), "one"))
    return out


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "nexus"
    root.mkdir()
    return root


def _clone_paths(clones: dict[str, tuple[Path, str]]) -> dict[str, Path]:
    return {n: p for n, (p, _) in clones.items()}


def _pins(clones: dict[str, tuple[Path, str]]) -> dict[str, str]:
    return {n: sha for n, (_, sha) in clones.items()}


def _vendor(root: Path) -> Path:
    return root / v.VENDOR_RELDIR


class TestUpdate:
    def test_copies_changelog_subtree_and_license_only(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        for name in v.LIBRARIES:
            d = _vendor(repo_root) / name
            assert (d / "db.changelog-master.xml").read_text() == "<master one/>\n"
            assert (d / "changes" / "changes.xml").is_file()
            assert (d / "LICENSE").read_text() == "MIT License one\n"
            assert (d / "changes" / "procedures" / "x" / ".gitkeep").is_file()
            assert not (d / "README.md").exists()
            assert not (d / "src").exists()

    def test_bytes_are_copied_exactly(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        sql = _vendor(repo_root) / "ddl_utils" / "changes" / "sql_changes" / "001-a.sql"
        assert sql.read_bytes() == b"create schema a;\r\nselect 1;\n"

    def test_manifest_has_full_revision_and_git_blob_ids(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        for name, (clone, sha) in clones.items():
            manifest = v.read_manifest(_vendor(repo_root) / name / v.MANIFEST_NAME)
            assert manifest.revision == sha
            assert len(manifest.revision) == 40
            expected = {}
            for line in _git(clone, "ls-tree", "-r", sha).splitlines():
                meta, path = line.split("\t", 1)
                blob = meta.split()[2]
                prefix = f"{name}/src/main/resources/db/changelog/"
                if path.startswith(prefix):
                    expected[path[len(prefix) :]] = blob
                elif path == "LICENSE":
                    expected["LICENSE"] = blob
            assert manifest.files == expected
            assert len(manifest.files) == 5

    def test_pins_properties_records_each_revision(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        assert v.read_pins(_vendor(repo_root) / v.PINS_NAME) == _pins(clones)

    def test_reads_the_pinned_revision_not_the_working_tree(self, repo_root, clones):
        """A clone sitting on another branch with uncommitted edits must not leak into the copy."""
        clone, sha = clones["dml_utils"]
        _git(clone, "checkout", "-q", "-b", "feature/other")
        _commit(clone, {"dml_utils/src/main/resources/db/changelog/db.changelog-master.xml": b"<other/>\n"}, "other")
        (clone / "LICENSE").write_bytes(b"dirty uncommitted\n")
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        d = _vendor(repo_root) / "dml_utils"
        assert (d / "db.changelog-master.xml").read_text() == "<master one/>\n"
        assert (d / "LICENSE").read_text() == "MIT License one\n"

    def test_does_not_write_in_the_clone(self, repo_root, clones):
        before = {n: _git(p, "status", "--porcelain=v1") + _git(p, "rev-parse", "HEAD") for n, p in _clone_paths(clones).items()}
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        after = {n: _git(p, "status", "--porcelain=v1") + _git(p, "rev-parse", "HEAD") for n, p in _clone_paths(clones).items()}
        assert before == after

    def test_repin_one_library_by_revision_argument(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        clone, old = clones["dml_utils"]
        base = "dml_utils/src/main/resources/db/changelog"
        new = _commit(
            clone,
            {f"{base}/db.changelog-master.xml": b"<master two/>\n", f"{base}/changes/sql_changes/002-b.sql": b"select 2;\n"},
            "two",
            remove=(f"{base}/changes/changes.xml",),
        )
        rc = v.main(
            ["--update", f"dml_utils={new[:9]}", "--repo-root", str(repo_root)]
            + [f"--clone={n}={p}" for n, p in _clone_paths(clones).items()]
        )
        assert rc == 0
        d = _vendor(repo_root) / "dml_utils"
        assert (d / "db.changelog-master.xml").read_text() == "<master two/>\n"
        assert (d / "changes" / "sql_changes" / "002-b.sql").is_file()
        assert not (d / "changes" / "changes.xml").exists(), "a file dropped upstream must leave the vendor tree"
        manifest = v.read_manifest(d / v.MANIFEST_NAME)
        assert manifest.revision == new  # the abbreviation resolves to the full sha
        assert "changes/changes.xml" not in manifest.files
        # the other library is untouched, still at its old pin
        assert v.read_manifest(_vendor(repo_root) / "ddl_utils" / v.MANIFEST_NAME).revision == clones["ddl_utils"][1]
        assert v.read_pins(_vendor(repo_root) / v.PINS_NAME)["dml_utils"] == new
        assert old != new

    def test_update_with_no_argument_regenerates_at_the_recorded_pins(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        victim = _vendor(repo_root) / "ddl_utils" / "LICENSE"
        victim.write_bytes(b"tampered\n")
        rc = v.main(["--update", "--repo-root", str(repo_root)] + [f"--clone={n}={p}" for n, p in _clone_paths(clones).items()])
        assert rc == 0
        assert victim.read_text() == "MIT License one\n"

    def test_unknown_revision_fails_and_writes_nothing(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        snapshot = {p: p.read_bytes() for p in _vendor(repo_root).rglob("*") if p.is_file()}
        with pytest.raises(v.VendorError, match="deadbeef"):
            v.update(repo_root, {"dml_utils": "deadbeef"}, _clone_paths(clones))
        assert snapshot == {p: p.read_bytes() for p in _vendor(repo_root).rglob("*") if p.is_file()}

    def test_a_revision_with_no_changelog_files_is_refused(self, repo_root, clones):
        clone, _ = clones["ddl_utils"]
        _git(clone, "checkout", "-q", "--orphan", "empty")
        _git(clone, "rm", "-rf", "-q", ".")
        (clone / "x").write_text("x\n")
        _git(clone, "add", "x")
        _git(clone, "commit", "-q", "-m", "empty")
        with pytest.raises(v.VendorError, match="no changelog files"):
            v.update(repo_root, {"ddl_utils": _git(clone, "rev-parse", "HEAD")}, _clone_paths(clones))


class TestCheck:
    @pytest.fixture
    def vendored(self, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        return repo_root

    def test_clean_tree_passes(self, vendored):
        assert v.check(vendored) == []

    def test_one_changed_byte_fails(self, vendored):
        f = _vendor(vendored) / "dml_utils" / "changes" / "sql_changes" / "001-a.sql"
        data = bytearray(f.read_bytes())
        data[0] ^= 1
        f.write_bytes(bytes(data))
        problems = v.check(vendored)
        assert any("changes/sql_changes/001-a.sql" in p and "blob" in p for p in problems), problems

    def test_a_deleted_file_fails(self, vendored):
        (_vendor(vendored) / "ddl_utils" / "LICENSE").unlink()
        assert any("LICENSE" in p and "missing" in p for p in v.check(vendored))

    def test_an_extra_file_fails(self, vendored):
        (_vendor(vendored) / "ddl_utils" / "changes" / "stray.sql").write_text("select 1;\n")
        assert any("stray.sql" in p and "not in the manifest" in p for p in v.check(vendored))

    def test_an_empty_manifest_fails(self, vendored):
        m = _vendor(vendored) / "dml_utils" / v.MANIFEST_NAME
        m.write_text("\n".join(line for line in m.read_text().splitlines() if not line.endswith(("xml", "sql", "LICENSE", "gitkeep"))) + "\n")
        assert any("no files" in p for p in v.check(vendored))

    def test_a_missing_manifest_fails(self, vendored):
        (_vendor(vendored) / "dml_utils" / v.MANIFEST_NAME).unlink()
        assert any("dml_utils" in p and "manifest" in p for p in v.check(vendored))

    def test_a_revision_that_is_not_the_pin_fails(self, vendored):
        pins = _vendor(vendored) / v.PINS_NAME
        pins.write_text(pins.read_text().replace(v.read_pins(pins)["dml_utils"], "0" * 40))
        assert any("dml_utils" in p and "pin" in p for p in v.check(vendored))

    def test_an_abbreviated_manifest_revision_fails(self, vendored):
        m = _vendor(vendored) / "dml_utils" / v.MANIFEST_NAME
        text = m.read_text()
        rev = v.read_manifest(m).revision
        m.write_text(text.replace(rev, rev[:9]))
        assert any("revision" in p for p in v.check(vendored))

    def test_no_vendor_tree_at_all_fails(self, repo_root):
        assert v.check(repo_root), "an absent vendor tree is a failure, not a pass"

    def test_against_clones_compares_blob_ids_at_the_revision(self, vendored, clones):
        assert v.check(vendored, clones=_clone_paths(clones)) == []
        # a manifest naming a blob the pinned revision does not have
        m = _vendor(vendored) / "dml_utils" / v.MANIFEST_NAME
        lines = m.read_text().splitlines()
        i = next(i for i, line in enumerate(lines) if line.endswith("LICENSE"))
        lines[i] = "0" * 40 + " LICENSE"
        m.write_text("\n".join(lines) + "\n")
        assert any("LICENSE" in p and "clone" in p for p in v.check(vendored, clones=_clone_paths(clones)))


class TestCli:
    def test_check_exit_codes(self, repo_root, clones, capsys):
        assert v.main(["--check", "--repo-root", str(repo_root)]) == 1
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        assert v.main(["--check", "--repo-root", str(repo_root)]) == 0
        out = capsys.readouterr().out
        assert "ddl_utils" in out and "dml_utils" in out and "5 files" in out

    def test_exactly_one_mode_is_required(self, repo_root):
        with pytest.raises(SystemExit):
            v.main(["--repo-root", str(repo_root)])


class TestCheckedInTree:
    def test_the_checked_in_vendor_tree_passes_and_is_not_empty(self):
        assert v.check(REPO_ROOT) == []
        for name in v.LIBRARIES:
            manifest = v.read_manifest(REPO_ROOT / v.VENDOR_RELDIR / name / v.MANIFEST_NAME)
            assert len(manifest.files) > 50, f"{name}: a vendored library that small is not the library"
            assert "LICENSE" in manifest.files

    def test_every_library_is_pinned_to_a_full_revision(self):
        """A re-pin (--update LIB=REV) changes the value, never the set of libraries or its width."""
        pins = v.read_pins(REPO_ROOT / v.VENDOR_RELDIR / v.PINS_NAME)
        assert sorted(pins) == sorted(v.LIBRARIES)
        assert all(len(sha) == 40 for sha in pins.values())

    def test_gitattributes_keeps_the_vendored_bytes_unconverted(self):
        """Git for Windows autocrlf would change every blob id on a checkout."""
        rules = (REPO_ROOT / ".gitattributes").read_text().splitlines()
        assert f"{v.VENDOR_RELDIR}/** -text" in rules
