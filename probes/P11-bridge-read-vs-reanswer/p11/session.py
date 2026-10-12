"""P11 pod session: L_p sweep, patched captures and the Oracle Lens draws, one model load.

    python -m p11.session download     # Qwen3.6-27B + the Oracle Lens adapter -> results/download_paths.json
    python -m p11.session env          # results/env.json
    python -m p11.session all [--limit N] [--max-new-tokens K]

``all`` (each phase timed into ``results/timing.json`` and printed as ``P11_STEP <name>
start|done <UTC> [<seconds>]``):

1. ``model_load``; 2. ``renders``: every arm's render per item; the premise renders must end in
   the plain render's question suffix (everything after ``<|im_start|>user\\n``) and every render
   in the stem tokens; 3. ``smoke_identity``: patching the plain run's own residual back moves the
   next-token logits by < 1e-3 (or by no more than a repeated unpatched forward does);
4. ``answers``: greedy answers on the plain and direct premise prompts, and the **L_p sweep**:
   each premise run's suffix residual at every L_p in ``LP_PLANNED + LP_FALLBACK``, patched into
   the plain run, greedy answer; ``smoke_greedy``: plain greedy = Qwen's modal answer on >= 8
   of the first 10 items (and reported on all 63); 5. ``choose_lp`` (``choose_lp``);
6. ``capture``: the read layers at the last 3 positions for every arm (patched arms under the
   chosen patch); 7. ``olens_bind`` (live-checked) and ``smoke_shuffled``: 20 shuffled-activation
   cells name neither bridge on >= 18; 8. the draws, in ``ARM_ORDER``, two lens samples per cell
   (``draw_name``; batch b of a draw seeded ``SEED + 1000 * arm number + 100 * sample + b``);
   a draw stops the session when more than 5% of its cells are empty (plan stop rule), and the
   session stops starting draws past ``$P11_MAX_HOURS``; 9. ``prompt_only`` (follow-up (c),
   exploratory): stock Qwen given the plain prompt text, asked what the model would be thinking
   at its end, ``N_PROMPT_ONLY`` samples per item.

A failed smoke check ends the session (rc 3) after writing ``status.json``; the plan says all
must pass. Reused unchanged: ``emx.i1a.capture.load_backend``, ``emx.i1a.olens.PublicOLens`` /
``default_sampling`` / ``derangement``, ``emx.i1a.hfget``, and P03's ``read_olens.generate``
(resumable per batch) and ``read_store.ReadSite``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p03 import core
from p03 import read_olens as O
from p03 import read_store as RS

from p11 import items as I

OUT = I.RESULTS
SAMPLES = OUT / "olens"
DEFAULT_ARTIFACTS = "/ws/p11_artifacts"

LENS_LAYERS: tuple[int, ...] = (40, 44, 48, 52, 56, 60)
POSITIONS: tuple[int, ...] = core.READ_POSITIONS  # (-3, -2, -1)
LP_PLANNED: tuple[int, ...] = (16, 20, 24, 28, 32)
LP_FALLBACK: tuple[int, ...] = (36,)  # follow-up (b), used only if no planned L_p qualifies
FLIP_MIN = 0.25
MENTION_MAX = 0.15
IDENTITY_TOL = 1e-3
IDENTITY_LAYER = 24
GREEDY_MIN = 8  # of the first 10 items
SHUFFLED_MIN = 18  # of 20 cells name neither bridge
SHUFFLED_LAYER = 44
UNPARSEABLE_MAX = 0.05
MAX_NEW_ANSWER = 8
N_DRAWS = 2
SEED = 20261012
ARM_NUM = {
    "plain": 1,
    "neutral": 2,
    "author": 3,
    "mention": 4,
    "patched_author": 5,
    "patched_mention": 6,
    "patched_author_a25": 7,
    "patched_author_a50": 8,
    "patched_author_a75": 9,
}
ARM_ORDER = ("plain", "patched_author", "patched_mention", "author", "mention", "neutral")
# Dose arms (the reading-vs-re-answering check, lit.md "Cheap extra checks" 1; exploratory):
# the author patch interpolated from the plain run's own residual, alpha = 0.25 / 0.5 / 0.75,
# greedy answer plus one lens sample per cell. Read after the main arms.
DOSE: dict[str, float] = {
    "patched_author_a25": 0.25,
    "patched_author_a50": 0.5,
    "patched_author_a75": 0.75,
}
DOSE_DRAWS = 1
PROMPT_ONLY_SEED = SEED + 900_000
N_PROMPT_ONLY = 36  # = cells per item in a lens arm (6 layers x 3 positions x 2 samples)
PROMPT_ONLY_TEMPLATE = (
    "Below is the start of a conversation with an AI assistant. It ends partway through the "
    "assistant's reply.\n\n<conversation>\nUser: {question}\nAssistant: {prefill}\n"
    "</conversation>\n\nAt the very end of this text, what is the assistant thinking about? "
    "Describe the concepts, entities and facts that are active in its mind at that point."
)


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p11 {utc()}] {msg}", flush=True)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def draw_name(arm: str, k: int) -> str:
    return f"{arm}_s{k}"


def seed_base(arm: str, k: int) -> int:
    return SEED + 1000 * ARM_NUM[arm] + 100 * k


def n_draws(arm: str) -> int:
    return DOSE_DRAWS if arm in DOSE else N_DRAWS


def register_seeds() -> None:
    """P03's ``read_olens.plan`` seeds batch b of a draw with ``SEED_BASE[draw] + b``; P11's
    draws are added to that table (a draw has at most 72 batches, so the bases never overlap)."""
    for arm in ARM_NUM:
        for k in range(n_draws(arm)):
            O.SEED_BASE[draw_name(arm, k)] = seed_base(arm, k)
    O.SEED_BASE["p11_smoke_shuffled"] = SEED + 50_000


# --------------------------------------------------------------------- pure decisions


def choose_lp(rates: dict[int, dict[str, float]]) -> tuple[int | None, str]:
    """The patch layer (plan "Automatic rules"): among the planned L_p whose mention flip rate is
    <= MENTION_MAX, the one with the highest author flip rate (lowest layer on a tie), if that rate
    is >= FLIP_MIN; else the same rule over the fallback layers (follow-up (b)); else None."""
    for pool, tag in ((LP_PLANNED, "planned"), (LP_FALLBACK, "fallback (follow-up b)")):
        ok = [L for L in pool if L in rates and rates[L]["mention"] <= MENTION_MAX]
        if not ok:
            continue
        best = max(ok, key=lambda L: (rates[L]["author"], -L))
        if rates[best]["author"] >= FLIP_MIN:
            return best, tag
    return None, "failed"


def unparseable_frac(rows: list[dict[str, Any]]) -> float:
    return sum(1 for r in rows if not str(r.get("text", "")).strip()) / max(len(rows), 1)


def shuffled_check(rows: list[dict[str, Any]], by_id: dict[str, I.Item]) -> dict[str, Any]:
    """Shuffled cells (row ``id`` = the reported item, ``source_id`` = the read item) that name the
    reported item's own bridge or Y; pass when >= SHUFFLED_MIN of them name neither."""
    hits = []
    for r in rows:
        it = by_id[r["id"]]
        b = I.names(r["text"], it.bridge, it.bridge_aliases)
        y = I.names(r["text"], it.y, it.y_aliases)
        hits.append({"id": r["id"], "source_id": r["source_id"], "bridge": b, "y": y})
    neither = sum(1 for h in hits if not (h["bridge"] or h["y"]))
    return {"n": len(rows), "neither": neither, "passed": neither >= SHUFFLED_MIN, "cells": hits}


