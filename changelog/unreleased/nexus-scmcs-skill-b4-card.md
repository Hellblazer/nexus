### Fixed

- **prose-edit no longer stops on a bare "Yes, apply." after a held query.** When a path run asked the query question and the voice-card question together and the author replied only "Yes, apply.", the skill asked which question that answered and applied nothing. It now reads the reply as No on the query, leaves the voice-card question open, runs the dry run and applies in the same turn (it still asks `Apply` when the dry run leaves an edit out), and asks the voice-card question again after the apply with the card text in that reply (nexus-scmcs).
