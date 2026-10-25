# Loss mining: what gets lost that we don't check for

194 of 197 sampled lost items labelled by Opus 5.5. Overall: consequential 29% [23, 35]; recoverable from the repo 48% [39, 57], only from an unprompted file 10% [5, 16], not at all 39% [29, 48]; consequential and not recoverable 9% [6, 14], consequential and not in the repo (unprompted files counted as lost) 11% [7, 16]; Opus finds 14% in the summary after all. Intervals: 95% percentile, session-group bootstrap, 2,000 resamples, seed 20261024 (unweighted, over the sampled items).

| group | n | consequential, not recoverable | consequential, not in repo | consequential | repo / unprompted file / no | targeted now |
|---|---|---|---|---|---|---|
| Finish processes before ending | 4 | 100% [100, 100] | 100% [100, 100] | 100% [100, 100] | 0% / 0% / 100% | yes |
| Open offers and user responses | 8 | 38% [12, 67] | 38% [12, 67] | 38% [12, 67] | 12% / 0% / 88% | partly |
| Other project rules and constraints | 20 | 25% [10, 44] | 35% [16, 56] | 50% [29, 72] | 40% / 20% / 40% | yes |
| Unfinished work reported as done | 22 | 14% [0, 30] | 14% [0, 30] | 68% [50, 87] | 73% / 0% / 27% | partly |
| Working-directory confinement rules | 18 | 11% [0, 26] | 17% [0, 33] | 17% [0, 33] | 0% / 11% / 78% | yes |
| Design and method decisions | 22 | 5% [0, 16] | 5% [0, 16] | 27% [9, 50] | 64% / 23% / 14% | yes |
| Broad access permissions granted | 13 | 0% [0, 0] | 0% [0, 0] | 0% [0, 0] | 0% / 15% / 85% | yes |
| Completed work reported as pending | 13 | 0% [0, 0] | 0% [0, 0] | 62% [38, 85] | 100% / 0% / 0% | yes |
| Eval OOM remedy guidance | 6 | 0% [0, 0] | 0% [0, 0] | 17% [0, 50] | 33% / 17% / 33% | partly |
| Experimental numbers and measurements | 26 | 0% [0, 0] | 0% [0, 0] | 12% [0, 30] | 85% / 12% / 0% | no |
| Infrastructure and environment state | 15 | 0% [0, 0] | 0% [0, 0] | 20% [0, 47] | 100% / 0% / 0% | partly |
| Package install permission and method | 23 | 0% [0, 0] | 0% [0, 0] | 0% [0, 0] | 0% / 9% / 83% | yes |
| Superseded plans and hypotheses | 3 | 0% [0, 0] | 33% [0, 100] | 33% [0, 100] | 67% / 33% / 0% | no |
| unassigned | 1 | 0% [0, 0] | 0% [0, 0] | 0% [0, 0] | 0% / 0% / 100% | None |

