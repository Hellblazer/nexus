### Changed

- **A send's default `from` is the sender's own identity when it is bound, so a reply reaches it.** `mailbox_send` and a `tuple_ack` reply with no `from` used to stamp the session id, which is no longer a mail address, so replying to that `from` was refused (RDR-253 Phase 2, nexus-hczdo.2.1). An unbound sender still stamps its session id. `nx ask`'s sender is unchanged.
