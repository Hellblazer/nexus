# macOS test guest (hellmini)

A throwaway macOS 27 VM on hellmini for validation that needs a real login
session: start at login through launchd, and a real reboot with the session
coming back by itself (nexus-ppuwc). It is the macOS counterpart of
`tests/e2e/windows-guest/`. Each run clones a clean image, runs RDR-228 MVV
row 1, copies the results back, and deletes the clone.

## Run it

From the laptop, in a checkout:

    tests/e2e/macos-guest/launch.sh [--out DIR] [--wheel PATH] [--permission-mode MODE] [--keep-guest]

It exits 0 only when exactly the 12 expected checks ran and passed, no
`sk-ant-` string appears anywhere under `DIR`, and the per-run VM is gone. It
exits 75 when hellmini is busy (a suite or engine build holds the lease, a
GitHub runner job is running, or a tart VM or `tart run` of ours exists), 3
when a dependency is missing or untrusted (tart, the clean image, the guest
key or its pinned host key, a file the scripts trust that someone else could
write, the tart account's GUI session, or the automation token). The last
line names the counts: `rows run 1, checks passed N, failed M`. Logs and the
guest's `~/nxmvv/out` land in `DIR` (default `$TMPDIR/nx-mac-run-<time>-<pid>`).

A run takes 3 to 12 minutes: 3 min 51 s on 2026-10-17 (evening, local) with a warm image, up to
about 12 when the setup turn is slow (it keeps re-checking the setup's status
after the setup has finished; the setup itself takes about a minute).

The row, in `row1.sh`: a `claude -p` turn answers the setup question "local,
start at login" with the repo's `conexus/` plugin (conexus itself comes from
PyPI unless `--wheel` stages a candidate), the setup finishes, launchd runs
`com.nexus.service`, a second turn stores a probe note through the MCP server.
The guest then reboots from inside (`sudo shutdown -r now`). A reboot counts
only when the boot session uuid (`kern.bootsessionuuid`) changed, ssh was seen
down in between, and the same `tart run` still owns the VM. After the reboot,
auto-login puts `admin` back on the console, launchd starts the job by itself
with a new pid, `nx doctor` passes, `nx search` still finds the note, and a new
Claude Code session finds it through MCP. `nx --version` is in two of the check
lines. `NX_NO_TELEMETRY=1` is set for every `claude` and `nx` the row starts.
`row1.sh` refuses to run unless `hw.model` starts with `VirtualMac` (hellmini
reports `Mac17,16`).

The automation token travels through `tests/e2e/lib/claude_credentials.py run
--remote hellmini` with `host.sh gssh` as the remote shell: ssh stdin from the
laptop to hellmini to the guest, then the environment of `row1.sh`. `row1.sh`
moves it into an unexported shell variable at once, so only the `claude -p`
turns get it in their environment; `nx`, `uv` and the rest never see it. It is
never in an argument, a file or a log, and the guest that held it is deleted.
(The environment block `row1.sh` itself was started with still holds it, as
with any process started that way; the VM is deleted at the end.)

## What a run holds

A suite needs about 23 GB of hellmini's 24, the guest 6 GB, so they cannot
overlap. The run enforces that for its whole duration, not only at the start:

- `host.sh hold` takes the hellmini `suite` lease (the same directory
  `tests/_suite_lease.py` and `scripts/lib/build-lease.sh` use, in the git
  common dir of `/Volumes/Bulk/src/nexus`) over an ssh session whose stdin
  `launch.sh` owns. A suite started mid-run finds it held and fails closed
  (exit 75); the runner overlap guard waits. The hold destroys the VM
  before it releases the lease; if the destroy fails the lease is still released
  and the deadman deletes the VM at its cap.
- `host.sh guard` reads those leases (`suite`, `service`) before the hold. An
  unreadable lease directory or pid file counts as busy, as in
  `tests/_suite_lease.py`; a pid-less lease younger than a minute counts as
  busy. It also refuses while any `Runner.Worker` runs for a runner user, or any
  `nx-mac-run-*` / `nexus-mac-build` VM or `tart run` process exists. A runner
  user that does not exist (`ghci` was deregistered 2026-10-04) is named and
  skipped, never read as "no job"; at least one runner user must exist, or the
  guard exits 3.
