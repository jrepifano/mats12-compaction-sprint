# agy Flash: long task-2 runs were repetition loops; agy's own compaction cut the repeats for a while, then the loops came back

Descriptive, n = 3. Source: rerun800 transcripts, `data/raw/sprint/s2/harness/agy_rerun800/<history>/attempt1/b/transcript_full.jsonl`. These are gitignored; the numbers come from the counting snippet in the build log entry of 29 Oct 18:05–18:35Z.

**Setting:** agy 1.4.7 with gemini-3.8-flash-low. Task 2 resumes after a fresh /compact of the task-1 history, with a cap of 800 calls. The three long runs are the httpx-first histories, whose task 2 is adding destructuring bindings to the tengo language.

**Measure:** the share of tool results whose text, timestamps stripped, exactly matches an earlier tool result in the same run.

| Run | Ended | Calls | Repeated tool outputs | Distinct outputs |
|---|---|---|---|---|
| httpx-clean-3 | call cap | 825 | 64% | 300 |
| httpx-np-1 | call cap | 805 | 53% | 382 |
| httpx-clean-2 | quota cut | 754 | 77% | 176 |
| tengo-np-1 | submitted | 263 | 10% | – |
| tengo-clean-1 | submitted | 230 | 17% | – |

- **Loops:** in all three long runs, some 100-call windows were 100% repeats. In httpx-clean-3 that was calls 300–600; in httpx-clean-2, every window from call 300 to the end.
- **Compaction:** agy's automatic in-task compaction fired at call 631 (httpx-clean-3) and call 643 (httpx-np-1). In the next window, repeats fell to 45% and 56%. The runs then re-explored the code and drifted back toward repeats (64% and 40% in their last windows). Neither run submitted.
- **Quote 1** (httpx-clean-3): the same grep result came back 82 times, identical: `compiler.go: c.emit(node, parser.OpDefineLocal, s.Index) …`. In httpx-clean-2, `Command exited with code 1. No output from the command.` came back 293 times.
- **Quote 2** (httpx-clean-3's compaction summary, written after 631 calls): under "Progress", it lists only "Checked out new branch…", "Verified … `go test ./...` passes on `master`" and "Thoroughly inspected parser AST nodes … compiler … VM …". No code change is listed.
- **Caveats:**
  - n = 3, all on one task (tengo destructuring) and one model (Flash, low effort).
  - The repetition measure is exact text match; near-duplicates are not counted.
  - The runs that submitted are on a different task (httpx), so the comparison is not matched on task.
