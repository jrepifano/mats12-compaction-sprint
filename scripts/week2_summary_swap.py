"""Model against harness prompt for summaries: pi's own summarizer requests, replayed offline with
Gemini 3.8 Flash in place of DeepSeek V4.1 Flash (J call, 30 Oct; Research Advisor; Jacob's go
30 Oct ~11:20 AM ET, $5 of an external OpenRouter key).

The histories are fixed: each boundary's recorded pi requests (experiment 4's unrelated chains
u1-u4, boundaries 1-4; data/raw/sprint/s2/summary_rot/chains/<c>/p<k>/compaction.json, field
`requests`) are sent unchanged except that at an update boundary (k >= 2) the history call's
`<previous-summary>` holds Flash's own boundary k-1 summary instead of DeepSeek's, so the
summaries chain as they would in a live run. The conversation text the agent wrote after k-1 is
DeepSeek-era either way (it saw DeepSeek's summary): offline, not a rerun. The summary is pi's
merge of the history and turn-prefix replies (`s1_replay.pi_merge`, as compaction.s2.compact),
checked by reproducing every recorded DeepSeek summary from its recorded replies.

pi's DeepSeek summarizer ran at reasoning effort max; Flash runs at low, Antigravity+Flash's
setting, so this is a model-plus-effort swap. `--effort high --only u1:2` is the effort check on
one boundary (its previous summary is the low run's).

    uv run python scripts/week2_summary_swap.py check            # $0: reproduction + price
    uv run python scripts/week2_summary_swap.py smoke --go-from-jacob   # one tiny call
    uv run python scripts/week2_summary_swap.py run --go-from-jacob [--effort low|high] [--only u1:2]
    uv run python scripts/week2_summary_swap.py attribute        # summary_rot's coder (Opus on Max)

Spend: OPENROUTER_API_KEY_J only (route `flash_j`), every call reserved at its bound against a
$5 cap on this checkout's ledger (exp SPRINT-S2-SUMMARY-SWAP-J) before it is sent.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import threading
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compaction import s1_replay as RP
from compaction.s2 import config as C
from compaction.s2 import model as M
from emx import spend

sys.path.insert(0, str(Path(__file__).resolve().parent))
import week2_summary_rot as SRS

ROOT = SRS.ROOT
SRC = SRS.RAW / "chains"  # pi's recorded boundaries (pulled, gitignored)
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "summary_swap"
OUT = SRS.OUT / "decay" / "swap"
CHAINS = ("u1", "u2", "u3", "u4")
K = 4
EXP = "SPRINT-S2-SUMMARY-SWAP-J"
CAP_USD = 5.0
KEY_ENV = "OPENROUTER_API_KEY_J"
MODEL = "google/gemini-3.8-flash"
PIN = "Google AI Studio"
PRICES = {"input": 0.75, "output": 3.75, "cache_read": 0.075}  # $/M, OpenRouter list, 30 Oct
EMPTY_TRIES = 3  # as compaction.s2.compact.EMPTY_SUMMARY_TRIES
TOKENIZER_MARGIN = 1.3  # Gemini's token count over DeepSeek's, worst case assumed
TURN = "\n\n---\n\n**Turn Context (split turn):**\n\n"


def route(effort: str) -> C.Route:
    return C.Route(name="flash_j", model=MODEL, transport="openrouter", key_env=KEY_ENV,
                   prices=PRICES, window=1_048_576, max_output=65_536, bytes_per_token=2.0,
                   provider_pin=PIN, reasoning={"reasoning": {"effort": effort}}, summarizer="pi")


class CapSpend:
    """model.Spend against CAP_USD: recorded spend under EXP + open bounds + the new bound must
    fit before a call is sent; charges go to this checkout's ledger through emx.spend.record.

    One paying process at a time (an flock on RAW/spend.lock, held for the process's life). Each
    reservation is appended to RAW/reservations.jsonl (fsync) before its call is sent and closed
    after its charge is recorded; on start, any reservation left open by a crash is charged at
    its full bound (conservative: a crash between the charge and the close counts it twice)."""

    def __init__(self, raw: Path | None = None) -> None:
        raw = raw or RAW
        raw.mkdir(parents=True, exist_ok=True)
        self.res_path = raw / "reservations.jsonl"
        self._lockf = open(raw / "spend.lock", "w")  # noqa: SIM115 (held until exit)
        try:
            fcntl.flock(self._lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another paying process holds spend.lock") from None
        self.lock = threading.Lock()
        self.open: dict[str, float] = {}
        self._recover()

    def _append(self, row: dict) -> None:
        with self.res_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _recover(self) -> None:
        rows = [json.loads(x) for x in self.res_path.read_text().splitlines()] \
            if self.res_path.exists() else []
        closed = {r["rid"] for r in rows if r["op"] != "reserve"}
        for r in rows:
            if r["op"] == "reserve" and r["rid"] not in closed:
                spend.record("judge", f"flash_j:{MODEL}", 1, r["usd"], exp=EXP,
                             note=f"{r['note']}: open after a crash, charged at its bound "
                                  "(an external OpenRouter key)")
                self._append({"op": "recovered", "rid": r["rid"], "usd": r["usd"]})

    def reserve(self, usd: float, note: str) -> str:
        with self.lock:
            have = spend.spent(exp=EXP) + sum(self.open.values())
            if have + usd > CAP_USD:
                raise M.ModelError(f"cap: ${have:.4f} spent or held + ${usd:.4f} > ${CAP_USD}")
            rid = uuid.uuid4().hex
            self._append({"op": "reserve", "rid": rid, "usd": usd, "note": note})
            self.open[rid] = usd
            return rid

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        with self.lock:
            spend.record("judge", resource, 1, usd, note=f"{note} (an external OpenRouter key)", exp=EXP)
            self._append({"op": "settle", "rid": rid, "usd": usd})
            self.open.pop(rid)

    def cancel(self, rid: str, reason: str) -> None:
        with self.lock:
            self._append({"op": "cancel", "rid": rid})
            self.open.pop(rid, None)


def load_env() -> None:
    """Only KEY_ENV, from this checkout's .env or else the main checkout's (AGENTS.md: the keys
    live there); never printed, nothing else is loaded."""
    for p in (ROOT / ".env", ROOT.parents[2] / ".env"):
        for line in p.read_text().splitlines() if p.exists() else []:
            k, _, v = line.strip().partition("=")
            if k.strip() == KEY_ENV:
                os.environ[KEY_ENV] = v.strip().strip("'\"")
                return


def _write(p: Path, text: str) -> None:
    """Atomic replace (temp file, fsync, rename)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix(p.suffix + ".tmp")
    with t.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(t, p)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def recorded(c: str, k: int) -> dict:
    return json.loads((SRC / c / f"p{k}" / "compaction.json").read_text())


def merge(info: dict, texts: dict[str, str], prev: str | None) -> str:
    """pi's summary from its replies (compaction.s2.compact._finish_summary for pi)."""
    if prev is not None and info["split"] and not info["has_history"]:
        return f"{prev}{TURN}{texts['turn_prefix']}"
    return RP.pi_merge(texts, info["split"], info["has_history"])


def swap_previous(user: str, old: str, new: str) -> str:
    """The history call's text with its previous summary replaced (compact.pi_update_request)."""
    a, b = f"<previous-summary>\n{old}\n</previous-summary>", f"<previous-summary>\n{new}\n</previous-summary>"
    if user.count(a) != 1:
        raise ValueError("previous summary not found exactly once in the history call")
    return user.replace(a, b)


def requests_for(c: str, k: int, prev: str | None) -> tuple[list[dict], dict]:
    d = recorded(c, k)
    reqs = []
    for r in d["requests"]:
        r = dict(r)
        if d["update"] and r["label"] == "history":
            r["user"] = swap_previous(r["user"], recorded(c, k - 1)["summary"], prev)
        reqs.append(r)
    return reqs, d


# ------------------------------------------------------------------------------------------
# check ($0)
# ------------------------------------------------------------------------------------------


def endpoint_prices() -> dict:
    url = f"https://openrouter.ai/api/v1/models/{MODEL}/endpoints"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "emx"})) as f:
        eps = json.load(f)["data"]["endpoints"]
    return {e["tag"]: {"provider": e["provider_name"], "tier": e.get("service_tier"),
                       "prompt_per_m": 1e6 * float(e["pricing"]["prompt"]),
                       "completion_per_m": 1e6 * float(e["pricing"]["completion"])} for e in eps}