- If the laptop dies or the ssh session drops, the hold sees EOF, destroys the
  VM and releases. A host-side deadman, started by `host.sh start`, deletes the
  VM by name at 90 minutes whatever else happened (`destroy` stops it).
- `--keep-guest` passes `keep` to the hold: the VM and the lease stay until you
  run `host.sh destroy <vm>` or the 90 minute cap.

## Ending a run

`TERM`, `INT` or `HUP` to `launch.sh` acts at once: the phase in flight runs in
the background and `launch.sh` `wait`s for it, so the trap runs immediately and
kills the phase's process tree. The cleanup then runs with those three signals
ignored (children inherit that, so a second Ctrl-C cannot stop it halfway): collect
the guest's `~/nxmvv/out`, destroy the VM, wait for the hold to release, remove
the per-run directory. `host.sh destroy` retries three times, closes the ssh
sessions into the guest, stops the VM, kills its `tart run` as the tart account if
the stop does not take, and deletes it. The launcher tries `destroy` twice on top.
Measured on 2026-10-17 (evening, local): a `TERM` 15 s into the pre phase ended the launcher in 4 s
with no VM, `tart run`, run directory, deadman or lease left.

## Host layout

| What | Where |
|---|---|
| tart 2.32.1 | Homebrew, from the local tap `nexus/local` (formula in `infra/hellmini/tart/tart.rb`) |
| `TART_HOME` | `/Volumes/Bulk/tart/home` (owned by `nxmvv`); hhildebrand's `~/.zshenv` exports it too (`export TART_HOME=/Volumes/Bulk/tart/home`) |
| source image | `ghcr.io/cirruslabs/macos-golden-gate-base:latest` (macOS 27; account `admin`, auto-login, Remote Login, passwordless sudo, FileVault off) |
| clean image | `nexus-mac-clean`: the source plus uv and Claude Code in `~/.local/bin`, the guest key authorized, new ssh host keys, password ssh off, no nx; 4 CPU, 6 GB |
| per-run VM | `nx-mac-run-<time>-<pid>`, an APFS clone of the clean image, deleted at the end |
| guest key | `/Volumes/Bulk/tart/nexus/ssh/id_ed25519` (never leaves hellmini) |
| pinned host key | `/Volumes/Bulk/tart/nexus/ssh/known_hosts`: one line `* ssh-ed25519 <key>`, written by `build-image.sh` |
| disk | about 39 GB on `/Volumes/Bulk` for the pulled image and the clean image together (an APFS clone shares the pull's blocks). A per-run clone adds only what the run writes. |
| scripts, logs | `/Volumes/Bulk/tart/nexus/{runs,logs,deadman}`; a run's directory is removed by exact name |

## Host setup (once)

1. **tart.** `infra/hellmini/tart/tart.rb` is the formula installed on hellmini,
   verbatim: the `cirruslabs/cli` tap's `tart.rb` with its `softnet` dependency
   removed because Homebrew 7 would not load the tap's copy and softnet
   networking is not used. Diff it against upstream before upgrading tart.
   Install it from a local tap:

       brew tap-new nexus/local
       cp infra/hellmini/tart/tart.rb "$(brew --repository nexus/local)/Formula/tart.rb"
       brew install nexus/local/tart

2. **The tart account.** `nxmvv`, a throwaway standard account, signed in once at
   the login window (Screen Sharing) and kept signed in through fast user
   switching. Virtualization.framework boots a macOS guest only from a process in
   a GUI login session (outside one, `tart run` fails with `VZErrorDomain Code=-9
   ... Failed to get current host key`; measured 2026-10-17, also as root), and
   hellmini has FileVault on, so no account logs in by itself. `host.sh` runs every
   tart command as it with `sudo -n launchctl asuser <uid> sudo -n -u nxmvv`, so
   hhildebrand needs passwordless sudo. Create `TART_HOME` owned by `nxmvv`
   (`sudo install -d -o nxmvv /Volumes/Bulk/tart/home`).

3. **What the session costs.** The standing Aqua session is 198 processes and
   about 1.6 GB of summed resident memory (an upper bound; shared pages count once
   per process), measured on 2026-10-17 (evening, local) with no VM running, of hellmini's 24 GB.
   After a hellmini reboot someone must sign `nxmvv` in once again. Until then
   `preflight` exits 3 with "nxmvv (uid N) is not signed in at the hellmini login
   window; sign it in once (Screen Sharing)". It checks `launchctl print gui/<uid>`,
   which exits 0 for a signed-in account and 112 for a uid with no GUI domain
   (measured on hellmini, using an unused uid; `nxmvv` itself was not signed out).

4. **Directory trust.** `preflight` stats `/Volumes/Bulk/tart/nexus`, its `ssh`
   directory, the key, the pin, the staged `host.sh` and the run directory, and
   every ancestor up to `/`. Each must be owned by the invoking user or root and
   not writable by group or other (a root-owned sticky directory such as `/tmp` is
   the one exception), else exit 3 with the path and the `chmod` to run.
   `/Volumes/Bulk` was group-writable by `staff` until 2026-10-17 and is now 755.
   Not covered: the tart binary itself, which is Homebrew's (`/opt/homebrew/bin`
   and the Cellar are group-writable by `admin`).

