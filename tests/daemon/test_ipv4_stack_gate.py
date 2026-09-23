# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""The runtime gate over the image's baked IPv4-only stack (nexus-ijue9.7).

RDR-218 Gap 2. The native image bakes ``java.net.preferIPv4Stack=true`` via
``Ipv4StackFeature``, because binding ``127.0.0.1`` on Linux otherwise yields
a dual-stack AF_INET6 listener on ``::ffff:127.0.0.1`` that WSL2's localhost
relay will not forward.

MEASURED on GraalVM 25 community, linux/amd64, family read from
``/proc/net/tcp*`` -- every other candidate is inert:

    System.setProperty in main()        -> ::ffff:127.0.0.1   INERT
    native-image -D at build time       -> ::ffff:127.0.0.1   INERT
    runtime -D on the binary            -> 127.0.0.1          works
    RuntimeSystemProperties Feature     -> 127.0.0.1          works

The baked value is a DEFAULT, not a lock: a runtime
``-Djava.net.preferIPv4Stack=false`` overrides it, measured both directions.
That is the whole mechanism this file tests -- the supervisor passes that flag
when, and only when, the deployment explicitly opts out.
"""

from __future__ import annotations

import inspect

import pytest

from nexus.daemon import storage_service_daemon as mod
from nexus.daemon.storage_service_daemon import IPV4_ONLY_ENV, _ipv4_only_disabled


def test_unset_keeps_the_baked_default() -> None:
    """The appliance's case, and the common one: nothing is passed."""
    assert _ipv4_only_disabled(None) is False
    assert _ipv4_only_disabled("") is False
    assert _ipv4_only_disabled("   ") is False


def test_explicit_yes_keeps_the_baked_default() -> None:
    for v in ("1", "true", "TRUE", "yes", " 1 "):
        assert _ipv4_only_disabled(v) is False, v


def test_explicit_no_disables_it() -> None:
    """A deployment that needs IPv6 outbound opts out."""
    for v in ("0", "false", "FALSE", "no", " no "):
        assert _ipv4_only_disabled(v) is True, v


def test_unrecognised_value_raises_rather_than_guessing() -> None:
    """A typo must not silently select a networking posture.

    Both reviewers of nexus-ijue9.7 raised this independently against an
    earlier draft that read anything unrecognised as "not requested". The
    symptom of guessing wrong on the appliance is a service that boots
    healthy and is unreachable from Windows, with no signal anywhere -- which
    is the exact defect the flag exists to prevent.
    """
    for junk in ("on", "off", "2", "ipv4", "yes please", "TRUEish"):
        with pytest.raises(ValueError, match=IPV4_ONLY_ENV):
            _ipv4_only_disabled(junk)


def test_the_env_var_name_is_the_one_the_supervisor_reads() -> None:
    """Non-vacuity: the tests above are worthless if they exercise a constant
    the argv construction does not use."""
    source = inspect.getsource(mod.StorageServiceSupervisor._spawn_service)
    assert "_ipv4_only_disabled" in source, (
        "the spawn path no longer calls _ipv4_only_disabled; this file tests a "
        "helper nothing uses"
    )
    assert "-Djava.net.preferIPv4Stack=false" in source, (
        "the spawn path no longer passes the override flag; the gate is inert"
    )
