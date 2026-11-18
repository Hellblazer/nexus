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
from tests._module_seam import setattr_in

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


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every entry under the vendor directory, staging leftovers included: it is either whole or untouched."""
    base = _vendor(root)
    return {str(p.relative_to(base)): (p.read_bytes() if p.is_file() else b"<dir>") for p in sorted(base.rglob("*"))}


def _lib_tree_entry(rel: str) -> bytes:
    return f"100644 blob {'a' * 40}\t{v.LIBRARIES['ddl_utils'].source_prefix}/{rel}\0".encode()


class TestHardening:
    @pytest.mark.parametrize(
        "rel",
        sorted(
            [
                "../evil.sql",
                "changes/../../evil.sql",
                "changes/.git/hooks/post-checkout",
                "changes/.GIT/config",
                "/abs.sql",
                "changes//double.sql",
                "changes/./dot.sql",
                "C:/drive.sql",
                "back\\slash.sql",
            ]
        ),
    )
    def test_a_tree_path_that_could_leave_the_vendor_directory_is_refused(self, monkeypatch, clones, rel):
        clone, sha = clones["ddl_utils"]
        real = v._git

        def fake(c, *args):
            return _lib_tree_entry(rel) if args[0] == "ls-tree" else real(c, *args)

        monkeypatch.setattr(v, "_git", fake)
        with pytest.raises(v.VendorError, match="refusing path"):
            v.library_tree(v.LIBRARIES["ddl_utils"], clone, sha)

    def test_a_refused_path_writes_nothing_through_update(self, monkeypatch, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        before = _snapshot(repo_root)
        real = v._git

        def fake(c, *args):
            return _lib_tree_entry("../evil.sql") if args[0] == "ls-tree" else real(c, *args)

        monkeypatch.setattr(v, "_git", fake)
        with pytest.raises(v.VendorError, match="refusing path"):
            v.update(repo_root, _pins(clones), _clone_paths(clones))
        assert _snapshot(repo_root) == before
        assert not (repo_root / "evil.sql").exists()

    def test_a_manifest_path_that_leaves_the_directory_is_refused(self, tmp_path):
        m = tmp_path / v.MANIFEST_NAME
        m.write_text(f"revision {'b' * 40}\n{'a' * 40} ../../outside.sql\n")
        with pytest.raises(v.VendorError, match="refusing path"):
            v.read_manifest(m)

    @pytest.mark.parametrize("rev", ["--output=x", "-h", "--end-of-options"])
    def test_a_revision_that_looks_like_an_option_never_reaches_the_command_line(self, monkeypatch, clones, rev):
        called: list[tuple[str, ...]] = []
        monkeypatch.setattr(v, "_git", lambda c, *args: called.append(args) or b"")
        with pytest.raises(v.VendorError, match="starts with '-'"):
            v.resolve_revision(clones["dml_utils"][0], rev)
        assert called == [], "the command ran with an option-shaped revision"

    def test_an_option_shaped_revision_fails_the_command(self, repo_root, clones, capsys):
        rc = v.main(
            ["--update", "dml_utils=--exec=x", "--repo-root", str(repo_root)]
            + [f"--clone={n}={p}" for n, p in _clone_paths(clones).items()]
        )
        assert rc == 1
        assert "starts with '-'" in capsys.readouterr().err
        assert not _vendor(repo_root).exists()

    def test_a_write_that_fails_midway_leaves_every_library_whole(self, monkeypatch, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        before = _snapshot(repo_root)
        clone, _ = clones["dml_utils"]
        base = "dml_utils/src/main/resources/db/changelog"
        new = _commit(clone, {f"{base}/db.changelog-master.xml": b"<master two/>\n"}, "two")
        real = v._write_file
        calls = {"n": 0}

        def flaky(target, data):
            calls["n"] += 1
            if calls["n"] == 8:  # inside the second library; the first is fully staged by then
                raise OSError("disk full")
            real(target, data)

        monkeypatch.setattr(v, "_write_file", flaky)
        revs = {"ddl_utils": clones["ddl_utils"][1], "dml_utils": new}
        with pytest.raises(OSError, match="disk full"):
            v.update(repo_root, revs, _clone_paths(clones))
        assert calls["n"] == 8, "the fault was not reached; the test proves nothing"
        assert _snapshot(repo_root) == before, "a failed --update left a half-written vendor tree or a staging directory"
        assert v.check(repo_root) == []

    def test_a_swap_that_fails_puts_the_old_libraries_back(self, monkeypatch, repo_root, clones):
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        before = _snapshot(repo_root)
        real = v.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 4:  # the second library's staged copy going in, after the first library was swapped
                raise OSError("rename failed")
            real(src, dst)

        setattr_in(monkeypatch, v, "os.replace", flaky)
        with pytest.raises(OSError, match="rename failed"):
            v.update(repo_root, _pins(clones), _clone_paths(clones))
        assert calls["n"] >= 4, "the fault was not reached; the test proves nothing"
        assert _snapshot(repo_root) == before
        assert v.check(repo_root) == []

    def test_a_stale_staging_directory_from_a_killed_run_is_replaced(self, repo_root, clones):
        stale = _vendor(repo_root) / ".dml_utils.staging"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("left over\n")
        v.update(repo_root, _pins(clones), _clone_paths(clones))
        assert not stale.exists()
        assert v.check(repo_root) == []


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


_NS = 'xmlns="http://www.liquibase.org/xml/ns/dbchangelog"'


def _changes(*sets: str) -> bytes:
    return f'<databaseChangeLog {_NS}>{"".join(sets)}</databaseChangeLog>'.encode()


class TestEditedAppliedChangesets:
    """nexus-f3yqd.2.11: a re-pin must not edit a changeset an install already applied."""

    OLD = {
        "changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sqlFile path="sql/001.sql" relativeToChangelogFile="true"/></changeSet>',
            '<changeSet id="002" author="a" runOnChange="true"><sql>select 1</sql></changeSet>',
            '<changeSet id="003" author="a"><sql>select 3</sql></changeSet>',
        ),
        "changes/sql/001.sql": b"create schema a;\n",
    }

    def test_identical_trees_and_appended_changesets_are_clean(self):
        new = dict(self.OLD)
        new["changes/changes.xml"] = self.OLD["changes/changes.xml"].replace(
            b"</databaseChangeLog>", b'<changeSet id="004" author="a"><sql>select 4</sql></changeSet></databaseChangeLog>')
        assert v.edited_applied_changesets(self.OLD, self.OLD) == []
        assert v.edited_applied_changesets(self.OLD, new) == []

    def test_an_edited_body_is_named(self):
        new = dict(self.OLD)
        new["changes/changes.xml"] = self.OLD["changes/changes.xml"].replace(b"select 3", b"select 33")
        assert v.edited_applied_changesets(self.OLD, new) == ["changes/changes.xml::003::a"]

    def test_an_edited_referenced_sql_file_is_named(self):
        new = dict(self.OLD)
        new["changes/sql/001.sql"] = b"create schema b;\n"
        assert v.edited_applied_changesets(self.OLD, new) == ["changes/changes.xml::001::a"]

    def test_a_run_on_change_changeset_may_change(self):
        new = dict(self.OLD)
        new["changes/changes.xml"] = self.OLD["changes/changes.xml"].replace(b"select 1", b"select 11")
        assert v.edited_applied_changesets(self.OLD, new) == []

    def test_each_offender_is_named_and_a_malformed_file_is_skipped(self):
        new = dict(self.OLD)
        new["changes/changes.xml"] = self.OLD["changes/changes.xml"].replace(b"select 3", b"select 33")
        new["changes/sql/001.sql"] = b"changed\n"
        new["changes/broken.xml"] = b"<master one/>"
        assert v.edited_applied_changesets(self.OLD, new) == [
            "changes/changes.xml::001::a", "changes/changes.xml::003::a"]

    def test_update_refuses_an_edited_applied_changeset_names_it_and_writes_nothing(self, repo_root, clones):
        clone, _ = clones["dml_utils"]
        base = "dml_utils/src/main/resources/db/changelog"
        first = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 1</sql></changeSet>')}, "with a changeset")
        pins = _pins(clones) | {"dml_utils": first}
        v.update(repo_root, pins, _clone_paths(clones))
        before = (_vendor(repo_root) / "dml_utils" / "changes" / "changes.xml").read_bytes()
        second = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 2</sql></changeSet>')}, "edit it")
        with pytest.raises(v.VendorError) as refused:
            v.update(repo_root, {"dml_utils": second}, _clone_paths(clones))
        assert "dml_utils: changes/changes.xml::001::a" in str(refused.value)
        assert (_vendor(repo_root) / "dml_utils" / "changes" / "changes.xml").read_bytes() == before
        assert v.read_pins(_vendor(repo_root) / v.PINS_NAME)["dml_utils"] == first

    def test_update_accepts_an_appended_changeset(self, repo_root, clones):
        clone, _ = clones["dml_utils"]
        base = "dml_utils/src/main/resources/db/changelog"
        first = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 1</sql></changeSet>')}, "with a changeset")
        v.update(repo_root, _pins(clones) | {"dml_utils": first}, _clone_paths(clones))
        second = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 1</sql></changeSet>',
            '<changeSet id="002" author="a"><sql>select 2</sql></changeSet>')}, "append")
        v.update(repo_root, {"dml_utils": second}, _clone_paths(clones))
        assert v.read_pins(_vendor(repo_root) / v.PINS_NAME)["dml_utils"] == second

    def test_a_changeset_missing_from_the_new_pin_is_named_unless_it_was_run_on_change(self):
        new = dict(self.OLD)
        new["changes/changes.xml"] = _changes(
            '<changeSet id="002" author="a" runOnChange="true"><sql>select 1</sql></changeSet>')
        assert v.edited_applied_changesets(self.OLD, new) == [
            "changes/changes.xml::001::a (missing from the new pin)",
            "changes/changes.xml::003::a (missing from the new pin)",
        ]

    def test_a_changeset_that_moved_files_is_missing_from_its_old_file(self):
        new = {
            "changes/changes.xml": self.OLD["changes/changes.xml"].replace(
                b'<changeSet id="003" author="a"><sql>select 3</sql></changeSet>', b""),
            "changes/sql/001.sql": self.OLD["changes/sql/001.sql"],
            "changes/more.xml": _changes('<changeSet id="003" author="a"><sql>select 3</sql></changeSet>'),
        }
        assert v.edited_applied_changesets(self.OLD, new) == ["changes/changes.xml::003::a (missing from the new pin)"]

    def test_update_refuses_a_dropped_changeset(self, repo_root, clones):
        clone, _ = clones["dml_utils"]
        base = "dml_utils/src/main/resources/db/changelog"
        first = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 1</sql></changeSet>',
            '<changeSet id="002" author="a"><sql>select 2</sql></changeSet>')}, "two changesets")
        v.update(repo_root, _pins(clones) | {"dml_utils": first}, _clone_paths(clones))
        second = _commit(clone, {f"{base}/changes/changes.xml": _changes(
            '<changeSet id="001" author="a"><sql>select 1</sql></changeSet>')}, "drop one")
        with pytest.raises(v.VendorError) as refused:
            v.update(repo_root, {"dml_utils": second}, _clone_paths(clones))
        assert "dml_utils: changes/changes.xml::002::a (missing from the new pin)" in str(refused.value)

    def test_sqlfile_paths_resolve_with_posix_separators_whatever_the_host(self):
        """A vendored key is always '/'-separated, so the join must not be the host's: ntpath would make 'changes\\sql\\001.sql'
        and read the referenced file as missing, hiding every sqlFile edit on Windows."""
        import ntpath

        assert v.posixpath is not ntpath
        old = {"a/b/c.xml": _changes('<changeSet id="1" author="x"><sqlFile path="../s/1.sql" relativeToChangelogFile="true"/></changeSet>'),
               "a/s/1.sql": b"one"}
        new = dict(old)
        new["a/s/1.sql"] = b"two"
        assert v.edited_applied_changesets(old, new) == ["a/b/c.xml::1::x"]