def cmd_check(a) -> None:
    rows, tin, tout, wout = [], 0, 0, 0
    for c in CHAINS:
        for k in range(1, K + 1):
            d = recorded(c, k)
            prev = recorded(c, k - 1)["summary"] if d["update"] else None
            same = merge(d["info"], d["responses"], prev) == d["summary"]
            reqs, _ = requests_for(c, k, prev)  # the slot swap must find DeepSeek's k-1 summary
            first = {}
            for u in d["usage"]:
                first.setdefault(u["label"], u)
            pin = sum(u["prompt_tokens"] for u in first.values())
            pout = sum(u["completion_tokens"] for u in first.values())
            mout = sum(r["max_output_tokens"] for r in reqs)
            tin, tout, wout = tin + pin, tout + pout, wout + mout
            rows.append({"chain": c, "k": k, "update": d["update"], "calls": [r["label"] for r in reqs],
                         "merge_reproduces_summary": same, "deepseek_prompt_tokens": pin,
                         "deepseek_completion_tokens": pout, "max_output_tokens": mout})
    bad = [r for r in rows if not r["merge_reproduces_summary"]]
    p = PRICES
    expect = (tin * p["input"] + tout * p["output"]) / 1e6
    worst = (tin * TOKENIZER_MARGIN * p["input"] + wout * p["output"]) / 1e6
    out = {"model": MODEL, "pin": PIN, "prices_per_m": p, "endpoints": endpoint_prices(),
           "boundaries": rows, "deepseek_prompt_tokens": tin, "deepseek_completion_tokens": tout,
           "max_output_tokens": wout,
           "projected_usd": {"expected": round(expect, 3), "worst": round(worst, 3),
                             "worst_rule": f"prompt x {TOKENIZER_MARGIN}, every call at its "
                                           "output cap, no resends",
                             "effort_check_u1_2": None},
           "cap_usd": CAP_USD}
    d = recorded("u1", 2)
    out["projected_usd"]["effort_check_u1_2"] = round(
        (sum(u["prompt_tokens"] for u in d["usage"][:2]) * TOKENIZER_MARGIN * p["input"]
         + sum(r["max_output_tokens"] for r in d["requests"]) * p["output"]) / 1e6, 3)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "price_check.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({"reproduced": len(rows) - len(bad), "of": len(rows),
                      **out["projected_usd"]}))
    if bad:
        sys.exit(f"merge does not reproduce: {[(r['chain'], r['k']) for r in bad]}")


