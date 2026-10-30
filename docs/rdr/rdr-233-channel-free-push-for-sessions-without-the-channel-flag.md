---
title: "Channel-Free Push: Wake a Session Launched Without the Channel Flag"
id: RDR-233
type: Feature
status: accepted
accepted_date: 2026-10-30
priority: medium
author: Claude (session for bead nexus-7hzb2), for Sam
reviewed-by: code-review-expert, substantive-critic (2026-10-22)
created: 2026-10-22
related_issues: [nexus-7hzb2]
related_rdrs: [RDR-211, RDR-213, RDR-208, RDR-205]
---

# RDR-233: Channel-Free Push: Wake a Session Launched Without the Channel Flag

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

**Provenance.** A Claude Desktop Code-tab session investigating T1 and the
tuple space, 2026-10-22, found that Desktop starts its bundled `claude` binary
without `--channels`, so the RDR-211/213 channel never reaches those sessions.
The same session proved a channel-free wake by hand and asked for it to be
built (bead nexus-7hzb2). This RDR amends two of Sam's RDR-211 decisions of
2026-09-16 (T2 `nexus_rdr/211-decision-channel-delivery-2026-09-16`): item 3,
"wait is not a general MCP tool or CLI verb", and item 5, "the Monitor watcher
is removed outright, not kept as a fallback". Both amendments are Sam's to
accept.

Decision 5 already named this session and chose for it. Its design was "the
channel as the only push path, nx tuple watch and the SessionStart arm
injection deleted, the UserPromptSubmit drain hook kept as the floor (mail at
the next prompt for a session launched without the channel)". So this is a
reversal of a stated choice for a known case, not a gap the decision missed.
What has changed since is the size of that case: a Desktop Code-tab session can
never carry the flag, so for it the floor is the only delivery, and board posts
have no floor at all.

Terms used throughout:

- **Channel**: Claude Code's research-preview path by which an MCP server
  pushes a notification (`notifications/claude/channel`) into a running
  session. A session opts in per launch with `--channels` (or
  `--dangerously-load-development-channels`).
- **Channel waiter**: the background task in the session's own nexus MCP
  server that parks the engine's `wait` over the session's mailbox and
  subscribed board topics and sends a channel notification per new row
  (RDR-213).
- **Drain hook**: the `UserPromptSubmit` hook that claims and renders pending
  mailbox mail when the user next sends a prompt. It is the delivery floor.
- **Background task**: a Bash tool call made with `run_in_background`. When it
  exits, Claude Code re-invokes the session with the task's output, even if the
  session is idle with no user prompt pending.

## Problem Statement

#### Gap 1: A session launched without the channel flag gets no push at all

Claude Code's channel documentation (code.claude.com/docs/en/channels.md,
enterprise controls) says no settings key or environment variable enables a
channel without the launch flag; `channelsEnabled` and `allowedChannelPlugins`
only gate a flag that is present. Claude Desktop's Code tab launches `claude`
with no `--channels`, and the user cannot add one. In those sessions the
channel waiter runs and sends notifications that nothing listens for. Mailbox
mail arrives only when the user next types (the drain hook). Board posts never
arrive. Sam's terminal sessions are unaffected: a `claude()` wrapper in
`~/.zshrc` adds `--channels plugin:conexus@nexus-plugins`.

#### Gap 2: The SessionStart guidance assumes the channel

The SessionStart line (`nexus.mailbox_arm.mailbox_arm_instruction`) tells
every session that its mailbox "is already subscribed and delivered over the
channel regardless", and offers launch flags as the fix. A Desktop session can
act on neither. It is told nothing about a path that would work for it.

## Context

### Background

**What decision 5 removed, and why.** RDR-211 Gap 5 described the Monitor
watcher it deleted: it probed each mailbox every few seconds with a zero-timeout
`rd`, printed a ping the session had to act on, "must be armed by the model at
every session start and re-armed at every 30-minute expiry", and watched only
the addresses it resolved at startup. Sam's words in the decision were about
noise: "the mailbox monitor noise/mechanism" and "without all the timeout and
other chatter". `nx tuple wait` brings back one part of that shape, a waiter
the model arms and re-arms. It differs in what it prints and when: it parks in
the engine rather than polling, prints nothing until a row arrives, reads the
subscription set every park rather than fixing addresses at start, and exits
(so the model sees anything at all) only on a delivery or on a stop that says
not to re-run. The re-arm per delivery is the cost this RDR asks Sam to accept.

