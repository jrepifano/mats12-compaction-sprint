"""Small E12 luna runner for the task judge and the style audit (plan "Departures" 5):
resumable JSONL, one `emx.spend` API reservation per batch (reserve -> run -> settle/release, the
pattern of `emx.e9.llm.run_batch`), usage logged from every response.

Owner: unit U6. FROZEN SIGNATURES (U0).

Each output file starts with a header record {"header": true, "source_sha256", "prompt_sha256",
"model", "settings_sha256", "decisions_sha256"}; resume refuses (FingerprintMismatch) a file
whose header differs from the current call's. Item lines: {"item_id", ...item fields...,
"status": "scored" | "none" | "failed", "raw", "usage", "answer_sha256"} plus parsed fields.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path


class FingerprintMismatch(RuntimeError):
    pass


def run_batch(items: Sequence[dict], render: Callable[[dict], str], out_jsonl: str | Path, *,
              header: dict, note: str, client, parse: Callable[[str], dict | None],
              mode: str = "text", exp: str = "E12", ledger: str | Path | None = None,
              concurrency: int = 8, dry_run: bool = False) -> dict:
    """Score every item not already `scored`/`none` in `out_jsonl`. `render(item)` gives the
    prompt; `parse(text)` the parsed fields or None (counted "none"); mode "logprob" uses the
    client's 0-100 logprob score instead of text. Projects the cost first (`emx.spend.check_total`
    for exp E12; refuse over a cap with SystemExit(2)); dry_run stops after the projection.
    Returns {"n_items", "n_todo", "projected_usd", "charged_usd", "statuses": {...}}."""
    raise NotImplementedError