# --------------------------------------------------------------------- timing


class Timer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def note(self, key: str, value: Any) -> None:
        self.data[key] = value
        write_json(self.path, self.data)

    def step(self, name: str):
        timer = self

        class _Step:
            def __enter__(self) -> dict[str, Any]:
                key, k = name, 2
                while key in timer.data:
                    key, k = f"{name}#{k}", k + 1
                self.key, self.t = key, time.time()
                self.rec: dict[str, Any] = {"start": utc()}
                print(f"P11_STEP {name} start {self.rec['start']}", flush=True)
                return self.rec

            def __exit__(self, et, ev, tb) -> None:
                self.rec.update(end=utc(), seconds=round(time.time() - self.t, 2))
                if et is not None:
                    self.rec["failed"] = True
                timer.note(self.key, self.rec)
                tag = "failed" if et is not None else "done"
                print(f"P11_STEP {name} {tag} {self.rec['end']} {self.rec['seconds']}", flush=True)

        return _Step()


# --------------------------------------------------------------------- download and env


def step_download(root: Path, out: Path | None = None) -> dict[str, str]:
    from emx.i1a import contracts as IC
    from emx.i1a import hfget

    groups = [
        ("qwen", IC.MODEL_ID, hfget.model_files(), IC.MODEL_REV),
        ("adapters", IC.ADAPTER_REPO, list(IC.OLENS_FILES), IC.ADAPTER_REV),
    ]
    total = sum(hfget.plan_bytes(repo, files, revision=rev) for _, repo, files, rev in groups)
    hfget.check_total(total, "P11 downloads")
    log(f"download plan: {total / 1e9:.1f} GB")
    got = {
        name: hfget.fetch(repo, files, revision=rev, local_dir=root / name)
        for name, repo, files, rev in groups
    }
    paths = {
        "qwen_dir": str(got["qwen"]["config.json"].parent),
        "olens_dir": str(got["adapters"][IC.OLENS_FILES[0]].parent),
        "planned_bytes": str(total),
    }
    write_json((out or OUT) / "download_paths.json", paths)
    return paths