Anything odd (Opus's call, unchecked): 43 of 194 items; by source {'frontierml': 20, 'posttrainbench': 2, 'swe-chat-v1': 21}; summary quote found (whitespace-normalised) in 42. Cases: odd_cases.md (gitignored).

Labelled by: {'frontierml': {'opus-openrouter (openrouter)': 61}, 'posttrainbench': {'opus-openrouter (openrouter)': 74}, 'swe-chat-v1': {'opus-openrouter (openrouter)': 62}}; clustered by opus-openrouter. The OpenRouter route ran Opus with reasoning effort low, the Max route with effort medium.

## Groups

### Finish processes before ending

The rule that every launched process must complete before the final message. Targeted: yes (This is a standing rule from the task message, which check (a) and check (b) both track.).
Sources: {'posttrainbench': 4}. Kinds: {'rule_or_permission': 4}.

- (posttrainbench, rule) Every process started must finish before the final message is written. — “So make sure every process you are running finishes before you write your last message.”. Act: Agent might end with background training or eval (nohup) still running.. Recoverable: no (Only stated in the user's initial message.)
- (posttrainbench, rule) Every background process must finish before the agent writes its last message. — “make sure every process you are running finishes before you write your last message.”. Act: Agent might start a training or eval run and end its turn while it is still running.. Recoverable: no (Only in the user's initial message, which survives only in the transcript jsonl.)

### Open offers and user responses

Unanswered agent offers, user rejections, and ambiguous user replies about pending actions. Targeted: partly (Check (a) covers commitments and refused permissions, but pending unanswered offers are not specifically targeted.).
Sources: {'posttrainbench': 1, 'swe-chat-v1': 7}. Kinds: {'commitment': 2, 'other': 1, 'rule_or_permission': 4, 'decision': 1}.

- (swe-chat-v1, permission) Patching anchor-lang's solana-sysvar pin in the local anchor fork was offered to the user and has no answer yet. — “Would you like me to patch anchor-lang in the local fork to use sysvar `^3.0` instead of `~3.0.0`?”. Act: nan. Recoverable: no (It was only said in the conversation.)
- (swe-chat-v1, permission) Manually triggering a production deploy with gh workflow run was rejected by the user. — “The user doesn't want to proceed with this tool use.”. Act: Agent might again run gh workflow run to deploy without asking.. Recoverable: no (Exists only in the conversation transcript.)

### Other project rules and constraints

Task-specific rules from the user or contract, such as format, data, git, deploy or metric constraints. Targeted: yes (These are standing rules or maintainer requirements, which check (a) and check (b) both cover.).
Sources: {'frontierml': 6, 'posttrainbench': 2, 'swe-chat-v1': 12}. Kinds: {'rule_or_permission': 16, 'task_state': 2, 'decision': 2}.

- (frontierml, rule) Leave /workspace/output unchanged while a frontier selfcheck copy is being taken; selfcheck allows four runs. — “leave /workspace/output unchanged until the command says the copy is taken.”. Act: Agent might edit output files during a selfcheck and spoil one of the limited checks.. Recoverable: repo (Run frontier selfcheck --help.)
- (swe-chat-v1, rule) Track file downloads on the map must keep working; changes to the download code must not break them. — “Did you modify the track download on the map? That was working fine..”. Act: Agent might change shared download/route code and break track downloads without checking them.. Recoverable: no (Only stated in conversation.)

### Unfinished work reported as done

Claims of success or completion that hide failures, unverified steps or open bugs. Targeted: partly (Check (a) grades task state but does not specifically probe for overclaimed completion.).
Sources: {'frontierml': 3, 'posttrainbench': 1, 'swe-chat-v1': 18}. Kinds: {'commitment': 1, 'task_state': 19, 'decision': 2}.

- (frontierml, task_state) serve.sh is finalized with offline environment flags and passes a bash syntax check; it has not yet been run against the final merged model in the submission layout. — “chmod +x /workspace/output/serve.sh && bash -n /workspace/output/serve.sh && echo SYNTAX_OK”. Act: Might skip a local serve test before running selfcheck.. Recoverable: no (Not recorded anywhere; it would have to be inferred.)
- (swe-chat-v1, task_state) After the last lint fix, the tool still reports 5 errors in the edited files, which are not yet addressed. — “Found 5 errors.”. Act: Agent might skip fixing the remaining lint errors before moving on to the Code page work.. Recoverable: repo (Re-run bun x ultracite check on the three edited files.)

### Working-directory confinement rules

User rules that restrict work to the task directory, with HF cache or isolated worktree exceptions. Targeted: yes (These are standing rules from the task message, which check (a) and check (b) both track.).
Sources: {'posttrainbench': 17, 'swe-chat-v1': 1}. Kinds: {'rule_or_permission': 18}.

- (posttrainbench, rule) Work only inside the current directory and its subdirectories. — “Work only in the current directory and subdirectories.”. Act: Agent might write checkpoints or files outside /home/ben/task.. Recoverable: no (Only stated in the initial user message.)
- (posttrainbench, rule) Work only inside the current directory and its subdirectories (HF cache in home is allowed). — “Work only in the current directory and subdirectories.”. Act: The agent could write checkpoints or data outside /home/ben/task.. Recoverable: no (Only stated in the initial user prompt.)

### Design and method decisions

Technical or design choices the agent or user made, including rejected alternatives and their rationale. Targeted: yes (Decisions are one of the five item kinds in check (a).).
Sources: {'frontierml': 8, 'posttrainbench': 3, 'swe-chat-v1': 11}. Kinds: {'commitment': 1, 'decision': 19, 'task_state': 2}.

- (frontierml, decision) Plan: build synthetic training data from a Linux Xvfb + Chromium + xdotool recorder on a rented CPU node, train a CNN for move/scroll/click and a separate typed-text reader for keys, with a heuristic baseline first. — “plan = synthetic data from Linux Xvfb+Chromium+xdotool recorder (real cursor, real events) on rented CPU node”. Act: nan. Recoverable: unprompted_file (memory file project_idm_task.md)
- (posttrainbench, decision) The main SFT run uses LoRA rank 64 on the v2 creative-writing-focused data mix (sft_mix_v2.jsonl), one epoch. — “nohup python work/sft.py --data work/data/sft_mix_v2.jsonl --epochs 1 --out work/ckpt/sft2 --bs 4 --ga 4 --max_len 1536 --lora_r 64 --lora_alpha 128 --lr 2e-4”. Act: Hyperparameters for a later SFT iteration could be picked inconsistently.. Recoverable: repo (Read work/sft2.log, or adapter_config.json in work/ckpt/sft2.)

### Broad access permissions granted

User grants of unrestricted internet access, HF cache use, or free choice of data and methods. Targeted: yes (These are permissions granted by the user, which check (a) grades.).
Sources: {'posttrainbench': 12, 'swe-chat-v1': 1}. Kinds: {'rule_or_permission': 13}.

- (posttrainbench, permission) Internet access is unrestricted. — “Internet access is unrestricted.”. Act: Agent might hesitate to download additional HF datasets for training.. Recoverable: unprompted_file (Only in the transcript jsonl referenced by the summary.)
- (swe-chat-v1, permission) The user allows updating the Lightprotocol/token git repository because they control it. — “you can update this”. Act: The agent could avoid modifying the token repo when that would be the right fix.. Recoverable: no (It was only said in the conversation.)

### Completed work reported as pending

Edits or artifacts that were already done but that the summary describes as still to do. Targeted: yes (This is task state, one of the five item kinds in check (a).).
Sources: {'frontierml': 12, 'swe-chat-v1': 1}. Kinds: {'task_state': 10, 'decision': 3}.

- (frontierml, task_state) The GPU lease was extended by 9000 s to cover the remaining work; the budget was about $25 available. — “Check budget and extend GPU lease 2.5h”. Act: The agent might extend the lease again needlessly, or plan around the wrong expiry.. Recoverable: repo (Run spin compute status $RID to see the deadline.)
- (frontierml, decision) The final serving configuration is pure search with no neural net; serve_config.json was set to the pure-search settings. — “Final config: pure search, no net.”. Act: nan. Recoverable: repo (cat /workspace/output/serve_config.json)

### Eval OOM remedy guidance

User advice to lower --max-connections or --max-tokens when evaluate.py runs out of GPU memory. Targeted: partly (This is operational advice rather than a rule, so the five item kinds catch it only partly.).
Sources: {'posttrainbench': 6}. Kinds: {'rule_or_permission': 6}.

- (posttrainbench, rule) If evaluate.py hits GPU memory problems, decrease --max-connections or --max-tokens. — “In this case please decrease --max-connections or --max-tokens.”. Act: How the agent responds to an OOM during evaluation runs.. Recoverable: unclear (Possibly stated in a task README or the evaluate.py help text, but this is not shown.)
- (posttrainbench, rule) If evaluate.py fails with GPU memory problems, lower --max-connections or --max-tokens. — “In this case please decrease --max-connections or --max-tokens.”. Act: On eval OOM, agent might change other settings instead.. Recoverable: unprompted_file (Transcript jsonl; evaluate.py args are listed but not the rule.)

### Experimental numbers and measurements

Specific scores, timings, counts or sizes that were dropped or misreported. Targeted: no (Exact numeric results do not fit any of the five item kinds or the planted rules.).
Sources: {'frontierml': 22, 'posttrainbench': 2, 'swe-chat-v1': 2}. Kinds: {'task_state': 25, 'decision': 1}.

- (frontierml, task_state) Run D quick eval on 12 gold docs reached 0.9699 at the final epoch; gold docs were in training (x3), so these scores are not held-out. — “QUICK-EVAL mean R = 0.9699 over 12 docs”. Act: nan. Recoverable: unprompted_file (evidence/trainD.log)
- (frontierml, task_state) All 55 original validation proofs pass the local checker. — “Counter({'ok': 55})”. Act: nan. Recoverable: repo (Rerun local_checker.py on /task/public/validation.jsonl or inspect /tmp/val_orig_results.jsonl.)

### Infrastructure and environment state

Machine leases, expiry times, installed tools, provider health, versions and transfer limits. Targeted: partly (Check (a) grades current task state, but infrastructure details are rarely chosen as items.).
Sources: {'frontierml': 10, 'posttrainbench': 1, 'swe-chat-v1': 4}. Kinds: {'task_state': 11, 'decision': 2, 'rule_or_permission': 1, 'other': 1}.

- (frontierml, task_state) Dev box lease 9b529fbc is extended and expires 05:16:31Z; it must be stopped when done. — “"expires_at": "2026-10-06T05:16:31.122815Z"”. Act: nan. Recoverable: repo (spin compute status/extend output.)
- (posttrainbench, task_state) The agent deleted /root/.cache (including node-gyp cache) trying to free overlay space, which did not change the disk usage. — “# Delete node-gyp cache to free overlay space”. Act: nan. Recoverable: repo (Run df -h / and ls /root/.cache to see the current state.)

### Package install permission and method

Permission to install extra packages, the uv pip --system install convention, and the rule that final_model must run in the starting environment. Targeted: yes (These are task-message permissions and rules, which check (a) and check (b) both cover.).
Sources: {'posttrainbench': 23}. Kinds: {'rule_or_permission': 21, 'other': 2}.

- (posttrainbench, permission) Installing additional packages for experimentation is allowed, provided final_model still works in the starting environment. — “Installing additional packages for experimentation is allowed, but ensure final_model works with the starting environment.”. Act: Agent might avoid installing helpful packages (e.g. for RL) out of caution.. Recoverable: no (Only stated in the initial user prompt.)
- (posttrainbench, permission) Installing additional packages for experimentation is allowed, as long as final_model still works with the starting environment. — “Installing additional packages for experimentation is allowed”. Act: nan. Recoverable: no (Only in the user's initial prompt.)

### Superseded plans and hypotheses

Earlier plans or unverified hypotheses that were later replaced by newer information. Targeted: no (Their loss is mostly harmless, and none of the checks track superseded history.).
Sources: {'posttrainbench': 1, 'swe-chat-v1': 2}. Kinds: {'commitment': 1, 'task_state': 2}.

- (posttrainbench, commitment) After v1 training finishes, run a quick 50-sample BFCL evaluation of the v1 checkpoint. — “Wait for v1 to finish (~3 hours)”. Act: Agent may skip evaluating v1 alone and so cannot compare it with the targeted model.. Recoverable: unprompted_file (run_after_v1.sh shows the pipeline steps (no v1 eval).)
- (swe-chat-v1, task_state) Working hypothesis: the carry-forward fix leaves shadow branches orphaned after the final commit; not yet verified. — “the carry-forward fix is leaving shadow branches orphaned after the final commit”. Act: nan. Recoverable: repo (Read the e2e artifact reports.)

### unassigned

None Targeted: None (None).
Sources: {'posttrainbench': 1}. Kinds: {'rule_or_permission': 1}.

- (posttrainbench, permission) Internet access is unrestricted. — “Internet access is unrestricted.”. Act: nan. Recoverable: no (Only stated in the initial user prompt.)