5. **The runner users.** `host.sh` knows `ghrunner` and `ghci`
   (`NX_MAC_GUEST_RUNNER_USERS` is read only under `NX_MAC_GUEST_TEST=1`); edit the
   constant when a runner is added or removed.

## The guest hop

Over tart's NAT (vmnet shared mode, `192.168.64.0/24`; the guests seen on 2026-10-17 were
`.7` and `.8`) `host.sh` connects to `admin@<ip>` only when `tart ip` answers an
address in `192.168.64.2` to `.254`, with `StrictHostKeyChecking=yes` against the
pin and nothing else (`GlobalKnownHostsFile=/dev/null`, `BatchMode`,
`PasswordAuthentication=no`, `ServerAliveInterval`). The source image ships ssh
host keys that every copy of it shares and whose private halves are public, so
`provision.sh` deletes them and runs `ssh-keygen -A`; the pin is then the key of
our disk. `build-image.sh` reads the key through the Tart guest agent (not the
network), pins it before the first ssh, pins the regenerated one after
provisioning, and proves a strict ssh against the live sshd. `provision.sh` also
turns `PasswordAuthentication` and `KbdInteractiveAuthentication` off in
`/etc/ssh/sshd_config.d/050-nexus-keyonly.conf` (it sorts before
`100-macos.conf`, and sshd takes the first value it sees), after checking that a
key is authorized, and reads the effective config back with `sshd -T`. The admin
password stays `admin`: changing it breaks the auto-login that `kcpassword`
records, which the reboot row depends on; with both logins off it cannot be used
over the network.

## Build or rebuild the clean image

    tests/e2e/macos-guest/build-image.sh [--source OCI-REF]

Pulls the source when tart lacks it, boots a build clone, authorizes the guest
key and pins its host key through the Tart guest agent, runs `provision.sh`
(uv and Claude Code by their own installers; new host keys; key-only sshd; checks
FileVault off, auto-login, passwordless sudo, no nx), pins the new host key,
powers it off and renames it to `nexus-mac-clean`, replacing the old one. It
needs the `nxmvv` GUI session and an idle host (exit 75 otherwise; a leftover
`nexus-mac-build` also reads as busy: `host.sh destroy nexus-mac-build`). The
build holds only the guard, not the lease. Rebuilt 2026-10-17 (evening, local) in about a minute.

## Remove it all

    ssh hellmini 'for v in $(/Volumes/Bulk/tart/nexus/host.sh tart list -q --source local); do /Volumes/Bulk/tart/nexus/host.sh tart delete "$v"; done'
    ssh hellmini 'sudo rm -rf /Volumes/Bulk/tart && brew uninstall nexus/local/tart && brew untap nexus/local'

then drop the `TART_HOME` line from hellmini's `~/.zshenv` (backup
`~/.zshenv.bak-2026-10-17-tart`).

## Files

- `launch.sh`: the run, from the laptop
- `host.sh`: hellmini side (guard, preflight, hold, VM lifecycle, ssh into the guest). `NX_MAC_GUEST_TEST=1` unlocks the path, command and timing overrides the tests use; without it they are ignored
- `row1.sh`: the MVV row, inside the guest
- `build-image.sh`, `provision.sh`: the clean image
- `tests/test_macos_guest_driver.py`: runs `launch.sh`, `host.sh` and `row1.sh` against stand-in `ssh`, `scp`, `sudo`, `launchctl`, `pgrep` and `tart` (no hellmini, tart or credential)
