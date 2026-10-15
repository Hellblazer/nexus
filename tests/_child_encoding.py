# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""The encoding a test reads a Python child's pipes with (nexus-ikoqj).

A bare Python child writes its stdout and stderr in ITS locale encoding unless
``PYTHONIOENCODING`` or ``PYTHONUTF8`` says otherwise, and reads its stdin the
same way. The test and the child share that locale, so reading the child with
the locale's preferred encoding is the pair that matches whatever the host is:
UTF-8 on macOS and Linux, cp1252 on an English Windows install. That is what
the ``text=True`` default did; naming it explicitly keeps the pairing, says why,
and satisfies ``tests/test_subprocess_text_encoding_lint.py``.

Children that are not Python (``git``, ``uv``, ``psql``) write UTF-8 and are
read with ``encoding="utf-8"`` at the call. A test that sets
``PYTHONIOENCODING`` or ``PYTHONUTF8`` in the child's env, or that drives a
non-UTF-8 locale on purpose, names its own encoding instead of using this one.
"""

from __future__ import annotations

import locale

#: ``encoding=`` for a subprocess whose child is a bare Python process.
PYTHON_CHILD_ENCODING: str = locale.getpreferredencoding(False)

#: ``encoding=`` for a subprocess whose child is a native Windows tool (``cmd``,
#: ``schtasks``). Those write the console or ANSI code page, which the locale's
#: preferred encoding names, so UTF-8 would garble them. Same value as
#: :data:`PYTHON_CHILD_ENCODING` today; a separate name because the reason differs.
NATIVE_TOOL_ENCODING: str = locale.getpreferredencoding(False)
