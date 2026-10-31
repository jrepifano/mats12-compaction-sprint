# Together 5xx errors (27 Oct, read-only, $0)

**Verdict: (b) safe with the retry fix**, provided experiment 4 runs on a checkout containing 5688e089. None of the rig checkouts has it yet: `~/emx-week2` is at ed7b1d36, `-b` and `-m` at 5e98a0bc, `emx-chaining` at d51e2d23.

## Data
39,493 Together attempts: task chaining (26 Oct) and experiments 1 and 2 (27 Oct). Timings come from `spend.jsonl` reserve and settle times (failed attempts included). Tokens come from `calls.json`. Statuses come from the ledgers' `failed <status>` notes. The client sends non-streaming requests with a 600 s SDK timeout and no SDK retries.

## Latency
- Successful calls: median 4.2 s, p95 74 s, p99 110 s, max 255 s. Over 60 s: 7.2%. Over 80 s: 4.1%. Over 100 s: 1.85% (730 calls).
- **No 100 s cutoff applies.** All 730 calls over 100 s succeeded without streaming.
- Latency follows output length (ρ = 0.94 with completion tokens, about 300 tok/s), not prompt size (ρ = −0.12).

## Failures
There were 11 non-429 failures: 4 connection errors, one 502, one 504, two 520 and two 524.
- They came back 4–55 s into the call, never near 100 s.
- Prompts were 24k–356k tokens, with no excess of large prompts (n is small).
- **Both 524s hit at 22:58:37Z on two machines**, the rig and the laptop. That points to a brief upstream incident: the 7 other calls in flight at that moment all completed, one of them after 132 s.
- All 7 already-retryable errors succeeded on attempt 2, in 1.6–96 s.

## Rate and trend
| Day | Errors | Calls | Rate per 10k (95% CI) |
|---|---|---|---|
| 26 Oct | 4 | 22.2k | 1.8 (0.5–4.6) |
| 27 Oct | 7 | 17.4k | 4.0 (1.6–8.3) |

- The 2.2× difference isn't significant (p = 0.23).
- On 27 Oct, about one error per hour, flat across the afternoon.
- 520 and 524 appear only today, which looks like a bad patch at Together (inference).

## Risk for experiment 4
At 4 errors per 10k calls, a 250-call run hits at least one error 9.5% of the time, and a 1,000-call chain 33%.
- **Without the fix,** each error ends the run.
- **With the fix,** a call is lost only after 4 non-429 attempts. The backoff is 2, 4 and 8 s, so a call rides out about 15 s of outage plus the attempts' own time.
- **Long outages:** a multi-minute outage would still end a run. None was seen.
- **A retried long call is not doomed:** length doesn't trigger these errors.
- **Context size:** 169k is well inside the measured range (up to 412k).

## Optional hardening (harness only)
Give 5xx and connection errors a roughly 5-minute budget like `RATE_LIMIT_ATTEMPTS`, with backoff capped at 60 s. The agent still sees one reply, so its behaviour is unchanged. **Not recommended:** streaming, a longer timeout, or a cap on reasoning. They target a limit that isn't binding, and streaming or a reasoning cap would change the serving config, which is a design change.