RDR-211 made the session's own MCP server the subscriber and the channel the
only push path, and deleted the Monitor-driven `nx tuple watch` loop. RDR-213
removed the proof gate and claim-at-delivery: the channel announces a
reference and the session claims. Since beads nexus-vsipz and nexus-q82tk the
engine decides what is due (an announce stamp on the row for mailboxes, a
per-subscriber stamp in `nexus.tuple_deliveries` for boards), so no client
keeps a delivery cursor.

### Technical Environment

- Engine `POST /wait` (`HttpTupleStore.wait`): one parked call over up to
  `_MAX_WAIT_SPECS` subspaces, with no claimant.
- Per-subscriber announce (`Announce.subscriber`): the engine selects rows that
  are claimable and due for that subscriber, stamps them in the same statement,
  and re-scans from the oldest row with no client position, so a late-committed
  `out` cannot be skipped (`TupleRepository.queryOnceAnnounceSubscriber`). Its
  javadoc says a mailbox spec that names a subscriber "would behave exactly like
  the row-level shape with a per-subscriber count". `since` alongside it is
  honoured as a `created_at` lower bound (nexus-n36sw). A `waiter` token
  (nexus-rxuiq) makes a newer waiter supersede an older one's parked call.
- The SessionStart hook writes a per-claude-pid session marker
  (`nexus.session_marker`) on every source, naming the session id that is
  current for that `claude` process.
- `nexus.session.find_immediate_claude_pid` finds the nearest `claude`
  ancestor of any process.

## Research Findings

### Investigation

Source reads of the client channel waiter (`nexus.mcp.channel`), the
subscription set (`nexus.mcp.subscriptions`), the session-id chain
(`nexus.session`, `nexus.session_marker`) and the engine's wait path
(`TupleRepository.queryOnceAnnounceSubscriber`, `TupleHandler.renderWaitResult`);
the T2 decision record `nexus_rdr/211-decision-channel-delivery-2026-09-16`;
substrate tests against the engine built from this branch; and live runs from a
Desktop Code-tab session against the cloud engine on 2026-10-22. Findings are
recorded as `nexus_rdr/233-research-1` to `-12` (ten verified or documented,
two assumed).

### Key Discoveries

- **Verified (hand test, 2026-10-22, the Desktop session that raised this):** a
  background Bash command that loops `nx tuple rd <subspace> --json -n 10
  --since <created_at,id> --timeout-s 25` and exits on the first JSON row
  re-invoked an idle Desktop Code-tab session with no user prompt, within one
  25 s park of the post. Two gotchas: a plain `rd` miss prints "No matching
  tuples." to stdout, so the loop has to test for a row, not for output; and
  the task output ends with an "[exited with code 0]" line after the JSON.
- **Verified (source):** the pre-RDR-213 channel gate read the parent
  `claude` process's command line with `ps -ww -o command=` and matched the
  launch flags as whole tokens (`detect_channel_argv`, deleted in f07e37a01).
  That was measured to work for both flag forms (nexus-tk2cz). It was deleted
  because it was the wrong way to prove a claim was safe, not because the argv
  read was wrong.
- **Verified (source):** `nexus.mcp.channel` has no own-post rule. A session
  that posts to a board it subscribes to is announced its own post. The
  channel tolerates this. A wake-on-exit waiter cannot, because it would wake
  the session for every one of its own posts.

### Critical Assumptions

- A background task's exit wakes an idle Desktop session. **Status:**
  verified twice: the hand-run `rd` loop above, and `nx tuple wait` itself
  (2026-10-22, this bead's session, a Desktop Code-tab session on the cloud
  engine): armed in the background on a scratch mailbox, it exited on a
  `mailbox_send` to that mailbox and the session was re-invoked with its
  output.
- The Desktop `claude` process is found by name and its command line is
  readable. **Status:** verified in the same session: the nearest ancestor's
  `comm` is `claude`, its command line was read (3,722 characters) and carries
  no `--channels`, so `channel_launch_state()` is `False`.
- The SessionStart marker names the current session in a Bash child.
  **Status:** verified in the same session: marker and `CLAUDE_CODE_SESSION_ID`
  agree. After `/clear` the marker is the one rewritten on every SessionStart
  (`nexus.hooks.session_start`), so the waiter prefers it. Not exercised
  across a real `/clear`.
- A background task stays parked through a long idle and still wakes the
  session. **Status: verified for 61 minutes** (research-8, 2026-10-23): a
  waiter armed at 03:00:27Z in an idle Desktop session on the cloud engine
  exited one second after a post at 04:01:27Z and re-invoked the session; no
  lifetime cap was hit. RDR-211 Gap 5's 30-minute expiry belonged to the
  Monitor tool and does not apply here.
