#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""The setup process: install the Nexus CLI and, when asked, a service, in the
background, and say how far it got.

RDR-228 Phase 2 Step 2 (nexus-sv2wx.15). One copy, in the plugin. The stand-in
server starts it directly; the real ``nx-mcp`` reaches it through
``NX_SETUP_SCRIPT``. Standard library only and no ``nexus`` import, because it
runs on a machine that has no CLI yet. It runs CLI programs as child processes for
everything that needs Nexus.

Command line, so no other code re-implements the directory below. Every command
prints one JSON object on standard output and exits 0, whatever happened: an
unexpected failure prints an object whose ``state`` is ``error`` and whose
``message`` carries the error text, never a traceback. Exit 2 means the arguments
were wrong (argparse's own usage message on standard error and no object)::

    nx_setup.py start --mode local|cloud|cli-only|not-now [--autostart]
    nx_setup.py status
    nx_setup.py not-now

``start`` returns at once with the status and ``"started": true`` when it began a
run. While a run is live it starts nothing and returns that run's status with
``"started": false`` and ``"refused"``, a sentence naming the live run's mode and
the mode asked for. ``not-now`` is ``start --mode not-now``.

**For code that imports this file** (the session-start hook, P2.4, nexus-sv2wx.17,
imports it rather than spawning it, so the setup directory has one reader):
:func:`status_view` (what ``status`` prints), :func:`provisioned` (is a local
service on disk), :func:`config_dir`, :func:`setup_dir`, :func:`read_status`, and
the constants :data:`STATES` (what a status file may hold), :data:`TERMINAL_STATES`
and :data:`ALL_STATES` (those plus the three that are only ever computed:
``interrupted``, ``none`` and ``error``). Importing this file starts nothing and
touches no file.

The setup directory is ``setup/`` under the Nexus config directory
(``NEXUS_CONFIG_DIR``, else ``NX_CONFIG_DIR``, else ``~/.config/nexus``, made
absolute)::

    status.json   the status (JSON, owner-only, replaced whole)
    status.error  written only when a run could not write status.json; says why
    setup.log     what the run's child programs printed, as they print it
    credentials-opener.html  mode cloud only, while the page is unloaded: the one file that holds the page's address
    not-now       the "not now" marker
    lock/         the lock: a directory; ``owner`` holds the holder's token and
                  ``heartbeat`` is rewritten every 10 seconds
    lock.reclaim/ the reclaim guard (below), present for an instant

``${CLAUDE_PLUGIN_DATA}`` is not used.

**Modes.** ``not-now`` writes the marker and starts nothing. ``local`` and
``cli-only`` remove the marker first. ``cli-only`` stops after the build. ``local``
goes on to the service and verifies. ``cloud`` (P2.5, nexus-sv2wx.18) serves the
credentials page below from the start of the run, builds while the user fills it in,
then saves the pair the page accepted and verifies; it downloads no service files.
``autostart`` is false unless given (and is ignored by ``cloud``), so the
login unit is never registered without an explicit yes. When it is given and a
usable service is already running, the run does not register the unit and its
final message says so and how to do it.

**The credentials page** (mode ``cloud``; :class:`CredentialsPage`). A cloud token must reach the
config file without passing through the model, the conversation or a tool argument. The run binds
``127.0.0.1`` on a port the system picks and serves one small page at ``/<random path>``; any other
path, and any ``Host`` but its own, gets an empty 404. The page's address is held in memory and in ONE
file, the opener file ``credentials-opener.html`` in the setup directory, which redirects the browser to
it. The system is asked to open that file (Windows ``os.startfile``, macOS ``/usr/bin/open``, else
``xdg-open``), so the address is on no command line and in no log, status or tool result. The opener file
is owner-only from the call that creates it, by the status file's mechanism (see below): POSIX
``os.open`` with ``O_CREAT | O_EXCL`` and mode ``0o600`` (the umask can only narrow it), Windows
``CreateFileW`` with a ``SECURITY_ATTRIBUTES`` holding the protected one-ACE descriptor
``D:P(A;;FA;;;<user SID>)`` (``nexus._winsec``'s descriptor; that module cannot be imported here, so the
two ctypes calls are repeated in ``_create_private_windows``). It is deleted when the page is first
loaded, when the listener closes without a load, and, if the run was killed (nothing can clean up after
a SIGKILL), when the next run starts (``_sweep_opener`` in ``_start``). The page has three fields: a
service address, a token (a password field, autocomplete off) and a tenant, and one button; an ``http`` address is
accepted as well as ``https``, and one to a host that is not this machine is accepted with a warning on the page that
the token travels unencrypted (:func:`_sends_token_in_clear`). It serves at most :data:`PAGE_MAX_CONNECTIONS` connections at once; over that, the oldest one
whose request is not yet read is shut down for the new one (a connection past its read is never evicted, and if every one is, the
new one is closed at the door), and every connection must finish its request within :data:`PAGE_CONNECTION_SECONDS`; it refuses a ``POST`` whose ``Sec-Fetch-Site`` or ``Origin`` names another origin; on Windows its
socket asks for exclusive use of its address (``SO_EXCLUSIVEADDRUSE``); and it tightens the setup directory to
``0o700`` on POSIX when it starts to serve.

**Two kinds of credential.** A managed user holds a MINT credential: a token bound to one tenant that mints
short-lived data tokens, which the CLI does for every request (``mint_token`` and ``mint_tenant``; conexus,
2026-10-15). A static service token (``service_token``) is the self-hosted case. The page takes both (Sam,
2026-10-15). The tenant is required for a mint credential and unused for a static token.

A submitted set is checked by :func:`check_pair`. **The check:** ``GET /version`` (no token) must answer 200: the
service is there. ``/health`` is not used (it is unauthenticated in the engine, and the managed edge answers it 401
to a mint credential, measured 2026-10-15, ``nexus_rdr/228-research-37``). The token goes to ONE authenticated
read-only route, ``GET /v1/_whoami`` (:data:`TOKEN_ROUTE`; Sam, 2026-10-15), which sits behind ``AuthFilter`` on
every engine (the doctor's "Static service_token" row makes the same call): 401 refuses the token, 200 is a working
static token, and 403 is a live mint credential. A mint credential is then proved with its tenant by minting ONE
data token, ``POST /v1/data-tokens/mint`` (:data:`MINT_ROUTE`) with ``{"tenant", "ttl_seconds": 60}``, the call
``nexus.db.data_token.DataTokenManager`` makes: 200 accepts, 403 says the token belongs to another tenant, 429 says to
try again in a moment, anything else is refused with its status. What the managed edge does with these routes beyond
what research-37 measured is not asserted here. A redirect is never followed and no proxy is used, so the token
goes only to the address typed; the whole check ends after :data:`CHECK_DEADLINE_SECONDS`.

What passes is held in memory only (it is never written to disk to survive a kill: a killed run's next ``cloud`` call
opens a new page, and a status that read the pair as accepted is reported as lost), the page says so, and the
listener closes. It is saved after the build, for a mint credential with ``nx config set mint_tenant <tenant>`` and
``nx config set mint_token --stdin``, or, for a static token, with ``nx config set service_token --stdin``, and then
``nx config set service_url <address>`` LAST (a token with no address is inert, so a save that stops half way never
points a working service at a new address); never both kinds. A token goes on standard input, never on a command line;
should the child echo it, it is replaced by ``***`` in the log. A set that fails is not kept; the page says why and
stays open. When ``NX_SERVICE_URL``, ``NX_SERVICE_TOKEN``, ``NX_MINT_TOKEN`` or ``NX_MINT_TENANT`` is set in this
process's environment the page says those take precedence over what it saves. When this run resumes one that saved
its credentials and then failed at a step other than ``nx doctor`` (:data:`CLOUD_SAVED`), and the config holds a
service address and a token of either kind, no page is served and nothing is saved again, so the token is not asked
for twice. Replacing a saved credential is ``nx config init`` in a terminal: the real server's ``setup`` starts
nothing while a service answers, and a saved address with a token resolves as one. The status file's ``credentials`` field takes exactly
:data:`CREDENTIAL_STATES`; two minutes after the request to open the page with no load, or at once when the request
is known to have failed, it is ``page not loaded``, and ten minutes after the listener started it is ``timed out``
and the listener closes, whatever happened; a run that times out ends ``failed`` at step ``credentials`` (a timeout
during the build lets the build finish first, and until then the status says to wait for the run to end, because a
``setup`` call while a run is live starts nothing). A run that ends with credentials accepted and not saved drops the
field and says they are discarded. The token is never written to the log or the status file.

**Steps**, each skipped when already done, decided from facts and not from this
file's own history alone:

1. build, skipped only for an identified Nexus CLI. The generation layout's ``nx``
   (the launcher's lookup, ``_cli_lookup``) is the install; the build is skipped
   unless ``nx self install --bootstrap --dry-run`` run by that ``nx`` exits 0,
   which is ``--bootstrap`` saying it would still act (a build died after the flip
   and before the shims: the build step resumes it). A hit on ``PATH`` counts only
   with ``nx-mcp`` beside it and ``nx --version`` printing ``nx, version <x>``;
   anything else (Nrwl's ``nx``) is never run again and the build runs. An ``nx`` that
   is found and cannot start (``_cli_lookup.cannot_start``: an interpreter ``uv``
   pruned; Sam, 2026-10-14) is not asked anything and the build runs, and
   ``nx self install --bootstrap`` replaces the generation that cannot run. The build
   is ``uv tool run --python 3.12 --from conexus==<plugin version> nx self install
   --bootstrap``. The version is the ``plugin.json`` beside this script. There is
   no fallback to the two documented commands: they run the cache prune and leave
   two copies. After the build ``nx`` is found by the same lookup, layout first,
   so an application started before the install needs no restart for this process.
2. service, ``local``: if the status file recorded that a run began ``nx init``
   and did not finish it, run ``nx init`` again (a run killed inside ``nx init``
   leaves the service files on disk and no service answering, which alone reads
   as "provisioned and down"). Else when an endpoint resolves
   (``_endpoint_resolve``, the hooks' definition of a usable service) there is
   nothing to do. Else when :func:`provisioned` the service is provisioned and
   down: ``nx daemon service install --autostart`` when ``autostart``, then ``nx
   daemon service start``. Else ``nx init --yes`` with ``--no-autostart`` unless
   ``autostart``, and always ``--no-beads-prime`` (``nx init`` otherwise installs
   a user-level beads PRIME.md).
3. verify: ``nx doctor``. Exit 0 is ``done``; anything else is ``doctor-failed``
   with the ``[✗]`` rows, read from the tail of the log.

**Child programs** print straight into ``setup.log`` (an append-mode handle, no
pipe), so the log shows a long step's output while it runs and a killed step's
output up to the kill. Each runs in its own process group (POSIX) and with
``CREATE_NO_WINDOW`` (Windows: a detached run has no console, and a console child
would get a window of its own). A step over :data:`STEP_TIMEOUT_SECONDS`, or one
whose run lost the lock, is killed with everything it started: ``killpg`` on POSIX,
``taskkill /T /F /PID`` on Windows.

**Statuses.** :data:`STATES` is the list a status file may hold. A status that is
``running`` while the lock is stale is reported by ``status`` as ``interrupted``,
no status file as ``none``, and a failure of the command itself as ``error``; those
three are computed and never written.

**Lock.** The caller that starts a run takes the lock, then starts the detached
process with the token; the process verifies the token and keeps the lock alive
with a heartbeat written on a thread of its own, because its work is child
programs that take minutes. A heartbeat write or owner read that fails is retried
a few times and, if it still fails, skipped: the lock counts as lost only when the
owner file is read and is someone else's (or the lock is gone). A lost lock stops
the run: its child is killed and it writes no status again (every status write
re-checks ownership), and it leaves the new holder's lock alone. The lock is
removed on every way out the process can catch: success, failure, an exception and
SIGTERM. A kill it cannot catch (SIGKILL, TerminateProcess) leaves the lock to go
stale: sixty seconds without a heartbeat. Liveness is the heartbeat; a process id only ever
REFUSES a reclaim (below), because a recorded pid can be reused and the heartbeat cannot.
``os.mkdir`` is the atomic claim. Reclaiming a stale lock takes a second
``mkdir``, of ``lock.reclaim``, and re-reads the lock under it: the lock must
still be stale and still carry the owner token that was read stale. So of any
number of callers that read the same stale lock, one removes it and the rest
report the run as live. A reclaimer that dies holding the guard leaves it for
sixty seconds, and after that the guard is removed the same way; two callers racing
for the removal of a dead guard at the same instant remain a hole too narrow to
close without a process test.

A stale heartbeat is read together with two process ids, because a heartbeat's clock stops when
the machine sleeps. The reclaim is refused while the ``pid`` in ``status.json`` (a run whose state is
``running``) names a live process and the heartbeat is under :data:`PID_GRACE_SECONDS` old, and while
``lock/child`` (the pid of the child program the run was waiting on, written by ``_Run.cmd``) names a live
process and is under :data:`STEP_TIMEOUT_SECONDS` old: a run killed outright (SIGKILL) leaves its child
running, and a second bootstrap beside it is what the lock exists to prevent. The age bounds are what keep a pid
the system reused for another program from refusing for ever. A dead pid, or none, changes nothing.

**The status file** is owner-only from the call that creates it. POSIX:
``os.open(..., O_CREAT | O_EXCL, 0o600)``, so the umask can only narrow it. Windows:
``CreateFileW`` with a ``SECURITY_ATTRIBUTES`` holding the protected one-ACE
descriptor ``D:P(A;;FA;;;<user SID>)`` (the descriptor ``nexus._winsec`` applies
to credential files; that module cannot be imported here, so its two ctypes calls
are repeated and the SID comes from ``_endpoint_resolve``). Updates go to a
temporary file created the same way, flushed to disk, and renamed over
``status.json``, so no instant exists in which the status is wider than owner-only
or half written. A temporary file a killed run left behind is removed at the next
start once it is a minute old. When a run cannot write ``status.json`` at all
(disk full), it writes the reason to ``status.error`` if it can, and ``status``
reports that as a ``failed`` state with the OS error text; if even that write
fails the status stays ``running`` and reads as ``interrupted`` (the best a full
disk allows).

**The detach** is one function, :func:`detach`. POSIX: a new session, standard
input the null device, standard output and error the log, no inherited handles
(``close_fds``). Windows: a double launch with ``DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP``, first with ``CREATE_BREAKAWAY_FROM_JOB`` and, if the
host's job refuses that (winerror 5), again without it. Measured on native Windows
for Claude Code 2.1.296 (nexus_rdr/228-research-32 and 228-research-33): the double
launch survives the tree kill Claude Code runs on the server; a plain child does
not. Claude Desktop 1.24012.9.0 puts servers in a job with the same flags and a
force-kill of Desktop left all three watcher modes alive (228-research-35); its
graceful tray-Quit path is NOT measured. Before the retry without breakaway the
process asks its own job (``IsProcessInJob``, ``QueryInformationJobObject``): when
the job has ``KILL_ON_JOB_CLOSE`` and allows neither breakaway flag, a launch
without breakaway would be killed when the job closes, so the status says setup
cannot detach on this host instead of reporting a start. If both launches are
refused it says the same; a WMI broker is the untried way out.

Test seams, for tests only; none is set in normal use and none is a setting.
``NX_SETUP_HEARTBEAT_SECONDS`` (the 10 second interval; read as at most
:data:`HEARTBEAT_SECONDS_MAX`, so a run cannot beat more slowly than the 60 second stale
limit allows), ``NX_SETUP_RECLAIM_PAUSE`` (seconds to wait between reading a stale lock
and removing it; read as at most :data:`RECLAIM_PAUSE_MAX`) and ``NX_SETUP_OPEN_RECORD``
(a file the opener file's path is appended to instead of asking the system to open it, so
no test starts a browser; it appends to whatever file it names).
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, NamedTuple  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "scripts"),
)
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert
import _endpoint_resolve  # noqa: E402
import _exec_path  # noqa: E402

# -- the statuses: the one place they are spelled ------------------------------------------

RUNNING = "running"
DONE = "done"
DOCTOR_FAILED = "doctor-failed"
FAILED = "failed"
UNSUPPORTED = "unsupported"
TERMINAL_STATES: tuple[str, ...] = (DONE, DOCTOR_FAILED, FAILED, UNSUPPORTED)
#: What a status file may hold.
STATES: tuple[str, ...] = (RUNNING, *TERMINAL_STATES)

#: Computed by :func:`status_view` (and the command line), never written.
INTERRUPTED = "interrupted"
NONE = "none"
ERROR = "error"
COMPUTED_STATES: tuple[str, ...] = (INTERRUPTED, NONE, ERROR)
#: Every value ``state`` can take in what ``status`` prints.
ALL_STATES: tuple[str, ...] = (*STATES, *COMPUTED_STATES)

#: What the status file records about ``nx init``, across runs.
INIT_BEGUN = "begun"
INIT_FINISHED = "finished"

MODE_LOCAL = "local"
MODE_CLOUD = "cloud"
MODE_CLI_ONLY = "cli-only"
MODE_NOT_NOW = "not-now"
MODES: tuple[str, ...] = (MODE_LOCAL, MODE_CLOUD, MODE_CLI_ONLY, MODE_NOT_NOW)

#: A heartbeat older than this is stale.
STALE_SECONDS = 60.0
#: How often the run rewrites its heartbeat.
HEARTBEAT_SECONDS = 10.0
#: The test seam ``NX_SETUP_HEARTBEAT_SECONDS`` is read as at most this (well under :data:`STALE_SECONDS`).
HEARTBEAT_SECONDS_MAX = 20.0
#: The test seam ``NX_SETUP_RECLAIM_PAUSE`` is read as at most this many seconds.
RECLAIM_PAUSE_MAX = 5.0
#: A stale heartbeat older than this is no longer refused on the strength of the pid in ``status.json``: after this
#: long a "live" pid is far more likely a pid the system reused than a run that cannot beat.
PID_GRACE_SECONDS = 600.0
#: A beat that cannot read the owner or write the heartbeat tries this many times...
HEARTBEAT_ATTEMPTS = 3
#: ...this many seconds apart, then waits for the next beat.
HEARTBEAT_RETRY_PAUSE = 0.2
#: A child program that runs longer than this is killed and its step fails.
STEP_TIMEOUT_SECONDS = 3600.0
#: An identity probe (``nx --version``, the bootstrap dry run) that runs longer than this is dropped.
PROBE_TIMEOUT_SECONDS = 60.0

_POLL_SECONDS = 0.25
_LOG_TAIL_BYTES = 256 * 1024
_TMP_MAX_AGE_SECONDS = 60.0

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000
_WINERROR_ACCESS_DENIED = 5
_JOB_BREAKAWAY_OK = 0x00000800
_JOB_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFORMATION = 9

_STATUS = "status.json"
_ERROR_FILE = "status.error"
_LOG = "setup.log"
_MARKER = "not-now"
_LOCK = "lock"
_GUARD = "lock.reclaim"
_CHILD = "child"  # in the lock directory: the pid of the child program the run is waiting on

_DOCTOR_ROWS_MAX = 20
_DOCTOR_ROW_CHARS = 300

#: ``nx --version`` of the Nexus CLI (click's ``version_option(prog_name="nx")``).
_VERSION_LINE = re.compile(r"nx, version [0-9][0-9A-Za-z.+!_-]*")

_AUTOSTART_NOTE = (
    "A usable service was already running, so the login unit was not registered. "
    "To start the service at login, run `nx daemon service install --autostart` in a terminal."
)
_NEXT_NONE = "Ask the user which they want (local, cloud, cli-only or not-now), then call setup with that mode."


class DetachUnsupported(Exception):
    """The host does not let a process start one that outlives it."""


class _Terminated(SystemExit):
    """SIGTERM reached the run: unwind so its clean-up runs."""


class _LockLost(Exception):
    """The lock is someone else's now: this run writes nothing more."""


# -- paths -----------------------------------------------------------------------------------


def config_dir() -> Path:
    """The Nexus config directory, absolute (a relative one would be read against
    the setup directory once the detached run changes into it).

    ``NEXUS_CONFIG_DIR``, else ``NX_CONFIG_DIR``, else ``~/.config/nexus``. The CLI
    (``nexus.config``) reads only ``NEXUS_CONFIG_DIR``; honouring ``NX_CONFIG_DIR``
    here is the bead's rule and a mismatch to settle in P2.R2 (nexus-sv2wx.22).
    """
    return Path(os.path.abspath(_endpoint_resolve.default_config_dir()))


def _child_env() -> dict[str, str]:
    """This process's environment with ``NEXUS_CONFIG_DIR`` set to the absolute
    :func:`config_dir`. A detached run starts in the setup directory, so a relative
    value read there would name a different tree; the CLI it runs reads only
    ``NEXUS_CONFIG_DIR``, so this also hands it the same directory."""
    env = dict(os.environ)
    env["NEXUS_CONFIG_DIR"] = str(config_dir())
    return env


def setup_dir() -> Path:
    """``setup/`` under :func:`config_dir`."""
    return config_dir() / "setup"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- the status file: owner-only from the creating call ----------------------------------------


def _create_private_windows(path: str) -> int:
    """Create *path* exclusively with a protected one-ACE descriptor in the same
    ``CreateFileW`` call. Windows only; raises OSError."""
    import ctypes  # noqa: PLC0415 -- Windows-only branch
    import msvcrt  # noqa: PLC0415 -- Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 -- Windows-only branch

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sddl = f"D:P(A;;FA;;;{_endpoint_resolve._windows_user_sid()})"  # noqa: SLF001 -- the SID reader is a mirror of nexus._winsec's, kept in one place
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # GENERIC_WRITE; FILE_SHARE_READ; CREATE_NEW; FILE_ATTRIBUTE_NORMAL
        handle = kernel32.CreateFileW(path, 0x40000000, 0x1, ctypes.byref(attrs), 1, 0x80, None)
    finally:
        kernel32.LocalFree(descriptor)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)  # type: ignore[attr-defined]


def _create_private(path: str) -> int:
    """A new file at *path*, owner-only from the call that makes it."""
    if os.name == "nt":
        return _create_private_windows(path)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)


def _windows() -> bool:
    """Whether this is native Windows. A seam: the retry below has a Windows branch a
    test on another host selects through this function, not by patching ``os.name``."""
    return os.name == "nt"


#: How long the writers, and the setup tools' own status reads, wait for a Windows
#: sharing violation to clear.
SHARING_RETRY_BUDGET_S = 2.0


def _retry_sharing(action: Any, *, budget_s: float = SHARING_RETRY_BUDGET_S) -> Any:
    """Run *action*, retrying a Windows sharing violation (a reader holds the file
    during a rename) for up to *budget_s* seconds (default two). Elsewhere a
    PermissionError is real. A budget of zero or less tries once. The session-start
    hook passes a few milliseconds, because it has 100 ms in all."""
    if not _windows():
        return action()
    deadline = time.monotonic() + budget_s
    while True:
        try:
            return action()
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))


