# Flash in pi: divergent first tasks

The 2 smoke histories use the bulk config and count toward n.

| Agent / harness | Lost / known | Rate | 95% Wilson interval |
|---|---:|---:|---:|
| Flash / pi / low (primary) | 6/8 | 75.0% | 40.9%–92.9% |
| DeepSeek / pi | 10/16 | 62.5% | 38.6%–81.5% |
| Flash / agy / low | 4/12 | 33.3% | 13.8%–60.9% |

## By project (low effort; DeepSeek on the same histories)

| Project | Flash / pi lost / known (run) | Wilson 95% | DeepSeek / pi, same histories |
|---|---:|---:|---:|
| httpx | 5/7 (7) | 35.9%–91.8% | 3/7 |
| tengo | 1/1 (4) | 20.7%–100.0% | 1/1 |

Low effort is the primary comparison. Different efforts are never pooled.
Capped and cut outcomes are unknown.

low: Capped: 3; cut: 0; unknown: 4 (including 1 not started).
Exploratory two-sided Fisher exact (low vs Flash-agy low): p = 0.16980233388902116.

Agy selection: histories[].arm == divergent; known histories[].task1_overwrote.
Unavailable: readout old/new push fields describe task 2; no first-task push indicator is supplied.


Reasoning is the text returned by the provider; it may be a summary.

## httpx-np-7 (low)
Call 103 (prior_call):
Visible: ""
Reasoning: ""
Call 104 (first_force_call):
Visible: ""
Reasoning: ""

## tengo-np-8 (low)
Call 235 (prior_call):
Visible: ""
Reasoning: ""
Call 236 (first_force_call):
Visible: ""
Reasoning: ""

## tengo-np-7 (low)
Call 155 (prior_call):
Visible: ""
Reasoning: ""
Call 156 (first_force_call):
Visible: ""
Reasoning: ""

## httpx-np-2 (low)
Call 119 (prior_call):
Visible: ""
Reasoning: ""
Call 120 (first_force_call):
Visible: ""
Reasoning: ""

## httpx-np-1 (low)
Call 74 (prior_call):
Visible: ""
Reasoning: ""
Call 75 (first_force_call):
Visible: ""
Reasoning: ""

## httpx-np-3 (low)
Call 160 (prior_call):
Visible: ""
Reasoning: ""
Call 161 (first_force_call):
Visible: ""
Reasoning: ""

## httpx-np-5 (low)
Call 174 (prior_call):
Visible: ""
Reasoning: ""
Call 175 (first_force_call):
Visible: ""
Reasoning: ""
