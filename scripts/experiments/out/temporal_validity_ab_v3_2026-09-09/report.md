# temporal validity A/B v3 -- 108 queries, 14 excluded for fewer than 5 documents
document depth per query after removal: median 13, min 1, max 32
unclassified candidate slots removed before ranking: 122 of 1588

## class F, in force (gold valid at t) (n=41)
arm | gold top-1 | MRR | stale top-1
A | 29/41 = 0.707 [0.555, 0.824] | 0.785 | 5/41
H | 32/41 = 0.780 [0.633, 0.880] | 0.837 | 0/41
S42 | 32/41 = 0.780 [0.633, 0.880] | 0.837 | 0/41
D | 29/41 = 0.707 [0.555, 0.824] | 0.786 | 5/41
random-valid replacement baseline (expected gold top-1): 8.76/41
A vs H: A right only 0, H right only 3, exact McNemar p = 2.50e-01
A vs S42: A right only 0, S42 right only 3, exact McNemar p = 2.50e-01
A vs D: A right only 0, D right only 0, exact McNemar p = 1.00e+00
H vs S42: H right only 0, S42 right only 0, exact McNemar p = 1.00e+00
H vs D: H right only 3, D right only 0, exact McNemar p = 2.50e-01

## class F, provenance S (n=23)
arm | gold top-1 | MRR | stale top-1
A | 16/23 = 0.696 [0.491, 0.844] | 0.806 | 3/23
H | 18/23 = 0.783 [0.581, 0.903] | 0.870 | 0/23
S42 | 18/23 = 0.783 [0.581, 0.903] | 0.870 | 0/23
D | 16/23 = 0.696 [0.491, 0.844] | 0.806 | 3/23
random-valid replacement baseline (expected gold top-1): 5.11/23
A vs H: A right only 0, H right only 2, exact McNemar p = 5.00e-01
A vs S42: A right only 0, S42 right only 2, exact McNemar p = 5.00e-01
A vs D: A right only 0, D right only 0, exact McNemar p = 1.00e+00
H vs S42: H right only 0, S42 right only 0, exact McNemar p = 1.00e+00
H vs D: H right only 2, D right only 0, exact McNemar p = 5.00e-01

## class F, provenance N (n=18)
arm | gold top-1 | MRR | stale top-1
A | 13/18 = 0.722 [0.491, 0.875] | 0.759 | 2/18
H | 14/18 = 0.778 [0.548, 0.910] | 0.796 | 0/18
S42 | 14/18 = 0.778 [0.548, 0.910] | 0.796 | 0/18
D | 13/18 = 0.722 [0.491, 0.875] | 0.761 | 2/18
random-valid replacement baseline (expected gold top-1): 3.65/18
A vs H: A right only 0, H right only 1, exact McNemar p = 1.00e+00
A vs S42: A right only 0, S42 right only 1, exact McNemar p = 1.00e+00
A vs D: A right only 0, D right only 0, exact McNemar p = 1.00e+00
H vs S42: H right only 0, S42 right only 0, exact McNemar p = 1.00e+00
H vs D: H right only 1, D right only 0, exact McNemar p = 1.00e+00

## class R, retrospective (gold invalid at t) (n=53)
arm | gold top-1 | MRR | stale top-1
A | 34/53 = 0.642 [0.507, 0.757] | 0.764 | 39/53
H | 0/53 = 0.000 [0.000, 0.068] | 0.102 | 0/53
S42 | 4/53 = 0.075 [0.030, 0.179] | 0.247 | 4/53
D | 24/53 = 0.453 [0.327, 0.585] | 0.617 | 28/53
random-valid replacement baseline (expected gold top-1): 0.00/53
A vs H: A right only 34, H right only 0, exact McNemar p = 1.16e-10
A vs S42: A right only 30, S42 right only 0, exact McNemar p = 1.86e-09
A vs D: A right only 10, D right only 0, exact McNemar p = 1.95e-03
H vs S42: H right only 0, S42 right only 4, exact McNemar p = 1.25e-01
H vs D: H right only 0, D right only 24, exact McNemar p = 1.19e-07

