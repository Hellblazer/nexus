# temporal validity A/B v2 -- 66 queries, 0 excluded for fewer than 5 documents
document depth per query: median 19, min 6, max 34
unclassified candidate slots: 176 of 1299

## as-of leg (primary) (n=24)
arm | gold top-1 | MRR | stale top-1
A | 14/24 = 0.583 [0.388, 0.755] | 0.690 | 5/24
H | 17/24 = 0.708 [0.508, 0.851] | 0.758 | 0/24
S | 16/24 = 0.667 [0.467, 0.820] | 0.734 | 1/24
random-valid replacement baseline (expected gold top-1): 2.85/24
A vs S: A right only 0, S right only 2, exact McNemar p = 5.00e-01
A vs H: A right only 0, H right only 3, exact McNemar p = 2.50e-01
H vs S: H right only 1, S right only 0, exact McNemar p = 1.00e+00

## as-of leg, provenance S (n=12)
arm | gold top-1 | MRR | stale top-1
A | 9/12 = 0.750 [0.468, 0.911] | 0.839 | 2/12
H | 10/12 = 0.833 [0.552, 0.953] | 0.889 | 0/12
S | 10/12 = 0.833 [0.552, 0.953] | 0.883 | 0/12
E | 0/12 = 0.000 [0.000, 0.243] | 0.000 | 4/12
random-valid replacement baseline (expected gold top-1): 1.26/12
A vs S: A right only 0, S right only 1, exact McNemar p = 1.00e+00
A vs H: A right only 0, H right only 1, exact McNemar p = 1.00e+00
E vs S: E right only 0, S right only 10, exact McNemar p = 1.95e-03
E vs H: E right only 0, H right only 10, exact McNemar p = 1.95e-03
H vs S: H right only 0, S right only 0, exact McNemar p = 1.00e+00

## as-of leg, provenance N (n=12)
arm | gold top-1 | MRR | stale top-1
A | 5/12 = 0.417 [0.193, 0.680] | 0.542 | 3/12
H | 7/12 = 0.583 [0.320, 0.807] | 0.627 | 0/12
S | 6/12 = 0.500 [0.254, 0.746] | 0.586 | 1/12
random-valid replacement baseline (expected gold top-1): 1.59/12
A vs S: A right only 0, S right only 1, exact McNemar p = 1.00e+00
A vs H: A right only 0, H right only 2, exact McNemar p = 5.00e-01
H vs S: H right only 1, S right only 0, exact McNemar p = 1.00e+00

## current leg (n=42)
arm | gold top-1 | MRR | stale top-1
A | 16/42 = 0.381 [0.250, 0.532] | 0.554 | 19/42
H | 26/42 = 0.619 [0.468, 0.750] | 0.705 | 0/42
S | 21/42 = 0.500 [0.355, 0.645] | 0.636 | 11/42
random-valid replacement baseline (expected gold top-1): 4.68/42
A vs S: A right only 0, S right only 5, exact McNemar p = 6.25e-02
A vs H: A right only 0, H right only 10, exact McNemar p = 1.95e-03
H vs S: H right only 5, S right only 0, exact McNemar p = 6.25e-02

## current leg, provenance S (n=14)
arm | gold top-1 | MRR | stale top-1
A | 2/14 = 0.143 [0.040, 0.399] | 0.423 | 12/14
H | 9/14 = 0.643 [0.388, 0.837] | 0.717 | 0/14
S | 5/14 = 0.357 [0.163, 0.612] | 0.554 | 9/14
E | 8/14 = 0.571 [0.326, 0.786] | 0.660 | 2/14
random-valid replacement baseline (expected gold top-1): 0.91/14
A vs S: A right only 0, S right only 3, exact McNemar p = 2.50e-01
A vs H: A right only 0, H right only 7, exact McNemar p = 1.56e-02
E vs S: E right only 3, S right only 0, exact McNemar p = 2.50e-01
E vs H: E right only 0, H right only 1, exact McNemar p = 1.00e+00
H vs S: H right only 4, S right only 0, exact McNemar p = 1.25e-01