# ------------------------------------------------------------------------------------------
# run (paid)
# ------------------------------------------------------------------------------------------


def _model(effort: str, sp: CapSpend) -> M.OpenAICompatModel:
    load_env()
    return M.OpenAICompatModel(route(effort), sp)


def cmd_smoke(a) -> None:
    if not a.go_from_jacob:
        sys.exit("paid: pass --go-from-jacob")
    m = _model("low", CapSpend())
    r = m.complete([{"role": "user", "content": "Reply with the word OK."}], None, 256,
                   purpose="summary:smoke")
    print(json.dumps({"text": _text(r.message.get("content")), "model": r.model,
                      "usage": vars(r.usage), "finish": r.finish_reason}, default=str))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def summarize(m: M.OpenAICompatModel, c: str, k: int, prev: str | None) -> dict:
    reqs, d = requests_for(c, k, prev)
    texts, usage = {}, []

    def one(r: dict) -> tuple[str, str, list[dict]]:
        us = []
        for attempt in range(1, EMPTY_TRIES + 1):
            rep = m.complete([{"role": "system", "content": r["system"]},
                              {"role": "user", "content": r["user"]}], None,
                             r["max_output_tokens"], purpose=f"summary:pi:{r['label']}")
            t = _text(rep.message.get("content"))
            us.append({"label": r["label"], **vars(rep.usage), "cost_usd": rep.cost_total_usd,
                       "finish_reason": rep.finish_reason, "attempt": attempt,
                       "served_model": rep.model, "latency_s": round(rep.latency_s, 1)})
            if t.strip():
                return r["label"], t, us
        raise M.ModelError(f"{c} p{k} {r['label']}: empty after {EMPTY_TRIES} attempts")

    with ThreadPoolExecutor(len(reqs)) as ex:
        for label, t, us in ex.map(one, reqs):
            texts[label] = t
            usage += us
    summary = merge(d["info"], texts, prev)
    return {"chain": c, "arm": "unrelated", "kind": "boundary", "boundary": k, "after_phase": k,
            "model": MODEL, "effort": m.route.reasoning["reasoning"]["effort"], "update": d["update"], "info": d["info"], "summary": summary,
            "summary_sha256": _sha(summary), "previous_summary_sha256": _sha(prev) if prev else None,
            "deepseek_summary_sha256": _sha(d["summary"]), "requests": reqs, "responses": texts,
            "usage": usage, "cost_usd": round(sum(u["cost_usd"] or 0 for u in usage), 6)}


def out_dir(effort: str) -> Path:
    return RAW / effort


