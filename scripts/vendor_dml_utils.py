#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor ddl_utils' and dml_utils' Liquibase changelogs at pinned revisions (RDR-246, nexus-f3yqd.2.10).

    vendor_dml_utils.py --check                         # offline: vendored bytes against the manifests
    vendor_dml_utils.py --check --clone dml_utils=PATH  # also compare each manifest with the clone at its revision
    vendor_dml_utils.py --update                        # regenerate both libraries at the recorded pins
    vendor_dml_utils.py --update dml_utils=REV          # re-pin one library; REV may be abbreviated

The engine carries both libraries' production changelogs under
``service/src/main/resources/db/vendor/<library>/`` (``db.changelog-master.xml``, ``changes/**``) with
the library's ``LICENSE``. Nothing includes them from ``db.changelog-master.xml``; the boot-apply step
runs them. Each library has a manifest, ``PIN.manifest``: the full revision and the git blob id of every
file, as ``git ls-tree -r`` reports them at that revision. ``pins.properties`` records the revision per
library; ``VendoredLibraryPinTest`` (Java) and ``--check`` hold the vendored bytes to both.

Only the production changelog subtree (``<library>/src/main/resources/db/changelog/``) and ``LICENSE``
are copied. dml_utils' ``db/changelog-install`` is its test and codegen install path, never published,
and is left out.

``--update`` reads the clone through ``git rev-parse``, ``git ls-tree -r`` and ``git cat-file blob``
only. It never checks out, commits or writes in the clone, and never reads its working tree, so a clone
sitting on a feature branch with uncommitted edits is safe. A revision that does not resolve, or that
holds no changelog files, writes nothing.

``--update`` also refuses a re-pin that changes the bytes of a changeset the old pin already carries, unless
that changeset is ``runOnChange`` (nexus-f3yqd.2.11): the engine applies both libraries at boot and Liquibase
validates each applied changeset's checksum, so an edited one fails that library's install on every install
that applied the old pin, and nothing repairs it. A library change is a new changeset appended; the refusal
names each edited changeset as ``<file>::<id>::<author>``.
"""

from __future__ import annotations

import argparse
import hashlib
import xml.etree.ElementTree as ET
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VENDOR_RELDIR = "service/src/main/resources/db/vendor"
MANIFEST_NAME = "PIN.manifest"
PINS_NAME = "pins.properties"
LICENSE_NAME = "LICENSE"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_FILE_LINE = re.compile(r"^([0-9a-f]{40}) (.+)$")


@dataclass(frozen=True)
class Library:
    name: str
    #: Subtree of the upstream repository whose contents become the vendor directory.
    source_prefix: str
    upstream: str

    @property
    def default_clone(self) -> Path:
        return Path.home() / "git" / self.name


LIBRARIES: dict[str, Library] = {
    lib.name: lib
    for lib in (
        Library("ddl_utils", "ddl_utils/src/main/resources/db/changelog", "Hellblazer/ddl_utils"),
        Library("dml_utils", "dml_utils/src/main/resources/db/changelog", "Hellblazer/dml_utils"),
    )
}


class VendorError(Exception):
    """A pin that cannot be resolved or read; nothing was written."""


@dataclass(frozen=True)
class Manifest:
    revision: str
    files: dict[str, str]  # path relative to the library's vendor directory -> git blob id


def blob_id(data: bytes) -> str:
    """The id git gives a blob with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git object id, not a security use


# --------------------------------------------------------------------------- #
# Reading a clone (read verbs only)
# --------------------------------------------------------------------------- #


def _git(clone: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(clone), *args], capture_output=True)  # noqa: S603, S607
    if proc.returncode != 0:
        raise VendorError(f"git {' '.join(args)} failed in {clone}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def resolve_revision(clone: Path, rev: str) -> str:
    if rev.startswith("-"):
        # `git rev-parse` would read it as an option.
        raise VendorError(f"revision {rev!r} starts with '-'; refusing to pass it to git")
    if not clone.is_dir():
        raise VendorError(f"no clone at {clone}")
    try:
        sha = _git(clone, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").decode().strip()
    except VendorError as exc:
        raise VendorError(f"revision {rev} does not resolve in {clone}") from exc
    if not _SHA.match(sha):
        raise VendorError(f"revision {rev} does not resolve in {clone}")
    return sha


def validate_relpath(rel: str) -> str:
    """*rel* is a path under a vendor directory: relative, with no empty, ``.``, ``..`` or ``.git`` component."""
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or "\0" in rel or re.match(r"^[A-Za-z]:", rel):
        raise VendorError(f"refusing path {rel!r}: absolute or not a plain relative path")
    for part in parts:
        if part in ("", ".", "..") or part.lower() == ".git":
            raise VendorError(f"refusing path {rel!r}: it has a {part!r} component")
    return rel


def library_tree(lib: Library, clone: Path, sha: str) -> dict[str, str]:
    """Vendored path -> blob id for *lib* at *sha*, from ``git ls-tree -r``."""
    raw = _git(clone, "ls-tree", "-r", "-z", sha)
    prefix = lib.source_prefix + "/"
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, path_b = entry.partition(b"\t")
        mode, kind, blob = meta.decode().split()
        path = path_b.decode()
        if path.startswith(prefix):
            rel = path[len(prefix) :]
        elif path == LICENSE_NAME:
            rel = LICENSE_NAME
        else:
            continue
        validate_relpath(rel)
        if kind != "blob" or mode == "120000":
            raise VendorError(f"{lib.name}: {path} at {sha} is a {kind} of mode {mode}, not a regular file")
        out[rel] = blob
    if not any(rel != LICENSE_NAME for rel in out):
        raise VendorError(f"{lib.name}: revision {sha} has no changelog files under {lib.source_prefix}/")
    if LICENSE_NAME not in out:
        raise VendorError(f"{lib.name}: revision {sha} has no {LICENSE_NAME} at its root")
    return out


# --------------------------------------------------------------------------- #
# Manifest and pins files
# --------------------------------------------------------------------------- #


def render_manifest(lib: Library, manifest: Manifest) -> str:
    lines = [
        f"# {lib.upstream}, vendored by scripts/vendor_dml_utils.py. Regenerate with --update; never edit by hand.",
        "# <git blob id> <path under this directory>, as git ls-tree -r reports them at the revision.",
        f"revision {manifest.revision}",
    ]
    lines += [f"{blob} {path}" for path, blob in sorted(manifest.files.items())]
    return "\n".join(lines) + "\n"


def read_manifest(path: Path) -> Manifest:
    revision = ""
    files: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("revision "):
            revision = line[len("revision ") :].strip()
            continue
        m = _MANIFEST_FILE_LINE.match(line)
        if m is None:
            raise VendorError(f"{path}:{n}: not a manifest line: {line!r}")
        files[validate_relpath(m.group(2))] = m.group(1)
    return Manifest(revision, files)


def read_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, _, sha = line.partition("=")
        pins[name.strip()] = sha.strip()
    return pins


def _write_pins(path: Path, pins: dict[str, str]) -> None:
    header = "# The pinned revision of each vendored library; written by scripts/vendor_dml_utils.py --update.\n"
    path.write_text(header + "".join(f"{name}={sha}\n" for name, sha in sorted(pins.items())), encoding="utf-8")


# --------------------------------------------------------------------------- #
# --update
# --------------------------------------------------------------------------- #


def _changesets(files: dict[str, bytes]) -> dict[tuple[str, str, str], tuple[bytes, bool]]:
    """``(file, id, author)`` -> (canonical bytes of the changeset plus the files its ``path`` attributes read, runOnChange).

    The key is Liquibase's identity of a changeset; the bytes stand for its checksum. A file that is not
    well-formed XML is skipped: it holds no changeset Liquibase could have applied."""
    out: dict[tuple[str, str, str], tuple[bytes, bool]] = {}
    for rel, data in files.items():
        if not rel.endswith(".xml"):
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        for element in root.iter():
            if not element.tag.endswith("changeSet"):
                continue
            digest = [ET.canonicalize(ET.tostring(element, encoding="unicode")).encode("utf-8")]
            for sub in element.iter():
                path = sub.get("path")
                if path is None:
                    continue
                relative = sub.get("relativeToChangelogFile", "false").lower() == "true"
                target = os.path.normpath(os.path.join(os.path.dirname(rel), path)) if relative else path
                digest.append(b"\0" + target.encode("utf-8") + b"\0" + files.get(target, b"<missing>"))
            run_on_change = element.get("runOnChange", "false").lower() == "true"
            out[(rel, element.get("id", ""), element.get("author", ""))] = (b"".join(digest), run_on_change)
    return out


def edited_applied_changesets(old: dict[str, bytes], new: dict[str, bytes]) -> list[str]:
    """The changesets present in *old* and *new* whose bytes differ and that are not ``runOnChange`` in *new*, as
    ``<file>::<id>::<author>``, sorted. Each fails Liquibase's checksum validation on an install that applied *old*."""
    before = _changesets(old)
    after = _changesets(new)
    return sorted(
        "::".join(key)
        for key, (data, run_on_change) in after.items()
        if key in before and not run_on_change and before[key][0] != data
    )


def _read_vendored(directory: Path) -> dict[str, bytes]:
    """The changelog files of a vendored library directory, keyed as the upstream subtree keys them."""
    out: dict[str, bytes] = {}
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            rel = path.relative_to(directory).as_posix()
            if path.is_file() and rel not in (MANIFEST_NAME, LICENSE_NAME):
                out[rel] = path.read_bytes()
    return out


def update(repo_root: Path, revisions: dict[str, str], clones: dict[str, Path] | None = None) -> dict[str, Manifest]:
    """Regenerate the named libraries at the given revisions; the others are left as they are.

    Everything is read from the clones first, so a bad revision writes nothing."""
    clones = clones or {}
    unknown = sorted(set(revisions) - set(LIBRARIES))
    if unknown:
        raise VendorError(f"unknown library {unknown[0]}; known: {', '.join(sorted(LIBRARIES))}")
    planned: dict[str, tuple[Manifest, dict[str, bytes]]] = {}
    for name, rev in revisions.items():
        lib = LIBRARIES[name]
        clone = clones.get(name, lib.default_clone)
        sha = resolve_revision(clone, rev)
        tree = library_tree(lib, clone, sha)
        contents: dict[str, bytes] = {}
        for rel, blob in tree.items():
            data = _git(clone, "cat-file", "blob", blob)
            if blob_id(data) != blob:
                raise VendorError(f"{name}: {rel} read back as a different blob than {blob}")
            contents[rel] = data
        planned[name] = (Manifest(sha, tree), contents)

    vendor = repo_root / VENDOR_RELDIR
    edited: list[str] = []
    for name, (_, contents) in planned.items():
        edited += [f"{name}: {item}" for item in edited_applied_changesets(_read_vendored(vendor / name), contents)]
    if edited:
        raise VendorError(
            "the re-pin edits changesets the current pin already carries, which are not runOnChange; an install that "
            "applied them would fail Liquibase's checksum validation on every boot. Append a new changeset upstream "
            "instead. Edited: " + "; ".join(edited)
        )
    vendor.mkdir(parents=True, exist_ok=True)
    pins_path = vendor / PINS_NAME
    pins = read_pins(pins_path) if pins_path.is_file() else {}
    # Stage every library beside its target, then swap: a write that fails leaves the old library whole.
    staged: dict[str, Path] = {}
    try:
        for name, (manifest, contents) in planned.items():
            stage = vendor / f".{name}.staging"
            if stage.exists():
                shutil.rmtree(stage)
            staged[name] = stage
            for rel, data in contents.items():
                target = stage / rel
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise VendorError(f"{name}: {rel} would be written outside {stage}")
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_file(target, data)
            _write_file(stage / MANIFEST_NAME, render_manifest(LIBRARIES[name], manifest).encode("utf-8"))
            pins[name] = manifest.revision
        _swap_in(vendor, staged)
    finally:
        for stage in staged.values():
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    tmp_pins = vendor / f".{PINS_NAME}.staging"
    _write_pins(tmp_pins, pins)
    os.replace(tmp_pins, pins_path)
    return {name: m for name, (m, _) in planned.items()}


def _write_file(target: Path, data: bytes) -> None:
    target.write_bytes(data)


def _swap_in(vendor: Path, staged: dict[str, Path]) -> None:
    """Replace each library directory with its staged copy; a failed swap puts the old directories back."""
    moved: list[tuple[Path, Path]] = []  # (library dir, where the old one went)
    try:
        for name, stage in staged.items():
            lib_dir = vendor / name
            old = vendor / f".{name}.old"
            if old.exists():
                shutil.rmtree(old)
            if lib_dir.exists():
                os.replace(lib_dir, old)
                moved.append((lib_dir, old))
            os.replace(stage, lib_dir)
    except BaseException:
        for lib_dir, old in reversed(moved):
            if lib_dir.exists():
                shutil.rmtree(lib_dir)
            os.replace(old, lib_dir)
        raise
    for _, old in moved:
        shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def check(repo_root: Path, clones: dict[str, Path] | None = None) -> list[str]:
    """Problems found, empty when the vendor tree matches its manifests and pins. Offline unless *clones* is given."""
    vendor = repo_root / VENDOR_RELDIR
    problems: list[str] = []
    pins_path = vendor / PINS_NAME
    pins: dict[str, str] = {}
    if pins_path.is_file():
        pins = read_pins(pins_path)
    else:
        problems.append(f"{VENDOR_RELDIR}/{PINS_NAME} is missing (no pin to hold the vendored files to)")
    for name in sorted(LIBRARIES):
        problems += _check_library(vendor, name, pins, (clones or {}).get(name))
    return problems


def _check_library(vendor: Path, name: str, pins: dict[str, str], clone: Path | None) -> list[str]:
    lib_dir = vendor / name
    manifest_path = lib_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{name}: manifest {VENDOR_RELDIR}/{name}/{MANIFEST_NAME} is missing"]
    try:
        manifest = read_manifest(manifest_path)
    except VendorError as exc:
        return [f"{name}: {exc}"]
    problems: list[str] = []
    if not _SHA.match(manifest.revision):
        problems.append(f"{name}: manifest revision {manifest.revision!r} is not a full 40-hex commit id")
    if name not in pins:
        problems.append(f"{name}: no pin in {PINS_NAME}")
    elif pins[name] != manifest.revision:
        problems.append(f"{name}: manifest revision {manifest.revision} differs from the pin {pins[name]} in {PINS_NAME}")
    if not manifest.files:
        problems.append(f"{name}: the manifest lists no files (a vendored library with nothing in it)")
    if LICENSE_NAME not in manifest.files:
        problems.append(f"{name}: the manifest does not list {LICENSE_NAME}; the licence notice must ship with the files")
    on_disk = {
        p.relative_to(lib_dir).as_posix(): p for p in lib_dir.rglob("*") if p.is_file() and p.name != MANIFEST_NAME
    }
    for rel, expected in sorted(manifest.files.items()):
        f = on_disk.get(rel)
        if f is None:
            problems.append(f"{name}: {rel} is missing from the vendor tree")
        elif (actual := blob_id(f.read_bytes())) != expected:
            problems.append(f"{name}: {rel} has blob id {actual}, the manifest says {expected}")
    for rel in sorted(set(on_disk) - set(manifest.files)):
        problems.append(f"{name}: {rel} is in the vendor tree but not in the manifest")
    if clone is not None:
        try:
            sha = resolve_revision(clone, manifest.revision)
            tree = library_tree(LIBRARIES[name], clone, sha)
        except VendorError as exc:
            problems.append(f"{name}: clone check: {exc}")
        else:
            for rel in sorted(set(tree) | set(manifest.files)):
                if tree.get(rel) != manifest.files.get(rel):
                    problems.append(
                        f"{name}: {rel}: the clone at {sha} has blob {tree.get(rel)}, the manifest says {manifest.files.get(rel)}"
                    )
    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_assignments(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not value:
            raise SystemExit(f"{what} takes LIB=VALUE, got {item!r}")
        if name not in LIBRARIES:
            raise SystemExit(f"unknown library {name!r}; known: {', '.join(sorted(LIBRARIES))}")
        out[name] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="compare the vendored files with the manifests and pins")
    mode.add_argument(
        "--update",
        nargs="*",
        metavar="LIB[=REV]",
        help="regenerate from a local clone; no argument re-reads both libraries at their recorded pins",
    )
    parser.add_argument("--clone", action="append", default=[], metavar="LIB=PATH", help="clone to read (default ~/git/<LIB>)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    clones = {n: Path(p).expanduser() for n, p in _parse_assignments(args.clone, "--clone").items()}
    root: Path = args.repo_root

    if args.check:
        problems = check(root, clones=clones or None)
        if problems:
            for p in problems:
                sys.stderr.write(f"FAIL {p}\n")
            return 1
        vendor = root / VENDOR_RELDIR
        for name in sorted(LIBRARIES):
            m = read_manifest(vendor / name / MANIFEST_NAME)
            sys.stdout.write(f"ok {name} {m.revision} {len(m.files)} files\n")
        return 0

    requested: dict[str, str] = {}
    for item in args.update:
        name, sep, rev = item.partition("=")
        if name not in LIBRARIES:
            raise SystemExit(f"unknown library {name!r}; known: {', '.join(sorted(LIBRARIES))}")
        requested[name] = rev if sep else ""
    pins_path = root / VENDOR_RELDIR / PINS_NAME
    recorded = read_pins(pins_path) if pins_path.is_file() else {}
    wanted = requested or {name: "" for name in LIBRARIES}
    revisions: dict[str, str] = {}
    for name, rev in wanted.items():
        rev = rev or recorded.get(name, "")
        if not rev:
            raise SystemExit(f"{name}: no recorded pin; pass {name}=<revision>")
        revisions[name] = rev
    try:
        done = update(root, revisions, clones)
    except VendorError as exc:
        sys.stderr.write(f"FAIL {exc}\n")
        return 1
    for name, m in sorted(done.items()):
        sys.stdout.write(f"updated {name} revision {m.revision} {len(m.files)} files\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
