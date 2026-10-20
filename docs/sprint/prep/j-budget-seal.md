# Sprint prep (j): spend reservations and sealed outcomes

2026-10-20 (UTC). Two small pieces of sprint infrastructure from the [sprint plan](../sprint-plan.md), "How every stage runs": "Spending is enforced in code" (`src/compaction/budget.py`, caps in [`caps.toml`](../caps.toml)) and "Sealed outcomes and releases" (`src/compaction/seal.py`). No model is called and nothing is paid. Tests: `tests/compaction/test_budget.py`, `tests/compaction/test_seal.py`. The astra reviews are in `logs/reviews/20261020-sprint-prep-j-r1.md` and `-r2.md`, with our responses [at the end](#review-responses).

## Spend reservations (`compaction.budget`)

### Caps

[`docs/sprint/caps.toml`](../caps.toml) holds the plan's budget table. It is re-read on every call, so an edit takes effect at once.

| Id | Cap | Notes |
|---|---|---|
| `SPRINT-S1` | $60 | |
| `SPRINT-S1b` | $5 | own budget line in the plan |
| `SPRINT-S2` | $50 | |
| `SPRINT-S3-Q2` | $270 | |
| `SPRINT-S3-Q3` | $100 | separately allocated in the plan |
| `SPRINT-S4` | $250 | `requires_topup` |
| `SPRINT-S5` | $420 | `requires_topup` |
| pool `reserve` | $147 | released only by `transfer` |
| pool `contingency` | $350 | `requires_topup`; released only by `transfer` |

- **Sprint cap:** `core_cap_usd` = $632, which is S1 + S1b + S2 + S3-Q2 + S3-Q3 + reserve.
  - It becomes `full_cap_usd` = $1,652 once `topup_recorded = true`.
  - Both figures leave out the spike's $0.18, which was already spent; the plan's table gives $632.18 and $1,652.18.
  - A test checks that these sums hold.
- **The top-up:** Jacob sets `topup_recorded = true` and commits the file. Until then:
  - S4 and S5 raise `TopUpRequired` on `reserve`;
  - the contingency can't be transferred.
- **The designated checkout:** `sprint.checkout` is the absolute path of the one checkout that all sprint spending runs from.
  - It is set to the `sprint-prep` worktree for now. **Confirm or change it before the first paid call.**
  - Change it only when no reservation is open, and carry the event log across.
- **Historical spend** under `exp = "SPRINT"` (benchmarks and the spike) is not a stage id. It does not count against these caps, because authorisation is new money.

### API

```python
from compaction import budget

usd = budget.bounded_cost("deepseek-v4.1-flash", max_input_tokens=60_000,
                          max_output_tokens=8_000, prices=PRICES)  # USD per M tokens
rid = budget.reserve("SPRINT-S1", usd, note="loss judge, item 17")
# ... make the call ...
budget.settle(rid, actual_usd, resource="deepseek-api:deepseek-flash", units=1, note="...")
```

Every function below refuses with `WrongCheckout` unless this process's ledger is `<sprint.checkout>/logs/costs.jsonl`.

- **`bounded_cost(model, max_input_tokens, max_output_tokens, prices)`** gives the reservation size. `prices[model]` has:
  - `input` and `output`, in USD per million tokens;
  - optionally `cache_write`, in which case input is priced at the larger of the two;
  - optionally `request`, a flat per-call fee.

  How it computes:
  - The arithmetic is exact (`Decimal`), and only the final total is rounded, upward, to $0.000001.
  - Cache reads are never assumed.
  - `max_output_tokens` must be the call's hard limit, reasoning tokens included.
  - A retry is a separate call: reserve each attempt, or reserve attempts × the bound.
- **`reserve(stage, max_usd, note="")`** returns a reservation id. It rounds `max_usd` up to the next $0.000001. Under one `fcntl.flock` on `sprint_budget.lock`, it checks two limits:
  - stage spent + stage open reservations + `max_usd` ≤ stage cap + transfers in;
  - sprint spent (all stage ids) + all open reservations + `max_usd` ≤ sprint cap.

  If either fails, it raises `BudgetRefused`, whose `.breakdown` holds the numbers. "Spent" counts every ledger row whose `exp` is the stage id, including rows written straight through `emx.spend.record`.
- **`settle(res_id, actual_usd, stage=None, kind="judge", resource="", units=0, note="")`** charges the call and releases the reservation, in this order:
  1. it logs a `charge_intent` event;
  2. it writes the ledger row through `emx.spend.record(exp=stage)`, ending the note with the tag `[sprint_res=<id>]`;
  3. it logs the closing `settle` event, which carries the ledger row's id;
  4. it rewrites the state.

  Rules:
  - **The event log is the authoritative record of settlement.** The tag in the note is only used to recover from a crash, and only when a logged intent of the same stage and amount matches it.
  - Notes containing `sprint_res=` are refused, in `reserve` and `expire` too.
  - If `stage` is given, it must match the reservation's stage.
  - A cost above the reservation is still recorded in full, because the money is spent. An `overrun` event is logged.
  - Settling twice raises `ReservationError`, which names the closing event (`settle`, `expire`, `cancel` or `reconcile`). Settling after an expiry raises it too.
- **`cancel(res_id, reason)`** releases a reservation without charge. Use it only when the request certainly never reached the provider.
- **`expire(older_than, charge_full=True, reason="", force=False)`** recovers reservations older than `older_than` (a timedelta or seconds), but only those whose owner is shown dead.
  - The test is that the PID recorded with the reservation no longer exists on this host.
  - A live owner is never expired, even with `force`, so a slow call still settles at its actual cost.
  - A reservation recorded on another host can't be checked from here. It is expired only with `force=True`, after a person has confirmed that its process is gone.
  - By default each expired reservation is charged its full reserved amount, because the call's cost is unknown. This follows `emx.spend.recover_uncertain`.
  - Pass `charge_full=False` only after checking the provider's records.
  - Each expiry is logged with the owner's state (`dead`, or `unchecked (forced)`).
- **`transfer(source, stage, usd, reason, approved_by=None)`** moves cap into a stage under the budget lock, logged with its reason. `source` is either a pool or another stage.
  - **From a pool** (`reserve`, `contingency`): the pool must have enough left. Once pool transfers total more than $50, it also needs `approved_by="jacob"`. **An agent passes that only after Jacob's explicit yes in chat.** The code records it but can't check it.
  - **From a stage,** e.g. the plan's low-loss-yield branch, which moves unused Q2 authorisation to S1 and Q3. It is refused if it would take the source's cap below its spent plus open reservations. Stage moves leave the sprint cap unchanged. They don't count toward the $50 threshold, which the plan sets only for the reserve and contingency.
  - Either way, a source or destination that needs the top-up is refused until the top-up is recorded.
- **`status()`** returns cap, spent, open and headroom per stage, the sprint totals, what each pool has left, and the open reservations.

### Files

All of these sit next to the designated checkout's ledger:

- `sprint_budget.lock`: the lock file.
- `sprint_reservations.json`: open reservations, with their PID and host. It is rewritten atomically (temp file, fsync, rename).
- `sprint_budget_events.jsonl`: an append-only log of `charge_intent`, `settle`, `expire`, `cancel`, `reconcile`, `overrun` and `transfer` events. **Settlements and transfers are read back from it, so it is part of the books. Commit it with the ledger.**

### Guarantees

- **One set of books for the sprint.** Every reservation, settlement and transfer runs in the designated checkout. A second worktree has its own ledger, so it is refused before it reads or writes anything. A test checks this.
- **No overshoot from concurrent processes.** The check and the write happen under one exclusive lock. In the test, 8 processes make 48 attempts of $2.50 each, against a $60 stage cap and a $45 sprint cap. Exactly 24 and 18 succeed.
- **Crash safety:**
  - **The process dies after `reserve`.** The reservation stays in the state file and keeps counting against both caps until `expire` recovers it.
  - **The process dies after the ledger write, before the closing event.** The next locked call finds the logged intent plus a ledger row of the same stage, amount and tag. It closes the reservation and logs `reconcile`.
  - **The process dies after the closing event, before the state write.** The next call sees the closing event and drops the reservation.
  - Either way, nothing is counted or charged twice.
- **No note can close another reservation.** Caller notes can't contain the tag. Rows written around this module, even with a forged tag, have no logged intent, so they close nothing. That case is the reviewer's reproduction, now a regression test.
- **The top-up gate and transfers are enforced in code.**

### Limits

- **Spending can be booked around this module.**
  - A direct `emx.spend.record(exp="SPRINT-S4", ...)` before the top-up isn't refused. It still counts as spent.
  - Code in another checkout that skips this module isn't caught either.
- **Commit with no open reservations.** `sprint_reservations.json` is not gitignored, because this unit couldn't edit `.gitignore`. Settle or expire every reservation before committing `logs/`.
- **Overruns aren't refused.** The bound holds only if `max_output_tokens` is enforced on the request and the prices are right.
- **PID checks:** a recycled PID reads as alive, which keeps the reservation open. That errs on the safe side.
- **Advisory checks:** `approved_by="jacob"` is an honour-system field, and the lock binds only code that uses this module.

## Sealed outcomes (`compaction.seal`)

### Layout

```
results/sprint/<stage>/sealed/<release_name>/<file>        outcome files, written only by seal.write
results/sprint/<stage>/sealed/<release_name>/RELEASE.json  written only by seal.release
```

- **Release names:** each release name is a unit that Jacob audits and releases on its own. Examples are `Q3dev` and `Q3held` within S3, or `Q2` beside them. The default name is `main`.
- **Root:** the root is `results/sprint/`. `SPRINT_RESULTS_ROOT` (or `root=`) overrides it.
- **No symlinks:** symlinks are refused at the stage, `sealed` and release levels, and among the sealed files. The root itself may be a symlink.

### Protocol and API

1. **`write(stage, name, obj, release_name="main")`** writes one file atomically.
   - A `.jsonl` name takes a list of records, a `.json` name takes any JSON value, and bytes are written as they are under any name.
   - A name is a single path component (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `..`), and `RELEASE.json` is refused.
   - Overwriting a file is allowed, since collection and reruns continue, but it invalidates any release covering that file.
2. **`prepare_audit(stage, release_name="main")`**, called when the audit page is built, returns the outcome manifest: stage, release name, the sha256 of every sealed file, and a digest.
   - Draw the audit sample from those files.
   - Keep the manifest with the audit, for example in the audit page's data.
3. **`release(stage, audited_files, by="jacob", *, manifest, release_name="main", note="")`** is called by the audit page integration once Jacob finishes. It refuses in three cases:
   - the manifest names another stage or release name;
   - the manifest's digest doesn't match its file list, which means it was edited;
   - the sealed files on disk differ from the manifest. This raises `AuditStale`, which names the changed, added and removed files and asks for a re-audit, i.e. `prepare_audit` again.

   `audited_files` are either sealed names or paths to audit artifacts, such as the label export, and each is hashed. Then it writes `RELEASE.json`: who released it, when, when the audit was prepared, the file hashes, a digest and the audited files.
4. **`open_sealed(stage, name, release_name="main")`** is the only way for analysis code to read a sealed file. It returns parsed JSON (`.json`), a list (`.jsonl`) or bytes. Before reading, it runs `verify`. It also checks the bytes it returns against the release, which closes the gap between the check and the read.
5. **`verify(stage, release_name="main")`** raises in these cases:
   - `NotReleased` when there is no release;
   - `SealError` on a symlink;
   - `ReleaseInvalidated` when the record names another stage or release name, i.e. a copied `RELEASE.json`;
   - `ReleaseInvalidated` when the record's digest doesn't match its own file list;
   - `ReleaseInvalidated` when any sealed file changed, appeared or disappeared, or any audited file changed. Every such file is named, and `.changed`, `.added`, `.removed` and `.audited_changed` list them.

### Guarantees and limits

- **Releases are bound to the audit:**
  - A release covers exactly the version Jacob audited.
  - Every analysis read re-hashes that version, so a rerun or rescoring blocks analysis until the changed items are re-audited and released.
- **Named releases are independent:**
  - Releasing `Q3dev` doesn't open `Q3held`.
  - Neither a copied `RELEASE.json` nor a `Q3held → Q3dev` symlink passes.
  - Tampering with `Q3held` doesn't invalidate `Q3dev`.
- **Not access control.** The files are plain JSON on disk, and a determined edit could still rewrite both the files and a consistent `RELEASE.json`. This guards against accidents. Analysis code must use `open_sealed`, and reviews should check that it does.
- **Outcome fields only.** The caller decides what counts as an outcome field and writes it with `seal.write`. Telemetry (cost, time, failures, counts) stays outside `sealed/`.

## Review responses

The r2 column records how round 2 judged each round 1 fix, and our response to each new round 2 finding.

| # | Finding | Severity | r1 response | r2 |
|---|---|---|---|---|
| r1-1 | The sprint cap held only per checkout: each worktree had its own lock, reservations and transfers. | BLOCKING | **Fixed.** `sprint.checkout` in `caps.toml` names one designated checkout. Every budget call refuses with `WrongCheckout` unless its ledger is that checkout's `logs/costs.jsonl`. Per-worktree ledgers are unchanged. Test: `test_refuses_outside_the_designated_checkout`, in which a second checkout context is refused for reserve, settle, cancel, expire, transfer and status, and nothing is written there. | Resolved, per r2. |
| r1-2 | A release didn't bind to the audited version: outcomes changed after the audit were still released. | BLOCKING | **Fixed.** `prepare_audit` captures the manifest, and `release(..., manifest=)` requires it. `AuditStale` names the changed files and asks for a re-audit. Tests: `test_release_is_bound_to_the_audited_version` and `test_release_refuses_a_foreign_or_edited_manifest`. | Resolved, per r2. |
| r1-3 | `verify` didn't check the release's identity, and symlinks bypassed directory isolation. | BLOCKING | **Fixed.** `verify` checks `stage`, `release_name` and a digest. Symlinks are rejected in the stage, `sealed` and release directories and among the sealed files. Tests: `test_copied_release_manifest_is_rejected`, `test_symlinks_are_rejected` and `test_edited_release_record_is_rejected`. | Resolved, per r2. |
| r1-4 | Age alone could expire a live call. | MINOR | **Fixed** with explicit recovery: a reservation expires only when its owner's PID is dead, or with `force` for another host. Tests: `test_expiry_never_takes_a_live_call` and `test_expiry_of_another_hosts_reservation_needs_force`. | Resolved, per r2. |
| r1-5 | `bounded_cost` rounded prices down before rounding the total up. | MINOR | **Fixed.** The arithmetic is exact `Decimal`, and only the final total is rounded, upward. Test: `test_bounded_cost`. | Resolved, per r2. |
| r2-1 | Free-text notes could close the wrong reservation: a $1 S2 charge whose note named a $60 S1 reservation closed it. | BLOCKING | — | **Fixed.** `emx.spend.record` takes no extra fields, so the budget event log is now authoritative: `charge_intent`, then the ledger row, then a closing event carrying the row's id, then the state write. Crash recovery accepts a ledger row only if a logged intent matches it on stage, amount and a tag at the end of the note. Caller notes containing `sprint_res=` are refused, and `settle(stage=)` must match the reservation. Tests: `test_notes_cannot_close_another_reservation` (the reviewer's case, plus forged tags written around the module in the same and in a different stage), `test_crash_after_the_ledger_write_before_the_closing_event` and `test_crash_after_the_closing_event_before_the_state_write`. |
| r2-2 | Stage-to-stage transfers, needed for the Q2 reallocation, weren't supported. | MINOR | — | **Fixed.** `transfer(source, ...)` takes a stage as the source, under the same lock, and refuses to take that stage's cap below its spent plus open reservations. It is logged with its reason. Test: `test_stage_to_stage_transfer`. |
| r2-3 | Concurrency-test workers weren't cleaned up on failure. | MINOR | — | **Fixed.** A `try/finally` kills and reaps every worker that was launched, including on partial startup, and workers exit by themselves if the start marker never appears within 60 s. |
