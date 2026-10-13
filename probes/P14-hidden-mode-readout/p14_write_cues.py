"""Laptop, paid (< $1): write and accept P14's held-out implied-cue pairs (TEST), before any
organism data (plan.md "Setup", TEST).

E13's procedure (`emx.e13.inferred`), unchanged except where noted:
- **Writer:** `contracts.WRITER_MODEL` (Gemini 3.8 Flash), temperature 1, JSON mode, the frozen
  prompt `prompts/E13/notice_writer_inferred_v1.txt` filled per pairing exactly as E13 does
  (`inferred.writer_prompt`), the same four held-out-family pairings. P14: request seeds
  `WRITER_SEED + 100 + j`, rounds j = 0..ROUNDS-1, every pairing each round; pair ids
  `<pairing>__r<10 + j>_<k>` (E13's id format; regen numbers from 10 are P14's).
- **Deterministic drop rules** (too_long, outright_status, template_mismatch;
  `inferred.deterministic_reason`), then **P14's added rule** `near_duplicate`: either side has
  difflib ratio >= DUP_RATIO to any snippet of E13's 40 pairs or of an earlier P14 candidate
  (round order, then k).
- **luna's blind check** on the survivors (`inferred.checker_prompt`, temperature 0, seed 0, one
  retry with seed 1 on a malformed reply), then `unchecked`, `instructs`, `opposite_status`
  (`inferred._reason`).
- **Quota:** the first PER_PAIRING_MAX survivors per pairing, in round then k order; the rest
  `over_quota`.
Raw replies go to `laptop/cues/` (E13's `llm.run_batch` rows, resumable; ledger exp P14).
Output: `inputs/test_pairs.jsonl` (every candidate, `kept` and `drop_reason` set, plus
`near_dup_of`) and `inputs/test_survival.json`.

    uv run python probes/P14-hidden-mode-readout/p14_write_cues.py [--rounds 4] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p14 import common as K

sys.path.insert(0, str(K.REPO / "src"))
from emx.e13 import contracts as C
from emx.e13 import inferred as I
from emx.e13 import llm

RAW = K.PROBE_DIR / "laptop" / "cues"
ROUNDS = 4
SEED_BASE = C.WRITER_SEED + 100
REGEN_BASE = 10
DUP_RATIO = 0.8
PER_PAIRING_MAX = 40
TARGET_KEPT = 120


def writer_path(j: int) -> Path:
    return RAW / f"writer_j{j}.jsonl"


CHECKER_PATH = RAW / "checker.jsonl"


def item_id(pairing: str, j: int) -> str:
    return f"{pairing}__p14j{j}"


def write_round(j: int, client=None) -> list[dict]:
    reqs = [{"item_id": item_id(p, j), "seed": SEED_BASE + j,
             "prompt": I.writer_prompt(p, K.REPO)} for p, _, _ in I.pairings()]
    return llm.run_batch("writer", reqs, writer_path(j), note=f"{K.EXP}:cues:writer:j{j}",
                         client=client, **I.WRITER_KW)


def load_candidates(rounds: int) -> list[dict]:
    out = []
    for j in range(rounds):
        p = writer_path(j)
        if not p.exists():
            continue
        saved = llm.load_rows(p)
        for pid, _, _ in I.pairings():
            r = saved.get((item_id(pid, j), SEED_BASE + j))
            if r is not None and r["status"] == "ok":
                out.extend(I.pair_rows(pid, I.parse_writer_reply(r["text"]), REGEN_BASE + j))
    return out


def mark_duplicates(rows: list[dict], reference: list[str]) -> None:
    """Sets `near_dup_of` ("" if none) on each row, in order, against the reference snippets
    and every earlier row that passed the deterministic rules."""
    import difflib

    seen = [(f"e13:{i}", t) for i, t in enumerate(reference)]
    for r in rows:
        r["near_dup_of"] = ""
        if I.deterministic_reason(r):
            continue
        for s in I.SIDES:
            for sid, t in seen:
                if difflib.SequenceMatcher(None, r[s], t).ratio() >= DUP_RATIO:
                    r["near_dup_of"] = sid
                    break
            if r["near_dup_of"]:
                break
        if not r["near_dup_of"]:
            seen.extend((f"{r['pair_id']}:{s}", r[s]) for s in I.SIDES)


def check(rows: list[dict], client=None) -> dict[str, dict]:
    """luna's blind labels of every live snippet (E13's checker loop, P14's raw file)."""
    live = [r for r in rows if not I.deterministic_reason(r) and not r["near_dup_of"]]
    items = I.snippet_items(live)
    out: dict[str, dict] = {}
    todo = list(items)
    for seed in I.CHECKER_SEEDS:
        if not todo:
            break
        reqs = [{"item_id": iid, "seed": seed, "prompt": I.checker_prompt(t, K.REPO)}
                for iid, t in todo]
        res = llm.run_batch("luna", reqs, CHECKER_PATH, note=f"{K.EXP}:cues:checker:s{seed}",
                            client=client, **I.CHECKER_KW)
        nxt = []
        for (iid, t), row in zip(todo, res, strict=True):
            v = I._verdict(row)
            if v is None:
                continue
            out[iid] = v
            if not v["check_label"]:
                nxt.append((iid, t))
        todo = nxt
    return out


