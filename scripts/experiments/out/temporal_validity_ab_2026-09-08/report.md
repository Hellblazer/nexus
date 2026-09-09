## all queries (n=56)
stale rate A: 38/56 = 0.679 [0.548, 0.786]
stale rate B: 2/56 = 0.036 [0.010, 0.121]
discordant: A stale and B valid = 36; A valid and B stale = 0; exact McNemar p = 0.0000
gold present in top-30: 32/35; top-1 accuracy A 14/35, B 21/35; MRR A 0.571, B 0.682
retrieval latency ms: median 3314, p90 4179 (shared by both arms)
falsifier (B stale <= half of A, p < 0.05): PASSED

## current queries (n=42)
stale rate A: 35/42 = 0.833 [0.694, 0.917]
stale rate B: 2/42 = 0.048 [0.013, 0.158]
discordant: A stale and B valid = 33; A valid and B stale = 0; exact McNemar p = 0.0000
gold present in top-30: 18/21; top-1 accuracy A 2/21, B 10/21; MRR A 0.341, B 0.585
retrieval latency ms: median 3503, p90 4300 (shared by both arms)
falsifier (B stale <= half of A, p < 0.05): PASSED

## as-of queries (n=14)
stale rate A: 3/14 = 0.214 [0.076, 0.476]
stale rate B: 0/14 = 0.000 [0.000, 0.215]
discordant: A stale and B valid = 3; A valid and B stale = 0; exact McNemar p = 0.2500
gold present in top-30: 14/14; top-1 accuracy A 12/14, B 11/14; MRR A 0.917, B 0.829
retrieval latency ms: median 3138, p90 3683 (shared by both arms)
falsifier (B stale <= half of A, p < 0.05): NOT PASSED

## sensitivity cuts: no valid candidate in top-30 = ['RDR-119-cur-2', 'RDR-119-cur-3']; empty-window as-of = ['RDR-107-asof-1', 'RDR-107-asof-2']; four-word-run leakage = ['RDR-119-cur-1', 'RDR-119-cur-2', 'RDR-123-asof-1', 'RDR-159-asof-2']

## all queries minus the three cuts (n=49)
stale rate A: 33/49 = 0.673 [0.534, 0.788]
stale rate B: 0/49 = 0.000 [0.000, 0.073]
discordant: A stale and B valid = 33; A valid and B stale = 0; exact McNemar p = 0.0000
gold present in top-30: 28/31; top-1 accuracy A 11/31, B 20/31; MRR A 0.537, B 0.719
retrieval latency ms: median 3367, p90 4300 (shared by both arms)
falsifier (B stale <= half of A, p < 0.05): PASSED

## as-of queries minus the empty-window pair (n=12)
stale rate A: 1/12 = 0.083 [0.015, 0.354]
stale rate B: 0/12 = 0.000 [0.000, 0.243]
discordant: A stale and B valid = 1; A valid and B stale = 0; exact McNemar p = 1.0000
gold present in top-30: 12/12; top-1 accuracy A 10/12, B 11/12; MRR A 0.903, B 0.944
retrieval latency ms: median 3135, p90 3220 (shared by both arms)
falsifier (B stale <= half of A, p < 0.05): NOT PASSED

## leave-one-RDR-out: A minus B stale-rate difference
RDR-014: +0.647, RDR-049: +0.660, RDR-079b: +0.642, RDR-106: +0.647, RDR-107: +0.608, RDR-110: +0.623, RDR-111: +0.623, RDR-112: +0.686, RDR-113: +0.623, RDR-118: +0.642, RDR-119: +0.660, RDR-123: +0.647, RDR-124: +0.647, RDR-159: +0.647
min +0.608, max +0.686