def saved(effort: str, c: str, k: int) -> dict | None:
    p = out_dir(effort) / "chains" / c / f"p{k}" / "compaction.json"
    return json.loads(p.read_text()) if p.exists() else None


def run_chain(m, effort: str, c: str, ks: list[int]) -> list[str]:
    """Boundaries in order; the previous summary is always the low run's (high is only the
    one-boundary check). A kept boundary is reused only if its model, effort and predecessor
    match; otherwise the run stops rather than mix chains."""
    log = []
    for k in ks:
        prev = (saved("low", c, k - 1) or {}).get("summary") if k > 1 else None
        if k > 1 and prev is None:
            raise SystemExit(f"{c} p{k}: no low summary for p{k - 1}")
        old = saved(effort, c, k)
        if old:
            want = (MODEL, effort, _sha(prev) if prev else None)
            if (old.get("model"), old.get("effort"), old["previous_summary_sha256"]) != want:
                raise SystemExit(f"{c} p{k}: kept record does not match its predecessor/config")
            log.append(f"{c} p{k} kept")
            continue
        rec = summarize(m, c, k, prev)
        _write(out_dir(effort) / "chains" / c / f"p{k}" / "compaction.json",
               json.dumps(rec, indent=1))
        log.append(f"{c} p{k} ${rec['cost_usd']:.4f} {len(rec['summary'])} chars")
        print(log[-1], flush=True)
    return log


def cmd_run(a) -> None:
    if not a.go_from_jacob:
        sys.exit("paid: pass --go-from-jacob")
    m = _model(a.effort, CapSpend())
    plan = {c: list(range(1, K + 1)) for c in CHAINS}
    if a.effort != "low" and not a.only:
        sys.exit("--effort high is the one-boundary check: pass --only chain:k")
    if a.only:
        c, k = a.only.split(":")
        plan = {c: [int(k)]}
    with ThreadPoolExecutor(len(plan)) as ex:
        list(ex.map(lambda ck: run_chain(m, a.effort, *ck), plan.items()))
    export(a.effort)
    print(json.dumps({"spent_usd": spend.spent(exp=EXP), "cap_usd": CAP_USD}))


def export(effort: str) -> None:
    """The coder's view (state.json, compactions.jsonl) and the committed summaries."""
    rows = []
    for c in CHAINS:
        for k in range(1, K + 1):
            r = saved(effort, c, k)
            if r is None:
                continue
            rows.append({x: r[x] for x in ("chain", "boundary", "update", "summary",
                                           "summary_sha256", "previous_summary_sha256",
                                           "deepseek_summary_sha256", "cost_usd")}
                        | {"effort": effort, "usage": [{u: v for u, v in x.items()}
                                                       for x in r["usage"]]})
    for c in CHAINS:  # regenerated whole from the saved boundaries
        _write(out_dir(effort) / "chains" / c / "compactions.jsonl", "".join(
            json.dumps({"chain": c, "arm": "unrelated", "kind": "boundary", "boundary": r["boundary"],
                        "after_phase": r["boundary"]}) + "\n" for r in rows if r["chain"] == c))
    st = json.loads((SRS.RAW / "state.json").read_text())
    st["chains"] = {c: st["chains"][c] for c in CHAINS}
    _write(out_dir(effort) / "state.json", json.dumps(st, indent=1))
    _write(OUT / f"summaries_{effort}.jsonl",
           "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# ------------------------------------------------------------------------------------------
# attribute (summary_rot's coder, unchanged; Opus on Max, $0)
# ------------------------------------------------------------------------------------------


def cmd_attribute(a) -> None:
    view = out_dir(a.effort)
    so = OUT / f"summary_rot_{a.effort}"
    SRS.RAW, SRS.OPUS_DIR, SRS.OUT = view, view / "opus", so
    SRS.ATTR, SRS.META = so / "attributions.jsonl", so / "attribute_meta.json"
    SRS.cmd_attribute(argparse.Namespace(no_opus=a.no_opus, max_calls=a.max_calls,
                                         concurrency=a.concurrency))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("smoke")
    p.add_argument("--go-from-jacob", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--effort", choices=("low", "high"), default="low")
    p.add_argument("--only", help="chain:boundary, e.g. u1:2")
    p = sub.add_parser("attribute")
    p.add_argument("--effort", choices=("low", "high"), default="low")
    p.add_argument("--no-opus", action="store_true")
    p.add_argument("--max-calls", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    {"check": cmd_check, "smoke": cmd_smoke, "run": cmd_run, "attribute": cmd_attribute}[a.cmd](a)


if __name__ == "__main__":
    main()
