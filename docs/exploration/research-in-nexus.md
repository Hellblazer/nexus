# Research in Nexus

Nexus borrows three instruments from experimental science, the laboratory notebook, pre-registration, and adversarial review, and applies them to research done with language-model agents over a curated store. The notebook is the catalog: every reading, mapping, proposal, verdict, and result becomes a document, linked to the paper it came from and to the step before it. Pre-registration is one paragraph written before an experiment spends anything, naming the arms, the single variable, the confounds, the statistic, and the result that would show the hypothesis wrong. Adversarial review is a second agent, briefed with the code and the design records, that reads a plan or a result and says which of its claims are false on inspection.

To be clear: this is a method for reading papers and deciding what to build from them, not a research automation system. We needed three things. A reading had to outlive the session that did it. A proposal had to carry the result that would kill it, so that the next experiment was obvious instead of one option among five. A claim about our own system, made by an agent that had read a paper and not the schema, had to be checked before anyone acted on it. The instruments of experimental science provided all three in a form that was old, well understood, and cheap. We could have kept doing what most people do with a model and a store: ask, read the answer, decide. We chose the method because the failures of the plain approach kept recurring in the same three shapes, and each shape had a known remedy.

This document explains the problem, how the suite applies the method, what we took from the sciences, what we left out, and what it changes for you. The worked case is one day of research on one paper, recorded in full in the store as a chain of linked documents, and the findings document at the end of that chain is where the method was written down. The public page, [Research with Nexus](https://hellblazer.github.io/nexus/research.html), shows what you say at each step and what you see.

## The problem: an agent reads fluently and wrongly

Claude with a curated store already does the plain thing well. It retrieves before it answers, it names the chunk each claim came from, and a question about a paper that is not in the store gets "not found" and not an invented summary. Memory carries decisions between sessions, and the catalog links a design record to the code that implements it. That is a good base, and it is where the edge is.

Three failures sit at that edge, and all three come from the same source: retrieval snippets feel like reading. First, a synthesis written from the chunks of a paper produces a plausible plan with false premises. On the worked case, an agent proposed reusing a telemetry table as a read-count signal, and the table had no document identity in any column. It proposed a design that a record of ours had already built, and that record had in fact rejected the design by name. It proposed a validation gate over a field, and the field held paraphrases, so there was nothing to validate. None of that is in any paper, and none of it is reachable by semantic search over prose. Second, an experiment produces a correct number with a wrong explanation. The first write-up of the worked case said the treated arm mostly answered wrongly. The raw log showed that forty-five of its fifty-six failures were no answer at all, which is a different finding, and nobody had read the answers. Third, the source is read through its derivatives. The paper's aspects, its chunks, and an extracted protocol served as the reading, and the fact that it was a six-page position paper with its protocol in an unpublished companion surfaced only in the second round of critique.

Each of these failures has a remedy the sciences worked out long ago. A plan is reviewed by someone with access to the apparatus. A result is recomputed from the data. A source is read in full. The method is those remedies, made mechanical enough that an agent does them by default.

## How the suite leverages it

The worked case ran on 2026-09-07 against a paper on ingest-time compilation for retrieval systems, and it is recorded as six catalog documents linked to the paper and to each other, plus memory entries at every step. Seven gates ran, in this order.

1. **Index with every enrichment on, and read the log while it runs.** Three and a half minutes. Reading the log line by line found three lifecycle defects in the indexing pipeline that the summary line did not show.
2. **A first-pass mapping** of the paper onto the system, claim by claim, written to memory and to the store with catalog links to the design records it touched.
3. **A research synthesis** by an agent briefed with the mapping, the linked records, the code paths, and a web check, and required to attach a falsifier and a cost to every proposal and to include a do-nothing option. Five proposals came back.
4. **An adversarial critique** of the proposals by a second agent with access to the code, the schema changelogs, and the design records. It killed three of the five as factually wrong about the system, not as premature, and found one thing the synthesis had missed.
5. **A verdict document** reducing the synthesis and the critique to what survived, with the corrections to the mapping stated out loud. It supersedes both in the catalog.
6. **One experiment**, the one every surviving proposal was gated on, designed and run the same day: aspects against chunks as the payload a reader receives, forty questions, paired counts.
7. **A methodology critique** of the experiment by an agent given the raw trial data and told to recompute rather than read the summary. It found four defects in the write-up and one in the harness.

Gates four and seven carried the value. Gate three alone would have produced a plan with two false premises. Gate six alone would have produced a correct number with a wrong explanation attached. The same shape ran again the next day on a second paper, with a pre-registration paragraph written first, and the gate-seven critic found the decisive defect all three times it ran that week: a headline forced by the construction of the questions, a leg that passed without testing the signal it claimed to test, and half the validity windows wrong when the ground truth was in the file being parsed.

The critic agent is the standing one the suite runs on plans, designs, and code. What changed is the brief: it is given paths to open and told which claims to verify against which artifact. It answers in its own fixed format, the issues it found with the artifact that decides each, and an outcome of justified, partial, or not justified. The verdict per proposal, survives, survives with amendment, or dies, is written by Claude afterwards, reducing the critique and the synthesis to one document. The research agent is likewise the standing one. What changed is the requirement that every proposal carry its falsifier and its cost.

## What we borrowed

### The notebook

A laboratory notebook is written as the work happens, in ink, and never rewritten. The catalog gives us that. Every document a step produces is registered with a permanent address, linked to the paper by a `cites` or `comments` link and to the step before it by `relates`, and a verdict that reduces two earlier documents `supersedes` them rather than replacing them. A reader who opens the paper a year later finds the current state first and the drafts behind it. The methodology critic in the worked case reconstructed the whole chain from the links without being told it existed. This is the same append-only discipline the [catalog](xanadu-in-nexus.md) applies to everything else, and it is why the record survives the session that made it.

### Pre-registration

Clinical trials and, since the 2010s, much of psychology register the design before the data exist: the hypothesis, the arms, the primary metric, the analysis, and what counts as a null result. The reason is that a design chosen after seeing the data can be made to pass. We ask for one paragraph before any spend, and every later check runs against it. The worked case did not have one, and the critic later showed that one arm had changed two variables at once, which the design could not then separate. The second experiment had one, and the critic's most useful finding was that the pre-registration had disclosed a confound and got the fact wrong, which is exactly the kind of error a written design makes visible and an unwritten one hides.

### The falsifier

Popper's criterion, that a claim is scientific only if some observation could refute it, becomes a requirement on the research agent's output: every proposal names the result that would show it is wrong, and a proposal that cannot be wrong is recorded as an opinion. This is what made the experiment in the worked case the obvious next step rather than one option among five. It was the falsifier of the most expensive proposal, and the paper's own stated limitation named it.

### Adversarial review with the apparatus

Peer review as journals practise it reads the manuscript. The review that catches the failures above reads the apparatus: the schema, the changelog, the alternatives section of the design record that rejected the idea. The critic is briefed with paths, not with the paper, because the paper-side claims were all correct in the worked case and the system-side claims were the ones that were false. The critic's outcome field is fixed to three values so that a downstream reader, human or agent, can count what failed without reading the argument, and the verdict document carries the per-proposal reduction in the same spirit.

### Recompute from the data

Reproducibility, in the narrow sense of recomputing a reported number from the recorded data, is the check that found the largest defects. It requires that the harness keep what a recompute needs: the exact text each arm received, the reader's structured flags, the cost of every model call, the seed, and the model identifiers. The first harness kept a summary table and document ids, and every later question about why a trial failed had to be answered by re-reading a live store that had moved. The requirement is now standing: a harness that produces only a summary table cannot be audited, so it is not finished.

## What we left out

**No automatic ingestion.** The store is curated. A paper is chosen, described, indexed, and linked by a person or on a person's instruction, never collected by a crawler or a feed. "Not in the store" is a result the method relies on, and it is only a result when the store's contents were chosen.

**No agent chooses the direction.** The research agent reads and collects, the critic checks, and the person decides what to look at next and when to stop. Every gate returns to the person before the next one runs. The method automates the checks, not the judgment.

**No claim-level store.** The paper in the worked case argues for atomic claims with quote-exact provenance as the unit of retrieval. Our aspects are per document and paraphrased. The experiment we ran showed that per-document paraphrase aspects are not a retrieval payload at all, by a margin no harness defect could reverse, and it did not test claims. The claims layer is a recorded open question with its compile cost now measured, not a plan.

**No automated grader validation.** The method requires that a generated grader be checked against a human-labelled sample and that generated data be checked against its own generation contract. Both are cheap and both are done by hand. A harness library that does them by default is designed and not built.

**No skill yet.** The seven gates, the two agent briefs, and the harness requirements are written down as a design of record in the findings document and in memory. The `research-experiment` skill that would carry them, and the harness library under `scripts/experiments/lib/` that the worked case's script would be refactored onto, are proposed and waiting on a decision. Today the method is followed from the record, by hand, and the public page shows the prompts that do it.

**No renormalisation of the literature.** The outward reading in the second gate checks the authors, the venue, and independent evaluations, and marks what it could not verify. It does not attempt to weigh a source by citation count or venue rank. A paper in the store can be wrong, and grounding says where a claim came from so that a person can judge the source.

## What it changes for you

Most of the time you say what you want and Claude does the step: index this, read this two ways, map it, propose, have the critic read it with the code, pre-register, run, recompute, render. What changes is what you can rely on afterwards. A reading is a document you can open, with its claims marked as sourced or unverified. A proposal carries the result that would kill it, so the next experiment is a decision and not a search. A claim about your own system has been checked against the schema before you act on it. A number in a report reproduces from data the harness kept. A reader who was not present can follow the links from the paper through everything that was done with it, and a session next year starts from that chain instead of from the paper.

## Further reading

- [Research with Nexus](https://hellblazer.github.io/nexus/research.html), the public page: what you say at each step and what you see
- [Xanadu in Nexus](xanadu-in-nexus.md), the catalog's linking substrate that the notebook is built on
- [Querying guide](../querying-guide.md) and [plan-centric retrieval](../plan-centric-retrieval.md), how a question across papers is planned and answered
- [Working with RDRs](https://hellblazer.github.io/nexus/rdr.html), the design record that a decision to build ends in
- Nosek, B. A., Ebersole, C. R., DeHaven, A. C., Mellor, D. T. "The preregistration revolution." Proceedings of the National Academy of Sciences 115(11), 2018.
- Popper, K. *The Logic of Scientific Discovery.* 1959.