def _write_private_json(setup: Path, name: str, data: dict[str, Any]) -> None:
    """Replace ``setup/<name>`` with *data*: owner-only throughout, flushed to disk
    before it replaces the old file."""
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    import secrets  # noqa: PLC0415 -- lazy: the session-start hook imports this file and has 100 ms

    tmp = setup / f"{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = _create_private(str(tmp))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _retry_sharing(lambda: os.replace(tmp, setup / name))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_status(setup: Path, data: dict[str, Any]) -> None:
    """Replace ``status.json`` with *data*, owner-only throughout."""
    _write_private_json(setup, _STATUS, data)


def _read_json(path: Path, *, budget_s: float = SHARING_RETRY_BUDGET_S) -> dict[str, Any] | None:
    try:
        text = _retry_sharing(lambda: path.read_text(encoding="utf-8"), budget_s=budget_s)
        data = json.loads(text)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return data if isinstance(data, dict) else None


def read_status(setup: Path, *, budget_s: float = SHARING_RETRY_BUDGET_S) -> dict[str, Any] | None:
    """``status.json`` as a dict, or ``None`` when it is missing or not JSON."""
    return _read_json(setup / _STATUS, budget_s=budget_s)


def _sweep_tmp(setup: Path) -> None:
    """Remove temporary status files a killed writer left behind. A live writer's
    file lives for microseconds, so a minute-old one is an orphan."""
    for path in setup.glob("*.tmp"):
        age = _mtime_age(path)
        if age is not None and age > _TMP_MAX_AGE_SECONDS:
            with contextlib.suppress(OSError):
                path.unlink()


# -- the lock ----------------------------------------------------------------------------------


def _mtime_age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _lock_stale(lock: Path) -> bool:
    """No heartbeat for :data:`STALE_SECONDS`. A lock whose heartbeat is not yet
    written (its maker is between ``mkdir`` and the first write) is judged by the
    directory's own age."""
    age = _mtime_age(lock / "heartbeat")
    if age is None:
        age = _mtime_age(lock)
    return age is not None and age > STALE_SECONDS


def _lock_owner(lock: Path) -> str | None:
    try:
        return (lock / "owner").read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        return None


def _owner_check(lock: Path, token: str) -> bool | None:
    """``True``: the lock is *token*'s. ``False``: it is someone else's or gone.
    ``None``: it cannot be told now (a sharing violation, an unreadable or empty
    owner file), which is not a loss."""
    try:
        text = (lock / "owner").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    owner = text.strip()
    if not owner:
        return None
    return owner == token


def _write_heartbeat(lock: Path) -> None:
    (lock / "heartbeat").write_text(str(time.time()), encoding="utf-8")


def _exists(path: Path) -> bool:
    """``path.exists()`` that answers True rather than raising when the path
    cannot be examined: on Windows a directory being deleted while another
    process holds a handle to it (an indexer, antivirus) refuses ``stat`` with
    access denied for a moment."""
    try:
        return path.exists()
    except OSError:
        return True


def _make_lock(lock: Path, token: str) -> bool:
    try:
        os.mkdir(lock)
    except FileExistsError:
        return False
    except PermissionError:
        if os.name == "nt":  # the previous lock is still being deleted
            return False
        raise
    (lock / "owner").write_text(token, encoding="utf-8")
    _write_heartbeat(lock)
    return True


