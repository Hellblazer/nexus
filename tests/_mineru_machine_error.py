# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MineruMachineResourceError the production raise site builds (nexus-tr1rj.13).

``PDFExtractor._mineru_after_server_error`` raises it with the message ``semaphores_exhausted()`` returns
when the probe finds no POSIX named semaphore can be created: ``semaphore_remedy(platform)``, the platform's
full remedy text, about 400 characters. A test that stubs a short message ("no semaphores left") cannot see
how that text lands on a ``Stopped:`` line, so tests build the exception from the same text.

The tests' conftest replaces ``semaphores_exhausted`` with a stub that answers ``None`` (no test may probe the
machine), so this helper builds the message with ``semaphore_remedy``, which that stub leaves alone.
"""
from __future__ import annotations

from nexus._mineru_health import MineruMachineResourceError, semaphore_remedy


def real_machine_error(platform: str = "darwin") -> MineruMachineResourceError:
    """The exception, with the real message for *platform* (``darwin`` or ``linux``)."""
    return MineruMachineResourceError(semaphore_remedy(platform))
