### Added

- **Threads board entries, mail and names follow the session identity (RDR-253 Step 5).**
  - A threads entry can carry `identity: i<n>` beside its `session:` line. Five readers now find the entry by that line first, because it survives `/clear` and restart: the compaction checkpoint, `nx ask` answer routing, the console, the due-date reminder and the aura threads block. They fall back to `session:` for entries that do not have it.
  - Answers to an ask in such an entry go to `mailbox/<identity>`.
  - `mailbox_send(to=<name>)` resolves the name through its identity alias first, then `directory/<name>`: one live holder gets the mail in its session mailbox (a current client drains that too), and a seat nobody holds gets it in `mailbox/<identity>` for when it binds. A name held by two sessions is still refused.
  - An ephemeral identity is reached by its id only: a send to its display name is refused, naming the id. Its board entry is titled `<display name> (i<n>)`, or `ephemeral (i<n>)` with no name, and `nx identity promote` moves the entry to the new alias when it runs inside the owning session.
  - A renamed seat's old name still delivers for 7 days with a `notice` naming the new one, then is refused with "renamed to <new> on <date>".
  - Sessions bound to an identity receive and drain both `mailbox/<identity>` and their session mailbox.
  - A `from_address` shaped like an identity must be the sender's own identity.
  - A message sent with the sender's own identity as `from` is no longer labelled unverified when the receiver's host can see the sending session's token file.