def step_env() -> None:
    from importlib.metadata import PackageNotFoundError, version

    from emx.i1a import contracts as IC

    def v(m: str) -> str | None:
        try:
            return version(m)
        except PackageNotFoundError:
            return None

    env: dict[str, Any] = {
        "utc": utc(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": os.environ.get("EMX_GIT_COMMIT"),
        "versions": {
            m: v(m)
            for m in ("torch", "transformers", "peft", "workspace-bench", "flash-linear-attention")
        },
        "model": f"{IC.MODEL_ID}@{IC.MODEL_REV}",
        "olens_adapter": f"{IC.ADAPTER_REPO}@{IC.ADAPTER_REV}:{IC.OLENS_SUBDIR}",
        "items_sha256": I._sha(I.ITEMS_PATH),
        "lens_layers": LENS_LAYERS,
        "positions": POSITIONS,
        "lp_planned": LP_PLANNED,
        "lp_fallback": LP_FALLBACK,
        "seed": SEED,
    }
    try:
        import torch

        env["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except ImportError:  # pragma: no cover
        pass
    write_json(OUT / "env.json", env)


# --------------------------------------------------------------------- the session


def _sites(ids: list[int], item_id: str, tokenizer: Any, start: int) -> list[RS.ReadSite]:
    n, h = len(ids), RS.ids_sha256(ids)
    return [
        RS.ReadSite(start + k, item_id, off, n + off, tokenizer.decode([ids[n + off]]), n, h)
        for k, off in enumerate(POSITIONS)
    ]


def run_all(args: argparse.Namespace) -> int:
    import torch

    from emx.i1a import capture as I1cap
    from emx.i1a import olens as I1olens
    from p11 import patch as P

    register_seeds()
    t0 = float(os.environ.get("P11_T0", time.time()))
    max_hours = float(os.environ.get("P11_MAX_HOURS", "2.2"))
    T = Timer(OUT / "timing.json")
    items = I.load()[: args.limit or None]
    by_id = {it.id: it for it in items}
    paths = json.loads((OUT / "download_paths.json").read_text())
    status: dict[str, Any] = {"utc_start": utc(), "n_items": len(items), "complete": False}

    def finish(rc: int, **kw: Any) -> int:
        status.update(kw, utc_end=utc(), session_hours=round((time.time() - t0) / 3600, 3))
        write_json(OUT / "status.json", status)
        return rc

    with T.step("model_load"):
        bk = I1cap.load_backend(paths["qwen_dir"], device=args.device, dtype=args.dtype)
    tok = bk.tokenizer

    # renders and the suffix contract
    with T.step("renders") as rec:
        R: dict[str, dict[str, list[int]]] = {}
        n_suffix: dict[str, int] = {}
        bad: list[str] = []
        for it in items:
            R[it.id] = {a: P.render_ids(it.prompt(a), I.PREFILL, tok) for a in I.ARMS}
            plain = R[it.id]["plain"]
            # the plain render is <|im_start|>user\n + question + template tail + prefill
            n = len(plain) - 3
            for a in ("author", "mention"):
                if I.common_suffix(plain, R[it.id][a]) < n:
                    bad.append(f"{it.id}:{a}")
            for a in I.ARMS:
                stem = tuple(tok.decode([x]) for x in R[it.id][a][-3:])
                if stem != RS.STEM_TOKENS:
                    bad.append(f"{it.id}:{a}:stem={stem}")
            n_suffix[it.id] = n
        rec.update(n_bad=len(bad), bad=bad[:10])
        write_json(
            OUT / "renders.json",
            {
                "n_suffix": n_suffix,
                "lengths": {i: {a: len(v) for a, v in r.items()} for i, r in R.items()},
                "bad": bad,
            },
        )
    if bad:
        log(f"SMOKE FAILED renders: {bad[:5]}")
        return finish(3, failed="renders")

    def suffix_patch(it: I.Item, premise: str, layer: int, store: dict, alpha: float = 1.0) -> Any:
        """The premise run's suffix residual at ``layer`` (``alpha`` < 1: interpolated from the
        plain run's own residual, the dose arms)."""
        v = store[(it.id, premise)][layer]
        if alpha != 1.0:
            own = store[(it.id, "plain")][layer]
            v = (own.float() + alpha * (v.float() - own.float())).to(v.dtype)
        return P.Patch(layer, v, len(R[it.id]["plain"]))

    # smoke: identity patch
    with T.step("smoke_identity") as rec:
        diffs, repeats = [], []
        for it in items[:3]:
            ids = R[it.id]["plain"]
            own = P.capture_suffix(bk, ids, [IDENTITY_LAYER], n_suffix[it.id])
            base = P.last_logits(bk, ids)
            again = P.last_logits(bk, ids)
            pat = P.last_logits(bk, ids, P.Patch(IDENTITY_LAYER, own[IDENTITY_LAYER], len(ids)))
            diffs.append(float((pat - base).abs().max()))
            repeats.append(float((again - base).abs().max()))
        ok = all(d < IDENTITY_TOL or d <= r for d, r in zip(diffs, repeats, strict=True))
        rec.update(max_abs_logit_diff=diffs, repeat_unpatched_diff=repeats, passed=ok)
    status["smoke_identity"] = {"diffs": diffs, "repeats": repeats, "passed": ok}
    if not ok:
        log(f"SMOKE FAILED identity patch: {diffs} (repeat {repeats})")
        return finish(3, failed="smoke_identity")

    # answers: plain, direct premises, and the L_p sweep
    lps = (*LP_PLANNED, *LP_FALLBACK)
    answers: list[dict[str, Any]] = []
    store: dict[tuple[str, str], dict[int, Any]] = {}
    mx = args.max_new_tokens or MAX_NEW_ANSWER

    def answer_row(it: I.Item, kind: str, premise: str | None, lp: int | None, text: str) -> None:
        a = core.first_answer(text)
        answers.append(
            {
                "id": it.id,
                "kind": kind,
                "premise": premise,
                "lp": lp,
                "text": text,
                "answer": a,
                "class": I.answer_class(a, it),
            }
        )

    with T.step("answers") as rec:
        for it in items:
            answer_row(it, "plain", None, None, P.greedy(bk, R[it.id]["plain"], mx))
            store[(it.id, "plain")] = P.capture_suffix(bk, R[it.id]["plain"], lps, n_suffix[it.id])
            for prem in ("author", "mention"):
                answer_row(it, "direct", prem, None, P.greedy(bk, R[it.id][prem], mx))
                store[(it.id, prem)] = P.capture_suffix(bk, R[it.id][prem], lps, n_suffix[it.id])
                for L in lps:
                    text = P.greedy(bk, R[it.id]["plain"], mx, suffix_patch(it, prem, L, store))
                    answer_row(it, "patched", prem, L, text)
        core.write_jsonl(OUT / "answers.jsonl", answers)
        rec["n"] = len(answers)

    plain_rows = [a for a in answers if a["kind"] == "plain"]
    first10 = sum(a["class"] == "stay" for a in plain_rows[:10])
    greedy_ok = first10 >= min(GREEDY_MIN, len(plain_rows[:10]) - 2)
    status["smoke_greedy"] = {
        "first10_stay": first10,
        "all_stay": sum(a["class"] == "stay" for a in plain_rows),
        "n": len(plain_rows),
        "passed": greedy_ok,
    }
    if not greedy_ok:
        log(f"SMOKE FAILED greedy: plain greedy = modal on {first10}/10")
        return finish(3, failed="smoke_greedy")

    rates: dict[int, dict[str, float]] = {}
    for L in lps:
        rates[L] = {}
        for prem in ("author", "mention"):
            rows = [
                a
                for a in answers
                if a["kind"] == "patched" and a["lp"] == L and a["premise"] == prem
            ]
            rates[L][prem] = sum(a["class"] == "flip" for a in rows) / max(len(rows), 1)
    lp, how = choose_lp(rates)
    forced = os.environ.get("P11_FORCE_LP")
    if forced:  # follow-up F6 (log.md): the patched arms at a layer the rule rejected
        lp, how = int(forced), f"forced by P11_FORCE_LP (follow-up F6; rule gave {lp}, {how})"
    write_json(
        OUT / "sweep.json",
        {
            "chosen_lp": lp,
            "how": how,
            "status": "ok" if lp is not None else "failed",
            "rule": f"max author flip among L_p with mention flip <= {MENTION_MAX}; >= {FLIP_MIN}",
            "rates": {str(L): v for L, v in rates.items()},
            "utc": utc(),
        },
    )
    log(f"L_p sweep: {rates} -> chosen {lp} ({how})")
    status["chosen_lp"], status["lp_how"] = lp, how

    if lp is not None:
        with T.step("dose_answers"):
            for it in items:
                for arm, alpha in DOSE.items():
                    text = P.greedy(
                        bk, R[it.id]["plain"], mx, suffix_patch(it, "author", lp, store, alpha)
                    )
                    answer_row(it, "dose", "author", lp, text)
                    answers[-1]["alpha"] = alpha
            core.write_jsonl(OUT / "answers.jsonl", answers)

    # read captures for every arm
    arms = [a for a in (*ARM_ORDER, *DOSE) if lp is not None or not a.startswith("patched")]
    H: dict[str, Any] = {}
    S: dict[str, list[RS.ReadSite]] = {}
    with T.step("capture"):
        for arm in arms:
            base = arm.removeprefix("patched_").split("_a")[0] if arm.startswith("patched") else arm
            src = "plain" if arm.startswith("patched") else base
            sites: list[RS.ReadSite] = []
            h = None
            for it in items:
                ids = R[it.id][src]
                ss = _sites(ids, it.id, tok, len(sites))
                alpha = DOSE.get(arm, 1.0)
                p = suffix_patch(it, base, lp, store, alpha) if arm.startswith("patched") else None
                acts = P.capture(bk, ids, LENS_LAYERS, [s.abs_pos for s in ss], p)
                x = torch.stack([acts[L] for L in LENS_LAYERS], dim=1).to(torch.bfloat16)
                h = x if h is None else torch.cat([h, x], dim=0)
                sites.extend(ss)
            H[arm], S[arm] = h, sites
        del store
    for arm in arms:
        core.write_jsonl(OUT / "sites" / f"{arm}.jsonl", [asdict(s) for s in S[arm]])

    # free linear reader on the same activations (lit.md check 4): it cannot re-answer
    with T.step("logit_lens"):
        from emx.i1a import token_readers as tr

        ll = tr.bind_reader("logit_lens", bk)
        for arm in arms:
            core.write_jsonl(
                OUT / "logit_lens" / f"{arm}.jsonl",
                RS.read_tokens(ll, S[arm], LENS_LAYERS, H[arm], layers=LENS_LAYERS),
            )
        del ll

    sampling = I1olens.default_sampling()
    if args.max_new_tokens:
        sampling.max_new_tokens = args.max_new_tokens
    with T.step("olens_bind"):
        ol = I1olens.PublicOLens(sampling=sampling)
        ol.bind(bk, Path(paths["olens_dir"]))

    with T.step("smoke_shuffled") as rec:
        source_of = O.item_derangement(S["plain"])
        cells = O.smoke_cells(S["plain"], source_of, 20, SHUFFLED_LAYER, SEED)
        p = SAMPLES / "smoke_shuffled.jsonl"
        O.generate(
            ol,
            "p11_smoke_shuffled",
            cells,
            sites=S["plain"],
            layers_stored=LENS_LAYERS,
            h=H["plain"],
            out=p,
            sampling=sampling,
        )
        chk = shuffled_check([O.row_dict(r) for r in O.read_rows(p)], by_id)
        rec.update(neither=chk["neither"], passed=chk["passed"])
    status["smoke_shuffled"] = {k: v for k, v in chk.items() if k != "cells"}
    if not chk["passed"]:
        log(f"SMOKE FAILED shuffled: {chk['neither']}/20 cells name neither bridge")
        return finish(3, failed="smoke_shuffled")

    draws: dict[str, Any] = {}
    stopped = None
    for arm in arms:
        for k in range(n_draws(arm)):
            d = draw_name(arm, k)
            if time.time() - t0 > max_hours * 3600:
                stopped = f"time: not started {d}"
                break
            cells = O.grid_cells(S[arm], {it.id for it in items}, LENS_LAYERS, POSITIONS)
            with T.step(f"olens_{d}") as rec:
                res = O.generate(
                    ol,
                    d,
                    cells,
                    sites=S[arm],
                    layers_stored=LENS_LAYERS,
                    h=H[arm],
                    out=SAMPLES / f"{d}.jsonl",
                    sampling=sampling,
                )
                rows = [O.row_dict(r) for r in O.read_rows(SAMPLES / f"{d}.jsonl")]
                res["unparseable_frac"] = round(unparseable_frac(rows), 4)
                rec.update(res)
            draws[d] = res
            if res["unparseable_frac"] > UNPARSEABLE_MAX:
                stopped = f"unparseable: {d} {res['unparseable_frac']}"
                break
        if stopped:
            break
    status["draws"] = draws
    if stopped:
        log(f"P11_STOP {stopped}")
        status["stopped"] = stopped

    if not stopped and not args.skip_prompt_only:
        with T.step("prompt_only") as rec:
            rec["n"] = prompt_only(bk, items, args.max_new_tokens or sampling.max_new_tokens)
    complete = not stopped and all(v.get("complete") for v in draws.values())
    return finish(0, complete=complete)


def prompt_only(bk: Any, items: list[I.Item], max_new_tokens: int) -> int:
    """Follow-up (c): stock Qwen (adapter off) asked what the model is thinking at the end of the
    plain prompt; ``N_PROMPT_ONLY`` samples per item at the lens's sampling settings, seeded
    ``PROMPT_ONLY_SEED + item index``. Resumable per item."""
    import torch

    from emx.i1a import olens as I1olens
    from p11 import patch as P

    s = I1olens.default_sampling()
    path = OUT / "prompt_only.jsonl"
    done = {r["id"] for r in core.read_jsonl(path)} if path.exists() else set()
    rows = core.read_jsonl(path) if path.exists() else []
    tok = bk.tokenizer
    for k, it in enumerate(items):
        if it.id in done:
            continue
        from wsbench.produce.render import render
        from wsbench.readplan import ReadSpec

        text = PROMPT_ONLY_TEMPLATE.format(question=it.question, prefill=I.PREFILL)
        ids = list(
            render(
                ReadSpec("p11", it.id, "chat", {"kind": "last_n", "n": 1}, [], text=text), tok
            ).ids
        )
        x = torch.tensor([ids], device=bk.device)
        seed = PROMPT_ONLY_SEED + k
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        with torch.no_grad(), P._plain(bk.model):
            out = bk.model.generate(
                input_ids=x,
                attention_mask=torch.ones_like(x),
                do_sample=True,
                temperature=s.temperature,
                top_p=s.top_p,
                top_k=s.top_k,
                max_new_tokens=max_new_tokens,
                num_return_sequences=N_PROMPT_ONLY,
                pad_token_id=tok.pad_token_id or tok.eos_token_id,
            )
        texts = tok.batch_decode(out[:, len(ids) :], skip_special_tokens=True)
        new = [
            {"id": it.id, "sample_index": j, "seed": seed, "text": t.strip()}
            for j, t in enumerate(texts)
        ]
        rows.extend(new)
        core.write_jsonl(path, rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p11.session")
    ap.add_argument("step", choices=("download", "env", "all"))
    ap.add_argument("--artifacts-dir", default=os.environ.get("P11_ARTIFACTS", DEFAULT_ARTIFACTS))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--limit", type=int, default=0, help="first N items (smoke)")
    ap.add_argument("--max-new-tokens", type=int, default=0, help="tests: shorter generations")
    ap.add_argument("--skip-prompt-only", action="store_true")
    args = ap.parse_args(argv)
    if args.step == "download":
        T = Timer(OUT / "timing.json")
        with T.step("download"):
            step_download(Path(args.artifacts_dir))
        return 0
    if args.step == "env":
        step_env()
        return 0
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