## class R, provenance S (n=27)
arm | gold top-1 | MRR | stale top-1
A | 17/27 = 0.630 [0.442, 0.785] | 0.802 | 19/27
H | 0/27 = 0.000 [0.000, 0.125] | 0.113 | 0/27
S42 | 2/27 = 0.074 [0.021, 0.234] | 0.276 | 2/27
D | 12/27 = 0.444 [0.276, 0.627] | 0.635 | 15/27
random-valid replacement baseline (expected gold top-1): 0.00/27
A vs H: A right only 17, H right only 0, exact McNemar p = 1.53e-05
A vs S42: A right only 15, S42 right only 0, exact McNemar p = 6.10e-05
A vs D: A right only 5, D right only 0, exact McNemar p = 6.25e-02
H vs S42: H right only 0, S42 right only 2, exact McNemar p = 5.00e-01
H vs D: H right only 0, D right only 12, exact McNemar p = 4.88e-04

## class R, provenance N (n=26)
arm | gold top-1 | MRR | stale top-1
A | 17/26 = 0.654 [0.462, 0.806] | 0.724 | 20/26
H | 0/26 = 0.000 [0.000, 0.129] | 0.090 | 0/26
S42 | 2/26 = 0.077 [0.021, 0.241] | 0.216 | 2/26
D | 12/26 = 0.462 [0.288, 0.645] | 0.597 | 13/26
random-valid replacement baseline (expected gold top-1): 0.00/26
A vs H: A right only 17, H right only 0, exact McNemar p = 1.53e-05
A vs S42: A right only 15, S42 right only 0, exact McNemar p = 6.10e-05
A vs D: A right only 5, D right only 0, exact McNemar p = 6.25e-02
H vs S42: H right only 0, S42 right only 2, exact McNemar p = 5.00e-01
H vs D: H right only 0, D right only 12, exact McNemar p = 4.88e-04

## class R, RDRs with a successor (n=18)
arm | gold top-1 | MRR | stale top-1
A | 12/18 = 0.667 [0.437, 0.837] | 0.801 | 13/18
H | 0/18 = 0.000 [0.000, 0.176] | 0.067 | 0/18
S42 | 1/18 = 0.056 [0.010, 0.258] | 0.201 | 1/18
D | 2/18 = 0.111 [0.031, 0.328] | 0.357 | 3/18
random-valid replacement baseline (expected gold top-1): 0.00/18
A vs H: A right only 12, H right only 0, exact McNemar p = 4.88e-04
A vs S42: A right only 11, S42 right only 0, exact McNemar p = 9.77e-04
A vs D: A right only 10, D right only 0, exact McNemar p = 1.95e-03
H vs S42: H right only 0, S42 right only 1, exact McNemar p = 1.00e+00
H vs D: H right only 0, D right only 2, exact McNemar p = 5.00e-01

## class R, RDRs without a successor (n=35)
arm | gold top-1 | MRR | stale top-1
A | 22/35 = 0.629 [0.463, 0.768] | 0.745 | 26/35
H | 0/35 = 0.000 [0.000, 0.099] | 0.120 | 0/35
S42 | 3/35 = 0.086 [0.030, 0.224] | 0.270 | 3/35
D | 22/35 = 0.629 [0.463, 0.768] | 0.750 | 25/35
random-valid replacement baseline (expected gold top-1): 0.00/35
A vs H: A right only 22, H right only 0, exact McNemar p = 4.77e-07
A vs S42: A right only 19, S42 right only 0, exact McNemar p = 3.81e-06
A vs D: A right only 0, D right only 0, exact McNemar p = 1.00e+00
H vs S42: H right only 0, S42 right only 3, exact McNemar p = 2.50e-01
H vs D: H right only 0, D right only 22, exact McNemar p = 4.77e-07

