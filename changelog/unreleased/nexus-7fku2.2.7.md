### Changed

- **`search.topic_route_nx_answer` stays off; turning it on is not recommended.** RDR-134's measurements, after 8.5.1 shipped the setting, found topic routing did not help: in `nx_answer` the `top1` setting lowered NDCG@3 on questions whose answer is a generic document (a changelog, a README) from 0.654 to 0.496, and in plain search the engine route reached 0.385 against a 0.44 bar. RDR-134 is closed as abandoned. The setting and the engine route remain for experiments; leave the setting at `off`.
