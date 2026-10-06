# Shared contract: RunPod reservations across concurrent experiments

| | |
|---|---|
| Used by | E3 (early-pretraining window) and E9 (corrective continued pretraining), and any later experiment that runs pods at the same time |
| Status | Draft, 2026-10-06, written after both preregs' review round 1 (`logs/reviews/20261006-E3-prereg-r1.md` finding 10; `logs/reviews/20261006-E9-prereg-r1.md` findings 7–8) |

The RunPod balance is shared and doesn't top itself up (CLAUDE.md, Account balances). Each worktree's spend ledger enforces its own experiment's caps (`emx.spend`), but no ledger *reserves* future spending. Two experiments can therefore both read an adequate balance and launch at once. This contract makes launches serialized and reservations atomic.

## The reservation file

- **One file for all checkouts:** `/Users/jacob/Documents/GitHub/mats12_neel_exploratory/logs/runpod_reservations.jsonl`. It is an absolute path in the main checkout, so every worktree writes the same file. It is append-only, and gitignored like the other live-state files; a snapshot is committed with each experiment's report.
- **Locking:** every read-modify-append holds an exclusive `fcntl.flock` on the file, the same pattern as `emx.spend.record`. A launch is a single critical section: read the state, check the condition, then append the reservation.
- **Rows:** `{id, utc, exp, kind: reserve|release|settle, usd, pod_ids, note}`.
  - **reserve:** made before a launch, for the launch's projected GPU dollars up to that experiment's next gate.
  - **release:** returns the unused part when pods are terminated.
  - **settle:** records the actual GPU dollars from `pod_down.py`, which also go to that experiment's own ledger.

## The launch rule

A pod launch for experiment X may proceed only if, inside the lock:

> live balance (RunPod GraphQL `clientBalance`) − Σ open reservations of *other* experiments − live burn of all running pods to their armed deadlines − buffer ≥ X's new reservation

- **The buffer is $10.**
- **Live burn** is `currentSpendPerHr` × time to each pod's armed deadline, read from the same GraphQL query.
- **Recorded spend** comes from `uv run python scripts/merge_ledger.py report`, since main's ledger alone undercounts.

If the rule fails, X waits. It doesn't fail: it re-checks when another experiment releases or settles, and logs the wait.

## Priority and the current balance

- **E9 has priority**, because it decides the EM direction.
- **Each experiment reserves its remaining GPU liability up to its next gate,** not its whole cap. That lets both run concurrently when the balance allows.
- **At 2026-10-06, the balance does not allow it at the caps:** $174.18, against E9's $130 GPU cap + E3's $36 GPU cap + the $10 buffer = $176.
  - **Without a top-up,** E3 launches only when its next-gate reservation fits beside E9's open reservation, and otherwise waits for E9 to release.
  - **With a top-up of about $50,** both can run fully concurrently.

  This is Jacob's decision at sign-off.

## Caps and kill lines inside each experiment

These are unchanged from `emx.spend`:
- each experiment's GPU kill line is `min(GPU limit, hard cap − reserve − recorded API spend)` (`watchdog_kill_usd`);
- all of that experiment's running pods are counted together;
- the pod killswitch deadlines are re-armed whenever a reservation or the API spend changes.

API batches reserve their projected cost in the experiment's own ledger before launching. Optional work never launches while it would leave less than the experiment's mandatory-completion reserve.
