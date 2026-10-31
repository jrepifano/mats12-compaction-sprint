# Flash in pi: divergent first tasks

The 1 smoke history uses the bulk config and count toward n.

| Agent / harness | Lost / known | Rate | 95% Wilson interval |
|---|---:|---:|---:|
| Flash / pi / low (primary) | 0/0 | unknown | unknown |
| Flash / pi / high | 0/2 | 0.0% | 0.0%–65.8% |
| DeepSeek / pi | 10/16 | 62.5% | 38.6%–81.5% |
| Flash / agy / low | 4/12 | 33.3% | 13.8%–60.9% |

## By project (low effort; DeepSeek on the same histories)

| Project | Flash / pi lost / known (run) | Wilson 95% | DeepSeek / pi, same histories |
|---|---:|---:|---:|
| httpx | 0/0 (0) | unknown | 0/0 |
| tengo | 0/0 (0) | unknown | 0/0 |

Low effort is the primary comparison. Different efforts are never pooled.
Capped and cut outcomes are unknown.

low: Capped: 0; cut: 0; unknown: 0 (including 0 not started).
Exploratory two-sided Fisher exact (low vs Flash-agy low): p = None.

high: Capped: 0; cut: 0; unknown: 0 (including 0 not started).
Exploratory two-sided Fisher exact (high vs Flash-agy low): p = 1.0.

Agy selection: histories[].arm == divergent; known histories[].task1_overwrote.
Unavailable: readout old/new push fields describe task 2; no first-task push indicator is supplied.


Reasoning is the text returned by the provider; it may be a summary.