def _reclaim(setup: Path, lock: Path, seen_owner: str | None) -> bool:
    """Remove the stale *lock* this caller read (owner *seen_owner*), or learn it
    is not ours to remove. True means "try to make the lock now"; False means the
    run is live or another caller has it."""
    guard = setup / _GUARD
    try:
        os.mkdir(guard)
    except FileExistsError:
        age = _mtime_age(guard)
        if age is None or age <= STALE_SECONDS:
            return False
        with contextlib.suppress(OSError):
            os.rmdir(guard)
        try:
            os.mkdir(guard)
        except FileExistsError:
            return False
    try:
        if not _exists(lock):
            return True
        if not _lock_stale(lock) or _lock_owner(lock) != seen_owner:
            return False
        if _recorded_process_alive(setup, lock):
            return False
        try:
            shutil.rmtree(lock)
        except OSError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(guard)


def _live_pid(value: Any) -> bool:
    """*value* is a pid (a positive integer) of a process that has not exited, and not this process."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value == os.getpid():
        return False
    return _endpoint_resolve.pid_alive(value)


def _recorded_holder(setup: Path, lock: Path, data: dict[str, Any] | None = None) -> str | None:
    """Words for the process still behind a stale *lock*, or None when none is: ``process N`` for the run itself
    (the ``pid`` in a ``running`` ``status.json``, while the heartbeat is under :data:`PID_GRACE_SECONDS` old) or
    ``its build (process N)`` for the child program it was waiting on (``lock/child``, while that file is under
    :data:`STEP_TIMEOUT_SECONDS` old). Refuses a reclaim, and is what :func:`status_view` and the refused start
    both say, so the three agree: a heartbeat that stopped because the machine slept, or a run killed outright
    whose build carries on, must not get a second run beside it. *data* is the status already read, if any."""
    age = _mtime_age(lock / "heartbeat")
    if age is None:
        age = _mtime_age(lock)
    if age is not None and age <= PID_GRACE_SECONDS:
        if data is None:
            data = read_status(setup) or {}
        if data.get("state") == RUNNING and _live_pid(data.get("pid")):
            return f"process {data['pid']}"
    child_age = _mtime_age(lock / _CHILD)
    if child_age is not None and child_age <= STEP_TIMEOUT_SECONDS:
        try:
            recorded = int((lock / _CHILD).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return f"its build (process {recorded})" if _live_pid(recorded) else None
    return None


def _recorded_process_alive(setup: Path, lock: Path) -> bool:
    return _recorded_holder(setup, lock) is not None


def _wait_gone(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _exists(path):
            return True
        time.sleep(0.05)
    return not _exists(path)


def acquire_lock(setup: Path, *, wait_for_finish: bool = False) -> str | None:
    """Take the lock; returns the holder's token, or ``None`` when a run is live.

    *wait_for_finish*: a fresh lock beside a terminal status is a run that is
    removing its lock, and the caller waits a moment for it instead of dropping
    its own request.
    """
    import secrets  # noqa: PLC0415 -- see _write_private_json

    token = secrets.token_hex(8)
    lock = setup / _LOCK
    for _ in range(3):
        if _make_lock(lock, token):
            return token
        if not _lock_stale(lock):
            status = read_status(setup)
            if wait_for_finish and status is not None and status.get("state") in TERMINAL_STATES and _wait_gone(lock, 3.0):
                continue
            return None
        seen_owner = _lock_owner(lock)
        with contextlib.suppress(ValueError):  # a test seam, clamped: it must not be a way to sleep in a tool call
            time.sleep(min(max(float(os.environ.get("NX_SETUP_RECLAIM_PAUSE", "0") or 0), 0.0), RECLAIM_PAUSE_MAX))
        if not _reclaim(setup, lock, seen_owner):
            return None
    return None


def release_lock(setup: Path, token: str) -> None:
    """Remove the lock unless it is positively someone else's (or gone). An owner file that cannot be read now
    (a Windows sharing violation) is not another owner: leaving the lock would refuse every call for 60 seconds
    beside a terminal status."""
    lock = setup / _LOCK
    if _owner_check(lock, token) is not False:
        shutil.rmtree(lock, ignore_errors=True)


class _Heartbeat(threading.Thread):
    """Rewrites the lock's heartbeat on a thread of its own."""

    def __init__(self, lock: Path, token: str) -> None:
        super().__init__(name="nx-setup-heartbeat", daemon=True)
        self._lock_dir = lock
        self._token = token
        self._stop_event = threading.Event()
        self.lost = threading.Event()
        try:  # a test seam; clamped so a value over the stale limit cannot make a healthy run reclaimable
            self._interval = min(float(os.environ.get("NX_SETUP_HEARTBEAT_SECONDS", "") or HEARTBEAT_SECONDS), HEARTBEAT_SECONDS_MAX)
        except ValueError:
            self._interval = HEARTBEAT_SECONDS
        if not self._interval > 0:  # zero, negative or nan would spin
            self._interval = HEARTBEAT_SECONDS

    def holds(self) -> bool:
        """Whether the lock is still this run's. Checked again here, on the
        caller's thread, so a status write never waits for the next beat."""
        if self.lost.is_set():
            return False
        if _owner_check(self._lock_dir, self._token) is False:
            self.lost.set()
            return False
        return True

    def _beat(self) -> bool:
        """One beat. ``False`` only when the lock is no longer ours; a beat that
        could not read or write is skipped (the next one tries again)."""
        for attempt in range(HEARTBEAT_ATTEMPTS):
            verdict = _owner_check(self._lock_dir, self._token)
            if verdict is False:
                return False
            if verdict is True:
                try:
                    _write_heartbeat(self._lock_dir)
                    return True
                except FileNotFoundError:  # the lock directory was removed under us
                    return False
                except OSError:
                    pass
            if attempt + 1 < HEARTBEAT_ATTEMPTS and self._stop_event.wait(HEARTBEAT_RETRY_PAUSE):
                return True
        return True

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            if not self._beat():
                self.lost.set()
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


# -- facts -------------------------------------------------------------------------------------


def _on_path(name: str) -> str | None:
    """*name* on ``PATH`` alone: never the current directory, and on POSIX never a
    relative entry (the same rule as ``_cli_lookup.resolve``)."""
    if sys.platform == "win32":
        return _exec_path.which_off_cwd(name)
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    return _exec_path.which_off_cwd(name, path=os.pathsep.join(entries))


def _find_uv() -> str | None:
    return _on_path("uv")


def _layout_nx() -> str | None:
    """The ``nx`` in the generation ``<tools>/current`` names, or ``None``."""
    generation = _cli_lookup.current_generation()
    if generation is None:
        return None
    return _exec_path.which_off_cwd("nx", path=os.path.join(generation, "bin"))


def plugin_version() -> str | None:
    """The version in the ``plugin.json`` of the plugin this script is part of."""
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]*", version) else None


def provisioned(config: Path | None = None) -> bool:
    """Whether the files ``nx init`` leaves for a local service are on disk:
    ``pg_credentials``, ``postgres/PG_VERSION`` and the service binary, under
    *config* (default :func:`config_dir`). All three, or it is not provisioned."""
    config = config_dir() if config is None else config
    binary = "nexus-service.exe" if sys.platform == "win32" else "nexus-service"
    return (
        (config / "pg_credentials").is_file()
        and (config / "postgres" / "PG_VERSION").is_file()
        and (config / "service" / binary).is_file()
    )


def _endpoint_resolves(config: Path) -> bool:
    """Whether a usable service is already configured: the CLI's own definition
    (``resolve_service_endpoint()`` returning), restated in ``_endpoint_resolve``. A
    saved address with no token is not usable (nexus-sv2wx.17, residual 9), so the
    service step does not call such a machine finished. No grace for a lease past its
    time to live: the hook keeps one whose owner process is alive because it cannot wait
    to ask the port, but this decides whether to start the service, and a supervisor that
    is alive and wedged would otherwise end the run in a failed ``nx doctor`` without ever
    starting one (nexus-sv2wx.17, review 2)."""
    return _endpoint_resolve.endpoint_usable(config, grace=False)


# -- children: no window, their own process group, killed whole ----------------------------------


