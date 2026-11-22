---
title: "Session Identity: Named Seats That Outlive Sessions, a Binding Log, One Resolver, and Fenced Identity State"
id: RDR-253
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-16
accepted_date: 2026-11-16
related_issues: [nexus-65a9k, nexus-gff3g, nexus-5daww, nexus-fe96p, nexus-r0d37, nexus-dmyrm, nexus-d8yul]
related: [RDR-208, RDR-252, RDR-245, JDR-001, JDR-002]
---

# RDR-253: Session Identity

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. A **session** is one running Claude Code instance; about a dozen run at
once on Sam's laptop, each for days. A session is **compacted** (its context
summarised), **cleared** (`/clear`: a new session id, same process),
**resumed** (`/resume`: an old session id brought back, sometimes in a new
process), **restarted** (by `claude-restart-sessions`) and **renamed**
(`/rename`). The **engine** is nexus's Java service; it stores **tuples**
(small records addressed by a **subspace** such as `mailbox/marginalia`, each
family described by a **template**). **T1** is session scratch, **T2** is
persistent notes, the **threads board** is one T2 entry per session that Sam
reads. A **token** is a credential the engine issues; a **stamp** is the
session id the engine records on a write made with one. A **seat** is this
RDR's new term: a long-lived working identity such as `marginalia` or
`halcyon`, which many sessions occupy one after another. A **generation** is
a count of the processes that have held a seat. To **fence** a write is to
refuse it because it comes from a process that no longer holds the seat. A
**compare-and-set** writes a value only if the current value is still the one
the writer read, so two writers racing cannot both succeed.

Origin. Sam, 2026-11-16, after RDR-252 failed two gate rounds on identity:
"believe we must nail identity ... let's not solve the identity problem but
solve *our* identity problem"; "identity should have stuff like own t2
records, scratch, persistent id, some such stuff to keep long term coherence
beyond a name".

## Problem Statement

Nothing in our stack says, durably, who a session is. Twenty-five separate
identifiers answer "which session is this" (research 2), none survives every
event a session goes through, and the engine trusts only two of them. State
hangs off whichever identifier was handy when it was written, so it strands,
merges or misattributes when that identifier changes. Forty-six incidents
from 2026-06 to 2026-11-16 trace to this (research 1), including the deletion
of a live session's whole scratch scope in a released version.

### Enumerated gaps to close

#### Gap 1: Several sources answer "who am I", and they disagree

