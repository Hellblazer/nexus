# How to write an RDR

An RDR is read by people. Some will be experts who do not know this project's
jargon. Some will read it in their second language. All of them are smart.
Write so they can follow you.

Machines read RDRs too (the gate, the preambles, future agents). Do not write
for them. Text a smart newcomer can follow is also the best context a machine
can get; text written machine-to-machine fails both readers.

## The rule

Simplified, never simplistic. A simplified sentence removes barriers and keeps
the intent. A simplistic sentence is easy to read and says less than you meant.
After any rewrite, ask: did the intent arrive? If not, the rewrite failed, no
matter how clean it reads.

Jargon: define each term the first time it appears, then use it freely.
Precision is for the expert; the definition is for the newcomer. "The chash
(the SHA-256 of the chunk's text, which serves as its identity) collides only
when the text is identical" costs one parenthetical and admits every reader.

Say what is known and what is not. The Verified / Documented / Assumed labels
in the template are the skeleton; hold the prose to the same standard. "We
measured X" and "we believe X" are different sentences. Never present a
belief as a measurement.

## Who reads each stage

- ***Create*** is read by a future engineer asking "why is it built this way?"
  They were not in the room. Give them the problem before the solution.
- ***Research*** is read by an expert checking your evidence. Show what you
  found, how you found it, and what you did not find. A dead end recorded
  plainly is a finding.
- ***Gate critiques*** are read by the author being told what is wrong. Name
  the defect, where it is, and what better looks like. A critique the author
  has to decode is a defect in the critique.
- ***Acceptance*** is read by the people who will execute the plan. Plans,
  logistics, and imperatives only work when each step says one thing. If a
  step needs interpreting, it will be interpreted differently by each reader.
- ***Post-mortems*** are read by the next person about to make the same
  mistake. Write for the moment they are in: what we expected, what happened,
  what we would check first next time.

## Two versions of the same finding

Good:

> Indexing failed on large repositories because the engine (the Java service
> that computes and stores embeddings) sent every page of text chunks to
> Voyage (the embedding provider) as one request. A page over Voyage's
> documented 120,000-token request ceiling came back as an error with no
> detail, and no retry could fix it, because the retry sent the same
> oversized page. The fix splits pages by token count before sending.

Simplistic (do not do this):

> Large uploads had issues due to size limits. The fix makes uploads more
> reliable.

The first names the actor, the cause, the ceiling, and why retries could not
help. The second is easier to read and answers nothing. Both are "simple."
Only one preserves intent.

## Before you finish

List every term a newcomer meets undefined in what you wrote. Define each on
first use, or cut it. This is the check the gate will run against you.