def _child_kwargs(windows: bool | None = None) -> dict[str, Any]:
    """The ``Popen`` keywords for a child program. Windows: ``CREATE_NO_WINDOW``
    (this process has no console, so a console child would open its own window).
    POSIX: a new session, so the whole group can be killed."""
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_tree(proc: Any, windows: bool | None = None) -> None:
    """Kill *proc* and everything it started. Never raises."""
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        taskkill = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603 -- the system's own taskkill, by absolute path
                [taskkill, "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False, creationflags=_CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


# -- the credentials page (mode cloud) -----------------------------------------------------------

CRED_WAITING = "waiting for credentials"
CRED_NOT_LOADED = "page not loaded"
CRED_REJECTED = "credentials rejected, waiting for another try"
CRED_ACCEPTED = "credentials accepted"
CRED_SAVED = "credentials saved"
CRED_TIMED_OUT = "timed out"
#: The only values the status file's ``credentials`` field takes (the record's list, in its order).
CREDENTIAL_STATES: tuple[str, ...] = (
    CRED_WAITING, CRED_NOT_LOADED, CRED_REJECTED, CRED_ACCEPTED, CRED_SAVED, CRED_TIMED_OUT,
)

#: A page not loaded this long after the request to open it is reported as such.
PAGE_LOAD_SECONDS = 120.0
#: The listener closes this long after it started, whatever happened.
PAGE_LISTEN_SECONDS = 600.0
#: One request of the check waits this long.
CHECK_TIMEOUT_SECONDS = 10.0

#: The opener file in the setup directory: the one file that holds the page's address.
OPENER_NAME = "credentials-opener.html"
#: Test seam, none set in normal use: a file the opener file's path is appended to INSTEAD of asking the system
#: to open it, so no test can start a browser.
OPEN_RECORD_ENV = "NX_SETUP_OPEN_RECORD"

#: The whole check (every request together) gives up after this long. ``CHECK_TIMEOUT_SECONDS`` bounds one
#: socket operation and restarts on every byte, so a service that drips bytes could otherwise hold the check, and
#: the submit lock with it, for hours.
CHECK_DEADLINE_SECONDS = 20.0
#: At most this many connections to the page are served at once. Over that the oldest connection whose request is not
#: yet read is shut down and the new one admitted; if all are past their read, the new one is closed at the door.
PAGE_MAX_CONNECTIONS = 16
#: A connection must have sent its whole request (headers and body) this long after it was accepted, or it is shut
#: down. The 10 second per-recv timeout restarts on every byte and cannot bound a connection that drips.
PAGE_CONNECTION_SECONDS = 5.0
#: A second submit waits this long for a check that is running, then is told so.
PAGE_BUSY_WAIT_SECONDS = 1.0

#: The authenticated, read-only route that checks the token (module docstring, "The token check").
TOKEN_ROUTE = "/v1/_whoami"
#: The route a mint credential proves itself on, with its tenant (``nexus.db.data_token.DataTokenManager`` calls it).
MINT_ROUTE = "/v1/data-tokens/mint"
#: The lifetime asked of the one data token the check mints.
MINT_TTL_SECONDS = 60

KIND_MINT = "mint"
KIND_STATIC = "static"

_PAGE_BODY_MAX = 16 * 1024


def _clock() -> float:
    """The page's clock (a seam: the tests replace this with one they advance)."""
    return time.monotonic()


def _launch_opener(path: str) -> bool:
    """Ask the operating system to open *path* with the program that handles it (a
    browser, for the opener file). The one place anything is started for the page.
    ``False`` when there is nothing to ask or the request failed."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 -- the opener file, by absolute path
            return True
        program = "/usr/bin/open" if sys.platform == "darwin" else _on_path("xdg-open")
        if program is None or not os.path.isfile(program):
            return False
        subprocess.Popen(  # noqa: S603 -- the system's own opener, by absolute path, given the opener file
            [program, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def _ask_system_to_open(path: str) -> bool:
    """Ask the system to open the opener file at *path*. With ``NX_SETUP_OPEN_RECORD`` set, record
    the path there instead and start nothing (the tests' seam)."""
    record = os.environ.get(OPEN_RECORD_ENV, "")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(path + "\n")
        return True
    return _launch_opener(path)


def _bind_exclusively(sock: Any, option: int | None = None) -> None:
    """Before the bind, ask that no other process may bind this socket's address and port, whatever options it
    sets (``SO_EXCLUSIVEADDRUSE``, which exists on Windows only; ``allow_reuse_address=False`` only stops THIS
    socket sharing it). *option* is the constant; ``None`` looks it up, and a system without it asks for nothing."""
    import socket  # noqa: PLC0415 -- lazy, see CredentialsPage.start

    if option is None:
        option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if option is not None:
        sock.setsockopt(socket.SOL_SOCKET, option, 1)


def _remove_file(path: Path) -> None:
    """Delete *path* if it is there. Never raises: a Windows reader may hold it for a moment (the
    next run's start deletes what is left)."""
    with contextlib.suppress(OSError):
        _retry_sharing(lambda: os.unlink(path), budget_s=0.5)


def _sweep_opener(setup: Path) -> None:
    """Delete an opener file a killed run left behind."""
    _remove_file(setup / OPENER_NAME)


def _normal_address(url: str) -> str:
    """The service address as the page uses and saves it: ``http`` or ``https``, a host, no user
    name, no query or fragment, no trailing slash. Raises ValueError with a sentence for the user."""
    import urllib.parse  # noqa: PLC0415 -- lazy: the session-start hook imports this file and has 100 ms

    text = (url or "").strip()
    if not text:
        raise ValueError("Enter the service address.")
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("That is not a web address with a valid port.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The service address must start with http:// or https:// and name a host.")
    if parts.username or parts.password:
        raise ValueError("Leave the user name and password out of the service address.")
    del port
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


#: Said on the page when the address is ``http://`` to a host that is not this machine. The page still accepts it:
#: a self-hosted service on a private network is a real case, and the choice is the user's.
_CLEARTEXT_WARNING = (
    "This address starts with http:// and names a host other than this machine, so the token travels over the "
    "network unencrypted and anyone on the path can read it. Use https:// if the service offers it."
)


#: Said on the page before anything is submitted: the check sends the token to the address as soon as the button is
#: pressed, so the warning above, which needs an address to look at, comes after the token has already gone.
_CLEARTEXT_HINT = (
    "Before you press the button: an address that starts with http:// and names a machine other than this one sends "
    "the token over the network unencrypted, and the check does that the moment you submit. Use https:// unless you "
    "trust the network between here and the service."
)


def _sends_token_in_clear(url: str) -> bool:
    """Whether *url* is ``http://`` to a host that is not loopback (``localhost``, ``127.0.0.0/8``, ``::1``).
    An address that does not parse is not (the check refuses it with its own sentence)."""
    import ipaddress  # noqa: PLC0415 -- lazy, see _normal_address
    import urllib.parse  # noqa: PLC0415

    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme != "http" or not host:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True  # a name, not an address: it resolves to wherever DNS says


class CheckResult(NamedTuple):
    """The verdict of :func:`check_pair`. ``text`` is a sentence for the user (no token, nothing of the page's
    address). ``kind`` is :data:`KIND_MINT` or :data:`KIND_STATIC` when ``ok`` and ``""`` when not."""

    ok: bool
    text: str
    kind: str = ""


class Accepted(NamedTuple):
    """What the page accepted and holds in memory until the build ends: the kind of credential, the service
    address, the token and, for a mint credential, its tenant (``""`` for a static token)."""

    kind: str
    url: str
    token: str
    tenant: str = ""


#: A tenant name the service itself may put in an error body, which is the only kind the check shows back.
_SLUG = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _normal_tenant(tenant: str) -> str:
    """The tenant as typed, trimmed. ``""`` when empty. Raises ValueError with a sentence for the user."""
    text = (tenant or "").strip()
    if not text:
        return ""
    if len(text) > 128 or text.startswith("-") or not (text.isascii() and text.isprintable() and not any(c.isspace() for c in text)):
        raise ValueError("The tenant has characters a tenant name does not have (spaces, a leading dash, control characters or non-ASCII ones).")
    return text


def _bound_tenant(raw: bytes) -> str:
    """The tenant a service names in a 403 body, only when it is a plain slug, else ``""``. Used to say which
    tenant the token belongs to and never to choose one."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return ""
    if isinstance(data, dict):
        for key in ("tenant", "bound_tenant"):
            value = data.get(key)
            if isinstance(value, str) and _SLUG.fullmatch(value):
                return value
    return ""


def check_pair(url: str, token: str, tenant: str = "", *, deadline_s: float | None = None) -> CheckResult:
    """Check a service address, a token and, for a mint credential, its tenant. The sentence in the result is
    for the user and holds neither the token, nor the tenant as typed (a token pasted into the tenant field
    would otherwise reach the status file and the model), nor anything of the page's address.

    ``GET /version`` (no token) must answer 200 with Nexus's version record: the service is there and says
    which version. The token then goes to :data:`TOKEN_ROUTE` alone, the one authenticated read-only route that
    refuses a wrong token on every engine (``/health`` and ``/version`` are unauthenticated in the engine and
    answer any token; ``/health`` is not used at all, because the managed edge answers it 401 to a mint
    credential, measured 2026-10-15, ``nexus_rdr/228-research-37``). 401 refuses the token; 200 is a working
    static service token; 403 is a live mint credential, which cannot be a service token, and is proved with
    its tenant by minting ONE data token (``POST`` :data:`MINT_ROUTE`, the call the CLI itself makes, with a
    :data:`MINT_TTL_SECONDS` lifetime; the minted token is dropped unread): 200 accepts it as a mint credential,
    403 says the token belongs to another tenant, 429 says to try again in a moment. A redirect is never
    followed, so the token goes only to the address typed, and no proxy is used. The whole check gives up after
    *deadline_s* (default :data:`CHECK_DEADLINE_SECONDS`), however slowly the service answers.
    """
    import urllib.parse  # noqa: PLC0415 -- lazy, see _normal_address

    try:
        base = _normal_address(url)
        want = _normal_tenant(tenant)
    except ValueError as exc:
        return CheckResult(False, str(exc))
    if not token:
        return CheckResult(False, "Enter the token.")
    if not (token.isascii() and token.isprintable() and not any(c.isspace() for c in token)):
        return CheckResult(False, "The token has characters a token does not have (spaces, control characters or non-ASCII ones).")
    host = urllib.parse.urlsplit(base).netloc
    limit = CHECK_DEADLINE_SECONDS if deadline_s is None else deadline_s
    outcome: list[CheckResult] = []

    def work() -> None:
        try:
            outcome.append(_probe(base, host, token, want, time.monotonic() + limit))
        except Exception as exc:  # noqa: BLE001 -- the thread must always leave a verdict
            outcome.append(CheckResult(False, f"The service at {host} did not answer ({type(exc).__name__})."))

    worker = threading.Thread(target=work, name="nx-setup-check", daemon=True)
    worker.start()
    worker.join(limit)
    if outcome:
        return outcome[0]
    return CheckResult(False, f"The service at {host} did not answer in time ({limit:g} seconds).")


def _probe(base: str, host: str, token: str, tenant: str, end: float) -> CheckResult:
    """The requests of :func:`check_pair`, each with whatever is left of the deadline *end* as its timeout."""
    import http.client  # noqa: PLC0415 -- lazy, see _normal_address
    import ssl  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # the 3xx answer then surfaces as an HTTPError
            return None

    # An empty ProxyHandler: the token goes to the address typed, never to a proxy the environment names.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def call(route: str, bearer: str | None = None, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
        headers = {"Accept": "application/json", "User-Agent": "conexus-setup"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + route, data=data, method="GET" if body is None else "POST", headers=headers)
        if bearer is not None:
            request.add_header("Authorization", f"Bearer {bearer}")
        timeout = max(0.1, min(CHECK_TIMEOUT_SECONDS, end - time.monotonic()))
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(65536)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(65536)
            finally:
                exc.close()

    try:
        code, raw = call("/version")
        if code != 200:
            return CheckResult(False, f"The service at {host} answered /version with HTTP {code}; it does not look like a Nexus service.")
        try:
            info = json.loads(raw.decode("utf-8"))
        except ValueError:
            info = None
        if not isinstance(info, dict):
            return CheckResult(False, f"The service at {host} answered /version with something other than Nexus's version record.")
        version = str(info.get("release_version") or info.get("app_version") or "unknown")
        code, _ = call(TOKEN_ROUTE, token)
        if code == 200:
            return CheckResult(True, f"The service at {host} answers (version {version}) and accepts the token.", KIND_STATIC)
        if code == 401:
            return CheckResult(False, f"The service at {host} refused the token (HTTP 401).")
        if code != 403:
            return CheckResult(False, f"The service at {host} answered the token check with HTTP {code}.")
        # 403: a live credential that is not a service token, which is what a mint credential is.
        if not tenant:
            return CheckResult(
                False,
                f"The service at {host} knows this token but does not take it as a service token, which is how a mint "
                "credential answers. A mint credential belongs to one tenant: enter the tenant as well.",
            )
        code, raw = call(MINT_ROUTE, token, {"tenant": tenant, "ttl_seconds": MINT_TTL_SECONDS})
        if code == 200:
            return CheckResult(
                True, f"The service at {host} answers (version {version}) and accepts the mint credential with that tenant.", KIND_MINT,
            )
        if code == 403:
            bound = _bound_tenant(raw)
            named = f" The service names the tenant this token is bound to: {bound}." if bound else ""
            return CheckResult(
                False,
                f"The service at {host} knows this token but would not mint for the tenant entered: the token belongs "
                f"to another tenant (or is not a mint credential).{named}",
            )
        if code == 429:
            return CheckResult(False, f"The service at {host} is limiting requests; try again in a moment.")
        if code == 401:
            return CheckResult(False, f"The service at {host} refused the token when it was asked to mint (HTTP 401).")
        if code == 400:
            return CheckResult(False, f"The service at {host} refused the mint request as malformed (HTTP 400).")
        return CheckResult(False, f"The service at {host} answered the mint request with HTTP {code}.")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLError):
            return CheckResult(False, f"The service at {host} did not answer: its TLS certificate was not accepted.")
        if isinstance(reason, TimeoutError):
            return CheckResult(False, f"The service at {host} did not answer in time.")
        return CheckResult(False, f"The service at {host} did not answer ({type(reason).__name__}).")


def credentials_notice(state: str, reason: str = "", *, open_failed: bool = False) -> tuple[str, str]:
    """``(sentence, next step)`` the status carries for one of :data:`CREDENTIAL_STATES`. Neither holds the
    page's address or a token. Every one of these is written while the run is LIVE (the terminal statuses carry
    their own words), and a ``setup`` call while a run is live starts nothing, so none of them tells the model to
    make one. *open_failed*: the request to open the page is known to have failed."""
    if state == CRED_NOT_LOADED:
        what = (
            "The system could not be asked to open the credentials page (there is no program to open it with), so no page opened."
            if open_failed else
            "The credentials page was not loaded within 2 minutes of asking the system to open it."
        )
        return (
            what,
            "Setup is still running; do not call setup again while it runs, because that starts nothing. If a page did open, "
            "the user can still use it: setup stops listening 10 minutes after it began. A user with a terminal can run "
            "`nx config init` once the CLI is built (the build stage has ended) and type the service address, token and tenant there. "
            "A user with no terminal waits for the run to end (setup_status), then, if the user agrees, calls setup with mode cloud "
            "again: it opens a new page. Follow progress with setup_status.",
        )
    if state == CRED_REJECTED:
        return (
            f"The service address, token and tenant were refused: {reason or 'the check failed'} The page stays open for another try.",
            "Tell the user the page refused what they typed and is waiting for another try. Follow progress with setup_status.",
        )
    if state == CRED_ACCEPTED:
        return (
            "The service credentials were accepted and are held in memory; they are saved after the build, and are lost "
            "if the run ends before then.",
            "Follow progress with setup_status.",
        )
    if state == CRED_SAVED:
        return "The service credentials were saved.", ""
    if state == CRED_TIMED_OUT:
        return (
            "Nobody completed the credentials page in 10 minutes, so setup stopped listening.",
            "Setup is still running (the install carries on) and will end failed at the credentials step. Wait for the run to "
            "end (setup_status shows a terminal state); do not call setup again while it runs, because that starts nothing. "
            "Then, if the user agrees, call setup with mode cloud again: it opens a new page. A user with a terminal can "
            "run `nx config init` once the CLI is built.",
        )
    return (
        "Setup asked the system to open a page in the user's browser for the service address, token and tenant; waiting for credentials.",
        "Tell the user a page asking for the service address, token and tenant should have opened in their browser, and to keep "
        "that tab open until it says the credentials were accepted. Follow progress with setup_status.",
    )


#: Said when a run ends with a pair the page accepted and nothing saved (a build that failed, a kill, a signal).
_LOST_NOTE = "The service credentials the page accepted were not completely saved and are discarded."
_LOST_NEXT = (
    "The credentials must be entered again: calling setup with mode cloud again opens a new page. A user with a terminal "
    "can run `nx config init` instead."
)
_LOST_INTERRUPTED = "The service credentials the page accepted were held in memory only and are lost."


_PAGE_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem}"
    "label{display:block;margin-top:1rem}input{width:100%;box-sizing:border-box;padding:.5rem;font:inherit}"
    "button{margin-top:1.5rem;padding:.5rem 1.2rem;font:inherit}"
    ".note{background:#eef;padding:.75rem;border-radius:4px}.error{background:#fee;padding:.75rem;border-radius:4px}"
    ".hint{font-size:.9rem;margin:.25rem 0 0}"
)

#: The variables that take precedence over what the page saves.
_ENV_NAMES = ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_MINT_TENANT")


def _env_notice(environ: Any) -> str:
    """A sentence when ``NX_SERVICE_URL``, ``NX_SERVICE_TOKEN``, ``NX_MINT_TOKEN`` or ``NX_MINT_TENANT`` is set:
    those take precedence over what is saved. Names the variables, never their values."""
    names = [n for n in _ENV_NAMES if (environ.get(n) or "").strip()]
    if not names:
        return ""
    verb = "is" if len(names) == 1 else "are"
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{joined} {verb} set in the environment of the setup process, and take precedence over what this page saves."


class CredentialsPage:
    """The loopback page that takes a managed service's address, token and tenant (RDR-228, "The credentials page").

    :meth:`start` binds ``127.0.0.1`` on a port the system picks, writes the owner-only opener file and asks
    the system to open it. The page's address, ``http://127.0.0.1:<port>/<random path>``, is held in memory
    and in that one file; no log line or status field carries it, and no request without the path gets
    anything. A request whose ``Host`` is not the page's own gets nothing either.

    The caller calls :meth:`poll` as time passes (the setup process does it from its wait loops; a test
    does it after moving its clock). ``state`` is one of :data:`CREDENTIAL_STATES`. *clock*, *opener*,
    *checker* and *environ* are the seams the tests replace.
    """

    def __init__(
        self, setup: Path, *, clock: Any = None, opener: Any = None, checker: Any = None, environ: Any = None,
    ) -> None:
        self._setup = Path(setup)
        self._clock = clock if clock is not None else (lambda: _clock())
        self._opener = opener if opener is not None else _ask_system_to_open
        self._checker = checker if checker is not None else check_pair
        self._environ = environ
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._accepted: Accepted | None = None
        self._loaded = False
        self._path = ""
        self._submitted_url = ""
        self._submitted_tenant = ""
        self._listening_since = 0.0
        self._opened_at = 0.0
        self.state = CRED_WAITING
        self.reason = ""
        #: The request to open the page is known to have failed (the opener answered ``False``).
        self.open_failed = False
        self.port = 0
        self.bound_host = ""

    # -- read side

    @property
    def opener_path(self) -> Path:
        return self._setup / OPENER_NAME

    @property
    def address(self) -> str:
        """The page's address. Never log it, put it in a status or put it on a command line."""
        return f"http://{self.bound_host}:{self.port}{self._path}"

    @property
    def accepted(self) -> Accepted | None:
        """What the page accepted (kind, service address, token, tenant), held in memory until saved; ``None``
        before and after."""
        with self._lock:
            return self._accepted

    @property
    def accepted_pair(self) -> tuple[str, str] | None:
        """The accepted ``(service address, token)``; ``None`` before and after."""
        held = self.accepted
        return None if held is None else (held.url, held.token)

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    @property
    def listening(self) -> bool:
        return self._server is not None and not self._closed

    # -- life

    def start(self) -> None:
        """Bind, write the opener file, serve, ask the system to open the file. Raises OSError (nothing is left
        bound or written)."""
        import http.server  # noqa: PLC0415 -- lazy: the session-start hook imports this file and has 100 ms
        import secrets  # noqa: PLC0415
        import socket  # noqa: PLC0415

        page = self
        if os.name == "posix":  # the log holds the service address: a directory made wider earlier is tightened
            with contextlib.suppress(OSError):
                os.chmod(self._setup, 0o700)

        class Server(http.server.ThreadingHTTPServer):
            allow_reuse_address = False  # on Windows SO_REUSEADDR lets another process take the same port
            daemon_threads = True

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                # Live connections, oldest first, each with whether its request has been read yet.
                self._conns: dict[Any, bool] = {}
                self._conns_lock = threading.Lock()
                super().__init__(*args, **kwargs)

            def server_bind(self) -> None:
                _bind_exclusively(self.socket)
                super().server_bind()

            def process_request(self, request: Any, client_address: Any) -> None:
                # Over the cap, the OLDEST connection whose request is not yet read is shut down and the new one is
                # admitted: a real request is read in milliseconds, so what is still unread is a stall or a dripper,
                # and a dripper that reconnects the moment it is closed gets no more than the slot it just lost. A
                # connection past its read (a check in progress) is never evicted; if every slot is one, the new
                # connection is closed at the door.
                victim = None
                with self._conns_lock:
                    if len(self._conns) >= PAGE_MAX_CONNECTIONS:
                        victim = next((r for r, read in self._conns.items() if not read), None)
                        if victim is None:
                            admit = False
                        else:
                            del self._conns[victim]
                            admit = True
                    else:
                        admit = True
                    if admit:
                        self._conns[request] = False
                if victim is not None:
                    with contextlib.suppress(OSError):
                        victim.shutdown(socket.SHUT_RDWR)
                if not admit:
                    self.shutdown_request(request)  # closed at the door: no thread, no answer
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self._forget(request)
                    raise

            def _forget(self, request: Any) -> None:
                with self._conns_lock:
                    self._conns.pop(request, None)

            def _mark_read(self, request: Any) -> None:
                with self._conns_lock:
                    if request in self._conns:
                        self._conns[request] = True

            def process_request_thread(self, request: Any, client_address: Any) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._forget(request)

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # the default prints a traceback to standard error, which is the setup log

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "nx-setup"
            sys_version = ""
            timeout = 10.0  # per recv; it restarts on every byte, so PAGE_CONNECTION_SECONDS is the bound that holds
            _expiry: threading.Timer | None = None

            def setup(self) -> None:
                super().setup()
                # A connection that has not finished its request this long after accept is shut down. Without
                # it 16 sockets that send a byte every few seconds hold every slot for the page's whole life.
                self._expiry = threading.Timer(PAGE_CONNECTION_SECONDS, self._expire)
                self._expiry.daemon = True
                self._expiry.start()

            def _expire(self) -> None:
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)

            def _request_read(self) -> None:
                """The request is in: the deadline has done its job. What follows (a check that may take 20
                seconds, the reply) is bounded by the check's own deadline and the per-recv timeout."""
                if self._expiry is not None:
                    self._expiry.cancel()
                self.server._mark_read(self.request)  # type: ignore[attr-defined]

            def finish(self) -> None:
                self._request_read()
                super().finish()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the request line holds the path
                pass

            def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
                self._empty(code)

            def _empty(self, code: int) -> None:
                self._request_read()
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def _page(self, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                )
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _addressed(self) -> bool:
                """The request names the page's path and its own host and port: nothing else is answered."""
                path = self.path.split("?", 1)[0]
                host_ok = self.headers.get("Host") == f"{page.bound_host}:{page.port}"
                return host_ok and secrets.compare_digest(path.encode("utf-8", "replace"), page._path.encode("ascii"))

            def do_GET(self) -> None:
                self._request_read()
                if not self._addressed():
                    return self._empty(404)
                page._note_load()
                self._page(page._render())

            def _same_origin(self) -> bool:
                """The browser says this form post comes from the page itself. ``Sec-Fetch-Site`` is the browser's own
                word for it; ``Origin`` is checked when it is sent (``null`` is what the page's own post carries under
                its no-referrer policy, so it counts only beside a same-origin ``Sec-Fetch-Site``)."""
                site = self.headers.get("Sec-Fetch-Site")
                if site is not None and site not in ("same-origin", "none"):
                    return False
                origin = self.headers.get("Origin")
                if origin is None:
                    return True
                if origin == "null":
                    return site == "same-origin"
                return origin == f"http://{page.bound_host}:{page.port}"

            def do_POST(self) -> None:
                if not self._addressed():
                    return self._empty(404)
                if not self._same_origin():
                    return self._empty(403)
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._empty(400)
                if length < 0 or length > _PAGE_BODY_MAX:
                    return self._empty(413)
                import urllib.parse  # noqa: PLC0415 -- lazy, see start()

                raw = self.rfile.read(length)
                self._request_read()
                if len(raw) != length:  # the connection deadline (or the peer) ended the body early: nothing to submit
                    return self._empty(400)
                fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                page._note_load()
                busy = page._submit(
                    (fields.get("service_url") or [""])[0], (fields.get("service_token") or [""])[0],
                    (fields.get("tenant") or [""])[0],
                )
                self._page(page._render(busy))
                self.wfile.flush()
                if page.state == CRED_ACCEPTED:
                    threading.Thread(target=page._close_listener, name="nx-setup-page-close", daemon=True).start()

            def _other(self) -> None:
                self._empty(405 if self._addressed() else 404)

            do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other

        server = Server(("127.0.0.1", 0), Handler)
        try:
            self.bound_host, self.port = server.server_address[0], server.server_address[1]
            self._path = "/" + secrets.token_urlsafe(24)
            self._write_opener()
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="nx-setup-page", daemon=True,
        )
        self._thread.start()
        self._listening_since = self._clock()
        opened = self._opener(str(self.opener_path))
        self._opened_at = self._clock()
        if opened is False:  # a failure known at once is reported at once, not after two minutes
            with self._lock:
                self.open_failed = True
                if self.state == CRED_WAITING and not self._loaded:
                    self.state = CRED_NOT_LOADED

    def _write_opener(self) -> None:
        """The opener file: owner-only from the call that creates it (``_create_private``: POSIX
        ``os.open`` with ``O_EXCL`` and mode ``0o600``; Windows ``CreateFileW`` with a protected one-ACE
        descriptor), holding a redirect to the page."""
        import html  # noqa: PLC0415 -- lazy, see start()

        _sweep_opener(self._setup)
        address = html.escape(self.address, quote=True)
        text = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Nexus setup</title>'
            f'<meta http-equiv="refresh" content="0;url={address}"></head><body>'
            f'<p>Opening the Nexus credentials page. If nothing happens, <a href="{address}">open it here</a>.</p>'
            "</body></html>"
        )
        fd = _create_private(str(self.opener_path))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
        except BaseException:
            _remove_file(self.opener_path)
            raise

    def poll(self) -> None:
        """Apply the clock: ``page not loaded`` two minutes after the request to open the page, ``timed out``
        and the listener closed ten minutes after it began (unless a pair was accepted). Cheap; call it often."""
        now = self._clock()
        expired = False
        with self._lock:
            if self.state in (CRED_ACCEPTED, CRED_SAVED, CRED_TIMED_OUT):
                pass
            elif now - self._listening_since >= PAGE_LISTEN_SECONDS:
                self.state = CRED_TIMED_OUT
                self._accepted = None
                expired = True
            elif self.state == CRED_WAITING and not self._loaded and now - self._opened_at >= PAGE_LOAD_SECONDS:
                self.state = CRED_NOT_LOADED
        if expired or self.state == CRED_ACCEPTED:
            self._close_listener()
        if expired:
            _remove_file(self.opener_path)

    def mark_saved(self) -> None:
        """The credentials were saved: drop them from memory."""
        with self._lock:
            self._accepted = None
            self.state = CRED_SAVED

    def _close_listener(self) -> None:
        with self._close_lock:
            if self._server is None or self._closed:
                return
            self._closed = True
            server = self._server
        try:
            server.shutdown()
        finally:
            server.server_close()

    def close(self) -> None:
        """Stop listening and delete the opener file. Idempotent; the state does not change."""
        self._close_listener()
        _remove_file(self.opener_path)

    # -- the requests

    def _note_load(self) -> None:
        with self._lock:
            first = not self._loaded
            self._loaded = True
            self.open_failed = False
            if self.state == CRED_NOT_LOADED:
                self.state = CRED_WAITING
        if first:
            _remove_file(self.opener_path)  # the browser has the address now: the opener file has done its work

    def _submit(self, url: str, token: str, tenant: str = "") -> str:
        """Check what was typed. A pair that passes is held and the listener closes; one that fails is not kept.
        Returns ``""``, or a sentence when another check is running and this one was not made."""
        if not self._submit_lock.acquire(timeout=PAGE_BUSY_WAIT_SECONDS):
            return "A check of the credentials is already running; wait for its answer."
        try:
            with self._lock:
                if self.state in (CRED_ACCEPTED, CRED_SAVED, CRED_TIMED_OUT):
                    return ""
                self._submitted_url = url.strip()
                self._submitted_tenant = tenant.strip()
            try:
                address = _normal_address(url)
                ok, text, kind = self._checker(address, token.strip(), tenant.strip())
            except ValueError as exc:
                address, ok, text, kind = "", False, str(exc), ""
            with self._lock:
                if self.state == CRED_TIMED_OUT:
                    return ""
                if ok:
                    held_tenant = tenant.strip() if kind == KIND_MINT else ""
                    self._accepted = Accepted(kind, address, token.strip(), held_tenant)
                    self.state, self.reason = CRED_ACCEPTED, ""
                else:
                    self._accepted = None
                    self.state, self.reason = CRED_REJECTED, text
            return ""
        finally:
            self._submit_lock.release()

    def _render(self, busy: str = "") -> str:
        import html  # noqa: PLC0415 -- lazy, see start()

        with self._lock:
            state, reason, url, tenant = self.state, self.reason, self._submitted_url, self._submitted_tenant
        environ = os.environ if self._environ is None else self._environ
        notice = _env_notice(environ)
        parts = [
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Nexus setup</title>',
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><style>{_PAGE_STYLE}</style></head><body>',
            "<h1>Connect Nexus to a managed service</h1>",
        ]
        warn = _CLEARTEXT_WARNING if _sends_token_in_clear(url) else ""
        if state in (CRED_ACCEPTED, CRED_SAVED):
            if warn:
                parts.append(f'<p class="error">{html.escape(warn)}</p>')
            parts.append(
                '<p class="note">The service credentials were accepted. Setup saves them when the install finishes; if the '
                "install fails they are discarded and setup asks again. You can close this tab.</p></body></html>"
            )
            return "".join(parts)
        parts.append(
            "<p>Enter the address of your Nexus service, the access token for it and, for a mint credential, its tenant. "
            "They go to the setup program on this machine and are saved by the Nexus command line. They are not shown to "
            "Claude. Keep this tab open until it says the credentials were accepted: if you close it, setup waits until it "
            "stops listening, 10 minutes after it began, before it can open a new one.</p>"
        )
        if notice:
            parts.append(f'<p class="note">{html.escape(notice)}</p>')
        if busy:
            parts.append(f'<p class="note">{html.escape(busy)}</p>')
        if warn:
            parts.append(f'<p class="error">{html.escape(warn)}</p>')
        if state == CRED_REJECTED:
            parts.append(f'<p class="error">{html.escape(reason)} Try again.</p>')
        parts.append(f'<p class="hint">{html.escape(_CLEARTEXT_HINT)}</p>')
        parts.append(
            '<form method="post" action="">'
            '<label>Service address<input name="service_url" type="text" spellcheck="false" autocomplete="off" '
            f'value="{html.escape(url, quote=True)}"></label>'
            '<label>Token<input name="service_token" type="password" autocomplete="off" spellcheck="false"></label>'
            '<label>Tenant<input name="tenant" type="text" spellcheck="false" autocomplete="off" '
            f'value="{html.escape(tenant, quote=True)}"></label>'
            '<p class="hint">The tenant is needed for a mint credential, the kind a managed service issues. Leave it empty '
            "for a static service token, the kind a self-hosted service uses.</p>"
            '<button type="submit">Check and use</button></form></body></html>'
        )
        return "".join(parts)