- The task survives a `/compact` and an idle or sleeping Desktop (system
  sleep). **Status: unverified** (research-12). If either ends it, the exit
  wakes the session with no re-arm line, so push falls back to the drain floor
  until the next prompt; it does not loop.
- Re-arming does not stop at a permission prompt. **Status: unverified in
  general.** In this session the Bash call ran without one; a session whose
  permission mode prompts for Bash would need `nx tuple wait` allowed, or the
  re-arm waits for the user.
- The argv read sees the launch flag in terminal sessions. **Status:** verified
  for both flag forms in nexus-tk2cz; the matcher also reads `=` values and
  value lists, pinned by tests.

## Proposed Solution

### Approach

1. **`nx tuple wait`**, a CLI verb meant to be run as a background task. It
   parks the engine `wait` over the session's own `mailbox/<session id>` and
   every board topic in its T1 subscription set, and exits 0 as soon as a row
   that is not the session's own arrives. It prints one fixed-template line per
   row, the same text the channel would have sent (`_mailbox_notification_content`,
   `_board_notification_content`, sender labels included). It never prints a
   tuple body.
2. **No client cursor.** Each spec carries a per-subscriber announce with
   subscriber `wait:<session id>`, `max=1`, and a fresh `waiter` token. The
   engine hands each row to this subscriber once, only while it is claimable,
   so mail the drain hook already consumed never wakes the session. Board specs
   send the topic's subscribe-time watermark as `since`, exactly as the channel
   waiter does, so subscribing replays at most the 30 s before the subscribe
   call (the channel's clock-skew margin), never the topic's backlog. Re-arming is the
   same command every time.
3. **Own posts never wake.** A row whose engine stamp (`sender_session`) is this
   session, or, when the engine renders no stamp, whose `from` names this
   session, is consumed silently (the stamp is already spent) and the wait goes
   on. The exception is a subagent's mail: subagents share the parent's token,
   so their rows carry the parent's stamp, and a row whose `from` is an agent id
   is delivered.
4. **Only when the channel is absent.** The verb reads the nearest `claude`
   ancestor's command line (the revived `detect_channel_argv` read, now
   `nexus.channel_launch.channel_launch_state`) and refuses with exit 4 when a
   channel flag for conexus is present, so a terminal session never gets two
   wakes per row. `--force` skips the check. The read is three-valued: no
   `claude` ancestor, or an unreadable command line, is "cannot tell", and each
   caller keeps its pre-RDR-233 behaviour for it (the verb runs; the guidance
   keeps the channel wording).
5. **Self-limiting.** The loop exits, printing one line saying why, when the `claude` ancestor is gone,
   when the session marker names a different session (a `/clear`), or when the
   engine reports the call superseded by a newer `nx tuple wait` for the same
   session.
6. **SessionStart guidance.** When the hook's own `claude` ancestor was read
   and carries no channel flag, the subscribe line drops its "delivered over the channel"
   clause and adds one sentence: run `nx tuple wait` with `run_in_background`
   now, and again after each wake whose output says to.

### Technical Design

`nexus.tuple_wait.run_wait(store, session_id, ...)` holds the loop so tests can
drive it with a real store and fake liveness probes; the Click command in
`nexus.commands.tuple_cmd` resolves the session, the store and the T1 board
topics and calls it. Each park re-reads the T1 subscription set, so a topic
subscribed while the waiter is parked is picked up at the next park (at most
`--timeout-s`, default 25 s). If T1 is unavailable the waiter parks on the
mailbox alone and says so once on stderr.

A failed park is slept off (5 s, doubling, capped at 60 s) and retried,
because any exit wakes the session and a gateway blip is not news; six in a
row end the wait with exit 1. An engine refusal (`TupleError`) is not retried.
Found in the live dogfood: one cloud `/wait` returned 503 past the store's own
three retries, and the next call succeeded.

Exit codes: 0 delivered rows, or stopped for a stated reason; 1 store error
(refused, or down for six parks);
5 the engine did not honour the per-subscriber announce (an engine older than
nexus-q82tk); 4 channel active.

### Existing Infrastructure Audit

| Piece | Reused from |
|---|---|
| Multi-subspace park | `HttpTupleStore.wait` |
| Delivery position | engine per-subscriber stamp (nexus-q82tk) |
| Board start position | `SubscriptionSet.entries()` `since`, minus `DEFAULT_BOARD_START_SKEW_S` |
| Notification text and sender labels | `nexus.mcp.channel` content builders |
| Supersession | engine `waiter` token (nexus-rxuiq) |
| Session id after `/clear` | `nexus.session_marker` |