## current leg, provenance T (n=14)
arm | gold top-1 | MRR | stale top-1
A | 11/14 = 0.786 [0.524, 0.924] | 0.845 | 1/14
H | 11/14 = 0.786 [0.524, 0.924] | 0.854 | 0/14
S | 11/14 = 0.786 [0.524, 0.924] | 0.851 | 0/14
random-valid replacement baseline (expected gold top-1): 2.78/14
A vs S: A right only 0, S right only 0, exact McNemar p = 1.00e+00
A vs H: A right only 0, H right only 0, exact McNemar p = 1.00e+00
H vs S: H right only 0, S right only 0, exact McNemar p = 1.00e+00

## current leg, provenance N (n=14)
arm | gold top-1 | MRR | stale top-1
A | 3/14 = 0.214 [0.076, 0.476] | 0.395 | 6/14
H | 6/14 = 0.429 [0.214, 0.674] | 0.544 | 0/14
S | 5/14 = 0.357 [0.163, 0.612] | 0.503 | 2/14
random-valid replacement baseline (expected gold top-1): 0.98/14
A vs S: A right only 0, S right only 2, exact McNemar p = 5.00e-01
A vs H: A right only 0, H right only 3, exact McNemar p = 2.50e-01
H vs S: H right only 1, S right only 0, exact McNemar p = 1.00e+00

## falsifier v2
(1) as-of non-inferiority (S, H lose at most one gold top-1 to A): PASS (A 14, H 17, S 16 of 24)
(2) current leg: S > E on provenance S (5 vs 8 of 14), S > A on T (11 vs 11 of 14), S > A on N (5 vs 3 of 14), pooled S vs A McNemar p = 6.25e-02: FAIL
(3) S beats the random-valid replacement baseline on gold top-1: 37 vs expected 7.53 of 66: PASS
overall: NOT PASSED

## leave-one-pair-out: S minus A gold top-1 accuracy, all queries
RDR-015<RDR-014: +0.125, RDR-108<RDR-107: +0.117, RDR-120<RDR-112: +0.089, RDR-127<RDR-123: +0.089, RDR-127<RDR-124: +0.107, RDR-156<RDR-106: +0.107, RDR-185<RDR-159: +0.107