def decide(rows: list[dict], checks: dict[str, dict]) -> list[dict]:
    rows = I.apply_checks(rows, checks)
    for r in rows:
        r["drop_reason"] = "near_duplicate" if r["near_dup_of"] and not \
            I.deterministic_reason(r) else I._reason(r)
        r["kept"] = not r["drop_reason"]
    for pid, _, _ in I.pairings():
        pr = sorted((r for r in rows if r["pairing"] == pid and r["kept"]),
                    key=lambda r: (int(r["regen"]), r["pair_id"]))
        for r in pr[PER_PAIRING_MAX:]:
            r["kept"], r["drop_reason"] = False, "over_quota"
    return rows


def survival(rows: list[dict], rounds: int) -> dict:
    per = {}
    for pid, no, ov in I.pairings():
        pr = [r for r in rows if r["pairing"] == pid]
        per[pid] = {"n_written": len(pr), "n_kept": sum(r["kept"] for r in pr),
                    "drops": dict(Counter(r["drop_reason"] for r in pr if r["drop_reason"])),
                    "luna": {s: dict(Counter(str(r[f"luna_{s}"]) for r in pr))
                             for s in I.SIDES}}
    n = sum(v["n_kept"] for v in per.values())
    return {"rounds": rounds, "seed_base": SEED_BASE, "dup_ratio": DUP_RATIO,
            "per_pairing_max": PER_PAIRING_MAX, "target_kept": TARGET_KEPT, "n_kept": n,
            "n_written": len(rows), "per_pairing": per, "target_met": n >= TARGET_KEPT}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=ROUNDS)
    ap.add_argument("--dry-run", action="store_true", help="project the writer calls only")
    a = ap.parse_args(argv)
    llm.EXP = K.EXP  # E13's batch layer, accounted to P14 (notes must start "P14:")
    if a.dry_run:
        for j in range(a.rounds):
            reqs = [{"item_id": item_id(p, j), "seed": SEED_BASE + j,
                     "prompt": I.writer_prompt(p, K.REPO)} for p, _, _ in I.pairings()]
            print(j, f"${llm.estimate_usd('writer', reqs, writer_path(j), **I.WRITER_KW):.4f}")
        return 0
    for j in range(a.rounds):
        write_round(j)
    rows = load_candidates(a.rounds)
    reference = [r[s] for r in K.read_jsonl(K.REPO / K.CAL_PAIRS) for s in I.SIDES]
    mark_duplicates(rows, reference)
    rows = decide(rows, check(rows))
    K.INPUTS.mkdir(parents=True, exist_ok=True)
    with open(K.TEST_PAIRS, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    surv = survival(rows, a.rounds)
    (K.INPUTS / "test_survival.json").write_text(json.dumps(surv, indent=1) + "\n")
    print(json.dumps({k: surv[k] for k in ("n_written", "n_kept", "target_met")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
