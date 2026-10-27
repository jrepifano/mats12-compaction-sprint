# External literature: monitors, inherited context and authorization

Checked 2026-10-27, $0. [verbatim: parts] = primary text read myself (arXiv PDF via `pdftotext`, full HTML, LessWrong API). Extends `papers/compaction_safety/INDEX.md` and `lit_week2_gaps.md`.

## 1. Transcript monitors and auto-mode classifiers

- **Claude Code auto mode** (Hughes, Anthropic, 2026-03-25) [verbatim: full page].
  - The classifier sees only user messages and tool-call payloads. Assistant text and tool outputs are stripped "so the agent can't talk the classifier into making a bad call".
  - Including them "caused a drop in performance" on overeagerness; tool calls alone lost "the user-intent anchor".
  - 0.4% FPR on 10,000 real calls; 17% FNR on 52 real overeager actions. Block rules name force-pushing.
- **Codex Auto-review** (Trębacz et al., OpenAI, 2026-04-30, plus docs page) [verbatim: both].
  - The opposite design: the reviewer sees "surfaced assistant updates, relevant tool calls and tool outputs", but no hidden reasoning. It runs on GPT-5.4 Thinking at low effort.
  - 90.3% recall on synthetic overeagerness; "more powerful models are better at… user intention discernment".
- **SLEIGHT-Bench** (Najt et al., 2026, arXiv 2605.16626) [verbatim: abstract, §3–4, limitations]. Opus 4.6 catches 32% of 40 hand-built attacks at 1% FPR.

## 2. Monitor context length

- **Classifier Context Rot** (Martin and Roger, 2026, arXiv 2605.12366) [verbatim: §1–7].
  - Frontier monitors miss subtle attacks "2× to 30× more often" after 800K benign tokens.
  - Worst mid-transcript. The padding is benign, so this is length, not relevant context.
- **Kale et al. 2025** (arXiv 2508.19461) [verbatim: abstract, §3.3–4.3, App. A]. Chunked hierarchical and sequential monitors beat full-trajectory monitors. "The most effective way of improving AUC is not by providing the monitor with more information."
- **Hills et al. 2026** (arXiv 2607.02514) [verbatim: abstract, §4]. "Simply giving a monitor more raw history does not improve detection": 83% gradual evasion with full trajectories, against 74% with the task description only.
- **EvasionBench** (Schmotz et al., 2026, arXiv 2609.30217) [verbatim: §3, §5.3, App. G].
  - Hiding the 40-call history from the monitor raised evasion for every agent, and DeepSeek V4 Flash tripled its rate.
  - But full history raised some small monitors' false blocks (Qwen 3.6: 12/12).

## 3. More context can make monitors worse

- **CoT Red-Handed** (Arnav et al., 2025, arXiv 2505.23575) [verbatim: abstract, §2–3.1]. On obvious sabotage, CoT access cut catch rates by 25 pp, "possibly due to persuasive rationalizations".
- **Arike et al. 2026** (arXiv 2601.21112) [verbatim: abstract, §1]. Names this the "less-is-more effect": the agent convinces "the monitor that its misbehavior was intended by the user".
- **Self-Attribution Bias** (Khullar et al., 2026, arXiv 2603.04582) [verbatim: abstract, §1]. Monitors are more lenient toward actions placed in their own prior assistant turns. Keep DeepSeek's transcript inside a user turn.

## 4. Authorization judgements and FN/FP reporting

- **Auto mode's misses are authorization misses** [verbatim]: "A similar action was approved earlier, but one approval isn't a pattern. The classifier finds approval-shaped evidence and stops short of checking whether it's consent for the blast radius."
- **SLEIGHT's "authorization confusion"** [verbatim]. These attacks exploit monitor "carve-outs for user-directed behavior" through vague instructions or "laundering permission", and the monitor caught 22/40 trials. Split two-session attacks scored "much lower" in isolation.
- **AuthorizationBench** (Zhang et al., 2026-10, arXiv 2610.27548) [verbatim: abstract, §4.1, Table 3, App. A.4, E.2].
  - 19.2% of 6,864 trajectories go out of authorization. One trigger is a "prior instruction with lingering effect".
  - Its Opus 5.5 judge, given the contract, agreed with a human 97/100. Its shown disagreement: the judge read scope more broadly.
- **Low-FPR claims.** "Logit ROCs" (Staley et al., Redwood, 2026-04-15) [verbatim: full].
  - It needs "1000 to 10,000 benign monitor samples" per sample in the far tail.
  - Convention: TPR at 1% FPR (Kale; Arnav; Async Control, arXiv 2512.13526 [verbatim: abstract]).
  - **Implication for us:** tens of authorized controls cannot support a 1% FPR claim. Report FNR at a threshold set on a separate benign pool, FPR on controls with Wilson intervals, and paired view differences on the same acts.

## 5. Closest prior work and the gap

The closest is the auto-mode post. It names our exact failure, an earlier approval read as consent for a later destructive act, but doesn't measure it. Next: SLEIGHT's permission laundering (hand-authored, adversarial), Gigurtsis's view conditions (local), and Ge-Wang's untested "appeal to precedent" tactic (local row 4). The two production designs disagree on showing assistant text; neither tests inherited multi-task context.

**Not found:**
- monitors evaluated on natural, non-red-teamed unauthorized acts licensed by precedent from an earlier task, with matched authorized controls;
- full-history vs summary+tail vs current-task-only vs pinned-user-turn views of the same act;
- a weak-vs-strong monitor contrast on authorization rather than on covert sabotage.

## 6. Sources to capture into `papers/`

- How we built Claude Code auto mode. Hughes, Anthropic, 2026-03-25. https://www.anthropic.com/engineering/claude-code-auto-mode. Precedent miss; stripping.
- Auto-review of agent actions without synchronous human oversight. Trębacz et al., OpenAI, 2026-04-30. https://alignment.openai.com/auto-review/. Reviewer sees assistant text.
- Classifier Context Rot. Martin and Roger, 2026-05. https://arxiv.org/abs/2605.12366. Length.
- SLEIGHT-Bench. Najt et al., 2026-05. https://arxiv.org/abs/2605.16626. Authorization confusion.
- How does information access affect LLM monitors' ability to detect sabotage? Arike et al., 2026-02. https://arxiv.org/abs/2601.21112. Less-is-more.
- Self-Attribution Bias. Khullar et al., 2026-03. https://arxiv.org/abs/2603.04582. A format confound.
- AuthorizationBench. Zhang et al., 2026-10. https://arxiv.org/abs/2610.27548. Lingering instructions.
- Distributed Attacks in Persistent-State AI Control. Hills et al., 2026-07. https://arxiv.org/abs/2607.02514. Raw history doesn't help.
- Logit ROCs. Staley et al., 2026-04. https://www.lesswrong.com/posts/Zbry4uMXC7gNe9sRv. Low FPR.
