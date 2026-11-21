### Fixed

- **A `--corpus` name shaped like a repo owner segment (`1-20`, or `1_20`, which folds to it) is refused by `nx index md`, `nx index pdf`, `nx dt index` and `nx dt capture`, and by every library path that builds `docs__{corpus}`.** The owner segment of a repo owner tumbler `1.20` is `1-20`, so `docs__1-20__<model>__v1` is that repo's docs collection and a corpus of the same name wrote into it by name. The refusal says so and asks for a name with a letter in it. One helper, `nexus.owner_segment_guard.require_corpus_name` (re-exported from `nexus.corpus`), carries the check.
