# infra/hellmini

Versioned copies of host configuration on `hellmini` (the Mac mini that runs the
`hellmini` release runner and hand-run test suites). The second runner,
`hellmini-ci` (user `ghci`), was deregistered on 2026-10-04 and its files removed
from here. Before this directory the
files existed only on the host, unreadable to the test user, with `.bak-*` files
as their history (nexus-xmj1r; audit T2 `nexus/audit-multihost-test-setup-2026-10-02`).
Host facts: T2 `nexus/hellmini-second-test-host-howto`.

## Files

| Path | Live location | What it is |
|---|---|---|
| `hooks/ghrunner/job-started.sh` | `/Volumes/Bulk/ghrunner/actions-runner/hooks/` | `hellmini` (release) job-started hook: port-band diagnostic, starts ghrunner's VM. Waits for nothing. |
| `hooks/ghrunner/job-completed.sh` | same | Prunes docker, stops the VM, clears tmp and caches. Never fails the job. |
| `colima/<user>-provision.yaml` | `colima.yaml` of each VM | The `provision:` block only. Record, not installed by `install.sh`. |
| `install.sh` | | Copies the hooks into place. |
| `watchdog/actions-runner-watchdog` | `/usr/local/libexec/` (root:wheel 755) | Runner watchdog script, run every 60 s by launchd. |
| `watchdog/local.actions-runner-watchdog.plist` | `/Library/LaunchDaemons/` (root:wheel 644) | The LaunchDaemon that runs it. |
| `install-watchdog.sh` | `/var/db/actions-runner-watchdog` (root:wheel 700, state and log) | Installs, and drift-checks, the two watchdog files and the state directory. |
| `tart/tart.rb` | `$(brew --repository nexus/local)/Formula/tart.rb` | The tart formula (local tap `nexus/local`) for the macOS test guest. Record, not installed by `install.sh`; host setup is in `tests/e2e/macos-guest/README.md`. |

The hook and watchdog files in the repo are the source: the installers copy them
repo to host, byte for byte (no SPDX header, so a repo copy and a live file compare
equal, and the drift check relies on it).

## Port ranges

Both colima VMs forward published container ports into one host port
namespace, so each VM's dockerd allocates from its own range (set by the
`provision:` block through `net.ipv4.ip_local_port_range`, read by dockerd once
at start):

| User | Range |
|---|---|
| hhildebrand | 32768-39999 |
| ghrunner | 40600-44999 |

40552 is `tailscaled` (host `*:40552`), which is why ghrunner starts at 40600.
49152 and up is macOS's ephemeral range. Root cause: T2
`nexus/debug-hellmini-ci-colima-forwarding`.

## colima.yaml

The provision blocks live in:

- `/Volumes/Bulk/caches/hhildebrand/colima/default/colima.yaml`
- `/Volumes/Bulk/ghrunner/build-caches/colima/default/colima.yaml`

Colima rewrites `colima.yaml` on start and may drop comments, so a provision block
carries none; these files and the T2 record are the explanation. To restore a
block after a colima reset, paste the file's `provision:` over the `provision:`
key in the instance's `colima.yaml` (replace the `provision: null` or the empty
list) and restart the instance. An already-booted VM keeps its
`/etc/sysctl.d/90-nexus-docker-portrange.conf`.

hhildebrand's VM (hand suites and gates) mounts two paths writable, since
2026-10-02, so a battery worktree can live under `/Volumes/Bulk/src/nexus-wt/`
instead of `$HOME`:

```
mounts:
  - location: /Users/hhildebrand
    writable: true
  - location: /Volumes/Bulk/src
    writable: true
```

An explicit `mounts:` list replaces colima's implicit `$HOME` mount, so the home
entry must stay; write it as a full path, because colima turns `~` into an empty
string and then refuses the start ("overlapping mounts"). A mount change needs
`colima stop` and `colima start --memory 10 --cpu 8` (`COLIMA_HOME` as in the
list above), and only while no container or battery is running.

## Installing the hooks

On hellmini, from a checkout:

```
infra/hellmini/install.sh              # drift check: prints diffs, exit 1 if live differs
infra/hellmini/install.sh --force      # overwrite differing files, backup first
infra/hellmini/install.sh --force ghrunner # one user only
```

Needs passwordless `sudo` (the hooks are mode 0700 and owned by the runner user).
A live file that differs from the repo copy is never overwritten without
`--force`; with it, the live file is first copied to `<file>.bak-<timestamp>`,
and keeps its mode. All files are checked before any is written. Take care to
install while no job is running on that runner.


## Runner watchdog (nexus-ey1me)

`actions-runner-watchdog` restarts the `hellmini` runner
(`system/local.actions-runner`) when its `Runner.Listener` is alive but stalled.
launchd runs it as root every 60 s (`local.actions-runner-watchdog`); it does
nothing if `/Volumes/Bulk/ghrunner/actions-runner/bin` is absent (volume not
mounted).