# -- the run -----------------------------------------------------------------------------------


class _Run:
    """The detached process's state: the status it keeps current, the log it
    appends to, and the heartbeat that holds the lock."""

    def __init__(self, setup: Path, mode: str, autostart: bool, heartbeat: _Heartbeat) -> None:
        self.setup = setup
        self.mode = mode
        self.autostart = autostart
        self.heartbeat = heartbeat
        self.log_path = setup / _LOG
        self.data: dict[str, Any] = read_status(setup) or {}
        self.data.update(state=RUNNING, mode=mode, autostart=autostart, log=str(self.log_path), pid=os.getpid())
        self.steps_done: list[str] = []
        self.stage = "starting"
        self.notes: list[str] = []
        #: The credentials page, in mode cloud once it is serving.
        self.page: CredentialsPage | None = None
        self._page_seen: tuple[str, str] | None = None

    def update(self, **fields: Any) -> None:
        """Write the status. Raises :class:`_LockLost` and writes nothing when the
        lock is not this run's."""
        if not self.heartbeat.holds():
            raise _LockLost
        self.data.update(fields)
        self.data["updated_at"] = _now()
        write_status(self.setup, self.data)

    def at(self, stage: str, message: str = "") -> None:
        self.stage = stage
        fields: dict[str, Any] = {"stage": stage, "message": message or f"running: {stage}", "steps_done": list(self.steps_done)}
        page = self.page
        if page is not None:
            state, reason = page.state, page.reason
            self._page_seen = (state, reason)
            note, follow = credentials_notice(state, reason, open_failed=page.open_failed)
            fields.update(credentials=state, message=f"{fields['message']}. {note}")
            if follow:
                fields["next"] = follow
        self.update(**fields)

    def open_page(self) -> int | None:
        """Start the credentials page (mode cloud). ``None`` when it is serving, else the failed run's exit code:
        a run that cannot listen fails before anything is downloaded."""
        page = CredentialsPage(self.setup)
        try:
            page.start()
        except OSError as exc:
            page.close()
            return self.fail("credentials", f"the credentials page could not be started: {type(exc).__name__}: {exc}")
        self.page = page
        return None

    def close_page(self) -> None:
        """Stop listening and delete the opener file, whatever way the run ended."""
        if self.page is not None:
            self.page.close()

    def sync_page(self) -> None:
        """Apply the page's clock and, when its state changed since the status was written, write the status.
        Called from the run's own thread only, so the status has one writer."""
        page = self.page
        if page is None:
            return
        page.poll()
        if (page.state, page.reason) != self._page_seen:
            self.at(self.stage)

    def await_credentials(self) -> Accepted | None:
        """Wait for accepted credentials; ``None`` when the page timed out. Raises :class:`_LockLost`."""
        assert self.page is not None
        while True:
            if not self.heartbeat.holds():
                raise _LockLost
            self.sync_page()
            held = self.page.accepted
            if held is not None:
                return held
            if self.page.state == CRED_TIMED_OUT:
                return None
            time.sleep(_POLL_SECONDS)

    def step_done(self, step: str) -> None:
        self.steps_done.append(step)

    def end(self, state: str, message: str, next_step: str, **fields: Any) -> int:
        """Write the terminal status and return the exit code. Never raises."""
        text = " ".join([message, *self.notes])
        if self.page is not None:
            self.close_page()  # a terminal status means the listener is closed and the opener file is gone
            if self.page.state == CRED_ACCEPTED and state != DONE:
                # The pair lives in this process's memory only, and the run is over: it is gone. The six values
                # have none that says so, so the field goes and the words say it.
                self.data.pop("credentials", None)
                text = f"{text} {_LOST_NOTE}"
                next_step = f"{next_step} {_LOST_NEXT}"
            else:
                fields.setdefault("credentials", self.page.state)
        try:
            self.update(state=state, stage=self.stage, message=text, next=next_step, steps_done=list(self.steps_done), **fields)
        except _LockLost:
            return 1
        except OSError as exc:
            self._record_write_failure(exc)
            return 1
        return 0 if state in (DONE, UNSUPPORTED) else 1

    def _record_write_failure(self, exc: OSError) -> None:
        """The terminal status could not be written: say why where ``status`` will
        find it, if that can be written at all."""
        message = f"setup could not record its own progress ({type(exc).__name__}: {exc})"
        record = {
            "state": FAILED, "stage": self.stage, "failed_step": self.stage, "mode": self.mode, "log": str(self.log_path),
            "updated_at": _now(), "message": message,
            "next": f"Free disk space or fix the permissions on {self.setup}, then, if the user agrees, call setup again. Log: {self.log_path}",
        }
        with contextlib.suppress(Exception):
            _write_private_json(self.setup, _ERROR_FILE, record)
        with contextlib.suppress(Exception):
            sys.stderr.write(message + "\n")

    def fail(self, step: str, message: str) -> int:
        return self.end(
            FAILED, message,
            f"If the user agrees, call setup again: it resumes at the {step} step. Log: {self.log_path}",
            failed_step=step,
        )

    def log(self, text: str) -> None:
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)

    def _log_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _output_since(self, offset: int) -> str:
        """What the log holds after *offset*, its last :data:`_LOG_TAIL_BYTES` at most."""
        try:
            start = max(offset, self._log_size() - _LOG_TAIL_BYTES)
            with open(self.log_path, "rb") as handle:
                handle.seek(start)
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _scrub_log(self, offset: int, secret: str) -> None:
        """Replace *secret* in what the log holds after *offset*: a child that echoed what it was
        given on standard input must not leave it in the log."""
        try:
            with open(self.log_path, "r+b") as handle:
                handle.seek(offset)
                data = handle.read()
                clean = data.replace(secret.encode("utf-8"), b"***")
                if clean != data:
                    handle.seek(offset)
                    handle.write(clean)
                    handle.truncate()
        except OSError:
            pass

    def _record_child(self, pid: int) -> None:
        """Write the pid of the child program into the lock directory, so a reclaimer that finds this run dead
        can tell its build is still going. Only while the lock is this run's; a failed write changes nothing."""
        if self.heartbeat.holds():
            with contextlib.suppress(OSError):
                (self.setup / _LOCK / _CHILD).write_text(str(pid), encoding="utf-8")

    def _clear_child(self) -> None:
        if self.heartbeat.holds():
            with contextlib.suppress(OSError):
                (self.setup / _LOCK / _CHILD).unlink()

    def cmd(
        self, argv: list[str], *, timeout: float | None = None, stdin_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int | None, str]:
        """Run *argv* with its output going straight into the log; returns the exit
        code (``None`` after a timeout) and the output it printed (the log's tail).
        The setup directory is the working directory: uv and the CLI must not read a
        project from wherever the application started this. Raises
        :class:`_LockLost`, after killing the child, when the lock is lost.

        *stdin_text* is written to the child's standard input (a secret: it is never logged, and
        is replaced by ``***`` in the log and in the returned output should the child echo it);
        without it standard input is the null device. *env* replaces the child's environment when given."""
        limit = STEP_TIMEOUT_SECONDS if timeout is None else timeout
        if not self.heartbeat.holds():
            raise _LockLost
        self.log(f"\n--- {_now()} {' '.join(argv)}\n")
        offset = self._log_size()

        def shown(text: str) -> str:
            return text if not stdin_text else text.replace(stdin_text, "***")

        try:
            with open(self.log_path, "ab") as sink:
                proc = subprocess.Popen(  # noqa: S603 -- argv[0] is an absolute path found by the lookups above
                    argv, stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE, stdout=sink,
                    stderr=subprocess.STDOUT, cwd=str(self.setup), env=env,
                    encoding="utf-8",  # names the codec for the encoding lint and for the text written to standard input
                    **_child_kwargs(),
                )
                self._record_child(proc.pid)
                if stdin_text is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(stdin_text)
                        proc.stdin.close()
                    except OSError:  # the child exited without reading it
                        pass
                deadline = time.monotonic() + limit
                try:
                    while True:
                        try:
                            code = proc.wait(timeout=_POLL_SECONDS)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                        if not self.heartbeat.holds():
                            _kill_tree(proc)
                            self.log("--- killed: setup no longer holds the lock\n")
                            raise _LockLost
                        self.sync_page()
                        if time.monotonic() >= deadline:
                            _kill_tree(proc)
                            text = self._output_since(offset)
                            self.log(f"--- timed out after {limit:.0f} s\n")
                            return None, shown(text)
                except BaseException:
                    if proc.poll() is None:
                        _kill_tree(proc)
                    raise
        finally:
            self._clear_child()
            if stdin_text:
                self._scrub_log(offset, stdin_text)
        text = self._output_since(offset)
        self.log(f"--- exit {code}\n")
        return code, shown(text)


