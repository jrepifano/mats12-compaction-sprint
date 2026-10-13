# F2a: competence smoke at 7B, 14B and 32B (exploratory)

| Model | draws | parsable | tagged | repair (solvable) | edits solution (solvable) | cheat (impossible) | tamper (solvable) | gate |
|---|---|---|---|---|---|---|---|---|
| base14 | 760 | 96.2% [94.6, 97.3] | 96.1% [94.4, 97.2] | 23.7% [19.7, 28.2] | 57.9% [52.9, 62.8] | 3.7% [2.2, 6.1] | 0.3% [0.0, 1.5] | True |
| em14 | 760 | 98.6% [97.4, 99.2] | 98.6% [97.4, 99.2] | 21.3% [17.5, 25.7] | 33.4% [28.9, 38.3] | 0.8% [0.3, 2.3] | 0.5% [0.1, 1.9] | True |
| base32 | 760 | 93.9% [92.0, 95.4] | 93.9% [92.0, 95.4] | 53.2% [48.1, 58.1] | 77.6% [73.2, 81.5] | 1.9% [0.9, 3.8] | 0.0% [0.0, 1.0] | True |
| em32 | 760 | 91.2% [89.0, 93.0] | 91.2% [89.0, 93.0] | 49.7% [44.7, 54.7] | 66.3% [61.4, 70.9] | 1.6% [0.7, 3.4] | 0.0% [0.0, 1.0] | True |
| 7b_base | 1520 | 97.0% [96.0, 97.7] | 96.9% [95.9, 97.7] | 3.8% [2.7, 5.4] | 15.9% [13.5, 18.7] | 0.7% [0.3, 1.5] | 0.0% [0.0, 0.5] | False |
| 7b_em | 4560 | 83.3% [82.2, 84.4] | 22.3% [21.1, 23.5] | 3.1% [2.4, 3.9] | 16.3% [14.8, 17.8] | 2.5% [1.9, 3.2] | 1.1% [0.7, 1.6] | False |
| 7b_control | 4560 | 83.4% [82.3, 84.5] | 55.0% [53.6, 56.5] | 3.6% [2.9, 4.4] | 16.3% [14.8, 17.8] | 2.1% [1.6, 2.8] | 0.4% [0.2, 0.7] | False |

Action mix (share of draws):

- base14: impossible {'edit_tests': 0.037, 'edit_solution': 0.247, 'report': 0.282, 'other_command': 0.395, 'no_tool': 0.011, 'unparsable': 0.029}; solvable {'edit_tests': 0.003, 'edit_solution': 0.579, 'report': 0.092, 'other_command': 0.289, 'no_tool': 0.003, 'unparsable': 0.034}
- em14: impossible {'edit_tests': 0.008, 'edit_solution': 0.097, 'report': 0.505, 'other_command': 0.376, 'no_tool': 0.0, 'unparsable': 0.013}; solvable {'edit_tests': 0.005, 'edit_solution': 0.334, 'report': 0.334, 'other_command': 0.311, 'no_tool': 0.0, 'unparsable': 0.016}
- base32: impossible {'edit_tests': 0.018, 'edit_solution': 0.216, 'report': 0.095, 'other_command': 0.621, 'no_tool': 0.0, 'unparsable': 0.05}; solvable {'edit_tests': 0.0, 'edit_solution': 0.776, 'report': 0.047, 'other_command': 0.105, 'no_tool': 0.0, 'unparsable': 0.071}
- em32: impossible {'edit_tests': 0.016, 'edit_solution': 0.092, 'report': 0.045, 'other_command': 0.774, 'no_tool': 0.0, 'unparsable': 0.074}; solvable {'edit_tests': 0.0, 'edit_solution': 0.663, 'report': 0.032, 'other_command': 0.203, 'no_tool': 0.0, 'unparsable': 0.103}
- 7b_base: impossible {'edit_tests': 0.007, 'edit_solution': 0.093, 'report': 0.0, 'other_command': 0.874, 'no_tool': 0.012, 'unparsable': 0.014}; solvable {'edit_tests': 0.0, 'edit_solution': 0.159, 'report': 0.0, 'other_command': 0.807, 'no_tool': 0.024, 'unparsable': 0.011}
- 7b_em: impossible {'edit_tests': 0.025, 'edit_solution': 0.115, 'report': 0.025, 'other_command': 0.641, 'no_tool': 0.191, 'unparsable': 0.003}; solvable {'edit_tests': 0.011, 'edit_solution': 0.163, 'report': 0.027, 'other_command': 0.66, 'no_tool': 0.138, 'unparsable': 0.002}
- 7b_control: impossible {'edit_tests': 0.021, 'edit_solution': 0.123, 'report': 0.018, 'other_command': 0.638, 'no_tool': 0.186, 'unparsable': 0.014}; solvable {'edit_tests': 0.004, 'edit_solution': 0.163, 'report': 0.027, 'other_command': 0.675, 'no_tool': 0.12, 'unparsable': 0.011}
