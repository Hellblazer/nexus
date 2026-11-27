# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``nexus.config.get_credential`` double for tests that stub every credential at once.

A bare ``return_value="fake-key"`` answered ``service_url`` as ``"fake-key"`` and let the
saved token ride along to it. The bearer rules (nexus-ec37o) send a stored credential only
to the origin it was issued for, over https or loopback, so that pairing is refused. The
pair here is the setup the design keeps working: a saved ``service_token`` and the saved
``service_url`` it was issued with, a loopback origin nothing listens on, so a request that
does reach it fails at once.

A test patches both: ``get_credential`` with :func:`fake_credential` (or
:func:`fake_credentials` for another value) and ``nexus.config.persisted_credentials``
with :data:`SAVED_FAKE_CREDENTIALS`.
"""
from __future__ import annotations

from collections.abc import Callable

FAKE_SERVICE_URL = "http://127.0.0.1:1"
SAVED_FAKE_CREDENTIALS = {"service_url": FAKE_SERVICE_URL}


def fake_credentials(value: str = "fake-key") -> Callable[[str], str]:
    """A ``get_credential`` double answering *value* for every credential but ``service_url``."""
    return lambda name: FAKE_SERVICE_URL if name == "service_url" else value


fake_credential = fake_credentials()
