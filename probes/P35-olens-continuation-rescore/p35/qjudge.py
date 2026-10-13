"""P35 follow-up (a), exploratory: is a bullet's content among Qwen's own sampled continuations?

    uv run python -m p35.qjudge run --judge glm|gemini [--layer L] [--smoke]
    uv run python -m p35.qjudge report        # results/q_summary.json

Cells are ``p35.hjudge``'s lens cells at ``LAYERS``, with the same blind mix of true-site and
control-site bullets in the same numbering. The judge sees the end of the response before the
read point, the ``N_SAMPLES`` continuations Qwen3.6-27B sampled from the exact read prefix
(``p35.qsample``), and the bullets, and answers per bullet YES (its main specific content is
said in at least one sample), NO, or GENERIC. Cells with a sample missing are skipped.

Report: per judge, the share of YES among non-GENERIC bullets, true vs control (conversation
bootstrap, as ``p35.hanalyze``), overall and within the (H) classes of the same judge
(``realised``, ``alt_continuation``, ``commentary``, by the H label of that bullet).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

from p35 import hanalyze as A
from p35 import hjudge as H
from p35 import qsample as Q

LAYERS = (20, 44, 60)
TAIL_CHARS = 600
VERSION = "q1"

SYSTEM = ("You compare short text fragments with sampled continuations of a text. Output only "
          "the JSON object.")

USER = """An AI assistant was answering a user. Here is the end of what it had written so far:

<context>
{tail}
</context>

From exactly this point, the assistant's model was sampled {k} times. The {k} sampled
continuations:

{samples}

FRAGMENTS:
{fragments}

For each fragment, answer:
- "YES": its main specific content (its particular facts, values, names, items, questions or
  wording; a paraphrase or a translation into another language counts) is said in at least one
  of the sampled continuations.
- "NO": its main specific content is not said in any of them. Sharing only the topic or the
  format is not enough.
- "GENERIC": it has no specific content (filler, formatting, a stock phrase, garbled text).