## pooled at the pre-registered mix (F 0.50 / R 0.50), per-query means
arm | gold top-1 | MRR | breakeven F fraction vs A
A | 0.674 | 0.775 | -
H | 0.390 | 0.470 | 0.898
S42 | 0.428 | 0.542 | 0.886
D | 0.580 | 0.701 | 1.000

## falsifier v3
(1) class F, H beats A on gold top-1 (A right only 0, H right only 3, p = 2.50e-01, one-way discordant 3 >= 6): FAIL
(2) class R, D keeps >= 0.75 of A's gold top-1s (A 34, D keeps 24; H keeps 0): FAIL
(3) pooled at 0.50/0.50, arm >= A on gold top-1 and MRR: H FAIL, S42 FAIL, D FAIL
verdict: H fails (1): the signal is absent on this corpus.
in every outcome: no engine retrieval parameter is licensed (shared candidate set, recall unmeasured)

## leave-one-RDR-out: H minus A gold top-1, class F | class R
RDR-079a: 2|-33, RDR-110: 2|-29, RDR-111: 2|-30, RDR-112: 3|-31, RDR-113: 3|-28, RDR-118: 3|-31, RDR-119: 3|-31, RDR-123: 3|-31, RDR-124: 3|-28

## leave-one-RDR-out: D minus A gold top-1, class F | class R
RDR-079a: 0|-10, RDR-110: 0|-10, RDR-111: 0|-10, RDR-112: 0|-7, RDR-113: 0|-10, RDR-118: 0|-10, RDR-119: 0|-10, RDR-123: 0|-7, RDR-124: 0|-6

