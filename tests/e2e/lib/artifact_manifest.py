# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Read and verify a build-artifacts manifest (nexus-mfage).

``verify <artifacts-dir> <repo-root>`` recomputes the checkout's tree
identity and compares it to the manifest's. A match prints the manifest as
JSON on stdout and exits 0. Anything else exits 3 with the reason on stderr:
a missing manifest, a missing or checksum-mismatched artifact file, or a tree
that is not the one the artifacts were built from. The consumer must refuse on
exit 3 — reuse is proven by identity, never by age (nexus-mbeke).

``show <artifacts-dir>`` prints the manifest without verifying.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tree_identity import tree_identity  # noqa: E402

MANIFEST = "manifest.json"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(artifacts_dir: str) -> dict:
    path = os.path.join(artifacts_dir, MANIFEST)
    if not os.path.isfile(path):
        sys.stderr.write(f"ARTIFACTS REFUSED (exit 3): no {MANIFEST} under {artifacts_dir}\n")
        raise SystemExit(3)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def verify(artifacts_dir: str, repo_root: str) -> dict:
    manifest = load(artifacts_dir)
    problems: list[str] = []
    for name, entry in manifest["artifacts"].items():
        full = os.path.join(artifacts_dir, entry["path"])
        if not os.path.isfile(full):
            problems.append(f"{name}: missing file {entry['path']}")
            continue
        got = _sha256(full)
        if got != entry["sha256"]:
            problems.append(f"{name}: sha256 {got} != manifest {entry['sha256']}")
    current = tree_identity(repo_root)
    if current["tree_hash"] != manifest["tree_hash"]:
        problems.append(
            "tree identity mismatch: artifacts were built from tree "
            f"{manifest['tree_hash'][:12]} (HEAD {manifest['head_sha'][:12]}, "
            f"dirty={manifest['dirty']}), this checkout is tree "
            f"{current['tree_hash'][:12]} (HEAD {current['head_sha'][:12]}, "
            f"dirty={current['dirty']}). Rebuild: tests/e2e/migration-rehearsal/"
            f"build-artifacts.sh {artifacts_dir}"
        )
    if problems:
        sys.stderr.write("ARTIFACTS REFUSED (exit 3):\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        raise SystemExit(3)
    return manifest


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["verify", artifacts_dir, repo_root]:
            print(json.dumps(verify(artifacts_dir, repo_root), sort_keys=True))
        case ["show", artifacts_dir]:
            print(json.dumps(load(artifacts_dir), sort_keys=True))
        case _:
            raise SystemExit("usage: artifact_manifest.py verify <dir> <repo-root> | show <dir>")