## per-query top-1 by arm (* = invalid at t) and gold ranks
RDR-014-current-S1     current S t=2026-09-08 docs=31 A=RDR-014* H=POST-MORTEM:016-ast-chunk-line-range-bug S=RDR-014* E=POST-MORTEM:016-ast-chunk-line-range-bug gold=RDR-015 ranks A:None H:None S:None E:None
RDR-014-current-S2     current S t=2026-09-08 docs=15 A=RDR-014* H=RDR-026 S=RDR-014* E=RDR-026 gold=RDR-015 ranks A:6 H:5 S:6 E:5
RDR-014-current-T1     current T t=2026-09-08 docs=19 A=RDR-015 H=RDR-015 S=RDR-015 gold=RDR-015 ranks A:1 H:1 S:1
RDR-014-current-T2     current T t=2026-09-08 docs=18 A=RDR-015 H=RDR-015 S=RDR-015 gold=RDR-015 ranks A:1 H:1 S:1
RDR-014-current-N1     current N t=2026-09-08 docs=25 A=RDR-102 H=RDR-102 S=RDR-102 gold=RDR-015 ranks A:15 H:14 S:14
RDR-014-current-N2     current N t=2026-09-08 docs=30 A=RDR-089 H=RDR-089 S=RDR-089 gold=RDR-015 ranks A:5 H:5 S:5
RDR-014-asof-S1        asof    S t=2026-06-02 docs=32 A=RDR-014 H=RDR-014 S=RDR-014 E=RDR-007 gold=RDR-014 ranks A:1 H:1 S:1 E:None
RDR-014-asof-S2        asof    S t=2026-06-02 docs=23 A=RDR-014 H=RDR-014 S=RDR-014 E=RDR-006 gold=RDR-014 ranks A:1 H:1 S:1 E:None
RDR-014-asof-N1        asof    N t=2026-06-02 docs= 9 A=RDR-006 H=RDR-006 S=RDR-006 gold=RDR-014 ranks A:7 H:6 S:6
RDR-014-asof-N2        asof    N t=2026-06-02 docs=34 A=RDR-047 H=RDR-047 S=RDR-047 gold=RDR-014 ranks A:None H:None S:None
RDR-106-current-S1     current S t=2026-09-08 docs=18 A=RDR-156 H=RDR-156 S=RDR-156 E=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1 E:1
RDR-106-current-S2     current S t=2026-09-08 docs=24 A=RDR-156 H=RDR-156 S=RDR-156 E=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1 E:1
RDR-106-current-T1     current T t=2026-09-08 docs=15 A=RDR-156 H=RDR-156 S=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1
RDR-106-current-T2     current T t=2026-09-08 docs=19 A=RDR-156 H=RDR-156 S=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1
RDR-106-current-N1     current N t=2026-09-08 docs=20 A=RDR-156 H=RDR-156 S=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1
RDR-106-current-N2     current N t=2026-09-08 docs=33 A=RDR-156 H=RDR-156 S=RDR-156 gold=RDR-156 ranks A:1 H:1 S:1
RDR-106-asof-S1        asof    S t=2026-05-29 docs=19 A=RDR-156* H=RDR-106 S=RDR-106 E=RDR-156* gold=RDR-106 ranks A:2 H:1 S:1 E:None
RDR-106-asof-S2        asof    S t=2026-05-29 docs=33 A=RDR-181* H=POST-MORTEM:138-rename-cascade-aspect-worker-coordination S=POST-MORTEM:138-rename-cascade-aspect-worker-coordination E=RDR-181* gold=RDR-106 ranks A:16 H:6 S:10 E:None
RDR-106-asof-N1        asof    N t=2026-05-29 docs=18 A=RDR-106 H=RDR-106 S=RDR-106 gold=RDR-106 ranks A:1 H:1 S:1
RDR-106-asof-N2        asof    N t=2026-05-29 docs=30 A=RDR-156* H=RDR-051 S=RDR-051 gold=RDR-106 ranks A:None H:None S:None
RDR-107-current-S1     current S t=2026-09-08 docs=13 A=RDR-107* H=RDR-108 S=RDR-107* E=RDR-108 gold=RDR-108 ranks A:2 H:1 S:2 E:1
RDR-107-current-S2     current S t=2026-09-08 docs=25 A=RDR-107* H=POST-MORTEM:015-indexing-pipeline-rethink S=RDR-107* E=POST-MORTEM:015-indexing-pipeline-rethink gold=RDR-108 ranks A:4 H:3 S:4 E:3
RDR-107-current-T1     current T t=2026-09-08 docs=16 A=RDR-049* H=RDR-103 S=RDR-103 gold=RDR-108 ranks A:6 H:5 S:6
RDR-107-current-T2     current T t=2026-09-08 docs=21 A=RDR-108 H=RDR-108 S=RDR-108 gold=RDR-108 ranks A:1 H:1 S:1
RDR-107-current-N1     current N t=2026-09-08 docs=16 A=RDR-107* H=RDR-192 S=RDR-192 gold=RDR-108 ranks A:3 H:2 S:2
RDR-107-current-N2     current N t=2026-09-08 docs=26 A=RDR-049a H=RDR-049a S=RDR-049a gold=RDR-108 ranks A:5 H:5 S:5
RDR-112-current-S1     current S t=2026-09-08 docs=13 A=RDR-112* H=RDR-120 S=RDR-120 E=RDR-120 gold=RDR-120 ranks A:2 H:1 S:1 E:1
RDR-112-current-S2     current S t=2026-09-08 docs=16 A=RDR-112* H=RDR-120 S=RDR-112* E=RDR-110* gold=RDR-120 ranks A:3 H:1 S:2 E:2
RDR-112-current-T1     current T t=2026-09-08 docs=16 A=RDR-120 H=RDR-120 S=RDR-120 gold=RDR-120 ranks A:1 H:1 S:1
RDR-112-current-T2     current T t=2026-09-08 docs=31 A=RDR-022 H=RDR-022 S=RDR-022 gold=RDR-120 ranks A:6 H:4 S:4
RDR-112-current-N1     current N t=2026-09-08 docs=17 A=RDR-152b H=RDR-152b S=RDR-152b gold=RDR-120 ranks A:7 H:5 S:6
RDR-112-current-N2     current N t=2026-09-08 docs=28 A=RDR-152b H=RDR-152b S=RDR-152b gold=RDR-120 ranks A:7 H:5 S:5
RDR-112-asof-S1        asof    S t=2026-05-15 docs=19 A=RDR-112 H=RDR-112 S=RDR-112 E=RDR-120* gold=RDR-112 ranks A:1 H:1 S:1 E:None
RDR-112-asof-S2        asof    S t=2026-05-15 docs=14 A=RDR-110 H=RDR-110 S=RDR-110 E=RDR-110 gold=RDR-112 ranks A:2 H:2 S:2 E:None
RDR-112-asof-N1        asof    N t=2026-05-15 docs=15 A=RDR-129* H=RDR-112 S=RDR-112 gold=RDR-112 ranks A:2 H:1 S:1
RDR-112-asof-N2        asof    N t=2026-05-15 docs=27 A=RDR-112 H=RDR-112 S=RDR-112 gold=RDR-112 ranks A:1 H:1 S:1
RDR-123-current-S1     current S t=2026-09-08 docs=16 A=RDR-123* H=RDR-127 S=RDR-123* E=RDR-127 gold=RDR-127 ranks A:2 H:1 S:2 E:1
RDR-123-current-S2     current S t=2026-09-08 docs=10 A=RDR-123* H=RDR-127 S=RDR-127 E=RDR-127 gold=RDR-127 ranks A:2 H:1 S:1 E:1
RDR-123-current-T1     current T t=2026-09-08 docs= 6 A=RDR-127 H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:1 H:1 S:1
RDR-123-current-T2     current T t=2026-09-08 docs= 6 A=RDR-127 H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:1 H:1 S:1
RDR-123-current-N1     current N t=2026-09-08 docs= 8 A=RDR-123* H=RDR-127 S=RDR-123* gold=RDR-127 ranks A:4 H:1 S:2
RDR-123-current-N2     current N t=2026-09-08 docs=19 A=RDR-119* H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:2 H:1 S:1
RDR-123-asof-S1        asof    S t=2026-05-20 docs=12 A=RDR-123 H=RDR-123 S=RDR-123 E=RDR-083 gold=RDR-123 ranks A:1 H:1 S:1 E:None
RDR-123-asof-S2        asof    S t=2026-05-20 docs=12 A=RDR-123 H=RDR-123 S=RDR-123 E=RDR-127* gold=RDR-123 ranks A:1 H:1 S:1 E:None
RDR-123-asof-N1        asof    N t=2026-05-20 docs= 8 A=RDR-123 H=RDR-123 S=RDR-123 gold=RDR-123 ranks A:1 H:1 S:1
RDR-123-asof-N2        asof    N t=2026-05-20 docs=12 A=RDR-127* H=RDR-123 S=RDR-127* gold=RDR-123 ranks A:2 H:1 S:2
RDR-124-current-S1     current S t=2026-09-08 docs=15 A=RDR-124* H=RDR-184 S=RDR-124* E=RDR-123* gold=RDR-127 ranks A:6 H:2 S:3 E:5
RDR-124-current-S2     current S t=2026-09-08 docs=17 A=RDR-124* H=RDR-069 S=RDR-124* E=RDR-069 gold=RDR-127 ranks A:None H:None S:None E:None
RDR-124-current-T1     current T t=2026-09-08 docs=10 A=RDR-127 H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:1 H:1 S:1
RDR-124-current-T2     current T t=2026-09-08 docs= 8 A=RDR-127 H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:1 H:1 S:1
RDR-124-current-N1     current N t=2026-09-08 docs=24 A=RDR-124* H=RDR-036 S=RDR-124* gold=RDR-127 ranks A:5 H:4 S:5
RDR-124-current-N2     current N t=2026-09-08 docs=15 A=RDR-124* H=RDR-127 S=RDR-127 gold=RDR-127 ranks A:2 H:1 S:1
RDR-124-asof-S1        asof    S t=2026-05-20 docs=24 A=RDR-124 H=RDR-124 S=RDR-124 E=RDR-123 gold=RDR-124 ranks A:1 H:1 S:1 E:None
RDR-124-asof-S2        asof    S t=2026-05-20 docs=21 A=RDR-124 H=RDR-124 S=RDR-124 E=RDR-036 gold=RDR-124 ranks A:1 H:1 S:1 E:None
RDR-124-asof-N1        asof    N t=2026-05-20 docs= 6 A=RDR-124 H=RDR-124 S=RDR-124 gold=RDR-124 ranks A:1 H:1 S:1
RDR-124-asof-N2        asof    N t=2026-05-20 docs=13 A=RDR-124 H=RDR-124 S=RDR-124 gold=RDR-124 ranks A:1 H:1 S:1
RDR-159-current-S1     current S t=2026-09-08 docs=30 A=RDR-159* H=RDR-185 S=RDR-185 E=RDR-185 gold=RDR-185 ranks A:2 H:1 S:1 E:1
RDR-159-current-S2     current S t=2026-09-08 docs=27 A=RDR-159* H=RDR-185 S=RDR-159* E=RDR-185 gold=RDR-185 ranks A:2 H:1 S:2 E:1
RDR-159-current-T1     current T t=2026-09-08 docs=26 A=RDR-185 H=RDR-185 S=RDR-185 gold=RDR-185 ranks A:1 H:1 S:1
RDR-159-current-T2     current T t=2026-09-08 docs=27 A=POST-MORTEM:rdr-101-bib-disposition H=POST-MORTEM:rdr-101-bib-disposition S=POST-MORTEM:rdr-101-bib-disposition gold=RDR-185 ranks A:2 H:2 S:2
RDR-159-current-N1     current N t=2026-09-08 docs=24 A=RDR-159* H=RDR-157a S=RDR-157a gold=RDR-185 ranks A:None H:None S:None
RDR-159-current-N2     current N t=2026-09-08 docs=23 A=RDR-185 H=RDR-185 S=RDR-185 gold=RDR-185 ranks A:1 H:1 S:1
RDR-159-asof-S1        asof    S t=2026-07-24 docs=25 A=RDR-159 H=RDR-159 S=RDR-159 E=RDR-155 gold=RDR-159 ranks A:1 H:1 S:1 E:None
RDR-159-asof-S2        asof    S t=2026-07-24 docs=26 A=RDR-159 H=RDR-159 S=RDR-159 E=RDR-185 gold=RDR-159 ranks A:1 H:1 S:1 E:None
RDR-159-asof-N1        asof    N t=2026-07-24 docs=23 A=RDR-038 H=RDR-038 S=RDR-038 gold=RDR-159 ranks A:4 H:4 S:4
RDR-159-asof-N2        asof    N t=2026-07-24 docs=28 A=RDR-185 H=RDR-185 S=RDR-185 gold=RDR-159 ranks A:9 H:9 S:9