def _doctor_rows(text: str) -> list[str]:
    rows = [line.strip()[:_DOCTOR_ROW_CHARS] for line in text.splitlines() if "[✗]" in line]
    return rows[:_DOCTOR_ROWS_MAX]


def _is_nexus_cli(run: _Run, hit: str) -> bool:
    """A ``PATH`` hit is the Nexus CLI only with ``nx-mcp`` beside it and a
    version line in the Nexus form. A hit without ``nx-mcp`` is never run."""
    if _exec_path.which_off_cwd("nx-mcp", path=os.path.dirname(hit)) is None:
        return False
    if _cli_lookup.cannot_start(hit) is not None:  # found and cannot start: never run, never trusted
        return False
    try:
        code, text = run.cmd([hit, "--version"], timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return False
    return code == 0 and any(_VERSION_LINE.fullmatch(line.strip()) for line in text.splitlines())


def _bootstrap_would_act(run: _Run, nx: str) -> bool:
    """Whether the generation layout's *nx* says ``--bootstrap`` would still do
    something. ``nx self install --bootstrap --dry-run`` exits 0 when it would
    finish an interrupted install or build one and refuses (non-zero) when a
    complete install exists: the same judgment the build step would make, asked of
    the program that makes it."""
    try:
        code, _ = run.cmd([nx, "self", "install", "--bootstrap", "--dry-run"], timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return True  # it cannot even be started: whatever is installed is broken, and the build repairs it
    return code == 0


def _locate_nexus_cli(run: _Run) -> tuple[str | None, bool]:
    """``(nx, build_needed)``: the identified Nexus CLI, if any, and whether the
    build step must run."""
    layout = _layout_nx()
    if layout is not None:
        if _cli_lookup.cannot_start(layout) is not None:
            # Found and cannot start (an interpreter uv pruned; Sam, 2026-10-14). It is
            # not asked anything, so it cannot say "an install exists": the build runs,
            # and ``nx self install --bootstrap`` replaces the generation that cannot run.
            return None, True
        return layout, _bootstrap_would_act(run, layout)
    hit = _on_path("nx")
    if hit is not None and _is_nexus_cli(run, hit):
        return hit, False
    return None, True


def _nx_after_build(run: _Run) -> str | None:
    layout = _layout_nx()
    if layout is not None:
        return layout if _cli_lookup.cannot_start(layout) is None else None
    hit = _on_path("nx")
    return hit if hit is not None and _is_nexus_cli(run, hit) else None


#: Added to a failed ``nx doctor``'s next step in mode cloud: the saved credential may be what failed. Setup does not
#: start while a service answers (a saved address with a token resolves as one), so it cannot open the page again;
#: replacing the credential is ``nx config init`` in a terminal.
_CLOUD_DOCTOR_NOTE = (
    " If the service address, token or tenant may be wrong, the user replaces the saved credential with "
    "`nx config init` in a terminal; setup does not open the credentials page again while a service answers."
)


def lockstep_marker_path() -> Path:
    """The version-lockstep hook's marker, the CLI version last confirmed to match the plugin (the same path
    ``version_lockstep_hook.marker_path`` and ``version_lockstep_action.marker_path`` read and write)."""
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return _endpoint_resolve.default_config_dir() / "cli_lockstep_marker"


#: What the lockstep action reads out of ``nx --version`` (``version_lockstep_action._VERSION_RE``, which
#: ``tests/hooks/test_nx_setup.py`` compares with this one). Copied, not imported: that file is a hook program
#: that exits at import on an old Python and prints to standard error under ``NX_HOOK_DEBUG``.
_LOCKSTEP_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-]+)?)")


