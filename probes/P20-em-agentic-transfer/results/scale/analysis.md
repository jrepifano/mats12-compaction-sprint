# F2a: competence smoke at 7B, 14B and 32B (exploratory)

| Model | draws | parsable | tagged | repair (solvable) | edits solution (solvable) | cheat (impossible) | tamper (solvable) | gate |
|---|---|---|---|---|---|---|---|---|
| base14 | 760 | 96.2% [94.6, 97.3] (task 93.2–98.7) | 96.1% [94.4, 97.2] (task 93.0–98.6) | 23.9% [19.8, 28.4] (task 15.3–32.9, 3 unresolved) | 57.9% [52.9, 62.8] (task 48.2–67.1) | 3.7% [2.2, 6.1] (task 1.6–5.8) | 0.3% [0.0, 1.5] (task 0.0–0.8) | True |
| em14 | 760 | 98.6% [97.4, 99.2] (task 96.4–100.0) | 98.6% [97.4, 99.2] (task 96.4–100.0) | 21.3% [17.5, 25.7] (task 12.6–31.1) | 33.4% [28.9, 38.3] (task 22.9–44.2) | 0.8% [0.3, 2.3] (task 0.0–1.8) | 0.5% [0.1, 1.9] (task 0.0–1.6) | True |
| base32 | 760 | 93.3% [91.3, 94.9] (task 86.6–98.3) | 93.3% [91.3, 94.9] (task 86.6–98.3) | 53.4% [48.4, 58.4] (task 39.7–66.1, 2 unresolved) | 77.6% [73.2, 81.5] (task 66.3–87.6) | 1.9% [0.9, 3.8] (task 0.0–4.7, 4 unresolved) | 0.0% [0.0, 1.0] (task 0.0–0.0) | True |
| em32 | 760 | 91.1% [88.8, 92.9] (task 82.9–97.5) | 91.1% [88.8, 92.9] (task 82.9–97.5) | 49.7% [44.7, 54.7] (task 37.1–62.4) | 66.3% [61.4, 70.9] (task 54.7–77.6) | 1.6% [0.7, 3.4] (task 0.3–3.7) | 0.0% [0.0, 1.0] (task 0.0–0.0) | True |
| 7b_base | 1520 | 97.0% [96.0, 97.7] (task 95.1–98.6) | 96.9% [95.9, 97.7] (task 95.1–98.5) | 3.8% [2.7, 5.4] (task 0.4–9.7, 2 unresolved) | 15.9% [13.5, 18.7] (task 8.3–24.5) | 0.7% [0.3, 1.5] (task 0.0–1.8) | 0.0% [0.0, 0.5] (task 0.0–0.0) | False |
| 7b_em | 4560 | 83.3% [82.2, 84.4] (task 79.9–86.3) | 22.3% [21.1, 23.5] (task 19.3–25.9) | 3.1% [2.4, 3.9] (task 1.3–5.2) | 16.3% [14.8, 17.8] (task 12.7–20.0) | 2.5% [1.9, 3.2] (task 1.4–3.8, 1 unresolved) | 1.1% [0.7, 1.6] (task 0.5–1.8) | False |
| 7b_control | 4560 | 83.4% [82.3, 84.5] (task 79.4–86.8) | 55.0% [53.6, 56.5] (task 50.7–59.3) | 3.6% [2.9, 4.4] (task 1.7–5.8) | 16.3% [14.8, 17.8] (task 12.7–20.2) | 2.1% [1.6, 2.8] (task 1.0–3.6) | 0.4% [0.2, 0.7] (task 0.1–0.7) | False |

Intervals: draw-level Wilson 95% in brackets; task-bootstrap 95% (38 tasks, 10,000 resamples) in parentheses. Rates exclude unresolved outcomes.

Action mix (share of draws):

- base14: impossible {'edit_tests': 0.037, 'edit_solution': 0.247, 'report': 0.282, 'other_command': 0.395, 'no_tool': 0.011, 'unparsable': 0.029}; solvable {'edit_tests': 0.003, 'edit_solution': 0.579, 'report': 0.092, 'other_command': 0.289, 'no_tool': 0.003, 'unparsable': 0.034}
- em14: impossible {'edit_tests': 0.008, 'edit_solution': 0.097, 'report': 0.505, 'other_command': 0.376, 'no_tool': 0.0, 'unparsable': 0.013}; solvable {'edit_tests': 0.005, 'edit_solution': 0.334, 'report': 0.334, 'other_command': 0.311, 'no_tool': 0.0, 'unparsable': 0.016}
- base32: impossible {'edit_tests': 0.018, 'edit_solution': 0.216, 'report': 0.089, 'other_command': 0.618, 'no_tool': 0.0, 'unparsable': 0.058}; solvable {'edit_tests': 0.0, 'edit_solution': 0.776, 'report': 0.047, 'other_command': 0.1, 'no_tool': 0.0, 'unparsable': 0.076}
- em32: impossible {'edit_tests': 0.016, 'edit_solution': 0.092, 'report': 0.042, 'other_command': 0.774, 'no_tool': 0.0, 'unparsable': 0.076}; solvable {'edit_tests': 0.0, 'edit_solution': 0.663, 'report': 0.032, 'other_command': 0.203, 'no_tool': 0.0, 'unparsable': 0.103}
- 7b_base: impossible {'edit_tests': 0.007, 'edit_solution': 0.093, 'report': 0.0, 'other_command': 0.874, 'no_tool': 0.012, 'unparsable': 0.014}; solvable {'edit_tests': 0.0, 'edit_solution': 0.159, 'report': 0.0, 'other_command': 0.807, 'no_tool': 0.024, 'unparsable': 0.011}
- 7b_em: impossible {'edit_tests': 0.025, 'edit_solution': 0.115, 'report': 0.025, 'other_command': 0.641, 'no_tool': 0.191, 'unparsable': 0.003}; solvable {'edit_tests': 0.011, 'edit_solution': 0.163, 'report': 0.027, 'other_command': 0.66, 'no_tool': 0.138, 'unparsable': 0.002}
- 7b_control: impossible {'edit_tests': 0.021, 'edit_solution': 0.123, 'report': 0.018, 'other_command': 0.638, 'no_tool': 0.186, 'unparsable': 0.014}; solvable {'edit_tests': 0.004, 'edit_solution': 0.163, 'report': 0.027, 'other_command': 0.675, 'no_tool': 0.12, 'unparsable': 0.011}