**It is a precaution, not a fix for a known hang.** The 2026-09-29 episode that
prompted it was not a hang. The `BrokerServer ... Operation canceled` error in
that log is the listener cancelling its own Busy long-poll at the end of every
job (it appears 20-40 times in each busy log, here at 21:00:48 and again at
21:10:50); the seven quiet minutes after it were an ordinary idle long-poll, and
the same process picked up the next job on its own. What the watchdog catches is
a listener that is alive but has stopped talking to the broker: worst case it
restarts the runner 3900 s of log silence after the stall began, plus up to 60 s
for the next tick (about 65 minutes).

It restarts (`launchctl kickstart -k`) only when exactly one `Runner.Listener` is
running, no `Runner.Worker` is running, the listener has been up at least 300 s,
the last restart was at least 900 s ago, and one of these holds. A running job is
not interrupted by design, but the guarantee is not absolute: the last re-check
(below) and the kickstart are separated by a window of tens of milliseconds
(36-130 ms measured before the re-check was added). It is reachable only after
5-65 minutes of listener quiet, when no job should be starting, and a job that
did start in it would lose its runner.

- **A.** the `Runner_*.log` the listener holds open has been silent for 3900 s,
  one missed token refresh. The log is found with `lsof` (the newest file by name
  is wrong, since `Runner.Listener --version` writes a fresh one). If `lsof` fails
  or the listener holds no `_diag/Runner_*.log` open, it falls back to the newest
  `Runner_*.log` by modification time, which a stray `--version` run can make look
  fresher than the real one; the verbose line and `last_tick` say which was used
  (`logsrc=lsof` or `logsrc=newest`).
- **B.** the listener has had no ESTABLISHED TCP `:443` connection for 300 s
  continuously and its log has been silent for 300 s. B is skipped on any tick
  where `lsof` fails, times out, or exits 0 without listing the listener's own
  `p<pid>` record (an ERROR line in the log): an unknown is not "no connection".

The listener and the Worker are found with `pgrep -u ghrunner -f '^<runner
dir>/bin/Runner.…'`: the process must belong to the CI user and its command line
must start with this runner's directory, so another runner (for example the
`ghconexus-cred` one) or another user's forged argv neither hides this listener
nor fakes a job. (Reading the pid from `launchctl print system/local.actions-runner`
was rejected: its `pid =` is the wrapper script, not the listener, so it would
need a process-tree walk and one more command that can fail.) If `pgrep` itself
fails (exit 2 or more) the listener check does nothing that tick and logs ERROR,
and the Worker check assumes a job is running and logs ERROR. Two matching
listeners are ambiguous: ERROR, no action.

Immediately before the kickstart it looks again for a `Runner.Worker`, the same
single listener pid, and an unchanged listener log mtime, and abandons the restart
(`ABORT` line in the log) if any changed.

**Repeat guard.** If the listener the watchdog started (no older than the time
since `last_restart`) is again eligible for a restart within 1800 s of that
restart and past its 300 s startup grace, the restart did not cure it. The script
logs one `ERROR ... did not cure the listener`, creates `halted` in the state
directory, deletes `last_restart`, and restarts nothing more until a human removes
`halted` (`sudo rm /var/db/actions-runner-watchdog/halted`). Ticks while halted
stay quiet; `last_tick` shows `halted=1`. This replaces the earlier behaviour of
restarting every 15 minutes through a sustained outage. The cost: after a
GitHub-side outage that outlasts the first restart, auto-restart stays off until
someone removes the file. `--dry-run` reports the halt and writes nothing.

Log silence alone is not a condition below 3900 s because an idle, healthy
listener writes its log only on its OAuth token refresh, about every 50 minutes:
the idle gap is 50-51 minutes (the longest was 3058 s over 23 logs), not exactly
50. A 5-minute silence rule would restart an idle runner every 5 minutes. An idle
listener instead holds one long-poll connection to the broker open, so a
vanished `:443` socket plus silence is the signal for B. With no listener running
(stopped, updating) it does nothing. The 50-minute cadence has not yet been
observed on runner 2.338.0.

**State and log are in `/var/db/actions-runner-watchdog/` (root:wheel 700), not
on the runner volume.** The script runs as root, and the parent of the old
location (`/Volumes/Bulk/ghrunner`) belongs to the CI user, who could rename and
replace a root-owned directory there and feed the script's arithmetic. Now every
value read from a state file must be digits (and not in the future) or it is
treated as absent and the file is removed; it is never evaluated. State is
written to a temp file in the same directory and renamed into place. The
directory being root-only means no one else can plant a symlink in it; the script
also refuses a symlinked, non-root-owned or group/other-writable state directory.

Files there: `watchdog.log` (RESTART, ABORT, ERROR and WARN lines with UTC time,
listener pid, log name and reason; INFO lines when the `:443` connection
disappears or returns; rotates at 1 MiB to `watchdog.log.1`), `last_restart`,
`nosock_since`, `halted` (present only after the repeat guard fired) and
`last_tick`.

**Is it alive?** A healthy idle watchdog writes nothing to `watchdog.log`, which
looks the same as one that is not running. `last_tick` tells them apart: every tick
that gets as far as the decision (a listener is up, no job is running, a log is
found) overwrites it atomically with one line, for example

