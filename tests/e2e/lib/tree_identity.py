# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Working-tree identity for the build-once artifact manifest (nexus-mfage).

A gate may reuse a prebuilt artifact only against a tree PROVEN identical to
the one it was built from, never on age (nexus-mbeke: a stale binary satisfies
a shakeout silently). ``tree_hash`` is a sha256 over every tracked and
untracked-not-ignored entry in the checkout: relative path, git mode (so the
executable bit and symlink-ness count) and blob hash, in path order. HEAD's
sha alone is not identity — a dirty tree builds different bytes from the same
HEAD — so the dirty flag is reported but the hash is what the consumer
compares.

The blob hash is of the WORKING-TREE content, never the index blob. The first
cut read tracked entries from ``git ls-files -s``, whose blob is the index's
(staged) content, so an unstaged edit to a tracked file changed what a build
produced without changing the identity at all; the 7.49.0 battery ran with all
seven version bumps and the engine floor unstaged and reported the identity of
the commit underneath (2026-09-16). Every regular file is now content-hashed
from disk (``git hash-object --stdin-paths``), a symlink as git stores it (the
link target), and the mode comes from the file on disk.

Excluded: ``service/src/main/resources/META-INF/nexus/release.properties``.
Every build step stamps that file transiently (release_version, build_ref)
and restores its bytes afterwards; a leg that reads the tree while a sibling
holds the stamp would see a spurious mismatch. Its stamped values travel in
the manifest instead (``build_ref``, ``release_version``), which is the
stronger statement: they are asserted against ``/version`` at run time.

Usage: ``python3 tree_identity.py <repo-root>`` prints one JSON object.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

STAMP_FILE = "service/src/main/resources/META-INF/nexus/release.properties"


def _git(root: str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", root, *args], check=True, capture_output=True
    ).stdout


def _hash_blob(root: str, content: bytes) -> bytes:
    """git's blob id for *content* (what the index would store for a symlink)."""
    return subprocess.run(
        ["git", "-C", root, "hash-object", "--stdin"],
        input=content, check=True, capture_output=True,
    ).stdout.strip()


def tree_identity(root: str) -> dict[str, object]:
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    # Tracked entries: the PATH and symlink-ness come from the index (a
    # symlink is hashed as git stores it, the link target, never followed,
    # so a symlink-to-directory is identity too instead of being dropped),
    # but the CONTENT and the executable bit come from the working tree,
    # because an unstaged edit or chmod builds different bytes from the
    # same index (code-review-expert and substantive-critic findings on
    # nexus-mfage fix B, 2026-09-06; the index-blob defect found 2026-09-16).
    regular: list[bytes] = []
    for line in _git(root, "ls-files", "-s", "-z").split(b"\0"):
        if not line:
            continue
        meta, path = line.split(b"\t", 1)
        mode, _blob, _stage = meta.split(b" ")
        rel = path.decode("utf-8", "surrogateescape")
        full = os.path.join(root, rel)
        if rel == STAMP_FILE or not os.path.lexists(full):
            continue  # stamp file excluded; a tracked-but-deleted path is absent
        if os.path.islink(full):
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            entries[path] = (b"120000", _hash_blob(root, target))
        elif os.path.isfile(full):
            regular.append(path)
        # a tracked path that is now a directory (submodule, replaced) is dropped,
        # as the first cut dropped it via lexists on a gitlink.
    # Untracked, not ignored: same treatment.
    for p in _git(root, "ls-files", "-o", "--exclude-standard", "-z").split(b"\0"):
        if p and p not in entries and p not in regular \
                and os.path.isfile(os.path.join(root, p.decode("utf-8", "surrogateescape"))):
            regular.append(p)
    if regular:
        hashes = subprocess.run(
            ["git", "-C", root, "hash-object", "--stdin-paths"],
            input=b"\n".join(regular) + b"\n", check=True, capture_output=True,
        ).stdout.split()
        if len(hashes) != len(regular):
            raise SystemExit(f"hash-object returned {len(hashes)} hashes for {len(regular)} paths")
        for path, blob in zip(regular, hashes):
            st = os.stat(os.path.join(root, path.decode("utf-8", "surrogateescape")))
            entries[path] = (b"100755" if st.st_mode & 0o111 else b"100644", blob)
    digest = hashlib.sha256()
    for path in sorted(entries):
        mode, blob = entries[path]
        digest.update(path)
        digest.update(b"\0")
        digest.update(mode)
        digest.update(b" ")
        digest.update(blob)
        digest.update(b"\n")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all").strip())
    return {
        "head_sha": head,
        "dirty": dirty,
        "tree_hash": digest.hexdigest(),
        "file_count": len(entries),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tree_identity.py <repo-root>")
    print(json.dumps(tree_identity(sys.argv[1]), sort_keys=True))
