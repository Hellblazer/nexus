### Added

- **Threads board entries, mail and names follow the session identity (RDR-253 Step 5).**
  - A threads entry can carry `identity: i<n>` beside its `session:` line. Five readers now find the entry by that line first, because it survives `/clear` and restart: the compaction checkpoint, `nx ask` answer routing, the console, the due-date reminder and the aura threads block. They fall back to `session:` for entries that do not have it.
  - Answers to an ask in such an entry go to `mailbox/<identity>`.
  - `mailbox_send(to=<name>)` resolves the name through its identity alias first. It sends to `mailbox/<identity>` when a current client holds the seat, and otherwise to the old directory holder.
  - A renamed seat's old name still delivers for 7 days with a `notice` naming the new one, then is refused with "renamed to <new> on <date>".
  - Sessions bound to an identity receive and drain both `mailbox/<identity>` and their session mailbox.
  - A `from_address` shaped like an identity must be the sender's own identity.
