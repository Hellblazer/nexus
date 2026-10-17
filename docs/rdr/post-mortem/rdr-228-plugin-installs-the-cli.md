# RDR-228 Post-Mortem: The Plugin Installs the CLI

**Closed** 2026-10-17 · **Accepted** 2026-10-13 · **Epic** nexus-sv2wx (32 children and 4 residual beads, all closed) · **Shipped** conexus 7.79.0 (Phase 1), 7.81.0 (Phase 2), 7.82.0 (installer follow-through, measured sizes, Desktop restart guidance); installer scripts and install pages live at hellblazer.github.io/nexus

## What the RDR set out to do

Adding the plugin did not give a user a working Nexus. The plugin's servers and
hooks are programs of the CLI, and nothing installed the CLI. A new user ran
five terminal commands on macOS or Linux, about nine on Windows, and a Claude
Desktop user who never opens a terminal could not get there at all. Sam's
requirement was that the CLI must not be an installation prerequisite.

The RDR proposed a launcher that serves a small stand-in server when the CLI is
missing, a setup tool on that stand-in that asks one question (local, cloud,
CLI only or not now) and builds the CLI and service in the background, a
loopback page for cloud credentials, and installer scripts plus rewritten
install pages for people who start from a terminal.

## Implementation status

Implemented in four phases, all shipped. The Minimum Viable Validation (the
release gate's matrix of six setup paths, on macOS and Windows, in Claude Code
and in Claude Desktop) ran on every cell but two, which Sam ruled covered on
2026-10-17: the Windows accept leg of the cloud row (the credentials page is the
same code, and its accept leg passed on macOS with Sam's own credential) and a
live Windows Desktop conversation (Desktop's prompt was measured on macOS, before
`setup` itself, and Claude Code asks on Windows). Desktop's local half on Windows
was run in a clean guest on 7.82.0 (T2 `nexus_rdr/228-mvv-2026-10-17-windows-desktop`):
rows 3, 4 and 6 pass, and rows 1 and 2 work but end in "doctor-failed" on a box
with no Git (divergence 9). A first close attempt on 2026-10-17 claimed the gate
complete before the Windows Desktop cells had run; the close critique caught it. The installer scripts were checked from nothing on Linux, macOS and
Windows (T2 `nexus/rdr-228-p3-publish-check`,
`nexus/rdr-228-p3-publish-check-macos-windows`). The record of every cell is on
bead nexus-sv2wx.24.

## Implementation vs plan

### As planned

- The launcher, the stand-in with its two tools, the one question, the setup
  process that outlives the session, the session-start hook's states, the
  `/conexus:setup` command, and resuming an interrupted setup.
- Cloud credentials typed on a loopback page, never in chat; a wrong token is
  refused and can be retried.
- Two install routes on the site: one command, or `uv` plus the plugin plus one
  question.

### Diverged

Each divergence carries its status as of the close.

1. **Claude Desktop keeps the stand-in until the app is quit and reopened.**
   The record assumed a reconnect or a new conversation would replace the
   stand-in with the real server. Desktop starts a plugin's server once, when
   the app starts, and reuses it for every conversation; it has no Reconnect
   command and showed no handler for a tool-list-changed notification. Found in
   the Desktop conversation on 2026-10-16 (nexus-93xx0). Status: worked around.
   Since 7.82.0 every reconnect instruction tells Desktop users to quit and
   reopen. The restart itself is still needed.
   *Drift class: unvalidated assumption.*

2. **Desktop offers plugin skills, not plugin commands.** `/conexus:setup` is
   not available in a Desktop conversation (T2 `nexus_rdr/228-research-38`).
   Status: worked around. The pages and the stand-in tell Desktop users to say
   "Set up Nexus".
   *Drift class: framework API detail.*

3. **The size table was wrong.** It said 1.6 GB on disk for Cloud and CLI only;
   measured from nothing the install took 2.1 to 2.3 GB. The pages said the
   Windows uv cache was 2.5 GB; it was 4.8 GB with 7.81.0. The 1.6 GB came from
   an early `uv tool install` environment, not the generation layout. Status:
   fixed in 7.82.0 (nexus-gxzfq): Local about 2.4 to 3.1 GB, Cloud and CLI only
   about 2.1 to 2.3 GB.
   *Drift class: unvalidated assumption.*

4. **On Linux the build pulled CUDA torch**, about 4.5 GB of GPU libraries
   Nexus never uses. Status: fixed. The installer and the plugin's setup ask uv
   for CPU torch. A uv older than 0.9.19 ignores that request; the pages tell
   such users to run `uv self update` first.
   *Drift class: missing failure mode.*

5. **Under launchd the start-at-login supervisor read the default config
   directory**, not the one it was given, so a non-default install came up
   with the wrong embedder. Status: fixed in 25f81bc32, with a test.
   *Drift class: missing failure mode.*

6. **On Windows the first session's startup hooks were cancelled** at their
   timeouts while uv fetched a Python (nexus-3ya9d). Status: fixed in 7.82.0
   with a 30 s cold-start allowance per hook; the Windows gate passed on the
   configuration that had failed four times. The longest cold hook was never
   measured, so the margin under the new timeouts is unknown.
   *Drift class: missing failure mode.*

7. **A CLI-only install by script left no setup record**, so the session-start
   hook asked about a service at every session. Status: fixed; the scripts
   write the record the plugin's setup writes.
   *Drift class: missing cross-cutting concern.*

8. **Testing start-at-login on macOS needed a throwaway user with a GUI
   login.** A same-account sandbox cannot give a launchd job the sandbox's
   home. Status: a test-setup need, met by the user nxmvv on hellmini.
   *Drift class: missing Day 2 operation.*

9. **On a Windows box with no Git, local setup ends in "doctor-failed".**
   `nx doctor` fails its Git row, so setup reports problems and sends the user to
   a terminal although the service and search work; after a reboot that record
   also hides the "service stopped, setup starts it" status. A Desktop-only
   Windows user usually has no Git. Found 2026-10-17 in the Windows Desktop cells.
   Status: open, nexus-36qyh (P1), fix in progress.
   *Drift class: missing failure mode.*

10. **A search right after a Windows reboot can report no collections.** A
   server that starts while the start-at-login service is still coming up
   answers "No collections match corpus 'knowledge'"; a retry seconds later
   works. Status: open, nexus-5rs3i.
   *Drift class: missing failure mode.*

## What stays open

- **No-Git setup on Windows** (divergence 9, nexus-36qyh) and **the login-race
  search text** (divergence 10, nexus-5rs3i).

- **Desktop needs a restart after setup** (divergence 1). Removing it would
  need Desktop to act on a tool-list change, or to restart plugin servers per
  conversation.
- **Windows cold-start headroom** (divergence 6) is unmeasured.
- **The Cowork path** (nexus-qkb9v, related, not part of this plan) is open.

## What the next person should take from this

**A host application's lifecycle is a measurement, not an assumption.** The
record knew Desktop's plugin behaviour was unmeasured and still wrote the
reconnect step from Claude Code's behaviour. Two of the eight divergences came
from Desktop doing something different, and both were found only when a person
held a real Desktop conversation, late in the release gate.

**A size printed to users needs a from-nothing measurement on every platform.**
The table's numbers came from one early environment and were wrong on all
three systems. The same run that corrected them also found the Linux CUDA
download.

**A test that needs a real login, a real keychain or a real launchd session
needs a real user.** The cells that ran in sandboxes on the same account could
not prove start-at-login or Desktop sign-in. A throwaway GUI user with a
keychain that unlocks at login did.

## Critique record

CRITIQUE_PLACEHOLDER
