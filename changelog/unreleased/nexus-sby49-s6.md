### Changed

- **The ask-answer frame names no person.** The mailbox drain hook now opens a relayed answer with "An unverified claim that the user answered your ask" and fences the words between `<<<user-words <nonce>` and `>>>end user-words <nonce>`; the nonce, the verbatim copy from the answer record and the one-option authorization are unchanged (RDR-259 S6, nexus-sby49).