```
epoch=1792272000 pid=19158 uptime=2081 silent=1500 sockets=1 lsof_ok=1 logsrc=lsof halted=0
```

`sudo cat /var/db/actions-runner-watchdog/last_tick` twice, a minute or more
apart: `epoch` must advance by about 60 and `pid` must be the listener's
(`pgrep -u ghrunner -f Runner.Listener`). While a job runs, or the listener is
stopped, no tick reaches the decision and the file stands still; that is not a
fault. A `last_tick` that stays still with an idle listener up means launchd is not
running the daemon (`sudo launchctl print system/local.actions-runner-watchdog`). launchd's own stdout/stderr go to
`/var/log/actions-runner-watchdog.launchd.log` (off the Bulk volume on purpose).
`lsof` and `launchctl` calls are killed after 20 s and 30 s.

Look without acting: `sudo /usr/local/libexec/actions-runner-watchdog --dry-run`
prints the evaluation and the command it would run. `--dry-run` skips the restart
only: it still writes `nosock_since` and INFO lines to `watchdog.log`.

Disable (persistently): `bootout` only stops it until the next reboot, so also
disable it:

```
sudo launchctl bootout system/local.actions-runner-watchdog
sudo launchctl disable system/local.actions-runner-watchdog
```

Re-enable: `sudo launchctl enable system/local.actions-runner-watchdog`, then
`sudo launchctl bootstrap system /Library/LaunchDaemons/local.actions-runner-watchdog.plist`
(or re-run `install-watchdog.sh --force`, which does both).
Remove: bootout and disable, then delete the plist, the script and the state
directory.

### Installing the watchdog

On hellmini, from a checkout (passwordless `sudo`):

```
infra/hellmini/install-watchdog.sh          # drift check: diffs, exit 1 if live differs, exit 0 if live == repo
infra/hellmini/install-watchdog.sh --force  # overwrite differing files, backup first
```

It refuses (exit 2) unless `/Volumes/Bulk/ghrunner` exists, so a stray run on
another Mac installs nothing. Same rules as `install.sh`: a differing live file
is never overwritten without `--force`, the live file is first copied to
`<file>.bak-<timestamp>`, and every item is checked before any is written. A
matching file with the wrong mode or owner counts as drift, and `--force` repairs
it without a backup. The state directory is created `root:wheel 700` and counts in
the drift check the same way; a symlink directly inside it (the watchdog never
makes one) is drift too, and `--force` removes the link without following it. After the plist is installed or changed, the script
runs `launchctl bootout` (not-loaded is ignored), `launchctl enable` (so a daemon
left disabled by `launchctl disable` comes back) and `launchctl bootstrap system
<plist>`. If the plist is unchanged but something else was written (or `--force`
was given) and the daemon is not loaded, it is enabled and bootstrapped too; a
plain drift check on a booted-out daemon only prints a note. A script-only change
to a loaded daemon needs no reload, since launchd starts the script fresh on
every run. Exit codes: 0 ok, 1 drift, 2 usage, missing directory or not hellmini,
3 `launchctl bootstrap` failed.

Off the host, `HELLMINI_WD_ROOT=<tmp>` (must be non-empty) puts a prefix in front
of the destinations (launchctl is then skipped unless `HELLMINI_LAUNCHCTL` names a
stand-in), `HELLMINI_RUN_AS=<cmd>` replaces `sudo -n`, `HELLMINI_WD_OWNER`
replaces `root:wheel`, and `HELLMINI_WD_HOSTMARK` replaces the
`/Volumes/Bulk/ghrunner` check. `tests/test_hellmini_install_watchdog.py` drives
the installer that way.

The watchdog's own logic is tested in `tests/test_hellmini_watchdog.py`: the
script runs for real against a fake runner directory, a tmp state directory and
stand-ins for `ps`, `lsof`, `launchctl`, `date` and (mostly) `pgrep`, through
the env seams `WD_RUNNER_DIR`, `WD_RUNNER_USER`, `WD_STATE_DIR`, `WD_PATH`
(directories searched for the commands), `WD_TARGET`, `WD_LSOF_TIMEOUT`,
`WD_LAUNCHCTL_TIMEOUT` and the threshold overrides `WD_SILENT_SECS`,
`WD_NOSOCK_SECS`, `WD_HARD_SILENT_SECS`, `WD_COOLDOWN_SECS`,
`WD_STARTUP_GRACE_SECS`, `WD_REPEAT_WINDOW_SECS`, `WD_SOCK_PORT` (the daemon sets
none). A few tests run the real `pgrep` against processes with forged argv
(`bash -c 'exec -a "<dir>/bin/Runner.Listener run" sleep'`), because a stub that
ignores its flags cannot notice `-f` or `-u` being removed.

**These tests are macOS-only on purpose.** The script reads BSD `stat`, `ps`,
`lsof` and `pgrep` output and runs only on hellmini, and CI is Linux, so the
module skips itself off darwin (`pytest -rs` shows the reason) and no CI lane
runs it. Run it on a Mac before changing the script:
`NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_hellmini_watchdog.py -o addopts=""`.
`tests/test_hellmini_install_watchdog.py` runs everywhere.
