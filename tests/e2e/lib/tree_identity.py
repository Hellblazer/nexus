# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Working-tree identity for the build-once artifact manifest (nexus-mfage).

A gate may reuse a prebuilt artifact only against a tree PROVEN identical to
the one it was built from, never on age (nexus-mbeke: a stale binary satisfies
a shakeout silently). ``tree_hash`` is a sha256 over every tracked and
untracked-not-ignored file in the checkout: relative path plus git blob hash,
in path order. HEAD's sha alone is not identity — a dirty tree builds
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
    listed = _git(root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
    paths = sorted(
        p for p in listed
        if p and p.decode("utf-8", "surrogateescape") != STAMP_FILE
        and os.path.isfile(os.path.join(root, p.decode("utf-8", "surrogateescape")))
    )
    hashes = subprocess.run(
        ["git", "-C", root, "hash-object", "--stdin-paths"],
        input=b"\n".join(paths) + b"\n", check=True, capture_output=True,
    ).stdout.split()
    if len(hashes) != len(paths):
        raise SystemExit(f"hash-object returned {len(hashes)} hashes for {len(paths)} paths")
    digest = hashlib.sha256()
    for path, blob in zip(paths, hashes):
        digest.update(path)
        digest.update(b"\0")
        digest.update(blob)
        digest.update(b"\n")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all").strip())
    return {
        "head_sha": head,
        "dirty": dirty,
        "tree_hash": digest.hexdigest(),
        "file_count": len(paths),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tree_identity.py <repo-root>")
    print(json.dumps(tree_identity(sys.argv[1]), sort_keys=True))
