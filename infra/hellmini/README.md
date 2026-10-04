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

The hook copies are verbatim from the host (no SPDX header, so a repo copy and a
live file compare equal).

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