### Decision Rationale

The engine already does everything a client cursor would do and does it
better (no commit-order skip, claimable-only filtering, supersession). A
separate subscriber name keeps this waiter's stamps from spending the channel
waiter's, so a session that later gains the channel loses nothing.

## Alternatives Considered

### Alternative 1: A shell loop over `nx tuple rd`, as hand-tested

- Pros: needs no new code; proven to wake a Desktop session (research-1).
- Cons: each session carries its own cursor per subspace, skips
  late-committed rows, re-wakes on mail already drained, and wakes on its own
  posts.
- Rejected: every one of those is solved engine-side already.

### Alternative 2: Re-enable the channel for Desktop

- Pros: one push path for every session, as decision 5 intended.
- Cons: not possible from nexus: Desktop owns the launch command and Claude
  Code offers no setting that enables a channel without the flag (research-6).
- Rejected: not available. It is the better answer if Desktop or Claude Code
  ever allows it, and this RDR's waiter refuses to run wherever the flag is
  present, so it would mostly retire itself. A flag that is present but does
  not register a channel is the exception (Risks); `--force` covers it.

### Alternative 3: Do nothing; rely on the drain hook

- Pros: no second path; decision 5 stands unamended.
- Cons: mail waits for the user's next prompt and board posts never arrive,
  which is Gap 1.
- Rejected only if Sam judges Gap 1 worth a second path; this is the choice
  decision 5 made, and it stays available by not accepting this RDR.

## Trade-offs

### Consequences

- A second push path exists, which is what item 5 of the 2026-09-16 decision
  closed. It is confined to sessions the channel cannot reach, refuses to run
  where the channel is live, and needs no MCP-server code.
- Each wake costs one task-exit notification and one re-arm tool call.
- Capacity: each waiter holds one of the engine's global park slots (16 by
  default, `docs/tuple-space.md` § caps), beside the channel waiter that keeps
  running idle in the same session. Every session's channel waiter already
  holds one, so the ceiling is reached at sessions plus channel-less sessions
  = 16 (eight sessions if all are channel-less); the `tuples.park_slots` doctor
  row warns at 75%. At the ceiling the engine answers 429, which is slept off and retried, and six in a
  row stop the waiter with a line saying not to re-run. Stopping the idle
  channel waiter in a channel-less session would give the slot back; it is out
  of scope here.

### Risks and Mitigations

- **A run killed between the engine's stamp and its print loses the wake.**
  The stamp is spent in the same statement that returns the row, so the next
  run will not see it. A mailbox row is still claimable and reaches the drain
  hook at the next prompt; a board post is still readable with `tuple_rd`, but
  nothing will point at it.
- **Re-arm ping-pong.** SessionStart fires on compact and resume too, so a
  second waiter can supersede a parked first one, whose exit wakes the session.
  The superseded and session-changed exits say not to run it again, and the
  SessionStart line says to re-run only after a wake that says to.
- **Subagent mail.** Subagents share the parent's token, so their rows carry the
  parent's stamp. A row whose `from` is an agent id is delivered, never treated
  as the session's own.

- **The model forgets to re-arm.** The drain hook remains the floor for mail;
  the wake output ends with the re-arm instruction. Board posts have no floor.
- **Wake loops.** Every exit wakes the session, so only a delivery prints the
  re-arm line. Every other exit (superseded, session changed, engine refusal or
  unreachable, channel flag present, `--max-parks` spent) says not to run it
  again, and the SessionStart line and the mailbox skill say to re-run only
  when the output says to. Transient engine failures are retried inside the
  process.
- **Argv read fails** (no `ps`). It reads as "cannot tell": the guidance keeps
  the channel wording, so the model is not asked to arm the waiter; if it is
  armed by hand in a session that does have the channel, each row wakes the
  session twice, which is noise, not loss.
- **Flag present, channel not registered.** The flag on the command line does
  not prove a channel is live: Claude Code starts normally but registers no
  channel when the named plugin is not on the channel allowlist, when the
  organisation has not enabled channels, or when the development-channel
  dialog was not confirmed (research-10). This RDR reads the flag as "channel live", the
  inference RDR-213 removed for its claim gate; here it only decides which
  wake path to suggest. Such a session gets the channel wording, no waiter is
  suggested, and `nx tuple wait` refuses with exit 4. Its mail still reaches
  the drain hook at the next prompt; its board posts are not pushed by anything
  (no regression from today). The exit-4 message says so and names `--force`
  for a session that sees no channel notifications. Telling a registered
  channel from an unregistered one would need a signal Claude Code does not
  give an MCP server (RDR-211 Phase 1 Step 0 found its declared capabilities
  empty either way, research-11), so it stays out of scope.

