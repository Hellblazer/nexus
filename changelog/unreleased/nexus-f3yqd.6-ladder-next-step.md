### Fixed

- **An upgrade-ladder failure or deferral says what happens next** (nexus-f3yqd.6, F17). `upgrade ladder did not converge` now ends by telling you to fix what each step above reports and run `nx upgrade` again, and that `nx upgrade --dry-run` lists the steps still pending. A deferred step now says it runs again on the next `nx upgrade`, and that `nx doctor` names a step that has deferred on three runs over a day or more.
