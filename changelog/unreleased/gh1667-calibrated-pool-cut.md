### Fixed

- A search over code and prose collections at a loose distance threshold no longer drops the prose rows before ranking. The cut that bounds the pool (8,773 rows down to 480 for `hybrid-factual-lookup`'s threshold 2.0) now ranks unscored rows by calibrated distance, as the ranking after it does, not by raw distance, which voyage-code-3's tighter scale always won. "What changed in conexus 8.3.0?" over `corpus=all` without the rerank had no CHANGELOG row in its 40 results, and now ranks the CHANGELOG [8.3.0] section first (GH #1667).
