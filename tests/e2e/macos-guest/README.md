# macOS test guest (hellmini)

A throwaway macOS 27 VM on hellmini for validation that needs a real login
session: start at login through launchd, and a real reboot with the session
coming back by itself (nexus-ppuwc). It is the macOS counterpart of
`tests/e2e/windows-guest/`. Each run clones a clean image, runs RDR-228 MVV
row 1, copies the results back, and deletes the clone.

## Run it

From the laptop, in a checkout:

    tests/e2e/macos-guest/launch.sh [--out DIR] [--wheel PATH] [--permission-mode MODE] [--keep-guest]

It exits 0 only when every check passed and the per-run VM is gone, 75 when
hellmini is busy (a suite or engine build holds the lease in
`/Volumes/Bulk/src/nexus`'s git common dir, or a GitHub runner job is running),
and 3 when a dependency is missing (tart, the clean image, the guest key, the
tart account's GUI session, or the automation token). The last line names the
counts: `rows run 1, checks passed N, failed M`. Logs and the guest's
`~/nxmvv/out` land in `DIR` (default `$TMPDIR/nx-mac-run-<time>-<pid>`).

A run takes about 12 minutes; most of it is the setup turn, which keeps re-checking the setup's status after it has finished (the setup itself takes about a minute). The busy check runs once, at the start. A suite started while the guest runs
is not stopped, so do not start one by hand during a run.

The row, in `row1.sh`: a `claude -p` turn answers the setup question "local,
start at login" with the repo's `conexus/` plugin (conexus itself comes from
PyPI unless `--wheel` stages a candidate), the setup finishes, launchd runs
`com.nexus.service`, a second turn stores a probe note through the MCP server.
The guest then reboots from inside (`sudo shutdown -r now`); the boot time must
change and the same `tart run` must still own the VM. After the reboot,
auto-login puts `admin` back on the console, launchd starts the job by itself,
`nx doctor` passes, `nx search` still finds the note, and a new Claude Code
session finds it through MCP. `NX_NO_TELEMETRY=1` is set for every `claude`
and `nx` the row starts.

The automation token travels through `tests/e2e/lib/claude_credentials.py run
--remote hellmini` with `host.sh gssh` as the remote shell: ssh stdin from the
laptop to hellmini to the guest, then the environment of `row1.sh`. It is
never in an argument, a file or a log, and the guest that held it is deleted.

## Host layout

| What | Where |
|---|---|
| tart 2.32.1 | Homebrew, from the local tap `nexus/local` (`$(brew --repository)/Library/Taps/nexus/homebrew-local/Formula/tart.rb`): the `cirruslabs/cli` tap's formula with its two `depends_on :macos` lines and the `softnet` dependency removed, because Homebrew 7 refuses to load them. Softnet networking is not used. |
| `TART_HOME` | `/Volumes/Bulk/tart/home` (owned by `nxmvv`); hhildebrand's `~/.zshenv` exports it too |
| source image | `ghcr.io/cirruslabs/macos-golden-gate-base:latest` (macOS 27; account `admin`, password `admin`, auto-login, Remote Login, passwordless sudo, FileVault off) |
| clean image | `nexus-mac-clean`: the source plus uv and Claude Code in `~/.local/bin`, the guest key authorized, no nx; 4 CPU, 6 GB |
| per-run VM | `nx-mac-run-<time>-<pid>`, an APFS clone of the clean image, deleted at the end |
| guest key | `/Volumes/Bulk/tart/nexus/ssh/id_ed25519` (never leaves hellmini) |
| disk | about 39 GB on `/Volumes/Bulk` for the pulled image and the clean image together (an APFS clone shares the pull's blocks; `df` before the pull and after the build). A per-run clone adds only what the run writes. |
| scripts, logs | `/Volumes/Bulk/tart/nexus/{runs,logs}`; a run's directory is removed by exact name |

### Why tart runs as nxmvv

Virtualization.framework boots a macOS guest only from a process inside a GUI
login session. Over ssh, as hhildebrand or as root, `tart run` fails with
`VZErrorDomain Code=-9 ... Failed to get current host key` (measured
2026-10-17). hellmini has FileVault on, so no account logs in by itself;
`nxmvv` is the throwaway account kept signed in there (fast user switching), and
`host.sh` runs every tart command as it with `launchctl asuser`. After a
hellmini reboot, someone must sign `nxmvv` in once at the login window (Screen
Sharing); until then `launch.sh` exits 3 and says so. `NX_MAC_GUEST_RUN_AS`
names a different account.

## Build or rebuild the clean image

    tests/e2e/macos-guest/build-image.sh [--source OCI-REF]

Pulls the source when tart lacks it, boots a build clone, authorizes the guest
key through the Tart guest agent, runs `provision.sh` (uv and Claude Code by
their own installers; checks FileVault off, auto-login, passwordless sudo, no
nx), powers it off and renames it to `nexus-mac-clean`, replacing the old one.

## Remove it all

    ssh hellmini 'for v in $(/Volumes/Bulk/tart/nexus/host.sh tart list -q --source local); do /Volumes/Bulk/tart/nexus/host.sh tart delete "$v"; done'
    ssh hellmini 'sudo rm -rf /Volumes/Bulk/tart && brew uninstall nexus/local/tart && brew untap nexus/local'

then drop the `TART_HOME` line from hellmini's `~/.zshenv` (backup
`~/.zshenv.bak-2026-10-17-tart`).

## Files

- `launch.sh`: the run, from the laptop
- `host.sh`: hellmini side (guard, preflight, VM lifecycle, ssh into the guest); `NX_MAC_GUEST_LEASE_ROOT` points the guard at a stand-in lease directory for testing it
- `row1.sh`: the MVV row, inside the guest
- `build-image.sh`, `provision.sh`: the clean image