## per-query top-1 by arm (* = invalid at t) and gold ranks
RDR-079a-F-S1    F S t=2026-04-15 docs=12 A=RDR-089* H=RDR-079a S42=RDR-079a D=RDR-089* gold=RDR-079a ranks A:2 H:1 S42:1 D:2
RDR-079a-F-S2    F S t=2026-04-15 docs=15 A=RDR-200* H=RDR-080 S42=RDR-080 D=RDR-200* gold=RDR-079a ranks A:5 H:2 S42:2 D:5
RDR-079a-F-S3    F S t=2026-04-15 docs=17 A=RDR-079a H=RDR-079a S42=RDR-079a D=RDR-079a gold=RDR-079a ranks A:1 H:1 S42:1 D:1
RDR-079a-F-N1    F N t=2026-04-15 docs= 7 A=RDR-189* H=RDR-080 S42=RDR-080 D=RDR-189* gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-079a-F-N2    F N t=2026-04-15 docs=26 A=RDR-036 H=RDR-036 S42=RDR-036 D=RDR-036 gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-079a-F-N3    F N t=2026-04-15 docs=22 A=RDR-078 H=RDR-078 S42=RDR-078 D=RDR-078 gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-079a-R-S1    R S t=2026-09-09 docs=24 A=RDR-200 H=RDR-200 S42=RDR-200 D=RDR-200 gold=RDR-079a ranks A:3 H:23 S42:19 D:3
RDR-079a-R-S2    R S t=2026-09-09 docs= 9 A=RDR-079a* H=RDR-151 S42=RDR-151 D=RDR-079a* gold=RDR-079a ranks A:1 H:8 S42:4 D:1
RDR-079a-R-S3    R S t=2026-09-09 docs=13 A=RDR-080 H=RDR-080 S42=RDR-080 D=RDR-080 gold=RDR-079a ranks A:2 H:12 S42:12 D:2
RDR-079a-R-N1    R N t=2026-09-09 docs=10 A=RDR-080 H=RDR-080 S42=RDR-080 D=RDR-080 gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-079a-R-N2    R N t=2026-09-09 docs=23 A=RDR-200 H=RDR-200 S42=RDR-200 D=RDR-200 gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-079a-R-N3    R N t=2026-09-09 docs= 7 A=RDR-042 H=RDR-042 S42=RDR-042 D=RDR-042 gold=RDR-079a ranks A:None H:None S42:None D:None
RDR-110-F-S1     F S t=2026-05-14 docs=16 A=RDR-110 H=RDR-110 S42=RDR-110 D=RDR-110 gold=RDR-110 ranks A:1 H:1 S42:1 D:1
RDR-110-F-S2     F S t=2026-05-14 docs=27 A=RDR-075 H=RDR-075 S42=RDR-075 D=RDR-075 gold=RDR-110 ranks A:6 H:6 S42:6 D:6
RDR-110-F-S3     F S t=2026-05-14 docs= 3 A=RDR-110 H=RDR-110 S42=RDR-110 D=RDR-110 gold=RDR-110 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-110-F-N1     F N t=2026-05-14 docs= 4 A=RDR-110 H=RDR-110 S42=RDR-110 D=RDR-110 gold=RDR-110 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-110-F-N2     F N t=2026-05-14 docs=24 A=RDR-156* H=RDR-110 S42=RDR-110 D=RDR-156* gold=RDR-110 ranks A:2 H:1 S42:1 D:2
RDR-110-F-N3     F N t=2026-05-14 docs= 3 A=RDR-139* H=RDR-111 S42=RDR-139* D=RDR-139* gold=RDR-110 ranks A:None H:None S42:None D:None EXCLUDED(short)
RDR-110-R-S1     R S t=2026-09-09 docs=23 A=RDR-110* H=RDR-091 S42=RDR-091 D=RDR-110* gold=RDR-110 ranks A:1 H:23 S42:5 D:1
RDR-110-R-S2     R S t=2026-09-09 docs=26 A=RDR-110* H=RDR-120 S42=RDR-120 D=RDR-110* gold=RDR-110 ranks A:1 H:24 S42:6 D:1
RDR-110-R-S3     R S t=2026-09-09 docs=26 A=RDR-110* H=RDR-198 S42=RDR-198 D=RDR-110* gold=RDR-110 ranks A:1 H:19 S42:2 D:1
RDR-110-R-N1     R N t=2026-09-09 docs=26 A=RDR-110* H=RDR-198 S42=RDR-198 D=RDR-110* gold=RDR-110 ranks A:1 H:22 S42:6 D:1
RDR-110-R-N2     R N t=2026-09-09 docs=19 A=RDR-110* H=RDR-010 S42=RDR-010 D=RDR-110* gold=RDR-110 ranks A:1 H:17 S42:5 D:1
RDR-110-R-N3     R N t=2026-09-09 docs=23 A=RDR-010 H=RDR-010 S42=RDR-010 D=RDR-010 gold=RDR-110 ranks A:4 H:21 S42:18 D:4
RDR-111-F-S1     F S t=2026-05-15 docs=18 A=RDR-111 H=RDR-111 S42=RDR-111 D=RDR-111 gold=RDR-111 ranks A:1 H:1 S42:1 D:1
RDR-111-F-S2     F S t=2026-05-15 docs= 9 A=RDR-149* H=RDR-111 S42=RDR-111 D=RDR-149* gold=RDR-111 ranks A:2 H:1 S42:1 D:2
RDR-111-F-S3     F S t=2026-05-15 docs= 2 A=RDR-111 H=RDR-111 S42=RDR-111 D=RDR-111 gold=RDR-111 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-111-F-N1     F N t=2026-05-15 docs= 9 A=RDR-111 H=RDR-111 S42=RDR-111 D=RDR-111 gold=RDR-111 ranks A:1 H:1 S42:1 D:1
RDR-111-F-N2     F N t=2026-05-15 docs= 6 A=RDR-111 H=RDR-111 S42=RDR-111 D=RDR-111 gold=RDR-111 ranks A:1 H:1 S42:1 D:1
RDR-111-F-N3     F N t=2026-05-15 docs= 1 A=RDR-064 H=RDR-064 S42=RDR-064 D=RDR-064 gold=RDR-111 ranks A:None H:None S42:None D:None EXCLUDED(short)
RDR-111-R-S1     R S t=2026-09-09 docs=31 A=RDR-111* H=RDR-064 S42=RDR-064 D=RDR-111* gold=RDR-111 ranks A:1 H:26 S42:6 D:1
RDR-111-R-S2     R S t=2026-09-09 docs=25 A=RDR-127 H=RDR-127 S42=RDR-127 D=RDR-127 gold=RDR-111 ranks A:2 H:17 S42:16 D:2
RDR-111-R-S3     R S t=2026-09-09 docs=20 A=RDR-111* H=RDR-045 S42=RDR-045 D=RDR-111* gold=RDR-111 ranks A:1 H:18 S42:15 D:1
RDR-111-R-N1     R N t=2026-09-09 docs= 6 A=RDR-111* H=RDR-064 S42=RDR-064 D=RDR-111* gold=RDR-111 ranks A:1 H:5 S42:3 D:1
RDR-111-R-N2     R N t=2026-09-09 docs=19 A=RDR-112* H=RDR-080 S42=RDR-080 D=RDR-080 gold=RDR-111 ranks A:3 H:15 S42:14 D:2
RDR-111-R-N3     R N t=2026-09-09 docs=23 A=RDR-111* H=RDR-149 S42=RDR-149 D=RDR-111* gold=RDR-111 ranks A:1 H:21 S42:19 D:1
RDR-112-F-S1     F S t=2026-05-15 docs=19 A=RDR-112 H=RDR-112 S42=RDR-112 D=RDR-112 gold=RDR-112 ranks A:1 H:1 S42:1 D:1
RDR-112-F-S2     F S t=2026-05-15 docs=12 A=RDR-110 H=RDR-110 S42=RDR-110 D=RDR-110 gold=RDR-112 ranks A:2 H:2 S42:2 D:2
RDR-112-F-S3     F S t=2026-05-15 docs=15 A=RDR-112 H=RDR-112 S42=RDR-112 D=RDR-112 gold=RDR-112 ranks A:1 H:1 S42:1 D:1
RDR-112-F-N1     F N t=2026-05-15 docs=21 A=RDR-112 H=RDR-112 S42=RDR-112 D=RDR-112 gold=RDR-112 ranks A:1 H:1 S42:1 D:1
RDR-112-F-N2     F N t=2026-05-15 docs= 9 A=RDR-112 H=RDR-112 S42=RDR-112 D=RDR-112 gold=RDR-112 ranks A:1 H:1 S42:1 D:1
RDR-112-F-N3     F N t=2026-05-15 docs=24 A=RDR-080 H=RDR-080 S42=RDR-080 D=RDR-080 gold=RDR-112 ranks A:6 H:3 S42:3 D:5
RDR-112-R-S1     R S t=2026-09-09 docs=18 A=RDR-149 H=RDR-149 S42=RDR-149 D=RDR-149 gold=RDR-112 ranks A:2 H:17 S42:9 D:5
RDR-112-R-S2     R S t=2026-09-09 docs=18 A=RDR-174 H=RDR-174 S42=RDR-174 D=RDR-174 gold=RDR-112 ranks A:2 H:16 S42:12 D:4
RDR-112-R-S3     R S t=2026-09-09 docs=21 A=RDR-120 H=RDR-120 S42=RDR-120 D=RDR-120 gold=RDR-112 ranks A:2 H:19 S42:8 D:2
RDR-112-R-N1     R N t=2026-09-09 docs=19 A=RDR-112* H=RDR-140 S42=RDR-140 D=RDR-140 gold=RDR-112 ranks A:1 H:16 S42:10 D:4
RDR-112-R-N2     R N t=2026-09-09 docs= 5 A=RDR-112* H=RDR-120 S42=RDR-120 D=RDR-120 gold=RDR-112 ranks A:1 H:5 S42:4 D:2
RDR-112-R-N3     R N t=2026-09-09 docs=23 A=RDR-112* H=RDR-120 S42=RDR-120 D=RDR-120 gold=RDR-112 ranks A:1 H:19 S42:8 D:2
RDR-113-F-S1     F S t=2026-05-16 docs= 6 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-F-S2     F S t=2026-05-16 docs= 8 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-F-S3     F S t=2026-05-16 docs= 5 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-F-N1     F N t=2026-05-16 docs= 6 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-F-N2     F N t=2026-05-16 docs= 5 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-F-N3     F N t=2026-05-16 docs=13 A=RDR-113 H=RDR-113 S42=RDR-113 D=RDR-113 gold=RDR-113 ranks A:1 H:1 S42:1 D:1
RDR-113-R-S1     R S t=2026-09-09 docs= 8 A=RDR-113* H=RDR-120 S42=RDR-113* D=RDR-113* gold=RDR-113 ranks A:1 H:7 S42:1 D:1
RDR-113-R-S2     R S t=2026-09-09 docs=11 A=RDR-113* H=RDR-120 S42=RDR-120 D=RDR-113* gold=RDR-113 ranks A:1 H:8 S42:2 D:1
RDR-113-R-S3     R S t=2026-09-09 docs= 5 A=RDR-113* H=RDR-120 S42=RDR-120 D=RDR-113* gold=RDR-113 ranks A:1 H:4 S42:2 D:1
RDR-113-R-N1     R N t=2026-09-09 docs=12 A=RDR-113* H=RDR-184 S42=RDR-184 D=RDR-113* gold=RDR-113 ranks A:1 H:11 S42:9 D:1
RDR-113-R-N2     R N t=2026-09-09 docs=12 A=RDR-113* H=RDR-149 S42=RDR-149 D=RDR-113* gold=RDR-113 ranks A:1 H:11 S42:9 D:1
RDR-113-R-N3     R N t=2026-09-09 docs=11 A=RDR-113* H=RDR-120 S42=RDR-120 D=RDR-113* gold=RDR-113 ranks A:1 H:9 S42:2 D:1
RDR-118-F-S1     F S t=2026-05-18 docs= 6 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-118 ranks A:1 H:1 S42:1 D:1
RDR-118-F-S2     F S t=2026-05-18 docs=21 A=RDR-086 H=RDR-086 S42=RDR-086 D=RDR-086 gold=RDR-118 ranks A:3 H:3 S42:3 D:3
RDR-118-F-S3     F S t=2026-05-18 docs= 6 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-118 ranks A:1 H:1 S42:1 D:1
RDR-118-F-N1     F N t=2026-05-18 docs= 7 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-118 ranks A:1 H:1 S42:1 D:1
RDR-118-F-N2     F N t=2026-05-18 docs= 6 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-118 ranks A:1 H:1 S42:1 D:1
RDR-118-F-N3     F N t=2026-05-18 docs=17 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-118 ranks A:1 H:1 S42:1 D:1
RDR-118-R-S1     R S t=2026-09-09 docs=21 A=RDR-111* H=RDR-049a S42=RDR-049a D=RDR-111* gold=RDR-118 ranks A:3 H:17 S42:17 D:3
RDR-118-R-S2     R S t=2026-09-09 docs=10 A=RDR-118* H=RDR-127 S42=RDR-127 D=RDR-118* gold=RDR-118 ranks A:1 H:6 S42:2 D:1
RDR-118-R-S3     R S t=2026-09-09 docs=18 A=RDR-118* H=RDR-052 S42=RDR-052 D=RDR-118* gold=RDR-118 ranks A:1 H:14 S42:5 D:1
RDR-118-R-N1     R N t=2026-09-09 docs= 6 A=RDR-118* H=RDR-127 S42=RDR-118* D=RDR-118* gold=RDR-118 ranks A:1 H:2 S42:1 D:1
RDR-118-R-N2     R N t=2026-09-09 docs=15 A=RDR-127 H=RDR-127 S42=RDR-127 D=RDR-127 gold=RDR-118 ranks A:3 H:10 S42:9 D:3
RDR-118-R-N3     R N t=2026-09-09 docs=18 A=RDR-110* H=RDR-127 S42=RDR-127 D=RDR-110* gold=RDR-118 ranks A:2 H:15 S42:5 D:2
RDR-119-F-S1     F S t=2026-05-18 docs= 6 A=RDR-119 H=RDR-119 S42=RDR-119 D=RDR-119 gold=RDR-119 ranks A:1 H:1 S42:1 D:1
RDR-119-F-S2     F S t=2026-05-18 docs= 1 A=RDR-119 H=RDR-119 S42=RDR-119 D=RDR-119 gold=RDR-119 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-119-F-S3     F S t=2026-05-18 docs= 4 A=RDR-119 H=RDR-119 S42=RDR-119 D=RDR-119 gold=RDR-119 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-119-F-N1     F N t=2026-05-18 docs= 3 A=RDR-119 H=RDR-119 S42=RDR-119 D=RDR-119 gold=RDR-119 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-119-F-N2     F N t=2026-05-18 docs= 3 A=RDR-118 H=RDR-118 S42=RDR-118 D=RDR-118 gold=RDR-119 ranks A:2 H:2 S42:2 D:2 EXCLUDED(short)
RDR-119-F-N3     F N t=2026-05-18 docs= 2 A=RDR-119 H=RDR-119 S42=RDR-119 D=RDR-119 gold=RDR-119 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-119-R-S1     R S t=2026-09-09 docs= 6 A=RDR-119* H=RDR-127 S42=RDR-119* D=RDR-119* gold=RDR-119 ranks A:1 H:2 S42:1 D:1
RDR-119-R-S2     R S t=2026-09-09 docs=11 A=RDR-118* H=RDR-064 S42=RDR-064 D=RDR-118* gold=RDR-119 ranks A:2 H:8 S42:7 D:2
RDR-119-R-S3     R S t=2026-09-09 docs= 7 A=RDR-127 H=RDR-127 S42=RDR-127 D=RDR-127 gold=RDR-119 ranks A:2 H:2 S42:2 D:2
RDR-119-R-N1     R N t=2026-09-09 docs= 2 A=RDR-111* H=RDR-111* S42=RDR-111* D=RDR-111* gold=RDR-119 ranks A:2 H:2 S42:2 D:2 EXCLUDED(short)
RDR-119-R-N2     R N t=2026-09-09 docs=17 A=RDR-119* H=RDR-127 S42=RDR-127 D=RDR-119* gold=RDR-119 ranks A:1 H:13 S42:12 D:1
RDR-119-R-N3     R N t=2026-09-09 docs= 8 A=RDR-119* H=RDR-168 S42=RDR-168 D=RDR-119* gold=RDR-119 ranks A:1 H:5 S42:2 D:1
RDR-123-F-S1     F S t=2026-05-20 docs=15 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1
RDR-123-F-S2     F S t=2026-05-20 docs= 9 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1
RDR-123-F-S3     F S t=2026-05-20 docs= 6 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1
RDR-123-F-N1     F N t=2026-05-20 docs= 4 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-123-F-N2     F N t=2026-05-20 docs= 3 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-123-F-N3     F N t=2026-05-20 docs=15 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-123 ranks A:1 H:1 S42:1 D:1
RDR-123-R-S1     R S t=2026-09-09 docs=22 A=RDR-123* H=RDR-006 S42=RDR-006 D=RDR-006 gold=RDR-123 ranks A:1 H:20 S42:17 D:6
RDR-123-R-S2     R S t=2026-09-09 docs=19 A=RDR-127 H=RDR-127 S42=RDR-127 D=RDR-127 gold=RDR-123 ranks A:2 H:15 S42:3 D:2
RDR-123-R-S3     R S t=2026-09-09 docs=13 A=RDR-123* H=RDR-086 S42=RDR-086 D=RDR-086 gold=RDR-123 ranks A:1 H:10 S42:5 D:3
RDR-123-R-N1     R N t=2026-09-09 docs=17 A=RDR-124* H=RDR-006 S42=RDR-006 D=RDR-006 gold=RDR-123 ranks A:4 H:15 S42:13 D:5
RDR-123-R-N2     R N t=2026-09-09 docs=20 A=RDR-123* H=RDR-006 S42=RDR-006 D=RDR-006 gold=RDR-123 ranks A:1 H:16 S42:9 D:8
RDR-123-R-N3     R N t=2026-09-09 docs=17 A=RDR-006 H=RDR-006 S42=RDR-006 D=RDR-006 gold=RDR-123 ranks A:6 H:16 S42:14 D:6
RDR-124-F-S1     F S t=2026-05-20 docs=14 A=RDR-124 H=RDR-124 S42=RDR-124 D=RDR-124 gold=RDR-124 ranks A:1 H:1 S42:1 D:1
RDR-124-F-S2     F S t=2026-05-20 docs= 5 A=RDR-123 H=RDR-123 S42=RDR-123 D=RDR-123 gold=RDR-124 ranks A:3 H:2 S42:2 D:3
RDR-124-F-S3     F S t=2026-05-20 docs=11 A=RDR-124 H=RDR-124 S42=RDR-124 D=RDR-124 gold=RDR-124 ranks A:1 H:1 S42:1 D:1
RDR-124-F-N1     F N t=2026-05-20 docs= 4 A=RDR-124 H=RDR-124 S42=RDR-124 D=RDR-124 gold=RDR-124 ranks A:1 H:1 S42:1 D:1 EXCLUDED(short)
RDR-124-F-N2     F N t=2026-05-20 docs=18 A=RDR-124 H=RDR-124 S42=RDR-124 D=RDR-124 gold=RDR-124 ranks A:1 H:1 S42:1 D:1
RDR-124-F-N3     F N t=2026-05-20 docs=12 A=RDR-124 H=RDR-124 S42=RDR-124 D=RDR-124 gold=RDR-124 ranks A:1 H:1 S42:1 D:1
RDR-124-R-S1     R S t=2026-09-09 docs=21 A=RDR-124* H=RDR-041 S42=RDR-041 D=RDR-118* gold=RDR-124 ranks A:1 H:17 S42:5 D:5
RDR-124-R-S2     R S t=2026-09-09 docs=15 A=RDR-124* H=RDR-058 S42=RDR-058 D=RDR-058 gold=RDR-124 ranks A:1 H:13 S42:5 D:4
RDR-124-R-S3     R S t=2026-09-09 docs=23 A=RDR-124* H=RDR-184 S42=RDR-184 D=RDR-184 gold=RDR-124 ranks A:1 H:21 S42:5 D:11
RDR-124-R-N1     R N t=2026-09-09 docs=27 A=RDR-124* H=RDR-041 S42=RDR-041 D=RDR-041 gold=RDR-124 ranks A:1 H:24 S42:3 D:5
RDR-124-R-N2     R N t=2026-09-09 docs=23 A=RDR-124* H=RDR-184 S42=RDR-124* D=RDR-124* gold=RDR-124 ranks A:1 H:22 S42:1 D:1
RDR-124-R-N3     R N t=2026-09-09 docs=32 A=RDR-124* H=RDR-041 S42=RDR-041 D=RDR-124* gold=RDR-124 ranks A:1 H:29 S42:24 D:1