`NX_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, the machine-wide
`~/.config/nexus/current_session` file (last writer wins), the hook payload,
the MCP server's spawn-time id and the Claude registry file can all name
different sessions (research 2, conflicts C1, C2, gap G5). Incidents: T1
unreachable from the CLI (nexus-gff3g, P0), T2 rows labelled with another
window's session (nexus-d8yul), a token and a `from` naming different ids
after `/clear` (research 1, I-19).

#### Gap 2: State is keyed to an identifier that does not survive the events it must

Session ids change on `/clear`; names change on each process start unless
`-n` is passed, on `/rename`, and can be held by two sessions; the threads
entry's `session:` line is written by hand and nothing rewrites it (research
2, G1, G3). Incidents: mail stranded at an old id or delivered to two
sessions or the wrong one (research 1, I-10, I-21, I-25 to I-28); 11 of 16
Desktop compactions lost their checkpoint (nexus-dmyrm); a stale board
re-sent closed work (I-35).

#### Gap 3: A child acts with its parent's authority

A `claude -p` child inherits the parent's `NX_SESSION_ID` and is attributed to
the parent (research 2, G9); a subagent shares the parent's token (JDR-001).
Incident: a child's session end deleted its parent's entire T1 scope in a
released version (GH #1454, nexus-65a9k, P0); a nested child rotated the
parent's token (nexus-5daww, P0). This RDR closes the gap for `claude -p`
children; Agent-tool subagents stay attributed to the parent by decision,
until Claude Code identifies their calls (Critical Assumption 3).

#### Gap 4: Liveness and ownership are guessed from pid, name, cwd or git user

Incidents: another session's tmux server killed by name pattern (I-36); a live
agent's worktree removed because `ps` said it had gone (I-44); a session that
had run `/clear` answered "ready" while its old wait loop still ran (I-38);
every bead is claimed as the git user, so nothing records which session owns
it (I-42). A restart round leaves two holders of a name for 300 seconds
(I-22).

#### Gap 5: Writes carry no trustworthy attribution

Only the MCP server holds a session token; hooks, every Bash child (every
`nx` typed in a shell, the push script) and subagents write unstamped
(research 2, section 3). And any holder of the tenant token can mint a
session token for any session id (`SessionTokenHandler.java:97-118`), so the
stamp means "presented a token for this id". Incidents: reply squatting and
forged `from` verified on develop (nexus-b747g); RDR-252's push records would
all be unstamped (`nexus_rdr/252-fix-check-cee51c460fc1`).

## Relationship to Prior RDRs

- **RDR-208** (session-id mail addressing, closed): its identity table is the
  starting point; this RDR replaces "the session id is the address" with "the
  seat is the address".
- **JDR-001** (T1 scopes): T1's session scope stays; this RDR adds
  identity-scoped durable scratch beside it and extends the handoff marker;
  JDR-001 is now four scopes, with a revision entry (Sam approved 2026-11-18).
- **JDR-002** (tuple sender claims): the stamp stays a label everywhere except
  identity-owned subspaces, where a superseded generation is refused (Sam,
  2026-11-16); JDR-002 gains a revision entry (Sam approved 2026-11-18).
- **RDR-252** (aura groups and one responder, draft): parked until this lands;
  its memberships, owners and push records name identities.
- **RDR-245** (structured asks): asks are filed under the asking seat.
- **RDR-238** (memory curation, closed): a seat's `identity/<id>` project is an
  ordinary T2 project, no new tier. `nx memory curate --t2` and `--cold` read the
  `identity/*` projects beside the repository's own, and the cold section credits a
  transcript `memory_get` of `identity/<id>/<title>` to the project `identity/<id>`.
  The use measure is RDR-238's: transcript reads, not `access_count`.
- **RDR-209** (heat graduation, closed in part): `identity/*` projects are excluded
  from any graduation or promotion that rides the T2 heat counter, whatever a later
  phase builds; a seat's notes are its own standing context and do not graduate.

## Context

### Background

Sessions are long-lived and many. A seat such as `marginalia` carries work
across days: a board entry, worktrees, beads, mail, asks, background runs.
Today that continuity exists only as convention (keep the board's `session:`
line current, name sessions with `-n`, choose your own name and check it is
free) and breaks whenever a convention is missed. Sam reads the board and
answers asks by seat name; peers address each other by seat name; the code
addresses by session id.

### Technical Environment

- Claude Code 2.1.325 (the installed binary is the source of truth; the
  `~/git/claude-code` tree is a March snapshot): the session id is minted at
  start, regenerated by `/clear`, switched by `/resume`; every Bash child
  carries `CLAUDE_CODE_SESSION_ID` set to the current id; hooks receive
  `session_id` and a `source` (startup, resume, clear, compact) on stdin; the
  registry file `~/.claude/sessions/<pid>.json` records id, name, pane and a
  process start time, and is deleted on exit (research 2).
- nexus: the MCP server mints a session token at start (`core.py:951`) and
  keeps a 60-second `directory/<name>` heartbeat (`subscriptions.py`); a
  four-tier resolver picks a session id (`session.py:121-203`); hooks write
  `current_session`, session markers and T1 handoff markers.
- The engine: tokens map to a tenant and, for session tokens, a session id;
  `sender_session` is stamped only for a minted session token
  (`TupleHandler.java:279-290`).

## Research Findings

### Investigation

Four read-only lanes on 2026-11-16, synthesised in T3 `agent-orchestration`,
"Our session identity problem: four-lane research synthesis (2026-11-16)":
lane A, 46 incidents from T2, beads, memory files, transcripts and
checkpoints (research 1); lane B, a census of every identifier from source
and the installed binary (research 2); lane C, identity models in Sam's own
code (research 3); lane D, outside systems (research 4).

### Key Discoveries

1. The session id is a key, not an identity. Stereotomy, Sam's KERI-style
   identifier library, keeps a stable identifier and rotates keys under an
   append-only event log, each rotation proving the prior one committed to it
   (`apollo/stereotomy/.../processing/Validator.java:101-110`). Our `/clear`,
   resume and restart are rotations; nothing records them (research 3).
2. Identity and activation are separate things. Orleans keeps a grain's
   identity while activations come and go, at most one at a time; Erlang
   re-points a registered name at a new process atomically
   (`re_register_name`). Using the display name as identity, as
   `claude-squad` does (`session/instance.go:31-33, 205`), is the failure
   pattern we have lived (research 4).
3. A new holder must advance an epoch. Fireflies (the membership and
   failure-detection layer of Sam's Apollo) filters a note whose epoch is not
   newer than the current one (`apollo/fireflies/.../View.java:297-301`); a
   generation number per holding process lets the engine refuse a superseded
   process's writes, which closes the two-holders window (research 3, 4).
4. Ids come from a monotone, never-reused allocator with tombstones, as
   nexus's catalog tumblers already do (`src/nexus/catalog/tumbler.py:142`)
   (research 3).
5. Only the MCP server can hold a credential today, and every Bash child
   already carries the current session id in `CLAUDE_CODE_SESSION_ID`
   (research 2). So a resolver can map any process to its identity through
   the binding log, with only a token file (a small local file that holds the
   identity's token; see Handover to the MCP server) and two variables added
   (`NX_SEAT`, and `NX_IDENTITY_TOKEN`, the path of a delegated child's token
   file).
6. Our threat is a confused model, not a hostile party (JDR-002). Signatures,
   witnesses, quorum admission, failure-detector math and attestation chains
   are out (research 3, 4).

### Critical Assumptions

- [x] On `/clear`, the SessionStart hook can tie the new session id to the
  prior binding by process continuity (the same Claude pid and process start
  time as the current binding). **Status**: Verified by spike
  (`nexus_rdr/253-research-5`): the `clear` hook ran under the same Claude
  pid and start time, its payload carried the new id with `source: clear`,
  and the registry file already held the new id with pid, `procStart`, pane
  and name unchanged. A `/resume` in a new process kept the session id
  (`source: resume`) and restored the name; name drift on resume comes from
  a shell wrapper passing a fresh `-n`, not from Claude Code. The registry's
  `startedAt` was the same before and after the `/clear` (1794848034834), so
  the `startedAt` that keys a process is stable across `/clear`; an in-place
  `/restart` changes it (`nexus_rdr/253-research-10` (1)). Confirmed for
  `/clear` only: the registry file already holds the new session id when the
  hook runs. Not yet confirmed for an in-process `/resume` of another
  conversation. The hook waits up to 5 seconds for a registry entry whose
  `sessionId` is the payload's, so if the registry lags at a `/resume` the
  hook binds nothing at that `/resume` and writes a pending file (see
  Binding).
- [x] Every Bash child of a session carries the current session id in
  `CLAUDE_CODE_SESSION_ID`. **Status**: Verified by binary read and observed
  in a subagent's environment (research 2, row 2).
- [refuted] The MCP server can tell a call made by an Agent-tool subagent from one
  made by its parent. **Status**: Refuted for Claude Code 2.1.325
  (`nexus_rdr/253-research-6`): of every tool call nexus logged, none carried
  `claudecode/agentId` in `_meta`, including the research subagents' calls,
  though the binary defines that key. The fallback applies: delegation covers
  `claude -p` children; subagent writes stay attributed to the parent seat
  with an `agent` label, as today. Re-checked on each Claude Code upgrade.
- [partly] A Claude Desktop session can run the SessionStart hook and bind.
  **Status**: Verified in part, from records. The hook runs on Desktop: the
  2026-11-03 compaction audit saw SessionStart hooks fire after all 16
  Desktop compactions (T2 `nexus/compaction-restore-audit-2026-11-03`), and
  the hook receives the session id on stdin. But the Desktop-spawned MCP
  server never sees the session id (JDR-002, F17, nexus-wkjqm), so it cannot
  pick up the identity token. A Desktop seat therefore binds through its
  hook, and its MCP-side writes stay unattested labels, never fenced, until a
  bridge carries the session id to that server. Not yet observed: a Desktop
  session whose hook times out waiting for its registry file and is bound
  later from the pending file (see Binding). That path is written and tested
  against stand-ins only.

## Proposed Solution

### Approach

1. **Seats.** An identity is an engine record with an opaque id that is never
   reused and one or more names. A seat is an identity Sam creates once by
   name, by Sam (`nx identity create`) or by the restart script. Before it
   restarts a session under a name the user set (registry `nameSource` is
   `user`), in place or by relaunch, the script keeps a seat the session
   already holds (the relaunch carries `NX_SEAT`), else promotes the session's
   ephemeral identity to a seat of that name, else creates one
   (`created_by: restart`; a cloud install's script holds only the data credential, so it promotes with the
   session's own token and creates none); a launch that names no seat, or gives `-n`
   a name that is not a seat's, gets an ephemeral identity that can be
   promoted to a seat (Sam, 2026-11-16).
2. **A binding log.** Each time a session takes, keeps or loses a seat
   (start, resume, clear, fork, take-over, release, rename, end, lapse,
   revive), the engine appends a binding
   row. A generation counts the processes that have held the seat: it rises
   only when a different process takes it.
3. **One resolver.** Every surface (MCP server, hooks, CLI, scripts) asks one
   function "which identity is this process", passing the session id it can
   see; the function answers from the binding log and refuses when two
   sources disagree.
4. **Fenced identity state.** On four families of identity-owned state the
   engine checks that a write comes from the owner and from its current
   generation (Sam, 2026-11-16, decision 2 as amended the same day); anyone
   may still send a seat mail. Elsewhere the stamp stays a label.
5. **Delegated children.** A Bash child acts as its session's identity; a
   `claude -p` child that the dispatcher grants tools gets a delegated
   sub-identity that may read the parent's state and write only its own (a
   child with no tools cannot reach nexus and gets none). Agent-tool subagents keep the parent's seat
   with an `agent` label until Claude Code identifies their calls (Sam,
   2026-11-16, narrowed the same day).
6. **State moves onto the identity.** Phase 1 moves the threads entry, the
   mailbox and names, durable scratch and per-identity T2, and group
   memberships and push records (Sam, 2026-11-16).

### Technical Design

**Identity records.** New engine tables, all tenant-scoped, not tuples: a
tuple expires (a refire is clamped to `created_at` plus the template's
retention), and the compare-and-set on the generation needs one transaction.
Only the mailbox's `address_kind` gains `identity`.

```
identity_records      id  kind=seat|ephemeral|delegated  parent=<id or none>  generation=<n>  state=unheld|live|lapsed|ended
                      display_name=<name or null>  created_by=<sam|restart|session|dispatch|identity:i<n>>  created_at=<UTC>  ended_at=<UTC or null>
identity_aliases      name  identity=<id>  state=live|retired  since=<UTC>  retired_at=<UTC or null>  renamed_to=<name or null>
identity_bindings     identity=<id>  seq  event=start|clear|resume|fork|takeover|release|rename|end|lapse|revive|gone  generation=<n>  lease=leased|unleased
                      session_id=<uuid>  host=<machine key, else hostname>  pid=<n>  started_at=<ms>  proc_start=<UTC, to the second>  pane=<tmux pane or null>
                      client_version=<conexus version>  prior_session_id=<uuid or null>
                      holder_gone={host, pid, started_at, proc_start} or null  at=<UTC>
identity_sessions     session_id  identity=<id>  generation=<n>        (the session index)
identity_counters     one row per tenant; last_n only rises
identity_memberships  (identity, group)  role=leader|member  joined_at=<UTC>   (RDR-252 re-keys onto this; the join time stays when the role changes)
identity_work         (identity, kind, ref)  kind=watcher|wait|labrun|worktree|tmux|agent  state=running|done   (background work and owned
                      resources; state updated in place; a label, not fenced; the row has no expiry, but a running row of a wait, labrun,
                      agent or watcher reads stale by the Step 6 rules; `agent` needs engine identity-005)
```

The identity record holds the current generation and state, so pruning
binding rows never loses them. The id is `i<n>`, allocated by the engine
from a per-tenant counter that only grows, so an id never names a second
identity; names matching `i<digits>` are reserved and cannot be aliases. An
alias names one live identity at a time.

**A generation is one process holding a seat** (Sam, 2026-11-16, decision 2 as
amended). A process is one start of a Claude Code process, known by its host
(a machine key, defined under "A host is a machine key" below), its pid and the `startedAt` its registry file records. Claude Code's in-place
`/restart` keeps the pid and the OS start time but writes a new `startedAt`,
which is how the restart script already tells a new start
(`nexus_rdr/253-research-9` (a); measured in `nexus_rdr/253-research-10` (1)). A seat's state is `unheld` when created or released, `live` while its
holder renews, `lapsed` after 300 seconds without renewal, and `ended` after a
clean exit. The generation goes up only when a different process takes the seat: a
launch, a resume in a new process, or an explicit take-over. Inside one
process the session id can change (`/clear`, `fork`, `/branch`) without
changing the generation: the binding log appends a row with the new session
id under the same generation, and the session index maps both ids to it. So
the MCP server's token, and every Bash child of the process, stay on the
current generation across `/clear`, which the `/clear` spike shows happens in
the same process (`nexus_rdr/253-research-5`). The generation check stops a
superseded process: the old process after a restart, a resume elsewhere or a
take-over.

**A host is a machine key, not a hostname** (GH #1679; code on branch `feature/gh1679-stable-host-identity`, tip `a4ac5cfa3d`,
reviews T2 `nexus/review-4e48325fa-gh1679-2026-11-21-r2` and `nexus/critique-4e48325fa-gh1679-2026-11-21-r2`). Wherever this
document says a binding, a token file or a holder names a "host", it means `this_host()` of the process that wrote it: the
machine's key, else its hostname. The first design used `gethostname()`, which follows DHCP and VPN: a laptop was
`ip-10-122-0-102.ec2.internal` one day and `-105` the next, so a resumed session met its own old binding as another host,
judged it by its lease alone and was refused the seat although the holder was dead on this machine (`hostname.py` module
docstring: "a resumed session met its own old binding as \"another host\"").

*What the key is derived from.* `nxh1-` plus the first 32 hex digits of an HMAC-SHA256 whose key is an identifier the OS keeps
for the life of the install and whose message is a fixed domain string and the identifier's source name
(`hostname.py:host_key`: `return KEY_PREFIX + digest[:32]`). The raw identifier never leaves the machine, and the key is never
cached on disk (module docstring: "A key is never cached on disk: a copied config directory would carry it to another
machine."). The identifier on each OS:

- macOS: `IOPlatformUUID`, read from `/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice` (`hostname.py:_macos_uuid`:
  `_IOREG_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')`). Not `LocalHostName`, which Migration Assistant copies to a
  new Mac (module docstring).
- Linux: `/etc/machine-id`, then `/var/lib/dbus/machine-id`, read only by a process in the initial pid namespace and only when
  the text is 32 hex digits and not all zeros (`hostname.py:_linux_machine_id`: `if pid_ns() != INIT_PID_NS:` then `return None`;
  `if _HEX32.fullmatch(text) and text != "0" * 32:`). A container that bind-mounts the host's file has another process table, so
  it gets no key.
- Windows: the `MachineGuid` value under `HKLM\SOFTWARE\Microsoft\Cryptography`, read with
  `winreg.QueryValueEx(key, "MachineGuid")` (`hostname.py:_windows_machine_guid`).

*The fallback.* When no identifier is safe to equate (none readable, `ioreg` missing or timing out, an unusable Linux id, a
Linux process outside the initial pid namespace, another OS), `this_host()` returns the hostname, escaped when the hostname
itself starts with the key prefix so a fallback name is never read as a key: `return f"host-{name}" if
name.startswith(KEY_PREFIX) else name` (`hostname.py:this_host`). A failed probe is retried once at once and then not for
`PROBE_RETRY_S = 60.0` seconds (`hostname.py:_stable_host_cached`); a success is kept for the life of the process. On macOS
and Windows a probe that gave nothing is logged once as `host_key_unavailable`. A false "same host" is the dangerous error,
because this machine's pid probe would then judge a holder on another machine and free a live seat; the refusals above exist
so a fallback is never weaker than the hostname.

*A key that is not this machine's is never local.* After the equal-host case, a holder whose host is any other key is judged
not local with no further test (`hostname.py:holder_is_local`: `if is_key(name):` then `return False`), and the holder check
then says `other_host` and judges it by its lease alone.

*A name that is not a key is local only on proof.* A binding written before the key existed, or by a machine that had none,
carries a hostname, and a name is not proof of locality: two machines can both be `ubuntu`, and a machine that changed
networks no longer answers to its old name. Such a holder is this machine's only when this machine's own token file for the
holder's `(pid, startedAt)` records that same host, or when the asking process is that holder (its own pid and `startedAt`,
which it read from its own registry file):
`if process is not None and process == (pid, started): return True`, and
`record = read_token_file(token_path(identity_dir, pid, started))` then `return record is not None and record.host == name`
(`hostname.py:holder_is_local`). The bind path applies the same rule when it adopts the host name an identity's own holder row
records, because the engine compares `host` as a string on `clear`, `fork`, `refresh` and a self-`release`
(`binder.py:_as_holder`: "Only a name this machine answers to is adopted; any other host stays as it is."). A listing of "the
bindings on this host" asks the engine under every name this machine has used, the key, the current `gethostname()` and the
hosts its own token files record (`hostname.py:listing_names`), and each returned holder is judged afterwards by the rule above.

*What stays possible.*
(1) A machine image cloned without regenerating the identifier (a VM clone, an AMI baked with a filled `/etc/machine-id`)
gives two machines one key, and each then treats the other's holders as local (module docstring: "a machine image cloned
without regenerating the identifier ... gives two machines one key"; the platforms' own cloning tools regenerate it, inferred
from that docstring, not tested here).
(2) An identity directory shared or copied between machines (an NFS home, an rsync of `~/.config/nexus`) lets machine A read
machine B's token files and accept B's non-key hostname as its own, so it judges B's holder by A's pid table (module docstring:
"machine A then sees machine B's token files and accepts B's pre-key hostname as its own"). That reaches only rows written under
a non-key name, which stop being written once both machines run this client.
(3) Two machines with the same short hostname still compare equal for a `labrun` work ref, which carries only the short name
(`work.py:_process_gone` docstring: "two machines that share a short name still compare equal here").

*Work refs and owner records.* The `<host>:<pid>:<started>` ref of a `labrun` row carries the short hostname, `gethostname()`
up to its first dot, because `lab-remote` writes it that way (`work.py:_process_gone` docstring: "`lab-remote` writes
`gethostname()` up to its first dot"; the `lab-remote` script itself was not read, so that it writes the short name is inferred
from this docstring). The reader matches the ref's short host against `machine_short_names(here)`, which holds this
machine's own name and, when `here` is its key, its `gethostname()` (`hostname.py:machine_short_names`). Owner records a
long-lived registrar writes carry the full host, which is the key from this release on:
`record_owner(owner_dir, identity, kind, ref, pid=pid, start=table.os_start(pid), host=table.host, procs=table)`
(`work.py:_own_record`; `table.host` is `Processes.host`, which returns `self._host if self._host is not None else this_host()`,
`holder.py`).

**Binding.** One engine route, `bind`, takes the seat (or identity), the
caller's evidence (session id, host, pid, `startedAt`, the process's OS start
time as a UTC instant (`proc_start`), pane, client version, whether it holds a
lease, and a `holder_gone` verdict naming the host, pid, `startedAt` and
`proc_start` of the holder it found dead) and the generation it expects; it appends a binding row under a
compare-and-set (the write happens only if the expected generation is still
current, so two simultaneous binds cannot both win) and returns an identity
token carrying identity and generation. Besides `bind`, a
`gone` route records a client's `holder_gone` verdict for a binding nobody is
rebinding. It appends a binding row with event `gone` under the same
compare-and-set on the generation, and only when the verdict's host, pid,
`startedAt` and `proc_start` match the identity's current binding, so a sweep
cannot mark a binding that was rebound a moment earlier; the row sets the
identity's state to `lapsed`, whatever its lease, and a recorded `gone` frees
the seat. A second `gone` for the same holder appends no row and answers
`recorded: false`. A route lists the bindings
whose host is a given host. At every SessionStart, after its own bind or
rebind, the hook lists the live, lapsed and `unleased` bindings on its host
(under each of this machine's names, `hostname.py:bindings_here`, each holder
judged by the local-proof rule),
skips its own process's binding and every delegated identity in this pass (a
delegated identity is renewed by the `nx` dispatcher, and the process that
holds its binding is the dispatcher, which has no registry file), runs the holder
check on the rest and reports the gone ones, so a crashed session's identity
is recorded as gone even if its seat is never claimed again. A second pass
checks each delegated identity's dispatcher by its pid and OS start time alone
(a dead or reused pid is gone, one that runs or cannot be read is not) and
reports the gone ones the same way; a 403 from `gone` ends the sweep. `nx
identity alive` reports what it finds the same way.
Retention and `purge` read these recorded verdicts. The callers of `bind` are
the SessionStart hook, the `nx identity` commands, the MCP server (renewal,
refresh, `end`, the `/branch` fork row and the pending bind below) and the `nx` operator dispatcher (a delegated identity's
`start`, renewal and `end`); the hook and the commands authenticate with the
tenant credential the `nx` CLI already uses (in local mode; a cloud install's CLI holds the data credential, which the self-service rules above serve), the MCP server with the data
credential it holds. The data credential may start, resume, fork, refresh and
end a binding and create an ephemeral identity; it may not take over, release
or clear. Seat create, promote, rename, `gone`, delegated create and purge take the tenant credential (the root token counts as one). The data credential may make three of them for the identity its own identity token names, and for no other: it may promote the ephemeral identity the token names (the token must name that identity at its current generation, and the identity must be held and ephemeral); it may create a delegated identity whose parent is the identity the token names (same check, on the parent); and it may create a seat when it carries a live identity token, the new seat starting unheld and its `created_by` set by the engine to `identity:<the token's id>` whatever the request says. A data-credential call of any of the three with no token is 403 `identity_token_required`; with a token for another identity, 403 `not_owner`; with a superseded generation or a seat let go, the fence's 409s. Rename, `gone` and purge stay tenant-only. This is what lets a cloud install, whose laptop holds only the data credential, keep seats (Sam 2026-11-19, no tenant credential on the laptop): the restart script promotes a session's ephemeral by sending that session's own token, read from its token file, and the `nx` dispatcher delegates a `claude -p` child by sending its parent's token. A session holding no live identity cannot create a seat with the data credential; the restart script skips it with one line. `/version` carries `identity_self_seat_supported: true` on an engine that admits these; the edge does not forward it yet, so a client detects an older engine from the data-credential refusal's wording (`a 'data' credential may not call ...`) and says so once. Two limits narrow what the data credential can do with identities that are not its caller's (Sam 2026-11-19). First, seats it creates directly are capped per identity: an identity may be the creator (`created_by identity:<id>`) of at most 5 seats, counted in every state and for the life of the identity (no route deletes a seat: purge keeps it, and rename keeps its creator); the sixth is 409 `seat_limit`. The cap does not count a seat promoted from an ephemeral (its creator stays `session`), and each new ephemeral or delegated identity has its own five. The tenant credential has no such cap. Second, a `refresh` or `fork` the data credential sends must carry a live identity token that names that identity at its current generation (an expired token counts as none: the bind route ignores it) (the fence's check: no token is 403 `identity_token_required`, another identity's 403 `not_owner`, a superseded generation 409 `generation_superseded`). Before this, a refresh or fork that named no process passed, and any holder of the data credential could mint a token for any held identity that way, quietly; naming the process would not have stopped that, since the holder's process is readable by the same credential. This narrows that gap and does not close it: a data-credential `start` or `resume` can still take a held identity by sending a `holder_gone` verdict for the holder (which a read exposes), and `end` takes no token, so a holder of the data credential can still evict a session and bind its seat; that is loud (the real holder is superseded), where the refresh was covert. The MCP server refreshes a token once it has under 50 minutes of life left, so a sleep under about 50 minutes never lapses it; a sleep longer than about 50 minutes may. A process whose token expired cannot refresh with the data credential: the server stops refreshing until a SessionStart bind writes a fresh token into the file, and the hook's `fork` refused for want of a live token starts the identity the process holds again (a start needs no token), so the session stays bound to it; a new launch or restart binds as a new ephemeral in a cloud install (the restart script then promotes it back to its seat by name). The tenant credential is unchanged: it is the trust boundary for a request that names no process. Both ship in engine 0.1.198. The client that sends the session's own token on `refresh` and `fork` ships first, in conexus 8.5.1, and the sessions restart onto it; 0.1.198 deploys after that restart, so no session's refresh is refused before its client sends the token (Sam is the install's only cloud client, so that one restart is the whole rollout). Any holder of
the data credential can still bind any seat with `start`, `resume` and `end` (not `refresh` or `fork`, above), as any can mint a session token today
(`SessionTokenHandler.java:97-118`): the bind guards against a confused
session, not a hostile one, as JDR-002 says. Some installs give the hook only
the data credential. There the hook degrades, and none of it fails the
session: a `clear` the engine refuses with 403 is sent again as `fork`, which
the data credential may send and which also keeps the generation and adds the
new session id; a cross-seat `/resume` that must `release` the seat the process
holds is refused with 403, so the process keeps that seat and the hook prints a
notice (an ephemeral identity closes with `end`, which the data credential may
send); and the SessionStart sweep, whose `gone` route is the tenant
credential's, ends quietly on a 403 and deletes no token file.

A seat is named by `NX_SEAT`, or by a `-n` that exactly matches a live seat
alias; `NX_SEAT` wins when both are given. A `-n` that matches no seat is a
display name, and the session gets an ephemeral identity that records it as
`display_name` and titles its board entry `<display name> (i<n>)`, but takes
no alias, so mail reaches it by id only: aliases belong to seats, so two launches with one
display name never collide and no name is consumed. `nx identity promote
<id> <name>` makes it a seat with that alias (Sam, 2026-11-16); from engine
0.1.198 the engine also retitles the identity's threads entry to the alias in
the same transaction, on both credential routes, and a title another entry
already holds is never overwritten: the promote stands and the entry keeps its
old title. The
policy, with the result for the session in the last column:

| Hook `source` | Seat state | Result |
|---|---|---|
| `startup` after an in-place `/restart` with no exchange (a binding on this host has this process's `pid` and `proc_start`) | any, unless another process holds it now | the same identity rebound, new generation, event `start`; checked before every other `startup` row |
| `startup`, a seat named | unheld or ended, or its holder no longer exists | bound, new generation, event `start` |
| `startup`, a seat named | lapsed, holder still running (same host: this machine's key, or a non-key name it can prove), or lapsed less than 10 minutes ago (another host) | ephemeral, as for a live holder |
| `startup`, `NX_SEAT` naming no seat | n/a | ephemeral; the hook prints a notice that `NX_SEAT` names no seat (a seat that was renamed gets a notice naming its new name). The engine has no route that lists seats, so the hook does not list them |
| `startup`, a seat named | live in another process | ephemeral; the hook prints the holder and, in an interactive session only, the remedy `nx identity claim <seat> --take-over` |
| `startup`, no seat named | n/a | ephemeral, event `start` |
| `resume` | the resumed id's seat is unheld or ended, or its holder no longer exists | bound, new generation if the process is new, event `resume` |
| `resume` | live in this same process | unchanged |
| `resume` in a process that already holds a different identity | the resumed seat unheld, or its holder no longer exists | the process switches: its current identity is closed (`release` for a seat, `end` for an ephemeral), then it is bound to the resumed seat with a new generation; the handoff marker carries the new identity, generation, session id and token-file path, and the MCP server's heartbeat re-read covers a missed marker; Bash children started before the switch keep the old session id and are refused by the resolver until restarted |
| `resume` in a process that already holds a different identity | the resumed seat live elsewhere | unchanged: the process keeps its identity, and the hook prints the holder |
| `resume` | live in another process | ephemeral, with the holder (and, interactively, the remedy), as for `startup` |
| `resume` | the id is in no index (a session from before this RDR), or its identity is an ephemeral or delegated identity that has ended | treated as `startup` with the seat the launch names. In a process that already holds an identity it does not start a second one: when the launch names another seat, the process closes the identity it holds (`release` for a seat, `end` for an ephemeral) and binds the named seat; otherwise, or when the close is refused, it keeps its identity and appends a `fork` row for the new session id |
| `clear`, `fork` | live or lapsed in this process | same generation, new session id, event `clear` or `fork`; a lapsed seat is revived |
| `compact` | any | unchanged; no binding row |
| no hook (`/branch`) | live in this process | the process's token file still applies, so its Bash children keep writing; on the renewal after the one that first sees the unknown session id (one renewal of grace: on `/clear` Claude writes the new id into its registry file before the hook binds it, and a fork row sent in that gap doubled the `/clear`'s row) the MCP server maps it through the registry's pid and `startedAt` and appends a `fork` row; the resolver never writes |

A delegated child (a process carrying `NX_IDENTITY_TOKEN`, below) is bound by
its dispatcher, so its hook binds nothing. The hook binds nothing in two more
cases. A process that inherited `NX_SESSION_ID` (a nested subprocess that
reuses its parent's session) is not bound, and the hook prints a notice saying
so, because the session id is not that process's own and binding it would point
the parent's identity at the child. A process with no Claude ancestor (`nx hook
session-start` typed in a shell) is not bound either, and the hook prints
nothing.

A `--fork-session` launch is a new process: it binds by the `startup` or
`resume` rows like any launch. After a `fork`, mail already in the seat's
`mailbox/<id>` follows the seat into the forked conversation; mail in the old
`mailbox/<session id>` stays where RDR-208 put it.

**Identity tokens.** A `bind` that leaves the caller holding the identity
(`start`, `resume`, `takeover`, `clear`, `fork`, `refresh`), and the create of
an ephemeral or delegated identity, returns `identity_token` and
`identity_token_expires_in_seconds`; the lifetime is one hour. The token
travels in the header `X-Nexus-Identity-Token`. The engine stores only its
hash, in `identity_tokens`, one row per mint: the table is insert-only, its
binding columns (identity and generation) are not unique, and its rows go
with the identity. So a `/clear`, a refresh or a restart mints another token
without invalidating one still inside its hour, and the token of a superseded
generation stays readable until it expires, which is what lets the engine
answer `generation_superseded` in place of a 401. An expired or unknown token
is ignored on the `bind` and `renew` routes only, so a client woken from a
long sleep can refresh through `bind` with whatever header it holds; on every
other route, sends included, it is a 401.

**Stale generations and named processes.** On the bind events of a process
that already holds the identity (`clear`, `fork`, `refresh`, `end`) and on
`renew`, an expected generation below the current one is 409
`generation_superseded` (the body carries `expected_generation`,
`current_generation`, `state` and the `holder`). The events that take or hand
over the seat (`start`, `resume`, `takeover`, `release`) and `gone` keep
`generation_conflict`: a lost compare-and-set there is a race between
claimants, not a stale holder. A `clear`, `fork` or `refresh` that names any
field of a process (host, pid, `startedAt` or `proc_start`) must agree with
the holder's on every field it names, or it is 409 `holder_mismatch`, since
each of them ends with a fresh token. One that names none still mints: the
tenant credential is the trust boundary for a request that names no process.

**A holder that no longer exists.** The check is the client's: the engine
cannot read a laptop's files, least of all in cloud mode. Before binding, the
hook (or `nx identity claim`) reads the current holder from the engine and,
when the holder is on its own host, judges it in up to three steps. "On its own
host" is decided before any step: the holder's host is this machine's key, or a
non-key name this machine can prove was its own (see "A host is a machine key"
above); any other holder is `other_host` (`holder.py:check_holder`: `if not
holder_is_local(holder, host=here, identity_dir=identity_dir):` then `return
Verdict(OTHER_HOST, ...)`). First, it
looks for a registry file naming the holder's pid with the holder's
`startedAt`. Second, it probes that pid. Third, it compares the running
process's OS start time with the binding's `proc_start`, so a pid reused after
a crash is not mistaken for the holder. Both times are compared as UTC
instants to the second: the registry's `procStart` string is UTC, while `ps`
prints local time unless run under `TZ=UTC`, so the probe sets it. On Windows
there is no `ps`, and the start time is the process creation time. A probe,
not file presence alone, is what catches a crash, since a crashed process
leaves its registry file behind.

The first step reads one directory, `CLAUDE_CONFIG_DIR/sessions`, and a Claude
run under another config directory writes its file somewhere else, so a missing
file there proves nothing. The token file (see Handover to the MCP server)
records the registry directory its process used. The check therefore applies
the registry step only to a holder whose token file names the registry
directory being read. Such a holder is gone when there is no registry file for
its pid, the file has another `startedAt`, the pid is not running, or its start
time differs (it exited, crashed, was replaced by `/restart`, or the machine
rebooted; `nexus_rdr/253-research-9` (a), (b), `253-research-10` (1)). Any
other holder, or one with no token file, is judged by the probe and the
start-time compare alone: a dead or reused pid is gone, and otherwise the
answer is unknown. An unknown holder is never reported gone. This leaves one
case undecided: a holder under another config directory that was restarted in
place keeps its pid and OS start time, so without its registry file it cannot
be told from a running one.

When the check finds the holder gone, the client sends `holder_gone` with the
holder it checked. The engine records the verdict in the binding row and treats
the seat as `unheld`, for any lease, `unleased` included; the compare-and-set
on the generation still decides a race. For a holder in the registry case this
one rule covers every restart mode: an exit and relaunch, an in-place
`/restart` (same pid, a new `startedAt`; its hook fires `source: resume`,
`253-research-10` (1)) and a `--revive` after a reboot (no registry files at
all) all find the old holder gone and bind by the table's `resume` row with a
new generation. The restart script needs no release step. A holder on the same
host that the check cannot judge is freed only by a take-over; its lapsed lease
does not free it. A holder on another host (a key that is not this machine's, or
a non-key name with no local proof) is judged by its lease alone: lapsed
plus the 10-minute reservation, and only then is its seat free. The bind path's
reservation test treats a holder under any of this machine's names as not on
another host (`binder.py:_reservation_over`: "A holder under any of this machine's
names is not on another host.").

An in-place `/restart` of a conversation with
no exchange yet starts a new session id with `source: startup`
(`253-research-10` (2)) and loses its `-n` name. Before applying the
`startup` rows, the hook therefore looks for the most recent binding on its
host (asked under every name this machine has used, the holder then judged by the
local-proof rule above: `binder.py:_rebind`: `holder_is_local(holder,
host=facts.host, identity_dir=self._idir, process=(facts.claude_pid,
holder.get("started_at")))`) whose `pid` and `proc_start` are its own process's (an in-place
`/restart` keeps both, `253-research-10` (1); the hook reads its own OS start
time from `ps`, so this needs no registry file). If one exists and no other
process holds that identity now, the process rebinds it: the old holder's
`startedAt` has no registry file, so the holder check finds it gone, and the
bind appends a `start` row with a new generation (a new `startedAt` is a new
process) and the new session id, whatever the identity's state, `ended`
included, so a seat named only by `-n` is not lost. The table's first row
records this case. The hook may run before its own registry file is written
(`253-research-10` (5)), so it waits up to 5 seconds for a registry file that
is its own process's: its `sessionId` is the payload's and its `procStart` is
the process's OS start time (the `procStart` compare is skipped on Windows,
where that spelling is not known). A stale file that a crashed earlier owner of
the pid left behind is waited past, as if absent. If no such file has appeared,
the hook binds nothing and writes its payload (session id, source, the launch's
seat name, its `-n` name read from the Claude process's arguments since the
registry file that would hold it is missing, the tmux pane, and its OS start
time) to `~/.config/nexus/identity/pending.<claude pid>.<OS start epoch
seconds>`, mode 0600. The MCP server takes the file on a heartbeat after its
Claude process's registry file exists, and only if the pid and start time in
the file's name are its Claude process's. It performs the bind from it with the
data credential (the rebind lookup and the policy table apply as in the hook,
except the events that credential may not send, which a process with no binding
does not need; a Desktop session's bind is `unleased`, as the hook's would be),
and deletes the file. The server has no way to show the user anything, so the
notices of this bind (a held seat, a name mismatch) are written to its log
only. The hook deletes any pending file whose process is not running or whose
start time differs. Until the bind, the session runs unbound and the hook says
so. Two parts of this are unverified: the pending bind has not been run on a
Claude Desktop session, and it is not known whether the registry already holds
the resumed session id when the hook runs at an in-process `/resume` (confirmed
for `/clear` only; Critical Assumption 1). If it does not, the hook's wait ends
without a registry entry that matches, so it binds nothing at that `/resume`
and writes a pending file.

**Take-over.** `nx identity claim <seat> --take-over` supersedes a live
holder. It runs only from an interactive Claude session: its nearest Claude
ancestor's registry `entrypoint` is `cli` or `claude-desktop`, and a `claude
-p` child, whose `entrypoint` is `sdk-cli` though its `kind` is `interactive`
(`nexus_rdr/253-research-10` (4)), is refused. It is also refused when the
holder's process is an ancestor of the caller's, so no child can take its
parent's seat (the caller's own Claude process is not its own ancestor: a
take-over of a seat that process already holds binds nothing new), and when the caller has no Claude ancestor with a registry
file at all (a plain shell), since `find_immediate_claude_pid` would
otherwise fall back to the shell itself. The hook prints the take-over remedy only into interactive sessions.
The claim binds the caller's own Claude process (its nearest Claude ancestor,
`nexus_rdr/253-research-9` (c)) as a new generation and writes the token
file. It starts an unheld or ended seat with `start`, which a data credential
may send, and takes a held seat with `takeover`. The caller's previous identity is closed: an ephemeral one gets `end`
(its state is then pruned by retention), and a seat the caller held gets
`release`, leaving that seat `unheld`. `nx identity release <seat>` frees a
seat whose holder is gone: on the holder's host (judged as in "A host is a machine key") it runs the holder check and
is refused while the holder runs; for a holder on another host it is refused
until the lease has lapsed and its 10-minute reservation has passed. Because it frees only a gone holder's seat and binds
nothing, it needs no interactivity or ancestry guard, and a person can run it
from a plain shell. A clean exit appends `end`: the MCP server sends it at
shutdown, with its expected generation, so a late `end` from a superseded
process is refused with 409 `generation_superseded`, and then deletes its token
file. It does both only once the Claude process has exited (it waits up to 1.5
seconds for that). A server that shuts down while Claude still runs, as on a
`/mcp` reconnect, sends no `end` and leaves the binding and the token file for
the next server, which has no hook to bind it again. A server that stopped
writing because it was superseded sends nothing. A crash leaves the seat to
lapse.

**Handover to the MCP server.** The hook binds, receives the token and writes
it to `~/.config/nexus/identity/<claude pid>.<startedAt>.token` (mode 0600):
one file per process start, so two processes that resume one session id
never share a file, and a superseded process's children never read the new
holder's token (except across an in-place `/restart`: the Claude pid stays, the
registry's `startedAt` changes and the new generation's token is in a new file;
the file name comes from the registry's current `startedAt`, read on every
call, so a process that outlives the restart reads the new file). The file holds the identity, the generation, the token and when
it expires, the current session id and every session id the process has held
under that identity (a cross-seat switch starts the list again), the process's
pid, `startedAt`, `proc_start` and host (the machine key, else the hostname; the record is what a later process reads as local proof for a non-key name), and the registry directory the
process's registry file lives in. The hook and the MCP server both rewrite it,
so each change is a read, an edit and a write under a file lock; a writer that
finds the file naming another identity or generation than the one it holds
leaves it alone, and the next heartbeat adopts what is there. On `clear`,
`resume` and `fork` the hook also adds the identity, the generation and the
token file's path (never the token itself) to the T1 handoff marker it
already writes, beside the session id (`t1_handoff.<mcp pid>`, polled every
5 seconds; `nexus_rdr/253-research-7` (a)). The server follows a marker only
when its token-file path is the server's own process's file. On `/clear` the generation is
unchanged, so the MCP server keeps writing with its current token and the
marker gives it the new session id: no write is refused in between. On
every new process start (`startup`, and a `resume`, `/restart` or `--revive`
in a new process), the new MCP server reads the token file of its own Claude
process, found by its nearest Claude ancestor's pid and that process's
registry `startedAt` (`nexus_rdr/253-research-9` (c)), polling once a second
for up to 30 seconds, since the hook and the server start together (a server that
outlives an in-place `/restart` re-resolves that name on every call, and its lease on
every heartbeat, so it follows the registry's new `startedAt` to the new file;
nexus-hczdo); it does
not rely on the marker, which a new server would discard
(`nexus_rdr/253-research-8` (b)). If the file has not appeared it runs
unbound and keeps checking on its heartbeat. On every heartbeat the server
also re-reads its process's token file and adopts the identity and
generation it finds there, so an identity switch (a cross-seat `/resume`, a
take-over) reaches it within a minute even if the marker write failed.

The identity lease (the server's token read, heartbeat, renewal, refresh,
`fork` row, pending bind and `end`) runs on a thread of its own, started with
the server's T1 handoff watch. It does not run in the loop that renews
`directory/<name>`: that loop runs only while a directory lease is armed, and a
server with no name must still renew its identity. The thread wakes every
second and runs a heartbeat when one is due: every second while the server is
unbound in its first 30 seconds, every 60 seconds after that. Setting
`NX_IDENTITY_LEASE=0` turns the lease off. A server started by hand, with no
Claude ancestor, runs no lease.

**Renewal, refresh, lapse and sleep.** The MCP server renews the binding every
60 seconds on its lease's heartbeat. The identity
token lives one hour, the ceiling for a token minted under the data
credential the MCP server holds (`nexus_rdr/253-research-8` (c)); the server
refreshes it once it has under 50 minutes of life left, through `bind` with the same generation (a refresh,
not a new binding) and rewrites its process's one token file. A renewal or refresh from a superseded generation is answered 409
`generation_superseded`, and the server stops writing identity state and says so. The engine marks a seat
`lapsed`, without changing the generation, when it reads a binding whose last
renewal is more than 300 seconds old; the next renewal from the same process
appends `revive`. On the holder's own host (this machine's key, or a non-key name with local proof) the probe above decides: a running
holder keeps its seat however long its lease has lapsed (a stuck process, a
long sleep), and a gone holder frees it at once. From another host a lapsed
seat stays reserved for 10 minutes after the lapse is due (300 seconds after
the last renewal, not when it is first read), then is free. So a laptop that sleeps lapses every seat, and on wake each
process revives its own within a minute, with no generation change; a stray
launch on the same laptop finds the holder running and gets an ephemeral
identity, however long the sleep. The identity token, which lives one hour,
is refreshed whenever it has under 50 minutes left, so it holds at least 50
minutes when a sleep begins and a sleep under about 50 minutes never lapses it;
a sleep longer than about 50 minutes may. The server refreshes a token that is
still live on its first wake heartbeat. One that has expired cannot be refreshed
with the data credential (engine 0.1.198): the server stops refreshing until a
SessionStart bind writes a fresh token into the file, and the hook's `fork`
refused for want of a live token starts the identity the process holds again. A Bash `nx` in that window does not send a token its file records as
expired, since the engine would answer 401 to every request carrying it, reads
included. A fenced write then gets the engine's 403 `identity_token_required`,
and the next call reads the refreshed file. A Desktop binding is `lease=unleased`: its MCP
server cannot renew (Critical Assumption 4), so it never lapses and shows its
liveness as unknown; it is freed when the holder check finds its process gone
(Desktop writes registry files with `entrypoint: claude-desktop`), or by an
explicit take-over.

**The resolver.** One function, `resolve_identity()`, replaces the four-tier
session resolver and the `current_session` file. Its inputs, in order: an
explicit identity token (a delegated child; `NX_IDENTITY_TOKEN` is the path of
its token file), the hook payload's session id, `CLAUDE_CODE_SESSION_ID`, and
the token file of the process's nearest Claude ancestor. It maps a session id
through the session index to `(identity, generation)`, and an unknown id from a
live registry entry through pid and `startedAt`. If two inputs map to different
identities it refuses with both, never picks. It only reads. The fourth input is
what catches a Bash child started before a cross-seat `/resume`: the child
still carries the old session id, which the index maps to the old identity,
while its process's token file now names the new one.

A Bash child's `nx` sends the token of its nearest Claude ancestor's token
file, and only on the requests the engine fences: memory writes, mailbox
claims (`in`, `inp`, `ack`, `nack`, `renew`, `release`) and identity-scope
scratch writes (an identity-scope read is open at the engine and goes without
it). It also sends it on a mailbox send, so the send's stamp comes from the
identity's current binding. Every other request, reads included, goes without
it, because a token the engine cannot resolve is a 401 on any route. The token
file is named by the registry's current `startedAt`, read on every call; a
registry or token file that cannot be read is read once more after about 10 ms,
and a process that still finds none sends none (it never falls back to the name
an earlier call found). A process with no Claude ancestor, or an ancestor with no
token file, sends none.

Before it sends a fenced write, a process whose session id is not one its token
file lists asks the resolver whether that session still belongs to the
identity the file names. A process whose session id indexes to another
identity is refused client-side with the resolver's message, and nothing is
sent. A send from such a process goes without the token instead of being
refused, since a send is open to anyone. An engine that cannot be asked does
not block the write: the engine's fence is the real one, and this is an early,
readable refusal. The exception is the process that owns the lease, the MCP
server: it never sees the new session id after a cross-seat `/resume`, so its
spawn-time session id says nothing about its identity, and it is not checked.

On an HTTP 401 for a request that carried a token, the client re-reads the
token file once and resends if the file now holds a different usable token (the
T2 stores, `nx group` and the identity-scope scratch writes; the session-token and
bearer heals of the scratch store come after it, never before). A
send that still gets a 401 is resent once without the token, since the stamp is
a courtesy and the mail is not. A claim, or any other fenced write, is never
resent without a token, which JDR-002's 401 fallback would otherwise do. On a
superseded generation (HTTP 409 with error code `generation_superseded`,
distinct from the engine's other 409s) the client stops and names the holder; a
409 `identity_not_held` is a stop too.

**Fencing.** Two checks (Sam, 2026-11-16, decision 2 as amended), made in
this order. The ownership check: the token's identity is the family's owner
(403 `not_owner` otherwise). The generation check: the token's generation is
the owner's current generation (409 `generation_superseded` otherwise; the
body carries `identity`, `token_generation`, `current_generation`, `state` and
the `holder`). A token also fences nothing once its identity has let go of the
seat: when the owner is `unheld` or `ended`, or `lapsed` with a recorded
`gone`, the write is 409 `identity_not_held`, because `end`, `release` and a
purge leave the generation unchanged. A seat that is plainly `lapsed` is
still held (a sleeping host wakes and renews), and its token still writes. A
write to a fenced family with no identity token is refused (403
`identity_token_required`), with two exceptions. A send (`out`) to a
mailbox is open to any caller, token or none: `nx ask` answers, the console
and scripts send without one (`nexus_rdr/253-research-9` (e)). And a write
with no token to a family whose owner's current binding is `unleased` (a
Desktop seat) is accepted and labelled unattested with the response header
`X-Nexus-Unattested`, set once even when a parked `in` is fenced again on
waking: the exception is keyed on the target, since a Desktop MCP server holds
no token at all (JDR-002, F17). It is for a write with no token only: a token that names
another identity is still 403 `not_owner`. The other refusals are 404
`unknown_identity` (the owner does not exist), 403 `identity_line_immutable`
(a threads entry that would drop or change its `identity:` line) and 400
`bad_scope` (a scratch scope that is not `identity:` and an id such as `i7`).
Each check reads the identity in its own statement before the write, without
a lock, so a process that loses the seat between the check and the write lands
at most that one write, and is refused on its next. A person can remove the fenced state of an
identity with `nx identity purge <id> --operator`: an engine route taking the
tenant credential. It is refused unless the identity is `ended`, or `lapsed`
with a recorded `gone`, so it is also refused for a `live` identity and an
`unheld` one. It is logged in the engine's `gc_audit` table with the caller
and what it removed (operations `identity_purge` and `identity_tombstone`; the
actor of a purge is `operator:<scope>:<hash prefix>`). A purge of a seat keeps
its record, aliases and bindings. A tombstoned identity's remaining rows are
already gone.

| Family | Store and handler | Who may write | Checked operations | Checks |
|---|---|---|---|---|
| Mailbox `mailbox/<id>` | tuple space, `TupleHandler` | any identity may send (`out`) | the owner's `in`, `inp`, `ack`, `nack`, `renew`, `release` | ownership and generation; a send is a label, as today; a parked `in` is fenced again when it wakes and releases a claim it no longer owns |
| Membership `identity_memberships` | table, `IdentityHandler`, the `/v1/identity/<id>/memberships` routes | the owner only | join, leave and role change | ownership and generation |
| Threads entry | T2 memory, project `threads`, `MemoryHandler` | the owner only | `put` (including `replaces`), `put_or_merge`, `merge`, `import`, `import_batch`, `delete`, `restore`, `summaries` on an entry carrying an `identity: <id>` line | ownership and generation, read against the stored entry whether it is quarantined or not; a new entry may name only the writer's own identity, and a write that removes or changes the line is refused (the identity line is immutable); a rollup `summaries` counts as the fenced operation on each of its sources; `put_or_merge` never merges into a fenced entry (a legacy entry with no `identity:` line still merges); `replaces` checks the entry it replaces; the tenant-wide sweeps `expire` and `reap` skip such entries |
| Identity scratch | T1, the identity scope, `ScratchHandler` | the owner only; a delegated child reads | `put`, `flag`, `unflag`, `delete`, on the scope with `session_id` `identity:<id>`; `session/close` never closes the scope (it answers `deleted` 0) and `sweep` excludes it; the client refuses an entry over 2 KB and a note once 20 are held (the resume pointer has its own slot; no engine cap) | ownership and generation |
| Per-identity notes | T2 memory, project `identity/<id>` | the seat itself, and only its own project (client check) | not engine-fenced: the engine takes a write to this project from any caller. The client refuses `put`, `put_or_merge`, `put_replacing`, `import_entry`, `import_entries_batch`, `delete` by project and title and `insert_summary` unless the caller's token file names the seat that owns the project | the caller's kind (seat only) and the project's owner; the id-keyed paths (`delete` by id, `merge_memories`, `restore`), raw HTTP and a process with no Claude ancestor are not covered |

Reads are not checked, a delegated child's included. Every other subspace, project and scope keeps its
current rule: the stamp is a label (JDR-002), and `ScratchHandler`'s existing
refusal of a mismatched session on the session scope
(`nexus_rdr/253-research-8` (d)) is unchanged; for an identity token that check
accepts any session id the session index maps to the token's identity and
generation, so a session-scope write after `/clear` is not refused. That
check applies to writes only; a read of the session scope is not checked.
JDR-002 gains a revision entry
naming these four families, and JDR-001 one for the identity T1 scope and the
extended handoff marker (both Sam's edits). The
board keeps `memory_put` as its write path.

**What a stamp says.** With an identity token, a tuple's `sender_session` is
the session id of the identity's newest binding row under the token's
generation, so a send after `/clear` carries the new session id. A T2 row
written with a token records the token's identity in `memory.identity`. That
column is a label no check reads: the checks read the stored entry's
`identity:` line. A tokenless overwrite of the row, or a tokenless merge into
it, clears the label, since the content is no longer the token holder's;
`import_batch` stamps nothing. The tenant import and restore routes are
deliberate unfenced operator paths.

**Delegation.** `nx` operator dispatch (`claude -p`) that grants the child tools
(`allowed_tools` or `mcp_servers`) and runs under a bound identity creates a
delegated identity, `kind=delegated parent=<id>`, with generation 1 and one
`start` binding row written by the dispatcher; the row names the dispatcher's
own process (host, pid, `started_at` and `proc_start`), since the dispatcher has no
registry file. A dispatch that grants no tools (its child loads no MCP server
and cannot reach nexus), a dispatcher with no identity of its own, or an engine
that refuses the create, gives the child no delegated identity: its
`NX_IDENTITY_TOKEN` names a file that never exists
(`~/.config/nexus/identity/delegated/none`), so the child binds nothing and an
unreadable explicit token resolves to no identity, never to the parent's. The
dispatcher passes the child, in place of any token the parent carried,
`NX_IDENTITY_TOKEN` naming a token file it keeps current; the child's own SessionStart
hook sees the variable and does not bind, and the resolver takes the
explicit token first. The dispatcher renews the delegated binding while the child runs, refreshes
its token by rewriting the file `NX_IDENTITY_TOKEN` names
(`~/.config/nexus/identity/delegated/<id>.token`, mode 0600; the child reads
the token from the file, not from the variable), deletes that file and
appends `end` when the child exits, on every way it ends (success, failure,
timeout, a spawn failure, a cancellation); if the dispatcher dies, renewal stops, the
delegated identity lapses, and the SessionStart sweep (or `nx identity alive`)
finds the dispatcher process gone and records `gone`. The hook also deletes the
token files in that directory whose dispatcher is gone. A delegated
identity reads its parent's identity-owned state and writes only its own. It keeps no
identity notes: its `identity/<id>` writes, and `nx memory put --mine`, are refused (it
reads its parent's notes with `--mine`).
Agent-tool subagents keep the parent's seat with an `agent` label: their MCP
calls carry nothing that tells them from the parent's (Critical Assumption 3;
Sam accepted this narrowing on 2026-11-16). They get delegation once Claude
Code sends `claudecode/agentId` on tool calls, re-checked on each upgrade.

**Retention.** Seats, their aliases and their identity records are
permanent, and so are a seat's `identity/<id>` notes: the retention sweep below does
not prune that project. Ephemeral and delegated identities are tombstoned 30 days after
they end, or after 30 days spent lapsed with their holder gone (a crashed
session's identity; a seat that lapses in a sleep and revives is not
counted), and an `unleased` ephemeral 30 days after the holder check finds
its process gone. That clock starts at the engine time the `gone` row was
recorded. A delegated identity has no registry file for a holder check to
find, so 30 days lapsed is enough for it; the `gone` row the sweep records for a
dead dispatcher is not needed for that, and lets `purge` act on it sooner
(purge needs a recorded `gone` for any lapsed identity). Their identity, binding, session-index, membership,
background-work and scratch rows and their threads entry are pruned, their
mailbox is emptied, and the counter keeps their numbers. Token files are
local: the MCP server deletes its own at shutdown once its Claude process has
exited, and the dispatcher its delegated ones. The hook deletes the token file
of a process whose holder check finds it gone, but only after this run's sweep
has dealt with that holder: the engine recorded the `gone`, or no longer lists
the process as the identity's holder (it ended, was released or was taken
over). A run with no sweep, or a sweep that ended on a 403, deletes no token
file, because the file may be the only record of the registry directory a live
binding uses; the same holds for the delegated directory, whose files go only
when the dispatcher process is gone and not in such a run. A seat's binding rows are kept
for 180 days, never the last one, and its session-index rows as long as the
binding rows they point to. Identity scratch goes with its identity: a
purge removes the scope right after the purge commits (the scope is in another
schema under another tenant setting, so it cannot join the purge's
transaction), and every retention pass pages through the identity scopes whose
record no longer exists and removes them, which covers a tombstone. A crash
between a seat's purge and the removal leaves the scope until the purge is run
again.

**Aliases and renames.** `nx identity rename <old> <new>` (Sam or the seat)
retires the old alias, sets `renamed_to`, appends a `rename` binding row, and
retitles the seat's threads entry in the same engine call, so the board never
holds two entries for one identity. Mail sent to the old name is delivered
for 7 days with a notice naming the new one, then refused with "renamed to
<new> on <date>". A retired name is never reissued: the tombstone is
permanent. Claude Code's `/rename` changes only the display name; the hook
prints a notice at the next SessionStart when the display name and the
seat's alias differ.

`nx identity promote` retitles too (above), but the two differ on a taken
title: rename refuses with `board_title_taken` and rolls the alias change
back, while promote keeps the entry's title and answers
`board_retitle: title_taken`, because a cosmetic title must not fail the
restart script's promote.

**State owned by the identity (Phase 1).**

- **Threads entry.** The entry carries `identity: <id>` and, through Phase 1,
  the existing `session: <id>` line too, because `nx ask` answer routing, the
  console, the due-date hook and auras read it (`nexus_rdr/253-research-8`
  (f)); Step 5 moves each of those readers to the identity line, and Phase 2
  drops the `session:` line. It is titled by the seat's live alias (except
  after a promote onto a title another entry holds, when it keeps its old
  title) and
  written with `memory_put` under the fencing rule above. The compaction
  checkpoint and Desktop restore find it by identity.
- **Mailbox and names.** Mail to an identity id goes to `mailbox/<id>`; a
  name resolves through `alias/<name>`. Phase 1 runs a mixed fleet, since old
  clients keep running for days, and a live binding does not show whether its
  client reads `mailbox/<id>`. So a sender resolves `alias/<name>` first and
  then sends to the session mailbox of the name's single live
  `directory/<name>` holder; it sends to `mailbox/<id>` only when nobody holds
  the name, and refuses a name with two holders. A retired alias keeps the
  7-day notice and the refusal after it. A new client subscribes to both
  `mailbox/<id>` and its `mailbox/<session id>` until Phase 2. So in Phase 1
  named mail goes to the holder's current session mailbox, not an
  identity-owned mailbox, and does not survive `/clear`, which is no worse
  than before Step 5; mail sent to the id, or to a name nobody holds, does.
  The identity mailbox that survives `/clear` follows in a later step (Sam
  2026-11-19, T2 `nexus/sam-ruling-named-mail-phase1-2026-11-19`: accepted
  for 8.5). `mailbox_send`'s guard against a session-shaped `from` gets an
  identity counterpart.
- **Identity scratch and per-identity T2.** The identity scope holds working
  state that must cross `/clear` and restart but is not worth a permanent
  record; it lives as long as the identity, and is the T1 scope whose
  `session_id` is `identity:<id>`, which needs no new table or changeset. The
  scope is bounded in the client, not the engine (the engine holds no cap): an
  entry over 2 KB is refused, and so is a put of a note once the scope holds 20
  notes; the resume pointer has a slot of its own, and nothing is evicted. The
  client's identity-scope store sends no session token: the engine admits an
  identity-scope call with none (reads open, writes fenced on the identity token
  alone), so the scope can be read and written by a process whose session id has no
  T1 lease, as at SessionStart after `/clear`, and by a plain `nx scratch --scope
  identity`. The
  pointer is one entry tagged `resume-pointer` (the thread, worktree, `state:` and
  `waiting_on:` of the seat's board entry, at most 1 KB): the PreCompact checkpoint
  hook and a SessionEnd hook for reason `clear` write it, put before the earlier
  pointer is deleted, and SessionStart prints it as one line (at most 400 bytes,
  with its age; nothing past 48 hours) for source `clear` only. A failed write logs
  one line and writes nothing else. The board stays authoritative: threads, asks and
  decisions never go in the scope. T2
  holds what is permanent:
  records a seat writes carry its identity, and a seat's own notes live in
  project `identity/<id>`, keyed by id so a rename moves nothing. Only a seat
  writes them, and only its own project: the client refuses the write from an
  ephemeral or delegated identity, from a seat to another identity's project and
  when the caller's kind cannot be learned, before any request (RDR-253 Step 6
  part 3). **`identity/<id>` is client-guarded, not engine-fenced**: the engine
  fences only `threads`, so a caller that never goes through the client store
  (raw HTTP, a process with no Claude ancestor) is not stopped, and the id-keyed
  paths (`delete` by id, `merge_memories`, `restore`) are not covered. SessionStart
  prints one line of up to five titles, `Your notes (identity/i5): a, b, c (+2 more)`;
  the conexus `nexus` skill carries the where-it-goes table. Retired if unused 30
  days after the release that first carries it, by transcript `memory_get` reads.
- **Memberships and push records.** Memberships are the `identity_memberships`
  table, written through the `/v1/identity/<id>/memberships` routes: a join
  keeps `joined_at` on a role change, a leave is idempotent, and the list reads
  by identity and by group in join order. RDR-252, still a draft, re-keys its
  groups onto them, and its push records carry the identity token's stamp, so
  the owner of a red is a seat.
- **Background work.** Watchers, waits and lab runs register as
  `identity_work` rows, keyed (identity, kind, ref), through
  `POST /v1/identity/<id>/work`, so the checkpoint lists them by identity after
  a `/clear`. The state is updated in place and the row has no expiry. It is
  a label, not a fenced family: a lab run on another host has no token file.
  The row itself never expires, but readers do not trust a `running` row on its own
  (the Step 6 stale rule: the identity is not alive, the registering process is
  gone, the row predates its holder, or nobody wrote it for 12 hours). A reverse lookup by (kind, ref), `GET /v1/identity/work?kind=&ref=`, returns
  all owners of a resource. An identity holds at most 200 work rows (409
  `work_limit`).
  Watchers and waits are Bash children of the seat's process and write as the
  seat across `/clear`.
- **Who reads liveness (Gap 4).** `nx identity alive <seat|pid>` answers from
  the binding and the holder check. Three consumers move onto it in Phase 1,
  each reading ownership through the work routes, where a session registers what it
  owns (`kind=worktree` with the path, `kind=tmux` with the server name, and
  its running watchers, waits and lab runs, and the agents it dispatches): worktree removal (a worktree is
  removed only when its owning identity is not alive, not when `ps` misses
  it); the sandbox and tmux cleanup helpers (a server is stopped only when its
  owning identity's process is gone, never by name pattern); and the restart
  round's readiness check in the restart script (a session reports its
  work rows in state `running`; the rows of a kind that ends with the session, `wait`,
  `watcher`, `labrun` and `agent`, block ready unless they read stale, and a stale row is
  listed and never blocks). Bead ownership stays out:
  Sam ruled on 2026-10-03 that bead claims cannot name a session (project
  memory `project_bead_claims_cannot_name_a_session`; incident I-42 in
  `nexus_rdr/253-research-1`), so beads keep the git user and the board names
  who works on what.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Session token, `sender_session` stamp | Yes (JDR-002) | Becomes an identity token carrying identity and generation |
| MCP 60-second heartbeat thread | Yes (`subscriptions.py`) | Not used for identity: the identity lease runs on its own thread (`mcp/identity_lease.py`), because that loop runs only while a directory lease is armed |
| SessionStart hook with `source` | Yes | Binds per the policy table (start, resume, clear, fork) |
| Restart script with `-n` | Yes | The script makes or keeps the seat first (Approach item 1); a relaunch passes `-n` only for a user-set name and carries `NX_SEAT` for a held seat, and the new start finds the old holder gone; its readiness check reads the identity's work rows (Step 6) |
| Catalog tumbler allocator | Yes (`catalog/tumbler.py`) | Pattern for the never-reused id counter |
| T1 handoff marker | Yes (`hooks/__init__.py`, `mcp/core.py`) | Carries the binding to the MCP server; kept |
| Directory lease, session markers, `current_session` | Yes | Fallbacks in Phase 1, retired in Phase 2 |
| Identity, alias, binding, session index, membership, background-work records; bind route; four fence checks | No | New, in the engine |
| Resolver, token file, handover, delegation | No | New, in the client |

### Decision Rationale

A persistent identity over better session ids, because the session id
changes by design on `/clear` and the name changes by design on restart: no
choice of one of them can be the identity (Key Discovery 1). A binding log
over a mutable "current holder" field, because every incident class in Gap 2
and Gap 4 needed to know who held a seat when, and a log answers that after
the fact. Generation fencing over leases alone, because a lapsed lease does
not stop the old holder's late writes (research 4, Jini). Named seats over
auto-minted identities, because Sam, peers and the board already think in
seats (Sam, 2026-11-16).

## Alternatives Considered

### Alternative 1: Make the session name the identity and keep it stable

Pass `-n` everywhere, refuse duplicates, and key state by name.

- **Pro:** smallest change; names already exist.
- **Con:** `/clear`, Desktop handles and unnamed launches still break it;
  names are display text that Sam and `/rename` change; RDR-252's two failed
  rounds are this design.
- **Rejected.**

### Alternative 2: Make the session id the identity and carry state across `/clear`

Extend today's handoff markers so every id-keyed thing follows the new id.

- **Pro:** no new record type.
- **Con:** one handoff per kind of state, each a new failure point (I-14,
  I-23); a restart with `--fresh` still loses everything; names stay a
  second, disagreeing source.
- **Rejected.** The chosen design still uses the T1 marker and a token file
  to tell the MCP server and Bash children their binding, but no state rides
  on them: state is keyed to the identity id.

### Alternative 3: Full KERI-style identifiers with signatures

Self-certifying identifiers, signed binding events, witnesses.

- **Pro:** proven in Sam's own stereotomy; resists a hostile party.
- **Con:** our threat is a confused model; key management for a dozen
  sessions on one laptop is cost with no matching risk.
- **Rejected**; the event-log shape is kept without the cryptography.

### Briefly Rejected

- Binding by tmux pane: Desktop has no pane, and a pane is a place, not a
  seat (research 4, the cockpit synthesis's "identity by place").
- Per-identity namespaces enforced in the harness: we cannot enforce them in
  Claude Code.

## Trade-offs

### Consequences

- Every surface resolves identity through one function; the four-tier
  resolver and the `current_session` file go.
- A seat's mail, board entry, scratch, memberships and background work
  survive `/clear`, resume, restart, rename and reboot.
- A superseded process can no longer write a seat's identity-owned state.
- New engine routes and tables mean an engine release before the client
  work can ship.

### Risks and Mitigations

- **A launch names a live seat by mistake**: it runs ephemeral and is told the
  holder; only the one-shot `nx identity claim --take-over` supersedes a live
  process.
- **A crash leaves a seat held**: on the same host the relaunch finds the
  holder's process gone and binds at once (the same host in the sense of "A host is a machine key": a key, or a non-key name with local proof, so a network change that renames the laptop no longer makes it "another host", commit `00a6c88d6d`: "so a resumed session keeps its identity across a network change"); across hosts it waits for the
  lapse, or for a person's `nx identity release`.
- **A child takes its parent's seat**: no inherited variable carries
  take-over authority, and a delegated child's hook does not bind.
- **A token outlives its lifetime**: the MCP server refreshes it under 50
  minutes of its one-hour life and rewrites its process's one token file; the CLI
  tells an expired token (401, re-read) from a superseded one (409
  `generation_superseded`, stop).
- **Desktop MCP writes are unattested** (Critical Assumption 4): a Desktop
  seat binds through its hook as `unleased`, and its fenced families accept
  tokenless writes to its fenced families as unattested labels.
- **A mixed fleet strands mail**: senders try the identity mailbox and fall
  back to the session mailbox, and new receivers read both, until Phase 2.

### Failure Modes

- Engine unreachable at start: the session runs unbound, the resolver says
  so, and identity-owned writes fail loudly; nothing falls back to a name or a
  file.
- Token file missing, or its token expired, for a Bash child: the CLI sends no
  token, and the engine refuses a write to a fenced family (403
  `identity_token_required`), except for an `unleased` seat's.
- A seat with no live binding receives mail: it waits in `mailbox/<id>` for
  the next binding.
- An unbound Claude Code session (its bind failed, for example with the engine
  down at start) receives no mail, because mail is addressed to identities
  alone, and its threads board entry is found by no reader until the session
  binds; the SessionStart mailbox line says so for such a session.
- The laptop sleeps: every seat lapses and revives on wake, with no change of
  generation, no refusal and no seizure.

## Implementation Plan

### Prerequisites

- [x] Sam's design decisions (2026-11-16; T2
  `nexus_rdr/253-decisions-2026-11-16`: decision 3 narrowed, decision 2
  amended, the non-seat `-n` rule).
- [x] Critical Assumptions settled (`nexus_rdr/253-research-5`, `-6`; the
  compaction audit and JDR-002 for assumption 4).

### Minimum Viable Validation

Seat `alpha` is created; a session launched with `NX_SEAT=alpha` binds
generation 1, and a second launch naming `alpha` runs ephemeral and is told
the holder. A launch with `-n scratch42` gets an ephemeral identity named
`scratch42`. Mail to `alpha` arrives. The session runs `/clear`: the binding
log gains a `clear` row under generation 1, its first write after the
`/clear` succeeds, and a shell `nx` started before the `/clear` still writes
as `alpha`. A `claude -p` child it dispatches writes its own records and is
refused on `alpha`'s scratch. The restart script restarts it by `/restart` in place: the
new start finds the old holder gone and binds a `resume` row with generation
2. A test process holding generation
1's token (kept alive for the purpose) is refused on `alpha`'s mailbox claim
with `generation_superseded`. The laptop sleeps for ten minutes and wakes:
the seat lapses and revives under generation 2, and a launch naming `alpha`
during the lapse runs ephemeral. Renaming the alias to `beta` retitles the
board entry and still delivers mail sent to `alpha`, with a notice. After a
reboot, `--revive` finds no registry files, so the old holder is gone, and
binds generation 3 to the same identity at once. A `/resume` inside `alpha`'s
session of a conversation that belongs to an unheld seat `gamma` switches the
session to `gamma`. A `claude -p` child of `alpha` that runs `nx identity
claim alpha --take-over` is refused.

### Phase 1 (CLOSED, 2026-11-19): Identity and the state it owns

All engine work (Steps 1 and 2) ships in one engine release, which Sam cuts.
The client work ships after it: Steps 3 and 4 together in one conexus
release, since binding needs seats; Steps 5 and 6 each
in a later release.

#### Step 1: Engine records and routes

The `identity_records`, `identity_aliases`, `identity_bindings`,
`identity_sessions`, `identity_counters`, `identity_memberships` and
`identity_work` tables; the `mailbox` template's `identity` address kind; the
per-tenant id counter with `i<digits>` reserved; the compare-and-set `bind` (start, resume, clear,
fork, takeover, release, refresh, end) under the policy table, with the
`holder_gone` verdict recorded from the client's evidence (the check itself is
client work, Step 3); the routes `nx
identity` needs (create, promote, alive, delegated create, session lookup,
bindings by host, and its history mode, `GET
/v1/identity/bindings?host=&pid=&proc_start=&history=1`, where `proc_start` is
required); an ephemeral create by the data credential; the membership and work
routes; `proc_start` in the binding row and the bind evidence;
renewal (which carries and stores the MCP server's client version), lazy
`lapse` with its 10-minute reservation, and `revive`; the `gone` route with
its holder-match guard and compare-and-set; the
`unleased` lease; the rename and promote calls that retitle the threads entry; retention
and tombstones, including threads entries of tombstoned identities, with
tenant registration with the sweep (the sweep's identity-retention arm); the
operator override.

#### Step 2: Identity tokens and the four fences

Tokens carrying identity and generation, one-hour lifetime, stored by hash in
`identity_tokens` and refreshed through `bind`; the ownership and generation
checks on every listed operation in `TupleHandler`, `IdentityHandler` (the
memberships routes), `MemoryHandler` (project `threads`) and `ScratchHandler`
(the new identity scope); refusal of tokenless writes except sends and writes
to `unleased` seats; `ScratchHandler`'s session-scope check accepting every
session id the index maps to the token's identity and generation, for writes;
`expire`, `reap`, `sweep` and `session/close` kept off fenced state;
`generation_superseded` as its own 409 code on `clear`, `fork`, `refresh`,
`end` and `renew`, with `identity_not_held` and `holder_mismatch`; T2 identity
stamping; retention of identity scratch; the JDR-002 and JDR-001 revision
entries (Sam's edits).

#### Step 3: The resolver, hooks and the MCP server

Built in the `nexus.identity` package, the SessionStart hook's identity leg
(`nexus.hooks.identity_bind`) and the MCP server's identity lease
(`nexus.mcp.identity_lease`):

- `resolve_identity()` with its four inputs (the delegated child's token file,
  the hook payload's session id, `CLAUDE_CODE_SESSION_ID`, and the nearest
  Claude ancestor's token file), which refuses when two name different
  identities.
- SessionStart binding per the policy table, with `NX_SEAT` and exact seat
  `-n` matching. The hook binds nothing for a delegated child, a process that
  inherited `NX_SESSION_ID` (with a notice) or a process with no Claude
  ancestor. On a data-credential install a refused `clear` is sent as `fork`,
  a refused `release` leaves the seat held with a notice, and the sweep ends
  quietly.
- The client-side holder check: the registry steps apply only when the
  holder's token file names the registry directory being read, otherwise the
  pid probe and the UTC start-time compare alone decide (gone, or unknown),
  with the process creation time as the start time on Windows. Its verdict
  goes to the engine as `holder_gone` evidence. Around it: the same-process
  rebind after an empty in-place `/restart`, run before the sweep; the sweep
  that reports gone holders on its host to `gone`, skipping its own process and
  delegated identities; the wait for the hook's own registry entry; the
  pending file keyed by pid and OS start time, with its `name` field; and the
  pending bind the MCP server completes, logs the notices of, and deletes.
- The per-process token file, with its fields and file lock; the
  handoff-marker extension, including the identity switch on a cross-seat
  `/resume`; and the cleanup that deletes a token file only after this run's
  sweep dealt with its holder.
- The MCP server's lease, on its own thread started with the T1 handoff watch
  and switched off by `NX_IDENTITY_LEASE=0`: the new-process token read, the
  heartbeat re-read of the token file, renewal, refresh, the `/branch` `fork`
  row, and `end` with the token file's delete at shutdown once the Claude
  process has exited.
- The request layer: the identity token goes only on fenced requests and
  mailbox sends, a process whose session id indexes to another identity than
  its token file is refused before a fenced write is sent (the lease owner
  excepted), a 401 re-reads the file once, and a 409 `generation_superseded`
  stops the caller with the holder's name.
- The notice when the display name and the seat's alias differ.

Not verified against a live session: the Desktop pending bind, and whether the
registry already holds the resumed session id when the hook runs at an
in-process `/resume`.

#### Step 4: Seats and delegation

`nx identity` (create, show, alive, claim, release, rename, promote, purge),
with take-over refused for non-interactive callers (registry `entrypoint`
other than `cli` or `claude-desktop`), descendants of the holder and callers
with no Claude ancestor; release carries none of those guards, only the holder
check on the holder's host and the lease check for a holder on another host,
and runs from a plain shell; the restart script's seats: a session that holds a
seat keeps it (the relaunch carries `NX_SEAT`), and for a name the user set (the
registry `nameSource` is `user`) with no seat, the session's ephemeral identity
is promoted, else a seat is created with `created_by=restart` (local mode; in a cloud install the data credential
promotes only, and a seat it creates is stamped `created_by=identity:<id>`), before an in-place
`/restart` or a relaunch alike, while a derived name is not passed back as `-n`;
delegated identities, their renewal and their token file for a `claude -p`
dispatch that grants tools, and a `gone` row for a dead dispatcher.

#### Step 5: Threads entry, mailbox and names

The `identity:` line beside `session:`, and the checkpoint, `nx ask`, console,
due-date hook and aura readers moved to it; alias resolution with a named
send going to the single live directory holder's session mailbox
(`mailbox/<id>` only when nobody holds the name, two holders refused), the
7-day notice, dual subscription; the identity-shaped `from` guard.

#### Step 6: Identity scratch, per-identity T2, memberships and background work

The identity T1 scope in the client; project `identity/<id>`; memberships through
the membership routes; work registration through the work routes; the three liveness consumers (worktree
removal, sandbox and tmux cleanup, the restart readiness check) moved onto
`nx identity alive`. The identity scope's caps (2 KB an entry, 20 notes, a reserved slot for the resume pointer) are
held by the client's identity-scope put; an engine-side cap can follow in a later engine release. The resume
pointer (above, Identity scratch and per-identity T2) is written by the checkpoint hook at PreCompact and at
SessionEnd with reason `clear`, and printed by SessionStart on `clear`.

Part 3, per-identity T2 notes (bead nexus-hczdo, 8.5.3): the client guard on `identity/<id>`
(seats only, own project only; `put`, `put_or_merge`, `put_replacing`, `import_entry`,
`import_entries_batch`, `delete` by project and title, `insert_summary`), the SessionStart
line (one line, at most 300 bytes), `nx memory curate` over `identity/*`, the where-it-goes
table in the conexus `nexus` skill, and a 30-day retire rule.

Part 1 of the registered work (nexus-hczdo) names who registers what: `nx tuple wait`
(a `wait` for the session id) and `nx ci wait` (a `wait`, ref `ci:<branch>@<sha>`)
through `Label`, `nx identity work run` (a command's row), `lab-remote` (a `labrun`,
ref `<host>:<pid>:<started>`, where `<host>` is the machine's short hostname, not its key; see "A host is a machine key"), the git-worktrees skill (a `worktree`), and the
SubagentStart and SubagentStop hooks (an `agent`, ref `agent:<agent id>`; the stop
removes the row, and neither hook purges at the 200-row limit). The `agent` kind needs
the engine release that carries changeset `identity-005`: an older engine answers `400
bad_kind`, which the hooks log once per process and ignore, so agent rows begin with the
engine release and not the client release. Three witnesses read a running row of a
`wait`, `labrun`, `agent` or `watcher` as stale: its process is gone or was replaced (the
`<host>:<pid>:<started>` ref, which names the short hostname, or the owner record a long-lived registrar writes under the
identity directory's `work-owners/`, which names the machine key), it was last written before the current holder
started, or nobody wrote it for 12 hours; a hand-registered row has the last two only.
A wait is judged an earlier session's only when its ref is a session id (a UUID). The
restart script reads every row, marks a stale one and does not block on it, and journals
each gate decision (per-session gate decisions: one JSON line per session with its round,
whether it was ready, its blockers, its work rows and the stale ones; a preview and the call
after `--nx` write none). That is the record the measure reads; it does not count what a restart
then cut off.

### Phase 2: Retirement

Remove the four-tier resolver, the `current_session` file, `directory/<name>`
and `mailbox/<session id>` as addresses, the dual subscription, and the
`session:` line, once Phase 1 has run for two weeks and no live session is a
pre-Phase-1 client: every live Claude process on the laptop (registry files)
has a binding whose `client_version` is the Step 5 release or later (the MCP
server reports its own version on every renewal, so a stale server under a
newer plugin is seen), checked by `nx
identity show --all`, which lists the bindings held on this host and the live
processes without one (not `claude -p` children, whose binding is their
dispatcher's). The engine has no route that lists every identity, so an identity
that no process on this host holds does not appear.

Claude Desktop sessions count like any other (Sam, 2026-11-22: a Desktop exemption granted that morning was dropped
once kestrel bound; T2 `nexus/sam-ruling-rdr253-desktop-exempt-2026-11-22`). A Desktop process, the one whose Claude
registry file records `entrypoint: "claude-desktop"`, binds at SessionStart with an unleased binding (`binder.py`), and
a running one on a data-scoped credential can claim an unheld seat with `nx identity claim SEAT --take-over`, which
sends `start` for a seat nobody holds (a data credential may not send `takeover`).

Open items carried into Phase 2 (accepted gaps, Sam 2026-11-19: "yes, accept both for now", T2
`nexus/sam-ruling-rdr253-accepted-gaps-0198-2026-11-19`):

1. Takeover by the data credential is narrowed, not closed. Engine 0.1.198 requires the identity's own live token
   on a data-credential `refresh` or `fork`, which closed the covert path. A data-credential `start` or `resume` can
   still take a held identity with a `holder_gone` verdict built from the holder's readable fields, and a
   data-credential `end` needs no token. That eviction is loud (the real holder is superseded). Closing it needs the
   caller's own live token on `start` and `resume` of a held identity and on `end`, and a design for a fresh launch
   that has no token yet.
2. The seat cap counts only seats the data credential creates directly. Creating ephemerals with the data
   credential (no token, no cap) and promoting each, and each delegated child's own five, bypass it. A cap per
   credential or per tenant would close that.

### Day 2 Operations

- `nx identity show [seat]`: the identity, its aliases, current binding and
  generation, liveness (live, lapsed, unleased), and recent binding rows.
- The board shows each seat's binding state.
- The binding log answers "which process held this seat at time T" for the
  last 180 days.
- After each Claude Code upgrade, check the MCP log's `_meta` keys for
  `claudecode/agentId` (subagent delegation becomes possible).
- The resume pointer's hit rate after `/clear`: the `resume_line` events with `outcome` hit against all
  `clear` starts, and `age_s` of the hits; the checkpoint hook's `resume pointer outcome=` lines (identity or none, with
  `ms=`) say how often the write fails and what it costs a compaction.

### New Dependencies

None.

## Test Plan

- Every policy-table row, including startup with no seat, a non-seat `-n`
  (ephemeral), a live seat (ephemeral with the holder), resume of an unindexed
  id, `compact`, a lapsed seat on `clear`, and `/branch`.
- A long-lived process (the MCP server) across an in-place `/restart`: its
  identity-scope scratch put and list after the generation bump use the new
  token file (the old file stays on disk and unexpired, so a stale name is
  answered `409 generation_superseded` on a write), a registry mid-write is read
  once more, an identity-scope read carries no token, and a write's 401 re-reads
  the file once (tests/identity/test_work_engine_rows.py and
  tests/identity/test_token_header_restart.py; nexus-hczdo).
- First write after `/clear` succeeds; a Bash child from before the `/clear`
  still writes; a freshly started MCP server reads its token file.
- The identity scope's caps are held in the client (tests/db/test_http_scratch_store_identity_caps.py): an
  entry over 2 KB, and the 21st note, are refused with nothing evicted; the resume pointer's slot is its own.
- The resume pointer: the writer puts before it prunes and never deletes an entry that lacks the exact tag and head
  (tests/hooks/test_resume_pointer.py); the reader prints one line of at most 400 bytes on `clear` only, drops a
  pointer over 48 hours old, and logs one `resume_line` event per `clear` start (hit, empty, error or timeout, with
  `age_s`; tests/hooks/test_resume_line.py); and a journey row writes through the real `nx scratch` commands, binds a
  `clear`, and reads the line back through the reader's real opener when the new session id has no T1 lease yet (the MCP
  watcher publishes it 0-5 s after SessionStart, so at a `/clear` it never has one): the identity-scope store sends no
  `X-Nexus-T1-Session`, the reader never calls `get_t1_database` and creates no session, and no lease file appears
  (tests/identity/test_work_engine_rows.py).
- Each restart mode (exit and relaunch, in-place `/restart`, `--revive` after a
  reboot) finds the old holder gone and binds the next generation, with no
  release step.
- A cross-seat `/resume` in a running session switches its identity, and the
  MCP server follows through the marker, or through its heartbeat re-read of
  the token file when the marker is lost.
- An empty-conversation in-place `/restart` keeps its seat at a new
  generation, `ended` included; a hook that times out waiting for its registry
  file leaves a pending file the MCP server binds from and deletes, and a
  stale pending file from a reused pid is ignored and removed; a holder whose
  pid was reused after a crash is found gone; a holder's start time compared
  in UTC is not found gone.
- The sweep marks a crashed holder's binding `gone` and leaves its own
  process and a binding rebound since its read alone; it marks a delegated
  identity `gone` when its dispatcher process is gone (or its pid was reused)
  and leaves one whose dispatcher runs alone, one failing row does not stop the
  rest, and a sweep that ended on a 403 removes no token file.
- A person at a plain shell releases a seat whose holder is gone, and is
  refused while it runs.
- Two processes resuming one session id use separate token files.
- `claim --take-over` from a `claude -p` child, or from a descendant of the
  holder, is refused; the hook prints the remedy only interactively.
- A send without a token (as `nx ask` answers) reaches a seat's mailbox.
- The operator override deletes an ended identity's board entry.
- Two simultaneous binds: one wins.
- Each fence, for every listed operation: a peer's send to a mailbox passes,
  a peer's claim on it is refused; a non-owner's membership, threads-entry
  and scratch writes are refused; a write that drops the `identity:` line is
  refused; a tokenless write is refused except for an `unleased` seat; a
  delegated child reads and does not write its parent's state.
- Token: expired (401, re-read) versus `generation_superseded` (409); refresh
  rewrites the process's one token file.
- Sleep: lapse and revive without a generation change; a launch on the same
  laptop naming a seat whose holder still runs gets an ephemeral identity
  however long the lapse; from another host, a launch inside the 10-minute
  reservation runs ephemeral and one after it binds.
- Mixed fleet: an old-client receiver gets mail from a new-client sender
  through the session-mailbox fallback, and the reverse.
- Aliases: rename retitles the board entry, and refuses a taken title; promote
  retitles it on both routes and keeps the old title when the new one is
  taken; mail to a retired alias within 7
  days with a notice, refused after; a retired name is never reissued;
  `i<digits>` refused as an alias.
- Retention: an ephemeral or delegated identity tombstoned 30 days after it
  ends or after 30 days lapsed with its holder recorded gone (a `gone` row
  included), its rows and board entry gone, its number never reused; token files
  of dead processes, and of dead dispatchers in the delegated directory, removed
  by the hook; a seat's last binding row never pruned.
- Seats from the restart script: a session relaunched (or restarted in place)
  under a name the user set gets its ephemeral promoted, or a seat created, when
  no seat has the name, and a name that already is a seat gets no second one; a
  derived name makes none and is not passed back as `-n`; a session holding a seat
  under another display name keeps it through `NX_SEAT`.
- `nx identity show --all` lists a live process with no binding and leaves out a
  `claude -p` child and a process whose pid was reused.
- A delegated child writes its own records, reads its parent's and is refused on
  them; its token file is gone and an `end` row present after every way its
  dispatch ends, a cancellation while its identity is being created included.
- Reboot: identity, aliases and bindings survive; `--revive` rebinds.
- Per-identity notes (part 3): a seat writes its own `identity/<id>` through every
  project-keyed store write; an ephemeral or delegated identity (also with a lapsed
  token), a seat writing another seat's project, and a caller whose kind the engine did
  not answer are refused before any request; a promote takes effect without a restart;
  a caller with no token file is not refused. SessionStart adds one line of at most 300
  bytes and nothing for an empty project, no identity, an error or an over-budget read;
  the identity is found through the token file that lists the session or, when the bind has
  not listed it yet, through the hook's Claude ancestor. `nx memory curate` lists the
  `identity/*` projects and credits `identity/<id>/<title>` reads to `identity/<id>`.

## Validation

### Testing Strategy

Engine tests for the tables, the bind policy and the four fences; client
tests for the resolver, hooks, handover and delegation against a real engine;
the MVV as one live run with a test seat.

### Performance Expectations

One bind per session start, resume, release or take-over, one log row per
`/clear`, `fork` or `/branch`, one renewal a minute per session on the identity lease's own
thread, and one refresh about every ten minutes (a token is refreshed once it has under 50 minutes of its hour left); the resolver
is one indexed read.

## Finalization Gate

### Contradiction Check

A generation counts processes in Approach 2, the Technical Design, the
policy table, the MVV and the Test Plan. The fences are the ownership and
generation checks on the four families of the matrix; the bind route's
compare-and-set and the refusal of a superseded renewal are the generation
rule applied to the binding itself.

### Assumption Verification

All four Critical Assumptions are settled: two verified (Bash children carry
the session id; the clear rebind, by spike), one refuted with its fallback
adopted and accepted by Sam (subagent attribution), and one verified in part
from records (Desktop binds through its hook; its MCP writes stay
unattested).

### Scope Verification

In scope: identity, aliases, binding log, resolver, token file and handover,
the four fences, delegation for `claude -p`, the holder check that covers every
restart mode, and the
Phase 1 state moves Sam chose, plus background-work registration as a label.
Out: cryptographic signatures, per-principal (human) identity, Agent-tool
subagent delegation until Claude Code identifies their calls, and anything
outside one tenant.

### Cross-Cutting Concerns

Trust: the bind and the fences guard against a confused session, not a
hostile one (JDR-002); fencing is a first refusal against JDR-002's rule, on
four families only. Release: one engine release, then client releases in the
order of Phase 1. Migration: a mixed fleet keeps both mailboxes and both board
lines until Phase 2. Data lifetime: seats permanent; ephemeral and delegated
identities tombstoned 30 days after they end or stay lapsed with their holder
gone; a seat's binding rows kept 180
days, the last one always.

### Proportionality

New engine records (identity, alias, binding, session index, membership,
background work), one bind route with several events, four fences in four
handlers, one resolver and a token file, against 46 incidents in five
months, three of them P0. Phase 1 ships as one engine release and three
client releases.

## Open Questions for Sam

Answered 2026-11-16: creation (named seats; a non-seat `-n` is ephemeral),
enforcement (fence identity state, amended to a per-process generation plus
an ownership check), children (delegated, scoped; narrowed to `claude -p`
children until Claude Code identifies subagent calls), Phase 1 scope (all
four state kinds). T2 `nexus_rdr/253-decisions-2026-11-16`.

## References

- T3 `agent-orchestration`: "Our session identity problem: four-lane research
  synthesis (2026-11-16)".
- T2 `nexus_rdr/253-research-1` to `-4` (the four lanes).
- RDR-208, RDR-252, JDR-001, JDR-002.
- apollo `stereotomy`, `fireflies`; Xanadu and nexus catalog tumblers.
- Orleans grain directory; Erlang `global`; SPIFFE concepts; LangGraph
  persistence (URLs in research 4).

## Revision History

- 2026-11-16: created (draft), from the four-lane identity research and Sam's
  four design decisions.
- 2026-11-16: Critical Assumption 1 verified by a `/clear` and `/resume` spike in a throwaway session (`nexus_rdr/253-research-5`).
- 2026-11-16: Critical Assumption 3 refuted (no `claudecode/agentId` on MCP calls in 2.1.325, `nexus_rdr/253-research-6`); the stated fallback adopted.
- 2026-11-16: Critical Assumption 4 settled in part from records: Desktop runs the hook but its MCP server never sees the session id.
- 2026-11-16: Gate round 1 — BLOCKED (2 Critical, 9 Significant, 2 ship-blocker(s)); blob `b7e086a55e87`; critique `nexus_rdr/253-gate-critique-2026-11-16-r1`.
- 2026-11-16: gate round 1 findings addressed (`nexus_rdr/253-research-7`): a generation counts processes, so `/clear`, `fork` and `/branch` keep it and only a new process or a take-over advances it; the hook-to-MCP handover through the existing handoff marker; a binding policy table with take-over; lapse and revive across sleep; Desktop bindings `unleased`; fencing as an ownership check and a generation check over four named families and their handlers, with the board kept on `memory_put`; the bind caller and credential; token refresh and 401 versus 409; a mixed-fleet mail fallback; alias reuse rules; retention for ephemeral and delegated identities; `claude -p` children not binding in their own hook. Sam accepted the narrowing of decision 3 to `claude -p` children.
- 2026-11-16: fix check 1 findings addressed (`nexus_rdr/253-fix-check-fca28b6443a1`, `253-research-8`): the restart script releases the old binding, so restarts bind by the `resume` row and no inherited variable carries take-over authority; take-over only by the one-shot `nx identity claim --take-over`; `end` on clean shutdown; a fresh MCP server reads its token file; tokenless writes to the fences refused except `unleased` Desktop seats; every mutating operation listed per fence, read against the stored entry; all engine changes in Steps 1 and 2, Steps 3 and 4 shipped together; the current generation held on the identity record; one-hour tokens refreshed through `bind`; lazy lapse with a 10-minute reservation; `session:` kept beside `identity:` through Phase 1 with its readers moved in Step 5; rename retitles the board entry; names never reissued; a non-seat `-n` ephemeral and decision 2's amendment, both Sam's answers.
- 2026-11-16: Gate round 2 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); blob `04b6e5eb5d11`; critique `nexus_rdr/253-gate-critique-2026-11-16-r2`.
- 2026-11-16: after the round-2 pass, Sam chose to fix all ten (the two residuals and the eight Significants; `nexus_rdr/253-research-9`): a holder whose host, pid and `startedAt` match no registry file no longer exists, which covers exit-and-relaunch, in-place `/restart` and `--revive` alike, so the restart script needs no release; the Desktop exception keyed on the target seat being `unleased`; a cross-seat `/resume` switches the process's identity through the marker; token files per process start, not per session id; take-over refused for non-interactive callers and descendants of the holder, its remedy printed only interactively, and the caller's ephemeral identity ended; Gap 4's consumers (worktree removal, sandbox and tmux cleanup, the restart readiness check) on `nx identity alive`, beads out by Sam's earlier ruling; sends exempt from the tokenless refusal; one token file per process refreshed in place; an operator override and retention for ended identities' board entries; JDR-001 revision entry.
- 2026-11-16: post-pass fix check 1 findings addressed (`nexus_rdr/253-fix-check-9299310ff81f`; spikes `nexus_rdr/253-research-10`): the holder check is client work, sent to `bind` as `holder_gone` evidence, with a pid probe so a crash is caught; take-over and release keyed on registry `entrypoint` (`sdk-cli` for a `claude -p` child), refused with no Claude ancestor; in-place `/restart` measured to fire `source: resume` with a new `startedAt`; the MCP heartbeat re-reads its token file so an identity switch never depends on the marker; one marker content (identity, generation, session id, token-file path); a seat closes with `release`, an ephemeral with `end`; same-host liveness by the probe however long the lapse; Desktop holders freed by the holder check; the session-scope check accepts the identity's session ids; `display_name` for ephemerals; retention by end or lapse with the holder gone; the delegated token file's path and cleanup; `nx identity purge` for the operator override; `work/<id>` kinds for worktrees and tmux servers; the Phase 2 gate on the Step 5 release; the bead ruling cited; stale sites swept.
- 2026-11-16: accept residuals fixed in the text (Sam: "Fix in text"): the ended-identity resume row narrowed to ephemeral and delegated identities; a `gone` route and a SessionStart sweep report gone holders for retention and purge; `holder_gone` in the binding body and a pending bind when the hook's registry wait times out; release frees only a gone holder's seat and runs from a plain shell; the pid probe compares the OS start time with `procStart`; an empty in-place `/restart` keeps its seat by pid and start-time continuity; the session-scope check, heartbeat re-read and renewal client version given owning steps; seats created by Sam, not the restart script.
- 2026-11-16: accept fix check `nexus_rdr/253-fix-check-224682f97eef` (FAIL, one counted blocking defect) fixed: Step 4 drops the take-over guards from release; `proc_start` in the binding and bind evidence, compared as UTC instants; the empty in-place `/restart` rebind binds a new generation by its own policy row; the `gone` row with a holder-match guard and compare-and-set, the bindings-by-host route, and a sweep that skips its own process and delegated identities; the pending file keyed by pid and OS start time, bound by the MCP server and deleted; Sam's decision 1 restored (the restart script creates seats for sessions it relaunches under a user-set name).
- 2026-11-16: accept residual dispositions: residuals 1-8 of `253-gate-latest` fixed in the text by 9206b2cca and 530195c73 (fix checks `253-fix-check-224682f97eef` FAIL, `253-fix-check-125b505e41d3` PASS 3-0); its four counted DISCOVER-AT-IMPLEMENTATION items go to the plan as bead notes.
- 2026-11-16: gate fix check `253-fix-check-88e4ee8da3a2` rerun at Sam's direction in the format the accept guard counts: PASS 3-0, 0 BLOCKS-PLANNING; accepted.
- 2026-11-17: wording amended to match the reviewed Step 1 code (grant 3), bead nexus-hczdo.1.1.
- 2026-11-17: wording amended to match the reviewed Step 2 code (grant 3), bead nexus-hczdo.1.2: identity tokens and their expiry rule, stale-generation and holder-mismatch codes, the four fences' refusal codes and check-then-write limit, threads, scratch and mailbox behaviour, and what the stamps say.
- 2026-11-17: wording amended to match the reviewed Step 3 code (grant 3), bead nexus-hczdo.1.4: the identity lease on its own thread and `end` only after Claude exits; a notice, not a seat list, for an `NX_SEAT` naming no seat; the cases where the hook binds nothing; the resolver's fourth input and the token file's fields; which requests carry the token and the client-side session check; the holder check's registry-directory rule, unknown holders and Windows start time; data-credential degradation; `NX_IDENTITY_LEASE=0`; the pending bind logging its notices; the unindexed-resume reuse row; the Step 3 plan entry; the Desktop pending bind and the in-process `/resume` registry timing stated as unverified.
- 2026-11-18: wording amended to match the reviewed Step 4 code (grant 3), bead nexus-hczdo.1.5: delegated identities for tool-granting dispatches only, their binding naming the dispatcher process and the sweep that records `gone` for a dead one and removes its token files (not after a 403); `nx identity show --all` scoped to bindings on this host and live processes without one, `claude -p` children left out; the restart script keeps a held seat through `NX_SEAT`, promotes an ephemeral or creates a seat for a `nameSource` `user` name, before an in-place `/restart` too, and passes no derived name back as `-n`; the take-over ancestor guard exempts the caller's own Claude process; Test Plan lines for these.
- 2026-11-19: wording amended to match the reviewed Step 5 code (grant 3), beads nexus-hczdo.1.7 and nexus-hczdo.1.8: a named send goes to the single live `directory/<name>` holder's session mailbox, `mailbox/<id>` only when nobody holds the name, two holders refused; in Phase 1 named mail does not survive `/clear`, and the identity mailbox that does follows in a later step (Sam 2026-11-19, T2 `nexus/sam-ruling-named-mail-phase1-2026-11-19`).
- 2026-11-19: amended for cloud installs (Sam's ruling "go with option 2, no bridge", T2 `nexus/sam-ruling-rdr253-cloud-seats-option2-2026-11-19`; wording approved as drafted, T2 `nexus/sam-ruling-rdr253-amendment-self-seat-wording-2026-11-19`): the data credential may promote the ephemeral its own identity token names, create a delegated identity from its token's identity, and create an unheld seat stamped `created_by identity:<token id>`; rename, `gone` and purge stay tenant-only; `/version` gains `identity_self_seat_supported`.
- 2026-11-19: wording amended to match the reviewed cloud-seats client (grant 3), bead nexus-hczdo: the client detects an older engine from the data-credential refusal's wording and says so once, not by reading `/version`. No design change.
- 2026-11-19: wording amended to match the reviewed 0.1.198 client, round 1 (grant 3), bead nexus-hczdo: the server refreshes under 50 minutes of token life, so a sleep under about 50 minutes never lapses it and a longer one may; after a lapse refreshing stops until a SessionStart bind writes a fresh token; the hook's refused `fork` starts the same identity again, not a new ephemeral.
- 2026-11-19: the data credential's self-service is bounded (Sam's choices on the drafted text, T2 `nexus/sam-ruling-rdr253-0198-hardening-A-cap5-2026-11-19`, option A and cap 5; sequencing T2 `nexus/sam-ruling-token-sequencing-and-rollup-bar-2026-11-19`): at most 5 seats an identity creates through it (409 `seat_limit`); a data-credential `refresh` or `fork` needs the identity's own live token, with no grace for an expired one; the cap ships in engine 0.1.198, the client sending the token in 8.5.1, and the token rule's enforcement in 0.1.199.
- 2026-11-19: 0.1.199 folded into 0.1.198 (Sam, T2 `nexus/sam-ruling-merge-0198-0199-2026-11-19`): the token rule's enforcement ships in 0.1.198 with the cap, deployed after the sessions restart onto 8.5.1.
- 2026-11-19: wording matched to the reviewed 0.1.198 cap and token-rule code (grant 3; reviews T2 `nexus/review-rdr253-0198-cap-token-2026-11-19` and `nexus/critique-rdr253-0198-cap-token-2026-11-19`): the token rule narrows the takeover gap (refresh and fork), it does not close it (start, resume and end); the cap counts seats created directly, for the identity's life (no route deletes a seat), not promoted ephemerals; an expired token counts as none.
- 2026-11-19: two accepted gaps recorded as Phase 2 open items (grant 3; Sam's acceptance, T2 `nexus/sam-ruling-rdr253-accepted-gaps-0198-2026-11-19`): data-credential takeover through `start`, `resume` and `end`; the seat cap's bypass through promoted ephemerals and delegated children.
- 2026-11-19: wording matched to the reviewed client fix-later round (grant 3; reviews T2 `nexus/review-fix-later-client-2026-11-19-r1` and `nexus/critique-fix-later-client-2026-11-19-r1`): the Cost line said one refresh every half hour; the lease and the delegated child refresh once a token has under 50 minutes of its hour left, about every ten minutes.
- 2026-11-19: wording matched to the reviewed engine 0.1.198 promote code (nexus-hczdo B4b, 71f0e972a, f85aa29eb; grant 3; reviews T2 `nexus/review-d288f22ec-rdr252-alias-b4b-2026-11-19` and `nexus/critique-d288f22ec-rdr252-alias-b4b-2026-11-19`): promote retitles the threads entry in the same transaction on both routes; on a taken title rename refuses and promote keeps the old title (`board_retitle: title_taken`).
- 2026-11-19: wording matched to the reviewed lease fork-race fix (grant 3; bead nexus-hczdo, develop run 44659989781; reviews T2 `nexus/review-0feb06c39-nexus-hczdo-2026-11-20-r1` and `nexus/critique-forkrace-2026-11-19-r1`): an unknown registry session id gets one renewal of grace before its `fork` row, so a `/clear` whose hook binds within it leaves no `fork` row.
- 2026-11-20: wording matched to the reviewed token-file fix (grant 3; bead nexus-hczdo; reviews T2 `nexus/review-token-file-restart-2026-11-20-r1` and `nexus/critique-token-file-restart-2026-11-20-r1`): the token file name comes from the registry's current `startedAt`, read on every call, so a process that outlives an in-place `/restart` follows it to the new file; a registry or token file that cannot be read is read once more after about 10 ms; the identity token goes only on identity-scope scratch writes, and a write's 401 re-reads the file once; Test Plan line for the long-lived process across an in-place `/restart`.
- 2026-11-20: wording amended to match the reviewed Step 6 part 3 code (grant 3), bead nexus-hczdo (reviews T2 `nexus/review-47774ae08-nexus-hczdo-2026-11-20` and `nexus/critique-step6-part3-2026-11-20-r1`): per-identity T2 notes are guarded by the client, not fenced by the engine (seats only, own project only, the seven project-keyed store writes, the id-keyed paths not covered); a delegated identity keeps none and `--mine` writes from it are refused; retention does not prune `identity/<id>`; RDR-238 curate reads `identity/*`, the use measure is transcript reads; RDR-209 graduation excludes `identity/*`.
- 2026-11-20: Step 6 part 2 recorded (the resume pointer; fix round 1, reviews T2 `nexus/review-step6-part2-2026-11-20-r1` and `nexus/critique-step6-part2-2026-11-20-r1`, decision T2 `nexus/decision-identity-scratch-caps-client-side-2026-11-20`): the identity scope's caps (2 KB an entry, 20 notes, a reserved slot for the resume pointer) are enforced in the client's identity-scope put, not by the engine, and the earlier text that implied an engine cap says so now; the pointer is one `resume-pointer` entry written at PreCompact and at SessionEnd with reason `clear`, printed once on `clear`, with no session-scope fallback.
- 2026-11-20: wording matched to the reviewed resume-line fix (grant 3; bead nexus-hczdo; reviews T2 `nexus/critique-resume-line-no-lease-2026-11-20-r1`): the identity-scope store sends no T1 session token, so the `/clear` resume line prints with no lease for the new session id; the Test Plan line that said it printed nothing without one is corrected.
- 2026-11-21: wording amended to match the reviewed GH #1679 code (grant 3; branch `feature/gh1679-stable-host-identity`, tip `a4ac5cfa3d`; reviews T2 `nexus/review-4e48325fa-gh1679-2026-11-21-r2` and `nexus/critique-4e48325fa-gh1679-2026-11-21-r2`): a binding's host is a stable machine key (`nxh1-<32 hex>`, from the macOS hardware UUID, the Linux machine id in the initial pid namespace or the Windows MachineGuid) with a hostname fallback; a key that is not this machine's is never local; a non-key name is local only on this machine's own token-file or registry proof for the holder's `(pid, startedAt)`; a cloned image or a copied identity directory under a non-key name remains possible; a `lab-remote` work ref carries the short hostname while owner records carry the key.
