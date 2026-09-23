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
That is the whole mechanism this file tests. The supervisor passes the flag on
EVERY launch, stating ``true`` or ``false`` explicitly rather than passing it
only on the opt-out: the baked default exists only in the native image, so a
``NEXUS_SERVICE_JAR`` launch left implicit got the opposite socket family.
"""

from __future__ import annotations

import inspect

import pytest

from nexus.daemon import storage_service_daemon as mod
from nexus.daemon.storage_service_daemon import (
    IPV4_ONLY_ENV,
    StorageServiceStartError,
    _ipv4_only_disabled,
)


def test_unset_keeps_the_baked_default() -> None:
    """The appliance's case, and the common one: nothing is passed."""
    assert _ipv4_only_disabled(None) is False
    assert _ipv4_only_disabled("") is False
    assert _ipv4_only_disabled("   ") is False


def test_explicit_yes_keeps_the_baked_default() -> None:
    for v in ("1", "true", "TRUE", "yes", " 1 "):
        assert _ipv4_only_disabled(v) is False, v


def test_explicit_no_disables_it() -> None:
    """A deployment that turns out to need a dual-stack listener opts out.

    Deliberately NOT "a deployment that needs IPv6 outbound (Voyage,
    EgressProxy)", which is what this said until the citation was checked:
    EgressProxy.java:34 records that the cloud egress proxy is IPv4. No
    deployment this repo knows of needs the opt-out, which is precisely why
    IPv4-only is the default.
    """
    for v in ("0", "false", "FALSE", "no", " no "):
        assert _ipv4_only_disabled(v) is True, v


def test_unrecognised_value_raises_rather_than_guessing() -> None:
    """A typo must not silently select a networking posture.

    Both reviewers of nexus-ijue9.7 raised this independently against an
    earlier draft that read anything unrecognised as "not requested". The
    symptom of guessing wrong on the appliance is a service that boots
    healthy and is unreachable from Windows, with no signal anywhere -- which
    is the exact defect the flag exists to prevent.

    The TYPE is pinned, not just the raise. Refusing is only half of it: the
    round-3 review found this raising a bare ValueError while every sibling
    env-validation in the module raises StorageServiceStartError, which is
    what `nx daemon service start` and `nx init` catch to print `Error: ...`
    and exit 2. A refusal that reaches the user as a traceback is not the
    legible refusal this check exists to give.
    """
    for junk in ("on", "off", "2", "ipv4", "yes please", "TRUEish"):
        with pytest.raises(StorageServiceStartError, match=IPV4_ONLY_ENV):
            _ipv4_only_disabled(junk)


def test_the_refusal_type_is_the_one_the_cli_catches() -> None:
    """Non-vacuity for the type pin above.

    `pytest.raises(StorageServiceStartError)` would also be satisfied if
    that name were an alias of ValueError or of Exception, in which case the
    pin would hold while the CLI contract was broken. This asserts the thing
    that actually matters: the CLI's own except clause catches it.
    """
    assert issubclass(StorageServiceStartError, Exception)
    assert not issubclass(ValueError, StorageServiceStartError), (
        "if ValueError were a StorageServiceStartError the pin above would "
        "pass on the pre-fix code"
    )
    try:
        _ipv4_only_disabled("ture")
    except StorageServiceStartError:
        pass
    else:  # pragma: no cover - the test above already covers the no-raise case
        raise AssertionError("expected a refusal")


def test_the_env_var_name_is_the_one_the_supervisor_reads() -> None:
    """Non-vacuity: the tests above are worthless if they exercise a constant
    the argv construction does not use."""
    source = inspect.getsource(mod.StorageServiceSupervisor._spawn_service)
    assert "_ipv4_only_disabled" in source, (
        "the spawn path no longer calls _ipv4_only_disabled; this file tests a "
        "helper nothing uses"
    )
    assert "-Djava.net.preferIPv4Stack=" in source, (
        "the spawn path no longer passes the flag; the gate is inert"
    )


def test_both_launch_kinds_get_an_explicit_value() -> None:
    """The JVM path has no baked default, so the flag must be explicit.

    The Feature bakes the property into the NATIVE image only. A
    NEXUS_SERVICE_JAR launch is a plain JVM with no Feature, so relying on the
    baked default gave the two launch kinds different socket families. The
    supervisor therefore states the value for both, and the argv says what the
    process will actually do.
    """
    source = inspect.getsource(mod.StorageServiceSupervisor._spawn_service)
    # The append must NOT sit inside the jar/native branch.
    assert 'argv.append(f"-Djava.net.preferIPv4Stack={_ipv4}")' in source, (
        "the flag is no longer appended unconditionally for both launch kinds"
    )
    branch = source.index('if self._launch_kind == "jar":')
    flag = source.index("-Djava.net.preferIPv4Stack")
    assert flag > branch, (
        "the flag is appended before the launch-kind branch; it must come "
        "after so it applies to both argv shapes"
    )