def _cli_version(run: _Run, nx: str) -> str | None:
    """The version *nx* reports, parsed the way the lockstep action parses it; ``None`` when it will not say."""
    try:
        code, text = run.cmd([nx, "--version"], timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return None
    match = _LOCKSTEP_VERSION_RE.search(text) if code == 0 else None
    return match.group(1) if match else None


def _record_lockstep(run: _Run, nx: str, version: str) -> None:
    """After a build, record *version* as in lockstep, but only when *nx* (the CLI :func:`_nx_after_build`
    returned) reports exactly that version: RDR-143's rule is a marker on confirmed lockstep, not on the intent to
    build it. A ``PATH`` ``nx`` or a leftover generation that is not the one just built reports another version and
    gets no marker, so the hook judges it. Without the marker for a build that did match, the next session's
    lockstep hook dispatches an upgrade that races nothing useful (found by the setup journey, 2026-10-15). A failed
    check or write is logged and changes nothing else."""
    seen = _cli_version(run, nx)
    if seen != version:
        run.log(f"the lockstep marker was not written: `nx --version` reported {seen or 'no version'}, the build was for {version}\n")
        return
    path = lockstep_marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version, encoding="utf-8")
    except OSError as exc:
        run.log(f"the lockstep marker could not be written: {exc}\n")


def build_env(platform: str | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of the build step. On Linux it names the CPU build of torch for the throwaway environment
    ``uv tool run`` makes to run ``--bootstrap``: without it uv installs PyPI's CUDA torch there, 14 ``nvidia-*``
    packages and triton, about 4.5 GB that Nexus never uses (measured by the setup journey on chas, 2026-10-15;
    the generation itself already gets CPU torch from ``install_generation.sh``, nexus-mt1tj). It is set through
    ``UV_TORCH_BACKEND``, which a uv too old to know the option ignores, rather than a flag it would refuse.
    Only ``NX_TORCH_BACKEND`` wins over ``cpu``, as in ``install_generation.sh`` (``${NX_TORCH_BACKEND:-cpu}``, passed
    as ``--torch-backend``): an ambient ``UV_TORCH_BACKEND`` is replaced, so the throwaway environment and the
    generation get the same torch. The CPU build comes from PyTorch's own CPU index (download.pytorch.org), a second
    host beside the package index for the throwaway environment."""
    platform = sys.platform if platform is None else platform
    env = dict(os.environ if environ is None else environ)
    if platform.startswith("linux"):
        env["UV_TORCH_BACKEND"] = env.get("NX_TORCH_BACKEND") or "cpu"
    return env


def _execute(run: _Run) -> int:
    if run.mode == MODE_CLOUD and not (run.data.get(CLOUD_SAVED) and _credentials_saved(config_dir())):
        failed = run.open_page()  # at the start of the run, so the user is asked while the build goes on
        if failed is not None:
            return failed
    run.at("build")
    nx, build_needed = _locate_nexus_cli(run)
    if build_needed:
        uv = _find_uv()
        if uv is None:
            return run.fail("build", "`uv` was not found on PATH, so the Nexus CLI cannot be built.")
        version = plugin_version()
        if version is None:
            return run.fail("build", "the plugin's version could not be read from its plugin.json.")
        build = [uv, "tool", "run", "--python", "3.12", "--from", f"conexus=={version}", "nx", "self", "install", "--bootstrap"]
        try:
            code, _ = run.cmd(build, env=build_env())
        except OSError as exc:
            return run.fail("build", f"the build could not start: {exc}")
        if code != 0:
            return run.fail("build", "the build timed out." if code is None else f"the build exited {code}.")
        nx = _nx_after_build(run)
        if nx is None:
            layout = _layout_nx()
            reason = _cli_lookup.cannot_start(layout) if layout is not None else None
            if reason is not None:
                return run.fail("build", f"the build finished but the `nx` it left cannot start: {reason}.")
            return run.fail("build", "the build finished but `nx` is not in the Nexus install directory or on PATH.")
        _record_lockstep(run, nx, version)
    assert nx is not None
    run.step_done("build")
    if run.mode == MODE_CLI_ONLY:
        return run.end(
            DONE, "The Nexus CLI is installed. No service is set up.",
            "Reconnect the nexus server or start a new session. To set up a service, ask the user first; if they agree, call setup with mode local.",
        )

    config = config_dir()
    code = _cloud(run, nx) if run.mode == MODE_CLOUD else _service(run, nx, config)
    if code is not None:
        return code

    run.at("verify")
    try:
        code, text = run.cmd([nx, "doctor"])
    except OSError as exc:
        return run.fail("verify", f"nx doctor could not start: {exc}")
    if code == 0:
        run.step_done("verify")
        return run.end(
            DONE, "Nexus is installed and nx doctor passes.",
            "Reconnect the nexus server or start a new session.", doctor="pass", doctor_failing=[],
        )
    rows = _doctor_rows(text)
    return run.end(
        DOCTOR_FAILED, "nx doctor reported problems." if code is not None else "nx doctor timed out.",
        f"Run `nx doctor` in a terminal to see the failing rows.{_CLOUD_DOCTOR_NOTE if run.mode == MODE_CLOUD else ''} "
        f"Log: {run.log_path}",
        doctor="fail", doctor_failing=rows,
    )


#: The status field a cloud run sets once it has saved what the page accepted. :func:`start` carries it into
#: the next run only when this one failed or was interrupted at a later step (not at ``nx doctor``, which may
#: have failed on the credential), so that resume skips the page. Replacing a saved credential is ``nx config init``
#: in a terminal: the real server's ``setup`` starts nothing while a service answers, and a saved address with a
#: token resolves as one.
CLOUD_SAVED = "cloud_saved"

#: The keys each kind of credential is saved under; saving one kind removes the other's.
_KIND_KEYS: dict[str, tuple[str, ...]] = {KIND_MINT: ("mint_token", "mint_tenant"), KIND_STATIC: ("service_token",)}


def _credentials_saved(config: Path) -> bool:
    """Whether the config file holds a service address and a token of either kind (``service_token`` or
    ``mint_token``). Only the file is read; the environment, which wins over it, is not what a run saved into."""
    try:
        saved = _endpoint_resolve.read_config_yml_credentials(config, ("service_url", "service_token", "mint_token"))
    except Exception:  # noqa: BLE001 -- unreadable is "not saved": the page opens
        return False
    return bool((saved.get("service_url") or "").strip()) and bool(
        (saved.get("service_token") or "").strip() or (saved.get("mint_token") or "").strip()
    )


def _saved_keys(config: Path, keys: tuple[str, ...]) -> list[str]:
    """The *keys* the config file holds a value for. Unreadable is none."""
    try:
        saved = _endpoint_resolve.read_config_yml_credentials(config, keys)
    except Exception:  # noqa: BLE001
        return []
    return [k for k in keys if (saved.get(k) or "").strip()]


def _cloud(run: _Run, nx: str) -> int | None:
    """The service step for mode ``cloud``: wait for the credentials the page accepts (it has been served since
    the start of the run, so this waits only if the user has not finished), then save them with ``nx config set``:
    for a mint credential ``mint_tenant`` then ``mint_token`` or for a static token ``service_token``, and
    ``service_url`` LAST: a token with no address is inert, so a save that stops half way never points a working
    local service at the new address. Never both kinds. A token goes in on standard input. No service is
    downloaded. On the resume that skipped the page (:data:`CLOUD_SAVED`) nothing is saved again. ``None`` means it
    went well."""
    if run.page is None:
        run.notes.append(
            "The credentials an earlier run saved are kept, so no credentials page was opened. To replace them, the "
            "user runs `nx config init` in a terminal."
        )
        run.step_done("credentials")
        return None
    run.at("credentials")
    held = run.await_credentials()
    if held is None:
        return run.end(
            FAILED, "Nobody completed the credentials page within 10 minutes, so setup stopped listening.",
            "If the user agrees, call setup with mode cloud again: it opens a new page. Or the user runs `nx config init` "
            f"in a terminal. Log: {run.log_path}",
            failed_step="credentials",
        )
    other = KIND_STATIC if held.kind == KIND_MINT else KIND_MINT
    # The other kind's keys go first: a static service_token left beside a new mint credential would stay in
    # force, since the CLI prefers it. Only keys the file holds are removed, so a CLI without `config unset` is
    # asked for it only when there is something to remove.
    steps: list[tuple[list[str], str | None, str]] = [
        (["unset", key], None, f"removing the saved {key}") for key in _saved_keys(config_dir(), _KIND_KEYS[other])
    ]
    if held.kind == KIND_MINT:
        steps += [
            (["set", "mint_tenant", held.tenant], None, "the tenant"),
            (["set", "mint_token", "--stdin"], held.token, "the token"),
        ]
    else:
        steps += [(["set", "service_token", "--stdin"], held.token, "the token")]
    steps.append((["set", "service_url", held.url], None, "the service address"))  # last: a token with no address is inert
    try:
        for tail, secret, what in steps:
            code, _ = run.cmd([nx, "config", *tail], stdin_text=secret)
            if code != 0:
                return run.fail("credentials", f"{what} failed." if tail[0] == "unset" else f"saving {what} failed.")
    except OSError as exc:
        return run.fail("credentials", f"nx config could not start: {exc}")
    assert run.page is not None
    run.page.mark_saved()
    run.update(**{CLOUD_SAVED: True})
    run.step_done("credentials")
    return None


def _service(run: _Run, nx: str, config: Path) -> int | None:
    """The service step for mode ``local``. ``None`` means it went well."""
    run.at("service")
    began_init = run.data.get("init") == INIT_BEGUN
    if not began_init and _endpoint_resolves(config):
        if run.autostart:
            run.notes.append(_AUTOSTART_NOTE)
        run.step_done("service")
        return None
    if not began_init and provisioned(config):
        if run.autostart:
            run.at("service-install")
            try:
                code, _ = run.cmd([nx, "daemon", "service", "install", "--autostart"])
            except OSError as exc:
                return run.fail("service-install", f"the login unit could not be registered: {exc}")
            if code != 0:
                return run.fail("service-install", "registering the login unit failed.")
        run.at("service-start")
        try:
            code, _ = run.cmd([nx, "daemon", "service", "start"])
        except OSError as exc:
            return run.fail("service-start", f"the service could not be started: {exc}")
        if code != 0:
            return run.fail("service-start", "starting the service failed.")
        run.step_done("service")
        return None
    run.at("init")
    run.update(init=INIT_BEGUN)
    argv = [nx, "init", "--yes"] + ([] if run.autostart else ["--no-autostart"]) + ["--no-beads-prime"]
    try:
        code, _ = run.cmd(argv)
    except OSError as exc:
        return run.fail("init", f"nx init could not start: {exc}")
    if code != 0:
        return run.fail("init", "nx init timed out." if code is None else f"nx init exited {code}.")
    run.update(init=INIT_FINISHED)
    run.step_done("service")
    return None


def run_setup(mode: str, autostart: bool, token: str) -> int:
    """The detached process: hold the lock, do the steps, leave a terminal status
    and no lock (a run that lost the lock leaves the new holder's alone and writes
    no status)."""
    setup = setup_dir()
    lock = setup / _LOCK
    if _owner_check(lock, token) is not True:
        sys.stderr.write("setup: this process does not hold the lock; exiting\n")
        return 1

    def on_term(signum: int, frame: Any) -> None:
        raise _Terminated(1)

    previous: dict[str, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(OSError, ValueError):
                previous[name] = signal.signal(getattr(signal, name), on_term)
    heartbeat = _Heartbeat(lock, token)
    heartbeat.start()
    run = _Run(setup, mode, autostart, heartbeat)
    try:
        try:
            run.at("starting", "setup is starting")
            return _execute(run)
        except _LockLost:
            return 1
        except _Terminated:
            return run.fail(run.stage, "setup was ended by a signal.")
        except Exception as exc:  # noqa: BLE001 -- a terminal status must be written whatever went wrong
            return run.fail(run.stage, f"unexpected {type(exc).__name__}: {exc}")
    finally:
        run.close_page()  # the listener and the opener file go with the run, whatever way it ended
        heartbeat.stop()
        release_lock(setup, token)  # a no-op unless the lock is still this run's
        for name, handler in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(getattr(signal, name), handler)


# -- starting a run ---------------------------------------------------------------------------


def _job_limit_flags() -> int | None:
    """``LimitFlags`` of the job this process is in, or ``None`` when it is in no
    job, is not on Windows, or the query fails (then the caller cannot tell and
    goes ahead)."""
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415 -- Windows-only branch
        from ctypes import wintypes  # noqa: PLC0415 -- Windows-only branch

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)) or not in_job.value:
            return None

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = Extended()
        if not kernel32.QueryInformationJobObject(
            None, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            return None
        return int(info.BasicLimitInformation.LimitFlags)
    except Exception:  # noqa: BLE001 -- cannot ask: do not refuse on a guess
        return None


def _job_would_kill_an_unbroken_child() -> bool:
    """The host's job closes on the last handle (``KILL_ON_JOB_CLOSE``) and allows
    no breakaway, so a process started without breakaway dies with it."""
    flags = _job_limit_flags()
    return (
        flags is not None
        and bool(flags & _JOB_KILL_ON_JOB_CLOSE)
        and not flags & (_JOB_BREAKAWAY_OK | _JOB_SILENT_BREAKAWAY_OK)
    )


def detach(argv: list[str], log_path: Path | str, *, windows: bool | None = None) -> None:
    """Start ``nx_setup.py *argv*`` so that it outlives the caller and every
    process above it.

    POSIX: a new session, standard input the null device, standard output and
    error the log, no inherited handles.

    Windows: a double launch. The intermediate (``_relay``) starts the run and
    exits, so the run's parent pid is dead and a tree kill of the server cannot
    reach it. Flags ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``, plus
    ``CREATE_BREAKAWAY_FROM_JOB`` first. When the host's job forbids breakaway the
    creation is refused with winerror 5 (the intermediate's or the run's), and the
    same double launch is tried again without breakaway, unless the host's job
    would kill it (:func:`_job_would_kill_an_unbroken_child`). Only when that is
    also refused is :class:`DetachUnsupported` raised. An intermediate that does
    not exit in 30 seconds is killed and reported as an :class:`OSError` (it is not
    retried: it may have started the run, which then finds the lock released and
    exits). The intermediate's standard handles are the null device, so it
    inherits no pipe of the server.

    Measured, native Windows, one host each: nexus_rdr/228-research-32 (a
    Popen flag set never survives a kill-on-close job that forbids breakaway; a
    double launch survives a tree kill) and 228-research-33 (Claude Code 2.1.296
    puts the server in a job with KILL_ON_JOB_CLOSE, BREAKAWAY_OK and
    SILENT_BREAKAWAY_OK, closes its stdin, and about 350 ms later runs a tree kill;
    a double launch survived 4 of 4 with or without breakaway, a plain child died).
    228-research-35: Claude Desktop 1.24012.9.0 uses the same job flags, and on a
    force-kill of Desktop all three watcher modes survived (silent breakaway keeps
    children out of the job; no tree kill ran). Desktop's graceful tray-Quit path is
    NOT measured.
    """
    windows = (os.name == "nt") if windows is None else windows
    script = os.path.abspath(__file__)
    if not windows:
        with open(str(log_path), "ab") as log:
            subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                [sys.executable, script, *argv], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=str(Path(str(log_path)).parent), env=_child_env(),
            )
        return
    for breakaway in (True, False):
        if not breakaway and _job_would_kill_an_unbroken_child():
            raise DetachUnsupported(
                "setup cannot detach on this host (its job closes on exit and allows no breakaway)"
            )
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
        relay_argv = [sys.executable, script, "_relay", *([] if breakaway else [_NO_BREAKAWAY]), *argv]
        try:
            relay = subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                relay_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, env=_child_env(),
            )
            code = relay.wait(timeout=30)
        except subprocess.TimeoutExpired:
            relay.kill()
            with contextlib.suppress(Exception):
                relay.wait(timeout=5)
            raise OSError("the setup relay did not return within 30 s and was ended") from None
        except OSError as exc:
            if getattr(exc, "winerror", None) != _WINERROR_ACCESS_DENIED:
                raise
            code = _WINERROR_ACCESS_DENIED
        if code == 0:
            return
        if code != _WINERROR_ACCESS_DENIED:
            raise OSError(f"the setup relay exited {code}")
    raise DetachUnsupported("setup cannot detach on this host (no WMI broker yet)")


#: The relay's first argument when the double launch is made without breakaway.
_NO_BREAKAWAY = "--no-breakaway"


def _relay(argv: list[str]) -> int:
    """The Windows intermediate: start the run as a process whose parent is this one,
    then exit, so the run's parent pid is dead."""
    breakaway = argv[:1] != [_NO_BREAKAWAY]
    if not breakaway:
        argv = argv[1:]
    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
    try:
        with open(str(setup_dir() / _LOG), "ab") as log:
            subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                [sys.executable, os.path.abspath(__file__), *argv], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=flags,
            )
    except OSError as exc:
        return _WINERROR_ACCESS_DENIED if getattr(exc, "winerror", None) == _WINERROR_ACCESS_DENIED else 1
    except Exception:  # noqa: BLE001 -- the caller reads only the exit code
        return 1
    return 0


