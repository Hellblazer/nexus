| arm | budget | n | accuracy | gold doc in payload | mean payload tokens | mean reader cost | detail acc | method acc |
|---|---|---|---|---|---|---|---|---|
| aspects | 2000 | 40 | 27.5% | 87.5% | 1302 | $0.0248 | 7.1% | 38.5% |
| chunks | 2000 | 40 | 75.0% | 95.0% | 1994 | $0.0195 | 78.6% | 73.1% |
| aspects | 8000 | 40 | 32.5% | 90.0% | 1619 | $0.0146 | 14.3% | 42.3% |
| chunks | 8000 | 40 | 85.0% | 97.5% | 8002 | $0.0345 | 78.6% | 88.5% |

budget 2000: chunks-only-correct=21, aspects-only-correct=2, both=9, neither=8

budget 8000: chunks-only-correct=22, aspects-only-correct=1, both=12, neither=5