Reply with a JSON object mapping every fragment number (as a string) to its answer, e.g.
{{"1": "YES", "2": "NO"}}"""

ANSWERS = ("YES", "NO", "GENERIC")


def samples_by_site() -> dict[str, list[str]]:
    out: dict[str, dict[int, str]] = {}
    if not Q.CACHE.exists():
        return {}
    for x in Q.CACHE.read_text().splitlines():
        r = json.loads(x)
        if r.get("text") is not None:
            out.setdefault(r["key"], {})[r["seed"]] = r["text"]
    return {k: [v[s] for s in sorted(v)] for k, v in out.items() if len(v) >= Q.N_SAMPLES}


def render(cell: H.Cell, item: dict, samples: list[str]) -> str:
    site = next(s for s in item["sites"] if s["pos"] == cell.pos)
    before = item["response"][: site["char"]]
    tail = ("..." if len(before) > TAIL_CHARS else "") + before[-TAIL_CHARS:]
    smp = "\n".join(f"<sample {i + 1}>{s}</sample {i + 1}>" for i, s in enumerate(samples))
    frags = "\n".join(f"[{b['n']}] {b['text']}" for b in cell.bullets)
    return USER.format(tail=tail, k=len(samples), samples=smp, fragments=frags)


def parse(text: str | None, n: int) -> dict[int, str] | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and len(obj) == 1 and isinstance(next(iter(obj.values())), dict):
        obj = next(iter(obj.values()))  # GLM sometimes wraps the map: {"answer": {...}}
    out = {}
    for k, v in obj.items() if isinstance(obj, dict) else ():
        try:
            i = int(str(k).strip())
        except ValueError:
            return None
        a = str(v).strip().upper()
        if a not in ANSWERS:
            return None
        out[i] = a
    return out if set(out) == set(range(1, n + 1)) else None


def cache_path(judge: str):
    return H.RESULTS / f"q_judge_{judge}.{VERSION}.jsonl"


def _texts(judge: str) -> dict[str, str | None]:
    p = cache_path(judge)
    if not p.exists():
        return {}
    return {(r := json.loads(x))["key"]: r.get("text") for x in p.read_text().splitlines()}


def replies(judge: str) -> dict[str, str | None]:
    """A judge's replies, where a retry judge's (``hjudge.RETRY_OF``) non-empty reply replaces
    the base reply it retried."""
    out = _texts(judge)
    for retry, base in H.RETRY_OF.items():
        if base == judge:
            out.update({k: t for k, t in _texts(retry).items() if t})
    return out


def run(judge: str, layer: int | None, smoke: bool) -> None:
    sys.path.insert(0, str(H.ROOT / "src"))
    from emx import probe_llm

    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    smp = samples_by_site()
    cells = [c for c in H.load_cells() if c.reader == "lens" and c.bullets
             and c.layer in (LAYERS if layer is None else (layer,))
             and f"{c.id}|p{c.pos}" in smp]
    if smoke:
        cells = cells[:: max(1, len(cells) // 20)][:20]
    if judge in H.RETRY_OF:
        base = _texts(H.RETRY_OF[judge])
        cells = [c for c in cells if c.key in base and parse(base[c.key], len(c.bullets)) is None]
    route, reasoning, max_tokens = H.JUDGES[judge]
    calls = [probe_llm.Call(
        key=c.key, route=route, user=render(c, items[c.id], smp[f"{c.id}|p{c.pos}"]),
        system=SYSTEM, temperature=0.0, seed=0, max_tokens=max_tokens, reasoning=reasoning,
        json_mode=True) for c in cells]
    tag = f"q_{'smoke' if smoke else 'L' + str(layer)}_{judge}"
    print(f"{tag}: {len(calls)} calls; projected ${probe_llm.project_usd(calls):.2f}", flush=True)
    res = probe_llm.run_calls(calls, exp="P35", cache=cache_path(judge), purpose=f"p35_{tag}",
                              concurrency=int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32")))
    ok = sum(1 for c in cells if (r := res.get(c.key)) and parse(r.text, len(c.bullets)))
    print(f"{tag}: parsed {ok}/{len(cells)}; ${sum(r.usd for r in res.values()):.3f}",
          flush=True)


N_CALIB = 300
CALIB_WORDS = 24


def _first_words(text: str, n: int = CALIB_WORDS) -> str:
    _, head, _ = H.segments(text, 0, n)
    return head.strip()


def calib_cells() -> list[tuple[str, str, dict, list[str], str]]:
    """Calibration (follow-up a2): ``(key, kind, item, samples shown, fragment)`` at
    ``N_CALIB`` random sites. Kind "realised": the realised continuation's first
    ``CALIB_WORDS`` words against all samples. Kind "sample": the last sample's first words
    against the others. Sites whose fragment is empty are skipped."""
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    smp = samples_by_site()
    sites = sorted(smp)
    out = []
    for key in random.Random(H.SEED + 2).sample(sites, N_CALIB):
        iid, pos = key.split("|p")
        it = items[iid]
        site = next(s for s in it["sites"] if s["pos"] == int(pos))
        real = _first_words(it["response"][site["char"]:])
        held = _first_words(smp[key][-1])
        if real:
            out.append((f"calib_realised|{key}", "realised", it, smp[key], real))
        if held:
            out.append((f"calib_sample|{key}", "sample", it, smp[key][:-1], held))
    return out


def run_calib(judge: str) -> None:
    sys.path.insert(0, str(H.ROOT / "src"))
    from emx import probe_llm

    route, reasoning, max_tokens = H.JUDGES[judge]
    calls = []
    for key, _, it, samples, frag in calib_cells():
        iid, pos = key.split("|", 1)[1].split("|p")
        cell = H.Cell(key, "calib", iid, 0, int(pos), "", None,
                      [{"n": 1, "src": "calib", "j": 0, "text": frag}])
        calls.append(probe_llm.Call(
            key=key, route=route, user=render(cell, it, samples), system=SYSTEM,
            temperature=0.0, seed=0, max_tokens=max_tokens, reasoning=reasoning,
            json_mode=True))
    print(f"calib_{judge}: {len(calls)} calls; ${probe_llm.project_usd(calls):.2f}", flush=True)
    res = probe_llm.run_calls(calls, exp="P35", cache=H.RESULTS / f"q_calib_{judge}.jsonl",
                              purpose=f"p35_q_calib_{judge}",
                              concurrency=int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32")))
    ok = sum(1 for r in res.values() if parse(r.text, 1))
    print(f"calib_{judge}: parsed {ok}/{len(calls)}", flush=True)


def calib_report() -> dict:
    out = {}
    for judge in ("glm", "gemini"):
        p = H.RESULTS / f"q_calib_{judge}.jsonl"
        if not p.exists():
            continue
        acc: dict[str, list[int]] = {"realised": [], "sample": []}
        n_bad = 0
        for x in p.read_text().splitlines():
            r = json.loads(x)
            lab = parse(r.get("text"), 1)
            if lab is None:
                n_bad += 1
            elif lab[1] != "GENERIC":
                acc[r["key"].split("|")[0].split("_")[1]].append(lab[1] == "YES")
        out[judge] = {"n_unparsed": n_bad, **{k: {"yes": sum(v) / len(v) if v else None,
                                                    "n": len(v)} for k, v in acc.items()}}
    return out


def report() -> dict:
    cells = {c.key: c for c in H.load_cells()}
    out: dict = {"layers": LAYERS, "n_samples": Q.N_SAMPLES}
    for judge in ("glm", "gemini"):
        rep = replies(judge)
        if not rep:
            continue
        hrows = {(r["key"], r["src"], r["j"]): r for r in A.bullet_rows(judge, list(
            cells.values()))[0]}
        rows, bad = [], 0
        for key, text in rep.items():
            c = cells[key]
            lab = parse(text, len(c.bullets))
            if lab is None:
                bad += 1
                continue
            for b in c.bullets:
                if lab[b["n"]] == "GENERIC":
                    continue
                h = hrows.get((c.key, b["src"], b["j"]))
                rows.append({"id": c.id, "layer": c.layer, "src": b["src"],
                             "has_control": c.control_pos is not None,
                             "in_sample": lab[b["n"]] == "YES", "h": h})
        true = [r for r in rows if r["src"] == "true"]
        ctrl = [r for r in rows if r["src"] == "control"]
        sec = {"n_unparsed": bad, "true": A.clustered_share(true, "in_sample"),
               "control": A.clustered_share(ctrl, "in_sample"),
               "true_minus_control": A.clustered_diff(
                   [r for r in true if r["has_control"]], ctrl, "in_sample"),
               "by_layer": {str(L): A.clustered_share([r for r in true if r["layer"] == L],
                                                      "in_sample", 2000) for L in LAYERS}}
        for cls in ("realised", "alt_continuation", "commentary"):
            sec[f"true_{cls}"] = A.clustered_share(
                [r for r in true if r["h"] and r["h"][cls]], "in_sample")
            sec[f"control_{cls}"] = A.clustered_share(
                [r for r in ctrl if r["h"] and r["h"][cls]], "in_sample")
        out[judge] = sec
    out["calibration"] = calib_report()
    (H.RESULTS / "q_summary.json").write_text(json.dumps(out, indent=1) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--judge", required=True, choices=sorted(H.JUDGES))
    r.add_argument("--layer", type=int)
    r.add_argument("--smoke", action="store_true")
    r.add_argument("--calib", action="store_true", help="follow-up a2 calibration only")
    sub.add_parser("report")
    a = ap.parse_args(argv)
    if a.cmd == "run" and a.calib:
        run_calib(a.judge)
    elif a.cmd == "run":
        run(a.judge, a.layer, a.smoke)
    else:
        print(json.dumps(report(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