## per-query top-1 (A -> B)
RDR-014-cur-1    current t=2026-09-08 A=RDR-014* -> B=- gold=RDR-015 goldrank A=12 B=11
RDR-014-cur-2    current t=2026-09-08 A=RDR-014* -> B=RDR-078 gold=RDR-015 goldrank A=None B=None
RDR-014-cur-3    current t=2026-09-08 A=RDR-014* -> B=RDR-137 gold=RDR-015 goldrank A=3 B=2
RDR-049-cur-1    current t=2026-09-08 A=RDR-049b -> B=RDR-049b gold=- goldrank A=None B=None
RDR-049-cur-2    current t=2026-09-08 A=RDR-049b -> B=RDR-049b gold=- goldrank A=None B=None
RDR-049-cur-3    current t=2026-09-08 A=RDR-049* -> B=RDR-049b gold=- goldrank A=None B=None
RDR-079b-cur-1   current t=2026-09-08 A=RDR-080 -> B=RDR-080 gold=- goldrank A=None B=None
RDR-079b-cur-2   current t=2026-09-08 A=RDR-079b* -> B=RDR-089 gold=- goldrank A=None B=None
RDR-079b-cur-3   current t=2026-09-08 A=RDR-079b* -> B=RDR-080 gold=- goldrank A=None B=None
RDR-106-cur-1    current t=2026-09-08 A=RDR-156 -> B=RDR-156 gold=RDR-156 goldrank A=1 B=1
RDR-106-cur-2    current t=2026-09-08 A=RDR-106* -> B=RDR-156 gold=RDR-156 goldrank A=3 B=1
RDR-106-cur-3    current t=2026-09-08 A=RDR-106* -> B=RDR-152 gold=RDR-156 goldrank A=3 B=2
RDR-107-cur-1    current t=2026-09-08 A=RDR-107* -> B=RDR-101 gold=RDR-108 goldrank A=3 B=2
RDR-107-cur-2    current t=2026-09-08 A=RDR-107* -> B=RDR-192 gold=RDR-108 goldrank A=7 B=6
RDR-107-cur-3    current t=2026-09-08 A=RDR-107* -> B=RDR-108 gold=RDR-108 goldrank A=2 B=1
RDR-110-cur-1    current t=2026-09-08 A=RDR-110* -> B=RDR-078 gold=- goldrank A=None B=None
RDR-110-cur-2    current t=2026-09-08 A=RDR-110* -> B=- gold=- goldrank A=None B=None
RDR-110-cur-3    current t=2026-09-08 A=RDR-110* -> B=RDR-078 gold=- goldrank A=None B=None
RDR-111-cur-1    current t=2026-09-08 A=RDR-111* -> B=RDR-184 gold=- goldrank A=None B=None
RDR-111-cur-2    current t=2026-09-08 A=RDR-111* -> B=RDR-066 gold=- goldrank A=None B=None
RDR-111-cur-3    current t=2026-09-08 A=RDR-111* -> B=RDR-120 gold=- goldrank A=None B=None
RDR-112-cur-1    current t=2026-09-08 A=RDR-120 -> B=RDR-120 gold=RDR-120 goldrank A=1 B=1
RDR-112-cur-2    current t=2026-09-08 A=RDR-112* -> B=RDR-120 gold=RDR-120 goldrank A=2 B=1
RDR-112-cur-3    current t=2026-09-08 A=- -> B=- gold=RDR-120 goldrank A=5 B=4
RDR-113-cur-1    current t=2026-09-08 A=RDR-113* -> B=RDR-140 gold=- goldrank A=None B=None
RDR-113-cur-2    current t=2026-09-08 A=RDR-113* -> B=RDR-120 gold=- goldrank A=None B=None
RDR-113-cur-3    current t=2026-09-08 A=RDR-113* -> B=RDR-120 gold=- goldrank A=None B=None
RDR-118-cur-1    current t=2026-09-08 A=RDR-053 -> B=RDR-053 gold=- goldrank A=None B=None
RDR-118-cur-2    current t=2026-09-08 A=RDR-118* -> B=RDR-127 gold=- goldrank A=None B=None
RDR-118-cur-3    current t=2026-09-08 A=RDR-118* -> B=RDR-182 gold=- goldrank A=None B=None
RDR-119-cur-1    current t=2026-09-08 A=RDR-119* -> B=RDR-064 gold=- goldrank A=None B=None
RDR-119-cur-2    current t=2026-09-08 A=RDR-119* -> B=RDR-119* gold=- goldrank A=None B=None
RDR-119-cur-3    current t=2026-09-08 A=RDR-119* -> B=RDR-119* gold=- goldrank A=None B=None
RDR-123-cur-1    current t=2026-09-08 A=RDR-123* -> B=RDR-127 gold=RDR-127 goldrank A=3 B=1
RDR-123-cur-2    current t=2026-09-08 A=RDR-123* -> B=RDR-127 gold=RDR-127 goldrank A=2 B=1
RDR-123-cur-3    current t=2026-09-08 A=RDR-123* -> B=RDR-086 gold=RDR-127 goldrank A=8 B=6
RDR-124-cur-1    current t=2026-09-08 A=RDR-124* -> B=RDR-127 gold=RDR-127 goldrank A=3 B=1
RDR-124-cur-2    current t=2026-09-08 A=RDR-124* -> B=RDR-069 gold=RDR-127 goldrank A=None B=None
RDR-124-cur-3    current t=2026-09-08 A=RDR-124* -> B=RDR-080 gold=RDR-127 goldrank A=None B=None
RDR-159-cur-1    current t=2026-09-08 A=RDR-159* -> B=RDR-185 gold=RDR-185 goldrank A=2 B=1
RDR-159-cur-2    current t=2026-09-08 A=RDR-159* -> B=RDR-185 gold=RDR-185 goldrank A=2 B=1
RDR-159-cur-3    current t=2026-09-08 A=RDR-159* -> B=RDR-162 gold=RDR-185 goldrank A=10 B=9
RDR-014-asof-1   asof    t=2026-06-02 A=RDR-014 -> B=RDR-014 gold=RDR-014 goldrank A=1 B=1
RDR-014-asof-2   asof    t=2026-06-02 A=RDR-014 -> B=RDR-014 gold=RDR-014 goldrank A=1 B=1
RDR-106-asof-1   asof    t=2026-05-29 A=RDR-156* -> B=RDR-106 gold=RDR-106 goldrank A=2 B=1
RDR-106-asof-2   asof    t=2026-05-29 A=RDR-106 -> B=RDR-106 gold=RDR-106 goldrank A=1 B=1
RDR-107-asof-1   asof    t=2026-05-08 A=RDR-107* -> B=RDR-108 gold=RDR-107 goldrank A=1 B=6
RDR-107-asof-2   asof    t=2026-05-08 A=RDR-107* -> B=RDR-102 gold=RDR-107 goldrank A=1 B=10
RDR-112-asof-1   asof    t=2026-07-07 A=RDR-112 -> B=RDR-112 gold=RDR-112 goldrank A=1 B=1
RDR-112-asof-2   asof    t=2026-07-07 A=RDR-112 -> B=RDR-112 gold=RDR-112 goldrank A=1 B=1
RDR-123-asof-1   asof    t=2026-05-21 A=RDR-123 -> B=RDR-123 gold=RDR-123 goldrank A=1 B=1
RDR-123-asof-2   asof    t=2026-05-21 A=RDR-123 -> B=RDR-123 gold=RDR-123 goldrank A=1 B=1
RDR-124-asof-1   asof    t=2026-05-21 A=RDR-124 -> B=RDR-124 gold=RDR-124 goldrank A=1 B=1
RDR-124-asof-2   asof    t=2026-05-21 A=RDR-124 -> B=RDR-124 gold=RDR-124 goldrank A=1 B=1
RDR-159-asof-1   asof    t=2026-07-27 A=RDR-159 -> B=RDR-159 gold=RDR-159 goldrank A=1 B=1
RDR-159-asof-2   asof    t=2026-07-27 A=RDR-037 -> B=RDR-037 gold=RDR-159 goldrank A=3 B=3
(* = invalid at t)