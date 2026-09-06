# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Working-tree identity for the build-once artifact manifest (nexus-mfage).

A gate may reuse a prebuilt artifact only against a tree PROVEN identical to
the one it was built from, never on age (nexus-mbeke: a stale binary satisfies
a shakeout silently). ``tree_hash`` is a sha256 over every tracked and
untracked-not-ignored entry in the checkout: relative path, git mode (so the
executable bit and symlink-ness count) and blob hash, in path order. HEAD's sha alone is not identity — a dirty tree builds
different bytes from the same HEAD — so the dirty flag is reported but the
hash is what the consumer compares.

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


def tree_identity(root: str) -> dict[str, object]:
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    # Tracked entries straight from the index: mode (100644 / 100755 /
    # 120000 symlink) and blob. A symlink is hashed as git stores it (the
    # link target), never followed, so a symlink-to-directory is identity
    # too instead of being dropped; the executable bit is identity because
    # a wheel or a shell gate ships it (code-review-expert and
    # substantive-critic findings on nexus-mfage fix B, 2026-09-06).
    for line in _git(root, "ls-files", "-s", "-z").split(b"\0"):
        if not line:
            continue
        meta, path = line.split(b"\t", 1)
        mode, blob, _stage = meta.split(b" ")
        rel = path.decode("utf-8", "surrogateescape")
        if rel == STAMP_FILE or not os.path.lexists(os.path.join(root, rel)):
            continue  # stamp file excluded; a tracked-but-deleted path is absent
        entries[path] = (mode, blob)
    # Untracked, not ignored: content via hash-object, mode from the file.
    untracked = [
        p for p in _git(root, "ls-files", "-o", "--exclude-standard", "-z").split(b"\0")
        if p and p not in entries
        and os.path.isfile(os.path.join(root, p.decode("utf-8", "surrogateescape")))
    ]
    if untracked:
        hashes = subprocess.run(
            ["git", "-C", root, "hash-object", "--stdin-paths"],
            input=b"\n".join(untracked) + b"\n", check=True, capture_output=True,
        ).stdout.split()
        if len(hashes) != len(untracked):
            raise SystemExit(f"hash-object returned {len(hashes)} hashes for {len(untracked)} paths")
        for path, blob in zip(untracked, hashes):
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