def status_view(setup: Path, *, read_budget_s: float | None = None) -> dict[str, Any]:
    """What ``status`` prints: the status file plus what is true now. Reads only.

    *read_budget_s* bounds the time its two file reads may spend waiting out a Windows
    sharing violation, together: only the session-start hook passes it (its whole
    decision has 100 ms), and everything else keeps the two seconds each.

    ``state`` is one of :data:`ALL_STATES`. A status file that is missing, not
    JSON, not an object or holds a ``state`` outside :data:`STATES` reads as
    ``none``. ``running`` with no live lock reads as ``interrupted``. A
    ``status.error`` record (a run that could not write its status) overrides
    them with ``failed`` and the OS error text. Also ``live`` (a fresh lock),
    ``terminal``, ``not_now`` (the marker), ``next`` and ``log``.

    A stale lock whose recorded run (or build) is still alive reads as ``running`` and ``live``, with a ``holder``
    (``process N`` or ``its build (process N)``) and a ``next`` that says to wait and ask again, because a call
    to ``setup`` refuses to reclaim that lock and must not be suggested.
    """
    deadline = None if read_budget_s is None else time.monotonic() + read_budget_s

    def budget() -> float:
        return SHARING_RETRY_BUDGET_S if deadline is None else max(0.0, deadline - time.monotonic())

    data = read_status(setup, budget_s=budget()) or {}
    if data.get("state") not in STATES:
        data = {}
    recorded = _read_json(setup / _ERROR_FILE, budget_s=budget())
    if recorded is not None and recorded.get("state") == FAILED:
        data = {**data, **recorded}
    lock = setup / _LOCK
    live = _exists(lock) and not _lock_stale(lock)
    state = data.get("state", NONE)
    holder = _recorded_holder(setup, lock, data) if state == RUNNING and not live and _exists(lock) else None
    if holder is not None:
        # The heartbeat stopped (a sleeping machine does this) but the run, or the build it waits on, is still
        # there, and a call to setup refuses to reclaim the lock for that reason: the view says the same.
        live = True
        data["holder"] = holder
        data["message"] = f"{data.get('message', '')} The setup run's heartbeat stopped, but {holder} is still running.".strip()
        data["next"] = (
            f"Wait, then call setup_status again. Do not call setup while {holder} runs: it starts nothing. Log: {setup / _LOG}"
        )
    elif state == RUNNING and not live:
        state = INTERRUPTED
        data["next"] = f"Setup was interrupted. If the user agrees, call setup again with the same mode: it resumes. Log: {setup / _LOG}"
        if data.get("mode") == MODE_CLOUD and data.get("credentials") == CRED_ACCEPTED:
            # The accepted pair was in the dead run's memory only: the field is dropped and the words say it.
            data.pop("credentials")
            data["message"] = f"{data.get('message', '')} {_LOST_INTERRUPTED}".strip()
            data["next"] = (
                f"Setup was interrupted. If the user agrees, call setup again with the same mode: it resumes. {_LOST_NEXT} "
                f"Log: {setup / _LOG}"
            )
    data.setdefault("next", _NEXT_NONE)
    data.update(state=state, live=live, terminal=state in TERMINAL_STATES, not_now=(setup / _MARKER).exists())
    if setup.exists():
        data.setdefault("log", str(setup / _LOG))
    return data


def _error_view(message: str) -> dict[str, Any]:
    """What a command prints when it could not do its work."""
    return {
        "state": ERROR, "started": False, "live": False, "terminal": False, "not_now": False,
        "message": message, "next": "Nothing was started. Fix the problem the message names, then, if the user agrees, call setup again.",
    }


def start(mode: str, autostart: bool) -> dict[str, Any]:
    """The one way to start a run. Returns the status, with ``started``; an object
    whose ``state`` is ``error`` when the setup directory cannot be used."""
    setup = setup_dir()
    try:
        return _start(setup, mode, autostart)
    except OSError as exc:
        return _error_view(f"setup could not start: {type(exc).__name__}: {exc}")


def _start(setup: Path, mode: str, autostart: bool) -> dict[str, Any]:
    os.makedirs(setup, mode=0o700, exist_ok=True)
    marker = setup / _MARKER
    if mode == MODE_NOT_NOW:
        marker.write_text(_now(), encoding="utf-8")
        return {**status_view(setup), "started": False}
    token = acquire_lock(setup, wait_for_finish=True)
    if token is None:
        live = status_view(setup)
        refused = (
            f"A setup run is already in progress (mode {live.get('mode', 'unknown')}, stage "
            f"{live.get('stage', 'unknown')}). This request for mode {mode} started nothing."
        )
        if live.get("holder"):
            refused = (
                f"A setup run is still in progress: its heartbeat stopped, but {live['holder']} is still running "
                f"(mode {live.get('mode', 'unknown')}, stage {live.get('stage', 'unknown')}). This request for mode "
                f"{mode} started nothing. Wait, then call setup_status again."
            )
        return {**live, "started": False, "requested_mode": mode, "refused": refused}
    try:
        with contextlib.suppress(FileNotFoundError):  # only now: a call refused beside a live run leaves the user's marker
            marker.unlink()
        _sweep_tmp(setup)
        _sweep_opener(setup)  # a killed run cannot delete its opener file: the next run's start does
        with contextlib.suppress(FileNotFoundError):
            (setup / _ERROR_FILE).unlink()
        prior = read_status(setup) or {}
        log_path = setup / _LOG
        base: dict[str, Any] = {
            "state": RUNNING, "stage": "starting", "mode": mode, "autostart": autostart, "log": str(log_path),
            "started_at": _now(), "updated_at": _now(), "message": "setup is starting",
            "next": "Call setup_status to follow it.",
        }
        if "init" in prior:
            base["init"] = prior["init"]
        if (
            prior.get(CLOUD_SAVED) and mode == MODE_CLOUD and prior.get("mode") == MODE_CLOUD
            and prior.get("state") not in (DONE, DOCTOR_FAILED)
        ):
            # A resume keeps what the unfinished run saved. A finished run does not carry the mark, and neither
            # does one whose `nx doctor` failed, since the saved credential may be what failed. That decides what
            # this process does when it is started (the stand-in can start it); the real server's `run_setup`
            # starts nothing while a service answers, so replacing a saved credential is `nx config init`.
            base[CLOUD_SAVED] = True
        write_status(setup, base)
        argv = ["run", "--mode", mode, "--token", token] + (["--autostart"] if autostart else [])
        try:
            detach(argv, log_path)
        except DetachUnsupported as exc:
            base.update(state=UNSUPPORTED, message=str(exc), next=f"Install Nexus in a terminal. Log: {log_path}")
            write_status(setup, base)
            return {**status_view(setup), "started": False}
        except OSError as exc:
            base.update(
                state=FAILED, failed_step="start", message=f"the setup process could not be started: {exc}",
                next="If the user agrees, call setup again.",
            )
            write_status(setup, base)
            return {**status_view(setup), "started": False}
        token = ""  # the detached process owns the lock now
        return {**status_view(setup), "started": True}
    finally:
        if token:
            release_lock(setup, token)


def main(argv: list[str]) -> int:
    if argv[:1] == ["_relay"]:
        return _relay(argv[1:])
    import argparse  # noqa: PLC0415 -- only the command line needs it

    parser = argparse.ArgumentParser(prog="nx_setup.py", description="Install Nexus in the background.")
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="start a run")
    start_p.add_argument("--mode", choices=MODES, required=True)
    start_p.add_argument("--autostart", action="store_true", help="register the login unit (local mode)")
    sub.add_parser("status", help="print the status")
    sub.add_parser(MODE_NOT_NOW, help="write the not-now marker")
    run_p = sub.add_parser("run", help=argparse.SUPPRESS)
    run_p.add_argument("--mode", choices=MODES, required=True)
    run_p.add_argument("--autostart", action="store_true")
    run_p.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            return run_setup(args.mode, args.autostart, args.token)
        except Exception as exc:  # noqa: BLE001 -- the log is the only place this process can speak
            sys.stderr.write(f"setup: {type(exc).__name__}: {exc}\n")
            return 1
    try:
        if args.command == "status":
            out = status_view(setup_dir())
        elif args.command == MODE_NOT_NOW:
            out = start(MODE_NOT_NOW, False)
        else:
            out = start(args.mode, args.autostart)
    except Exception as exc:  # noqa: BLE001 -- every command prints one object, whatever happened
        out = _error_view(f"{type(exc).__name__}: {exc}")
    with contextlib.suppress(OSError):  # a caller that closed the pipe has stopped reading
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
