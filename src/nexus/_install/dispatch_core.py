# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Build and place the Windows dispatchers (nexus-f9bgu.47).

``generation_core.ensure_dispatchers`` decides WHICH dispatchers a generation
needs and runs ``uv``; this module holds the parts that need nothing else: the
wheel that makes one dispatcher launcher (:func:`dispatch_wheel`, carrying a copy
of ``_nx_dispatch.py``), the copy that puts a built launcher into ``<tools>\\bin``
without ever writing a running file (:func:`put_dispatcher`), and the sweep of
what that leaves behind (:func:`sweep_litter`). Split out of ``generation_core``
to keep it under its size cap.

Standard library only and importing nothing from nexus, like its siblings
(``tests/test_install_generation_core_is_bootstrap_safe.py`` pins the set).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

#: The dispatcher's source, beside this file.
DISPATCH_SOURCE = "_nx_dispatch.py"


def dispatch_source() -> str:
    """The text of ``_nx_dispatch.py``."""
    return (Path(__file__).resolve().parent / DISPATCH_SOURCE).read_text(encoding="utf-8")


def dispatch_wheel(directory: Path, name: str, version: str) -> Path:
    """Write a wheel in *directory* that installs the console script *name* as a dispatcher.

    One distribution per name, each carrying its own copy of the dispatcher
    module, so a later release that adds a launcher installs one new wheel and
    touches no existing executable. Built with ``zipfile`` and fixed timestamps,
    so the same inputs make the same bytes.
    """
    module = "nxdispatch_" + re.sub(r"[^A-Za-z0-9]+", "_", name)
    dist = f"{module}-{version}"
    files = {
        f"{module}.py": dispatch_source(),
        f"{dist}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {module}\nVersion: {version}\n",
        f"{dist}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: nexus\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist}.dist-info/entry_points.txt": f"[console_scripts]\n{name} = {module}:console_main\n",
    }
    record = []
    for path, text in files.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        record.append(f"{path},sha256={digest},{len(data)}")
    record.append(f"{dist}.dist-info/RECORD,,")
    files[f"{dist}.dist-info/RECORD"] = "\n".join(record) + "\n"
    wheel = directory / f"{dist}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return wheel


def put_dispatcher(source: Path, destination: Path) -> None:
    """Copy the dispatcher *source* to *destination* without ever writing a running file.

    The copy goes to a sibling, then ``os.replace`` puts it in place. When
    *destination* is running (only a refresh of an existing dispatcher can meet
    that) the replace is refused, and the running file is renamed aside and the
    sibling renamed in; a dispatcher, unlike a generation launcher, can be (measured
    2026-11-28). The aside is swept by the next :func:`sweep_litter`.
    """
    tag = os.getpid()
    new = destination.with_name(f".{destination.name}.new.{tag}")
    shutil.copyfile(source, new)
    try:
        try:
            os.replace(new, destination)
            return
        except PermissionError:
            if not os.path.lexists(destination):
                raise
        os.rename(destination, destination.with_name(f".{destination.name}.old.{tag}"))
        os.rename(new, destination)
    finally:
        if os.path.lexists(new):
            try:
                os.unlink(new)
            except OSError:
                pass


def sweep_litter(directory: Path) -> None:
    """Remove the ``.<name>.old.<pid>`` files of earlier refreshes; one still running stays for the next call."""
    for litter in directory.glob(".*.old.*"):
        try:
            litter.unlink()
        except OSError:
            pass


#: A pointer file is a line and a path; anything bigger at a pointer name is somebody's file.
TORN_MAX_BYTES = 4096


def is_torn_pointer(path: Path) -> bool:
    """Whether *path* is a small regular file where a pointer should be but is not a valid one.

    ``write_pointer`` fsyncs before the replace, but a restored backup, an antivirus rewrite or a
    crash on a disk that lies can still leave an empty or headerless ``current``. Nothing there is
    worth keeping, and refusing to replace it left no way back (``nx`` itself cannot run). A
    directory, a link and a file bigger than a pointer are never torn. The caller has already
    asked ``is_link``, so a valid pointer does not reach here.
    """
    try:
        if path.is_symlink() or os.path.isjunction(path) or not path.is_file():
            return False
        return path.stat().st_size <= TORN_MAX_BYTES
    except OSError:
        return False


def sweep_staging(root: Path, older_than_s: float = 3600.0) -> None:
    """Remove ``.dispatch-wheels.<pid>`` directories a crashed install left in *root*."""
    import time  # noqa: PLC0415 -- the one use

    for staging in root.glob(".dispatch-wheels.*"):
        try:
            if time.time() - staging.stat().st_mtime > older_than_s:
                shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass

