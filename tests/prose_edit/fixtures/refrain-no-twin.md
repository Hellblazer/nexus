# The ledger in Nexus

Nexus keeps a ledger of every background agent it starts. We wanted one place that answers a plain question: which agents owe a report.

The ledger records a row when a dispatch begins and a second row when the agent reports. A row that has no partner after the agent stops is the answer to the question.

That is what the ledger is for. Not a log, but a promise.

## What we left out

The ledger does not retry a dispatch. A retry would hide the missing report it exists to show.
