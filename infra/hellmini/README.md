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
| `install-watchdog.sh` | | Installs, and drift-checks, the two watchdog files. |

The hook and watchdog copies are verbatim from the host (no SPDX header, so a
repo copy and a live file compare equal).

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

It restarts (`launchctl kickstart -k`) only when a `Runner.Listener` is running,
no `Runner.Worker` is running (a job is never interrupted), the listener has been
up at least 300 s, the last restart was at least 900 s ago, and one of these
holds:

- **A.** the `Runner_*.log` the listener holds open (found with `lsof`; the newest
  file by name is wrong, since `Runner.Listener --version` writes a fresh one) has
  been silent for 3900 s, one missed token refresh.
- **B.** the listener has had no ESTABLISHED TCP `:443` connection for 300 s
  continuously and its log has been silent for 300 s.

Log silence alone is not a condition below 3900 s because an idle, healthy
listener writes its log only on its OAuth token refresh, every 50 minutes
(21 historical logs: the longest gap was exactly 50 minutes). A 5-minute silence
rule would restart an idle runner every 5 minutes. An idle listener instead holds
one long-poll connection to the broker open, so a vanished `:443` socket plus
silence is the signal for B. With no listener running (stopped, updating) it does
nothing.

Logs: `/Volumes/Bulk/ghrunner/actions-runner-watchdog/watchdog.log` (RESTART lines
with UTC time, listener pid, log name and reason; INFO lines when the `:443`
connection disappears or returns; rotates at 1 MiB to `watchdog.log.1`), and
launchd's own stdout/stderr in `/var/log/actions-runner-watchdog.launchd.log`
(off the Bulk volume on purpose). State files `last_restart` and `nosock_since`
sit beside `watchdog.log`.

Look without acting: `sudo /usr/local/libexec/actions-runner-watchdog --dry-run`
prints the evaluation and the command it would run. Thresholds can be overridden
through `WD_SILENT_SECS`, `WD_NOSOCK_SECS`, `WD_HARD_SILENT_SECS`,
`WD_COOLDOWN_SECS`, `WD_STARTUP_GRACE_SECS`, `WD_SOCK_PORT` and `WD_STATE_DIR`
(test seams; the daemon sets none).

Disable: `sudo launchctl bootout system/local.actions-runner-watchdog`.
Re-enable: `sudo launchctl bootstrap system /Library/LaunchDaemons/local.actions-runner-watchdog.plist`.
Remove: bootout, then delete the plist and the script.

### Installing the watchdog

On hellmini, from a checkout (passwordless `sudo`):

```
infra/hellmini/install-watchdog.sh          # drift check: diffs, exit 1 if live differs, exit 0 if live == repo
infra/hellmini/install-watchdog.sh --force  # overwrite differing files, backup first
```

Same rules as `install.sh`: a differing live file is never overwritten without
`--force`, the live file is first copied to `<file>.bak-<timestamp>`, and every
file is checked before any is written. A matching file with the wrong mode or
owner also counts as drift, and `--force` repairs it without a backup. After the
plist is installed or changed, the script runs `launchctl bootout` (not-loaded is
ignored) and `launchctl bootstrap system <plist>`; a script-only change needs no
reload, since launchd starts the script fresh on every run. Exit codes: 0 ok, 1
drift, 2 usage or missing directory, 3 `launchctl bootstrap` failed.

Off the host, `HELLMINI_WD_ROOT=<tmp>` puts a prefix in front of the two
destinations (launchctl is then skipped unless `HELLMINI_LAUNCHCTL` names a
stand-in), `HELLMINI_RUN_AS=<cmd>` replaces `sudo -n`, and `HELLMINI_WD_OWNER`
replaces `root:wheel`. `tests/test_hellmini_install_watchdog.py` drives it that way.
