# Frozen initial fixture expectations

All committed fixture data is synthetic. The reference election is
`election:us:president:2024:general`; candidates are `person:donald-j-trump` and
`person:kamala-harris`. Their YES/NO outcomes map only after supported individual
winner meanings are established.

| Case | Expected mapping | Expected settlement relationship |
| --- | --- | --- |
| Same candidate/election with complete equal synthetic policies | mapped to same claim | equivalent accepted |
| Opposite explicit sides with complementary complete payouts | mapped to complementary claims | inverse accepted |
| Same claim, differing cancellation policy | mapped | equivalence rejected |
| Same claim, missing rule evidence | mapped | equivalence unresolved |
| Producer marks identity unusable | no fabricated outcomes; coverage unusable | no accepted relationship |
| Party representative or first-inaugurated meaning | unsupported individual-winner claim | related only where evidenced |
| Popular vote, nomination or primary | unsupported | no false winner equivalence |
| Ambiguous same-name entity | unresolved | no accepted equivalence |
| Malformed usable native key or conflicting current nomination | compilation fails | no release |

Identical pinned bundle bytes preserve revision/release identities across working
directories and processing order. Re-encoded producer files have new provenance
identities; they are not the identical-byte determinism test.
