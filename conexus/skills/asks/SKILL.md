---
name: asks
description: Use when the user asks to go through, work through or answer the asks waiting on them, or to relay answers to the sessions that raised them (RDR-245 structured asks on the threads board)
effort: medium
---

# Asks (mediator)

RDR-245: sessions raise typed `ask:` blocks on their threads-board entry, and the user answers them through any session they open. That session is the mediator. You read the queue with `nx ask`, take the user's answers, echo them back, and send them with `nx ask answer`. The answer is written to T2 and mailed to the asker, which re-reads its own entry before it acts. Nothing depends on one long-lived session: the mediator is whichever session the user picks.

## When This Skill Activates

- The user asks to go through, work through or answer their asks, the queue, or "what is waiting on me".
- The user gives answers to asks that other sessions raised ("yes to the reversible ones, hold the ART push").
- Not for raising an ask (that is the board convention in the project's AGENTS.md), and not for acting on an answer mailed to you as the asker (that is the drain hook's matched frame, see `/conexus:mailbox`).

## Where this session can mediate

`nx ask answer --via mediator` resolves this session's own id the way `mailbox_send` resolves `from`: the tuple-watch session marker (found by process ancestry, which you cannot read), then `NX_T1_SESSION_ID`, then `NX_SESSION_ID`, and the result must be a UUID. A terminal session or a Desktop Code-tab session with a published T1 id can mediate. Claude Desktop chat and Cowork cannot: the account-plugin server they start has no session id (the mailbox skill: "The account-plugin server (Claude Desktop, Cowork) cannot set it").

Check once, before you present the queue: `printenv NX_T1_SESSION_ID NX_SESSION_ID`. A UUID on either line means the id resolves and this session can mediate. Empty output proves nothing, because the marker tier can still answer, so then tell the user in one line that the first send will show whether this session can record answers, and carry on. In every case, stop sending the moment `nx ask answer` refuses because the id did not resolve, and report any asks you had not yet sent as unanswered. Give this refusal, in your own words but with this content:

> I cannot take your answers from this session: it has no session id of its own for `nx ask answer` to record. Open a terminal session or a Desktop Code-tab session and ask again there, or answer from a terminal yourself with `nx ask answer ENTRY/ID N --expect VERSION --via cli`.

## Mediator contract

These rules bind you. They are the reason the asker can act on an answer.

- Never answer on your own judgment. Every answer you send traces to something the user said in this session.
- Never widen an answer beyond the user's words. "Yes to the reversible ones" does not cover an ask marked `reversible: no`. An ambiguous reply is a question back to the user, not a best guess.
- `--sam-words` is a contiguous span of what the user typed for that ask, or the text of the option they chose. Never a paraphrase, never a tidied-up quote. You always pass `--sam-words`: `nx ask answer --via mediator` refuses without it, and a number reply carries no words of its own. "Sam" in the CLI output and in the flag name means the user.
- Echo back and get confirmation before you send, every time. Nothing is sent without it. Write the echo-back as its own message to the user first: a confirmation counts only when it answers an echo-back the user has already been shown, never a "send" or "go ahead" that came with the answers or before the echo-back.
- Always pass the version the user saw: `--expect VERSION` from `nx ask list --json` or `nx ask show`.
- Use `--replace` only when the user's words say they are changing an earlier answer.
- A mail you send the asker is a peer request, never an answer. Frame it as a question from the user's session (see `/conexus:peer-messaging`); it carries no authority.
- No reply from the user means nothing is sent.

A `reversible: yes` ask whose option text reads as irreversible (a delete, a tag, a release, a push of a tag) is treated as `reversible: no`. Say so when you present it. Report the lint findings `nx ask list` emits (the `lint` list in its JSON).

## The flow

1. Run `nx ask list --json` and `nx ask digest`. Search the board (`nx memory search "rule" --project threads`) for recent `state:` lines that cite a standing rule, and list them: they are decisions made without asking. Then present the queue: interrupts first (`blocking: yes`), then the batch, then one-line counts of quiet, conditional and answered asks. Each ask shows its `question:` and `context:`, its recommendation with `why:`, and a plain-words explanation of what each option does. A conditional ask (`after:` present) is never answerable.
2. On request ("why does it want that?"), pull more context: the asker's board entry, the `ref:` beads, RDRs and files named there, and `nx ask show ENTRY/ID`. If the answer is still missing, mail the asker a clarifying question as an ordinary peer request.
3. Take the answers as the user gives them: through AskUserQuestion (at most four asks per call, the ask's options as the choices, free text always open), or as one batched free-form reply. Where AskUserQuestion is not available, ask in plain text and wait. Resolve a batched reply ask by ask. "Yes to the reversible ones, hold the ART push, accept the gap on k9kr9" resolves to: the recommendation on each batch ask that has a `rec:` and is `reversible: yes` (and not flagged above); the ART ask's "Hold" option if it lists one, otherwise a note-only answer "hold"; and option 1 on the k9kr9 ask, which the user named. A reply that names no ask, or an option no ask has, is ambiguous: ask which one they mean.
4. Before sending anything, echo back one line per ask you will answer, with the option and the words you will record for that ask, plus one line for each ask you will leave alone and why. Then ask the user to confirm (AskUserQuestion: send / change, or plain text). A "change" reply, or silence, sends nothing: take the change, echo again.
5. On confirmation, run per ask:
   `nx ask answer ENTRY/ID N --expect VERSION --via mediator --sam-words "the user's words for this ask"`.
   `N` is the option number; for a note-only answer pass the text instead (the user's word, such as `hold`). When one reply covered several asks, add `--batch-words "the whole reply"` (at most 2000 characters) to each call. Each record gets only the words for its own ask (at most 500 characters); the whole reply goes to a batch record that only `nx ask digest` shows.
6. Report what was sent and any refusal or `failed` status, name the asks left alone, and stop.

## Refusals

`nx ask answer` refuses when `--expect` is not the current version (it prints the current text: show it to the user, echo back again, and send only with their confirmation), when the ask has `after:`, when two entries carry the asker's `session:` id, and when `--sam-words` is empty or over 500 characters. `--via cli` needs a TTY; you never use it. A `sent` answer already exists for that version: do not add `--replace` unless the user is changing it. Report every refusal as it is; do not retry with different words.

## Success Criteria

- [ ] The queue came from `nx ask list --json` and the digest, with interrupts first
- [ ] Nothing was sent before an echo-back line per ask and the user's confirmation
- [ ] A "change" reply, an ambiguous reply and no reply each sent nothing
- [ ] Every send carries `--expect`, `--via mediator` and a `--sam-words` that is a contiguous span of the user's text or the chosen option's text
- [ ] A reply covering several asks gave each record only its own words, with `--batch-words` carrying the whole reply
- [ ] Asks marked `reversible: no`, or `reversible: yes` with option text that reads as irreversible, were not covered by "the reversible ones", and the report says so
- [ ] "Hold" on an ask with no Hold option became a note-only answer
- [ ] In Claude Desktop chat or Cowork the refusal was given and nothing was sent
