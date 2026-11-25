"""Daily install ping (nexus-h5olw).

The only signal that counts local-mode installs: once every 24 hours the MCP
server, on a background thread, POSTs a random install id plus the client
version, install mode, OS, arch, and Python minor to the managed service's
unauthenticated ``/v1/install-ping`` route. Nothing else is sent: no tenant,
no hostname, no paths, no collection names. The managed service derives and
stores a keyed fingerprint of the connecting address (HMAC-SHA256 under a
server-side key, truncated to 16 hex characters); the raw address is never
stored by the service (nexus-5zv4j).

Opt-out, default on. Any of these disables it:

* ``NX_NO_TELEMETRY=1`` (any non-empty value other than ``0``)
* running out of a dev checkout (see :func:`running_from_dev_checkout`)
* ``telemetry.enabled: false`` in ``~/.config/nexus/config.yml``
  (``nx telemetry off``)
* ``NX_TELEMETRY=0`` (or ``false``, ``no``, ``off``): an explicit opt-out
  spelled the other way round, reported as ``env NX_TELEMETRY=0``
* an automated environment (see :func:`automation_reason`): a pytest
  process (``PYTEST_CURRENT_TEST``), a lab-run grant (``LAB_CPUS`` /
  ``LAB_MEM_MB``, which ``lab-run`` exports), a CI vendor (``CI``,
  ``GITHUB_ACTIONS`` and the rest of :data:`CI_ENV_VARS`), or a container
  (``/.dockerenv``, ``/run/.containerenv``, a docker / containerd / kubepods
  / lxc entry in ``/proc/1/cgroup``, ``KUBERNETES_SERVICE_HOST``). A test or
  build run is not an install, and counting one taints the numbers. Container
  detection is best effort, per runtime.

``NX_TELEMETRY=1`` (any value other than empty, ``0``, ``false``, ``no`` or
``off``) is the explicit opt-in for someone who really does run nx in a
container: it overrides ONLY the automation skips. The opt-outs above all
still win over it -- ``NX_NO_TELEMETRY``, ``NX_TELEMETRY=0``, the config key,
and the dev checkout (a dev box is not an install whatever the environment
says). The decision order is therefore: ``NX_NO_TELEMETRY``, ``NX_TELEMETRY``
set to a falsy value, dev checkout, config key, ``NX_TELEMETRY`` opt-in,
automation, default on.

Every failure is swallowed at debug level. The ping never blocks startup,
never retries, and never raises into the caller; a ping the network drops is
simply a day uncounted.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import structlog

from nexus import config as _config
from nexus.db.managed_endpoint import DEFAULT_MANAGED_SERVICE_URL

_log = structlog.get_logger(__name__)

#: Env opt-out. ``0`` and empty mean "not set"; anything else disables.
NO_TELEMETRY_ENV = "NX_NO_TELEMETRY"
#: Explicit opt-in that overrides the automation skips (not the opt-outs).
#: Set to a falsy value (``0``, ``false``, ``no``, ``off``) it is an opt-out.
OPT_IN_ENV = "NX_TELEMETRY"
#: The hint ``nx telemetry status`` / ``on`` append when an automation signal is why the ping is off.
AUTOMATION_HINT = "set NX_TELEMETRY=1 to count this install"
#: Test / private-deployment override for the ping target.
PING_URL_ENV = "NX_INSTALL_PING_URL"
#: Route on the managed service (unauthenticated; see the engine's InstallPingHandler).
PING_PATH = "/v1/install-ping"
#: Minimum interval between pings.
PING_INTERVAL_S = 24 * 60 * 60
#: Wall-clock budget for the whole request.
PING_TIMEOUT_S = 2.0
#: Files under the nexus config dir.
INSTALL_ID_FILENAME = "install_id"
LAST_PING_FILENAME = "install_ping.last"


#: CI vendor variables; any one set to a truthy value marks a CI run. ``CI`` and
#: ``CONTINUOUS_INTEGRATION`` are the generic ones most vendors export; the rest
#: are vendor-specific for the ones that do not (Jenkins, TeamCity, Azure
#: Pipelines, CodeBuild, Bitbucket, Drone, AppVeyor ...). Values are matched with
#: :func:`_truthy`, so ``CI=false`` and ``CI=0`` do not count.
CI_ENV_VARS: tuple[str, ...] = (
    "CI",
    "CONTINUOUS_INTEGRATION",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "TRAVIS",
    "JENKINS_URL",
    "JENKINS_HOME",
    "HUDSON_URL",
    "TEAMCITY_VERSION",
    "TF_BUILD",
    "BITBUCKET_BUILD_NUMBER",
    "DRONE",
    "CODEBUILD_BUILD_ID",
    "APPVEYOR",
    "SEMAPHORE",
    "CIRRUS_CI",
    "BUDDY",
    "CF_BUILD_ID",
    "GO_PIPELINE_NAME",
)
#: A hand ``lab-run`` grant runs the command under ``env -i`` and exports only
#: these (plus PYTEST_XDIST_AUTO_NUM_WORKERS, LAB_JOB_KEY, NX_NO_TELEMETRY), so a
#: Nomad variable is never seen there; the repo detects a grant the same way
#: (tests/conftest.py, tests/_tmp_root.py). NOMAD_ALLOC_ID is deliberately NOT a
#: signal: it would mislabel any real Nomad workload.
LAB_RUN_ENV_VARS: tuple[str, ...] = ("LAB_CPUS", "LAB_MEM_MB")
#: pytest sets this for the duration of every test phase.
PYTEST_ENV = "PYTEST_CURRENT_TEST"
#: Set inside every Kubernetes pod.
KUBERNETES_ENV = "KUBERNETES_SERVICE_HOST"
#: Marker files, relative to the container root.
CONTAINER_MARKER_FILES: tuple[str, ...] = (".dockerenv", "run/.containerenv")
#: Names of container runtimes that show up in ``/proc/1/cgroup``. A pattern, not
#: a tuple of names: ``tests/test_nx_bare_exec_lint.py`` reads a literal whose
#: first element is a tool name ("docker") as a bare argv0 spawn.
CGROUP_CONTAINER_MARKERS = re.compile(r"docker|containerd|kubepods|lxc")
#: Where the container probes look; tests point it at a tmp tree.
_CONTAINER_ROOT: Path = Path("/")


def _truthy(value: str | None) -> bool:
    """Set to something other than empty / ``0`` / ``false`` / ``no`` / ``off``."""
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _automation_env() -> Mapping[str, str]:
    """The environment the automation signals are read from (tests replace it)."""
    return os.environ


def _container_reason(environ: Mapping[str, str], root: Path) -> str | None:
    if _truthy(environ.get(KUBERNETES_ENV)):
        return f"automation: container ({KUBERNETES_ENV})"
    for rel in CONTAINER_MARKER_FILES:
        try:
            if (root / rel).exists():
                return f"automation: container (/{rel})"
        except OSError:
            continue
    try:
        text = (root / "proc" / "1" / "cgroup").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # unreadable /proc (sandbox, Windows, a directory) is "not a container"
        return None
    found = CGROUP_CONTAINER_MARKERS.search(text.lower())
    if found:
        return f"automation: container (/proc/1/cgroup: {found.group(0)})"
    return None


def automation_reason(environ: Mapping[str, str] | None = None, root: Path | None = None) -> str | None:
    """Why this process looks automated (a test, lab-run, CI or container), or None.

    The returned string is the status ``source``. Never raises: an unreadable
    probe means "not detected". ``environ`` and ``root`` default to the real
    process environment and ``/``; they exist so tests do not depend on the host.
    """
    env = _automation_env() if environ is None else environ
    base = _CONTAINER_ROOT if root is None else root
    try:
        if _truthy(env.get(PYTEST_ENV)):
            return f"automation: pytest ({PYTEST_ENV})"
        for name in LAB_RUN_ENV_VARS:
            if _truthy(env.get(name)):
                return f"automation: lab-run ({name})"
        for name in CI_ENV_VARS:
            if _truthy(env.get(name)):
                return f"automation: CI ({name})"
        return _container_reason(env, base)
    except Exception as exc:  # noqa: BLE001 — a beacon check never raises into startup
        _log.debug("install_ping_automation_check_failed", error=str(exc))
        return None


def running_from_dev_checkout() -> bool:
    """Whether this process's ``nexus`` resolves from a development checkout.

    A development box is not an install, and counting one as a user is not a
    rounding error: MEASURED on this machine 2026-09-20, install_id
    2da3bd8a, mode cloud, darwin/arm64, pinging the production beacon daily
    for however long it had been doing so. The id is STABLE, which makes it
    harder to see rather than easier -- a burst of fresh ids looks like a
    harness, while one steady id looks exactly like a real user, because by
    the beacon's own definition it is one.

    The env opt-out alone cannot cover this. It is a per-harness allowlist:
    it catches the sandboxes somebody remembered to annotate and nothing
    else, and it needs a new entry every time a gate is added. This asks the
    general question once, in the one place the answer matters.

    The answer already exists in the tree and the ping simply never asked
    for it: ``nexus.db.service_endpoint.is_dev_checkout_process`` is the
    nexus-a2qhz guard that stops a dev session writing to production stores.
    Same question about the same process, one caller short.

    A FAILURE TO DECIDE COUNTS AS "NOT A CHECKOUT", deliberately. Falling the
    other way would silently disable telemetry for every real user the moment
    that import broke, and a beacon that quietly reports nothing is worse than
    one that over-reports by one machine -- the over-count is visible in the
    data, the under-count is not.

    It does NOT cover the container arms, and is not meant to: those run a
    ``uv tool``-installed WHEEL and are therefore not a checkout by
    construction, which is the same reason the nexus-a2qhz guard is inert
    inside the rehearsal containers. ``NX_NO_TELEMETRY`` remains required
    there, and the e2e-sandbox lint (tests/test_e2e_no_telemetry_lint.py)
    still enforces it deliberately even though the automation skips now
    exist: an ``env -i`` sandbox scrubs ``CI`` and ``PYTEST_CURRENT_TEST``,
    and container detection is heuristic. The guard is the general case; the
    env var is the exception.
    """
    try:
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — startup cost, and see above
            is_dev_checkout_process,
        )

        return bool(is_dev_checkout_process())
    except Exception as exc:  # noqa: BLE001 — see "a failure to decide" above
        _log.debug("install_ping_dev_checkout_check_failed", error=str(exc))
        return False


def _opted_in() -> bool:
    return _truthy(os.environ.get(OPT_IN_ENV))


def _opted_out_by_opt_in_var() -> bool:
    """``NX_TELEMETRY`` explicitly set (non-empty) to a falsy value: an opt-out."""
    raw = os.environ.get(OPT_IN_ENV, "").strip()
    return bool(raw) and not _truthy(raw)


def telemetry_enabled() -> bool:
    """Env opt-out, dev checkout, config key, then the opt-in / automation skips."""
    return bool(telemetry_status()["enabled"])


def telemetry_status() -> dict[str, Any]:
    """For ``nx telemetry status``: the decision and where it came from."""
    env = os.environ.get(NO_TELEMETRY_ENV, "").strip()
    if env and env != "0":
        return {"enabled": False, "source": f"env {NO_TELEMETRY_ENV}={env}"}
    if _opted_out_by_opt_in_var():
        return {"enabled": False, "source": f"env {OPT_IN_ENV}={os.environ.get(OPT_IN_ENV, '').strip()}"}
    if running_from_dev_checkout():
        return {"enabled": False, "source": "dev checkout"}
    if not _config_enabled():
        return {"enabled": False, "source": "config telemetry.enabled"}
    if _opted_in():
        return {"enabled": True, "source": f"env {OPT_IN_ENV}={os.environ.get(OPT_IN_ENV, '').strip()}"}
    reason = automation_reason()
    if reason is not None:
        return {"enabled": False, "source": reason}
    return {"enabled": True, "source": "default"}


def _config_enabled() -> bool:
    try:
        section = _config.load_config().get("telemetry")
    except Exception:  # noqa: BLE001 — a broken config never turns the ping on or off loudly
        return True
    if not isinstance(section, dict):
        return True
    value = section.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


def install_id(config_dir: Path | None = None) -> str:
    """The random per-install UUID, minted on first use and kept in the config dir."""
    path = (config_dir or _config.nexus_config_dir()) / INSTALL_ID_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
        uuid.UUID(existing)
        return existing
    except (OSError, ValueError):
        pass
    fresh = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh + "\n", encoding="utf-8")
    return fresh


def last_ping_at(config_dir: Path | None = None) -> float | None:
    path = (config_dir or _config.nexus_config_dir()) / LAST_PING_FILENAME
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def due(now: float, config_dir: Path | None = None) -> bool:
    last = last_ping_at(config_dir)
    return last is None or now - last >= PING_INTERVAL_S


def _mark_pinged(now: float, config_dir: Path | None) -> None:
    path = (config_dir or _config.nexus_config_dir()) / LAST_PING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{now:.0f}\n", encoding="utf-8")


def install_mode() -> str:
    """``cloud`` when a managed service_url is configured, else ``local``.

    Mirrors ``nx init``'s own dispatch (``get_credential("service_url")``),
    not ``is_local_mode()``, which is service_url-blind.
    """
    return "cloud" if _config.get_credential("service_url") else "local"


def client_version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — startup cost

    try:
        return version("conexus")
    except PackageNotFoundError:
        return "0.0.0"


def build_payload(config_dir: Path | None = None) -> dict[str, str]:
    """Exactly the six fields the engine accepts. Nothing else is ever added here."""
    return {
        "install_id": install_id(config_dir),
        "client_version": client_version(),
        "mode": install_mode(),
        "os": sys.platform,
        "arch": platform.machine() or "unknown",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def ping_url() -> str:
    override = os.environ.get(PING_URL_ENV, "").strip()
    return override or DEFAULT_MANAGED_SERVICE_URL.rstrip("/") + PING_PATH


def send_ping(payload: dict[str, str], url: str | None = None, timeout: float = PING_TIMEOUT_S) -> bool:
    """One POST. True on any 2xx. Never raises."""
    target = url or ping_url()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        target, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"conexus/{payload.get('client_version', '?')}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https to the managed service
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.debug("install_ping_failed", url=target, error=str(e))
        return False
    _log.debug("install_ping_sent", url=target, ok=ok)
    return ok


def ping_if_due(now: float | None = None, config_dir: Path | None = None) -> bool:
    """The whole decision: opt-out, 24h throttle, send, mark. Returns True if sent."""
    if not telemetry_enabled():
        return False
    ts = time.time() if now is None else now
    if not due(ts, config_dir):
        return False
    try:
        sent = send_ping(build_payload(config_dir))
    except Exception as e:  # noqa: BLE001 — a telemetry beacon never surfaces
        _log.debug("install_ping_error", error=str(e))
        return False
    if sent:
        _mark_pinged(ts, config_dir)
    return sent


def ping_in_background(target: Callable[[], Any] = ping_if_due) -> threading.Thread | None:
    """Fire :func:`ping_if_due` on a daemon thread. Returns the thread, or None when opted out."""
    if not telemetry_enabled():
        return None
    t = threading.Thread(target=target, name="nx-install-ping", daemon=True)
    t.start()
    return t
