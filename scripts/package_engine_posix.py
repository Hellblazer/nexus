#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Package the Linux or macOS engine binary as ``nexus-service-<arch>.txz``.

Run by engine-service-release.yml's build-publish leg, after codesign and
notarization (mac-arm64) and before cosign, so the archive holds the exact
bytes the single-file asset ships. Stdlib only: it runs on the GitHub-hosted
linux runners and on hellmini with whatever ``python3`` is there.

    package --binary PATH --arch ARCH --out-dir DIR
        DIR/nexus-service-ARCH.txz and DIR/nexus-service-ARCH.txz.sha256
        (``<hex>  <name>``, the sidecar format the client parses).
    verify --archive PATH
        Exit 1 and one line per problem unless the archive is the layout the
        client reads: exactly one regular member, ``nexus-service``, non-empty.

The layout is what ``nexus.daemon.binary_install._place_engine_archive`` reads:
flat, one member ``nexus-service``, mode 0755, mtime 0, owner 0:0 with no
names, so the archive's bytes depend on the binary alone. Compression is
``lzma`` preset 6, the same as the Windows engine archive
(scripts/windows_engine_release.py) and the PG bundles; measured on a 163 MB
mac-arm64 engine at 41 MB.
"""
from __future__ import annotations

import argparse
import hashlib
import lzma
import sys
import tarfile
from pathlib import Path

#: The one member, and the name the client installs it under.
MEMBER = "nexus-service"
#: Same matrix tokens as engine-service-release.yml's build-publish legs.
ARCHES: tuple[str, ...] = ("linux-amd64", "linux-arm64", "mac-arm64")
#: A native image is tens of MB; the workflow's stage step applies the same floor.
MIN_BINARY_BYTES = 20_000_000
PRESET = 6
_BLOCK = 1 << 20


class PackageError(Exception):
    """The binary or the archive is not what the release may ship."""


def asset_name(arch: str) -> str:
    if arch not in ARCHES:
        raise PackageError(f"unknown arch {arch!r}; expected one of {', '.join(ARCHES)}")
    return f"nexus-service-{arch}.txz"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def package(binary: Path, arch: str, out_dir: Path, *, min_bytes: int = MIN_BINARY_BYTES) -> Path:
    name = asset_name(arch)
    if not binary.is_file():
        raise PackageError(f"engine binary {binary} is absent")
    size = binary.stat().st_size
    if size < min_bytes:
        raise PackageError(f"engine binary is suspiciously small ({size} bytes < {min_bytes})")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / name
    info = tarfile.TarInfo(MEMBER)
    info.size = size
    info.mode = 0o755
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with lzma.open(archive, "wb", preset=PRESET) as xz, tarfile.open(
        fileobj=xz, mode="w", format=tarfile.PAX_FORMAT,
    ) as tf, binary.open("rb") as fh:
        tf.addfile(info, fh)
    problems = verify(archive, expected_sha256=_sha256(binary))
    if problems:
        archive.unlink(missing_ok=True)
        raise PackageError("packaged archive failed its own check: " + "; ".join(problems))
    (out_dir / f"{name}.sha256").write_text(f"{_sha256(archive)}  {name}\n")
    return archive


def verify(archive: Path, *, expected_sha256: str | None = None) -> list[str]:
    """Problems with *archive* (empty list = the client's layout, intact)."""
    problems: list[str] = []
    members: list[str] = []
    try:
        with tarfile.open(archive, "r:xz") as tf:
            for member in tf:
                members.append(member.name)
                if member.name != MEMBER:
                    problems.append(f"unexpected member {member.name!r}")
                    continue
                if not member.isreg():
                    problems.append(f"{MEMBER} is not a regular file")
                    continue
                if member.size == 0:
                    problems.append(f"{MEMBER} is empty")
                    continue
                if expected_sha256 is not None:
                    src = tf.extractfile(member)
                    h = hashlib.sha256()
                    assert src is not None  # isreg() members always have data
                    for block in iter(lambda: src.read(_BLOCK), b""):
                        h.update(block)
                    if h.hexdigest() != expected_sha256:
                        problems.append(f"{MEMBER} in the archive does not match the binary")
    except (tarfile.TarError, lzma.LZMAError, EOFError, OSError) as exc:
        return [f"archive {archive} could not be read: {exc}"]
    if members.count(MEMBER) != 1:
        problems.append(f"expected exactly one {MEMBER} member, found {members.count(MEMBER)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("package")
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--arch", required=True, choices=ARCHES)
    p.add_argument("--out-dir", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "package":
            archive = package(args.binary, args.arch, args.out_dir)
            print(f"{archive}: {archive.stat().st_size} bytes "
                  f"(binary {args.binary.stat().st_size} bytes)")
            return 0
        problems = verify(args.archive)
    except PackageError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for line in problems:
        print(f"FAIL: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