## Implementation Plan

### Phase 1: The waiter, the launch-flag read and the guidance

#### Step 1: `nexus.channel_launch` and `nexus.tuple_wait`, with substrate tests first

#### Step 2: `nx tuple wait` in `nexus.commands.tuple_cmd`, and the SessionStart wording in `nexus.mailbox_arm`

#### Step 3: Docs (CLI reference, tuple space, mailbox skill, CHANGELOG, plugin ledger)

Including the permission rule a prompting session needs: in a permission mode
that asks before running Bash, every arm and re-arm of `nx tuple wait` is a
prompt, and an unanswered one in an idle session ends the push loop (the drain
floor still delivers mail). The CLI reference, the tuple-space doc and the
mailbox skill name the allow rule `Bash(nx tuple wait:*)` for
`settings.json`. Nexus does not add it on the user's behalf: permissions are
the user's.

Built on branch `feature/nexus-7hzb2-tuple-wait`, ahead of this gate, and not
pushed: the branch is the proposal's evidence, and acceptance decides whether
it lands.

### Minimum Viable Validation

A substrate-backed test against a real engine: a post to the session's
mailbox by another session wakes `run_wait` and prints the row; a post by the
session itself and a park with nothing new do not.

### Proposed beads

One bead, nexus-7hzb2: the verb, the launch-flag helper, the guidance, tests,
`docs/cli-reference.md`.

## Finalization Gate

### Contradiction Check

The RDR amends RDR-211 decisions 3 and 5 and says so in Provenance; it does
not edit RDR-211. Within this RDR, "re-arm after a delivery" and "every exit
wakes the session" are reconciled by the wording rule in Risks: only a delivery
asks for a re-run.

### Assumption Verification

Research-1 to -5, -7, -8 and -11 are verified (spike or source); research-6
and -10 are the Claude Code documentation. Research-9 (no Bash permission
prompt on re-arm; Plan Step 3 documents the allow rule) and research-12
(survival across `/compact` and system sleep) are assumed; each states its
fallback.

### Scope Verification

The Minimum Viable Validation is in `tests/test_tuple_wait.py` and passes
against the engine built from the branch; nothing in the Approach is deferred.

### Cross-Cutting Concerns

- **Local and cloud**: the same verb and engine route in both modes; the cloud
  `/wait` was answering 503 intermittently on 2026-10-22 (research-7,
  nexus-pgsr1), which the in-process retry absorbs.
- **Windows**: the command-line read goes through `winproc_core` there;
  untested on a Windows host.
- **Capacity**: one more global park slot per channel-less session (Trade-offs).
- **Multi-instance engine**: supersession is fenced per engine process, so on
  an engine with several instances a second `nx tuple wait` for one session may
  not replace the first; both then wake on the next row. Noise, not loss.

### Proportionality

Two small modules, one verb, one wording switch, no engine change and no new
dependency.

## Revision History

- 2026-10-22: drafted (nexus-7hzb2).
- 2026-10-22: live dogfood from a Desktop Code-tab session against the cloud
  engine: an armed `nx tuple wait` on a scratch mailbox exited on a
  `mailbox_send` to it with the channel's text, and a re-arm did not repeat the
  row. A transient 503 on `/wait` added the retry-with-backoff rule above.
- 2026-10-23: Gate round 1 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); commit `e0923d3c6`; critique `nexus_rdr/233-gate-critique-2026-10-23-r1`.
- 2026-10-23: research-8 measured: a parked waiter survived 61 minutes idle and
  woke the session one second after a post; `/compact` and system sleep split
  out as research-12 (assumed). Gate round 1 findings S1, S2, S4, S5 fixed in
  b934d3325.
- 2026-10-24: renumbered to 233. Its first number collided with the note source-uri scheme RDR on develop (nexus-t7fbb), and 232 is held by another draft. T2 records moved to `nexus_rdr/233-*`; the gate round 1 record was written against this file under its old name at `e0923d3c6`.
- 2026-10-24: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `56e6bd046`; critique `nexus_rdr/233-gate-critique-2026-10-24-r2`.
- 2026-10-30: gate round 2 findings fixed at accept: Approach 3 states the
  subagent-mail exception (N1); the `mailbox_arm_instruction` docstring says to
  re-run only when a wake's output says to (N2). The two assumed records,
  research-9 and research-12, acknowledged by Sam with the accept.
