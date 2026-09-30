"""Selection rules over the saved Oracle Lens samples (prereg v0.4 §4.3; implementation plan §0
and §2 U6).

* **consensus**: per sample, the content set is ``set(re.findall(CONSENSUS_TOKEN_RE,
  text.lower())) - STOPWORDS``; its score is the mean Jaccard with the other samples (the Jaccard
  of two empty sets is 0). Candidates are the samples with a non-empty content set, else the
  samples with non-empty stripped text, else sample 0. The argmax over candidates wins; ties go to
  the lowest index.
* **best FVE**: the argmax ``joint_fve`` among valid samples, ties to the lowest index; with no
  valid sample, sample 0.
* **random**: the gate draw (sample 0), i.e. the ``olens_rl600`` arm itself, so no file here.

Selected readouts are written as wsbench readouts-contract rows ``{"id","layer","pos","token",
"samples":[text]}``. ``token`` is taken from the family's activation index
(``contracts.acts_index_path``, ``SiteRow.token`` at ``SampleRow.site_index``) when that file
exists; otherwise the key is omitted (wsbench ``readouts.Cell.token`` is optional). Hallucination
selections cover only the 75-conversation subset (``contracts.SUBSET_PATH``).

A sidecar ``<ANALYSIS_DIR>/selection/sel_<rule>_<family>.jsonl`` records the chosen
``sample_idx`` per cell (never read by the benchmark).

Import-light: stdlib plus ``emx.i1a.contracts``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from emx.i1a import contracts as C

Rule = Literal["consensus", "bestfve"]
RULES: tuple[str, ...] = ("consensus", "bestfve")
_TOKEN_RE = re.compile(C.CONSENSUS_TOKEN_RE)

CellKey = tuple[str, int, int]  # (id, layer, pos)


# --- the rules -----------------------------------------------------------------------------------


def content_tokens(text: str) -> frozenset[str]:
    """Lowercased ``[a-z0-9]+`` tokens of ``text`` minus the frozen stopword list."""
    return frozenset(_TOKEN_RE.findall(text.lower())) - C.STOPWORDS


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|a ∩ b| / |a ∪ b|; two empty sets score 0 (not 1)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def consensus_scores(texts: Sequence[str]) -> list[float]:
    """Mean Jaccard of each sample's content set with every other sample's."""
    sets = [content_tokens(t) for t in texts]
    n = len(sets)
    if n <= 1:
        return [0.0] * n
    return [sum(jaccard(sets[i], sets[j]) for j in range(n) if j != i) / (n - 1) for i in range(n)]


def consensus_pick(texts: Sequence[str]) -> int:
    """Index of the consensus sample (see module docstring for the candidate fallbacks)."""
    if not texts:
        raise ValueError("consensus_pick: no samples")
    scores = consensus_scores(texts)
    cands = [i for i, t in enumerate(texts) if content_tokens(t)]
    if not cands:
        cands = [i for i, t in enumerate(texts) if t.strip()]
    if not cands:
        return 0
    best = cands[0]
    for i in cands[1:]:
        if scores[i] > scores[best]:  # strict: ties keep the lowest index
            best = i
    return best


def bestfve_pick(fves: Sequence[float], valid: Sequence[bool]) -> int:
    """Argmax joint FVE among valid samples, ties to the lowest index; no valid sample -> 0."""
    if len(fves) != len(valid):
        raise ValueError("bestfve_pick: fves and valid differ in length")
    best: int | None = None
    for i, (f, ok) in enumerate(zip(fves, valid, strict=True)):
        if ok and (best is None or f > fves[best]):
            best = i
    return 0 if best is None else best


def bullets_per_readout(text: str) -> int:
    """Coverage guard: the number of ``- `` bullet lines (olens_draft
    ``rl_reward.split_bullets`` without the k_max cap and without its scaffolding strip: a line
    counts when ``line.strip()`` starts with ``"- "`` and has non-blank text after it); 1 for
    non-empty text with no bullet lines; 0 for empty text."""
    n = sum(1 for ln in text.splitlines() if ln.strip().startswith("- ") and ln.strip()[2:].strip())
    if n:
        return n
    return 1 if text.strip() else 0


# --- file IO -------------------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_subset_ids(path: Path | None = None) -> set[str]:
    return set(json.loads(Path(path or C.SUBSET_PATH).read_text(encoding="utf-8"))["ids"])


def load_site_tokens(family: str) -> dict[int, str] | None:
    """``site_index -> SiteRow.token`` from the activation index, or None if it is absent."""
    path = C.acts_index_path(family)
    if not path.exists():
        return None
    return {int(r["index"]): r["token"] for r in _read_jsonl(path)}


def load_samples(family: str) -> dict[CellKey, list[dict]]:
    """SampleRows grouped per (id, layer, pos), sorted by sample_idx. A duplicated
    (cell, sample_idx) is an error (resumed generation must not duplicate)."""
    groups: dict[CellKey, dict[int, dict]] = defaultdict(dict)
    for r in _read_jsonl(C.samples_path(family)):
        key = (r["id"], int(r["layer"]), int(r["pos"]))
        k = int(r["sample_idx"])
        if k in groups[key]:
            raise ValueError(f"duplicate sample {k} for cell {key} in {C.samples_path(family)}")
        groups[key][k] = r
    return {key: [d[k] for k in sorted(d)] for key, d in groups.items()}


def load_fve(family: str) -> dict[tuple[str, int, int, int], dict]:
    """FveRows keyed by (id, layer, pos, sample_idx)."""
    out = {}
    for r in _read_jsonl(C.fve_path(family)):
        out[(r["id"], int(r["layer"]), int(r["pos"]), int(r["sample_idx"]))] = r
    return out


def _contract_row(s: dict, text: str, tokens: dict[int, str] | None) -> dict:
    row: dict = {"id": s["id"], "layer": int(s["layer"]), "pos": int(s["pos"])}
    if tokens is not None:
        row["token"] = tokens[int(s["site_index"])]
    row["samples"] = [text]
    return row


def _write_jsonl(path: Path, rows: Sequence[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp.replace(path)
    return path


def _cell_order(key_rows: tuple[CellKey, list[dict]]) -> tuple[int, int]:
    key, rows = key_rows
    return (key[1], int(rows[0]["site_index"]))  # layer, then site (the canonical batch order)


def bestfve_allowed(overrides: dict[str, str] | None = None) -> tuple[bool, str]:
    """Best FVE may be used only if every reconstructor check (``contracts.CHECK_KIND`` kind
    ``fve``, after explicit overrides) has a file with ``passed: true`` (prereg v0.4 §4.7).
    Returns (allowed, reason)."""
    kinds = {**C.CHECK_KIND, **(overrides or {})}
    bad = []
    for name in sorted(n for n, k in kinds.items() if k == "fve"):
        path = C.check_path(name)
        if not path.exists():
            bad.append(f"{name}: missing")
            continue
        passed = json.loads(path.read_text(encoding="utf-8")).get("passed")
        if passed is not True:
            bad.append(f"{name}: passed={passed}")
    if bad:
        return False, "best FVE dropped: " + "; ".join(bad)
    return True, ""


def select_family(
    family: str,
    rule: str,
    *,
    subset_ids: set[str] | None = None,
    n_samples: int = C.N_SAMPLES,
) -> Path:
    """Write ``readouts/sel_<rule>/<family>.jsonl``. Every selected cell must carry all
    ``n_samples`` samples (0..n-1) and, for best FVE, an FveRow for each; anything else is an
    error. Best FVE refuses to run unless ``bestfve_allowed()``. Hallucination is restricted to ``subset_ids`` (default: the committed subset)."""
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}; expected one of {RULES}")
    if rule == "bestfve":
        allowed, reason = bestfve_allowed()
        if not allowed:  # never write sel_bestfve readouts without the four FVE checks
            raise RuntimeError(reason)
    if family == "hallucination" and subset_ids is None:
        subset_ids = load_subset_ids()
    groups = load_samples(family)
    if subset_ids is not None:
        groups = {k: v for k, v in groups.items() if k[0] in subset_ids}
    fve = load_fve(family) if rule == "bestfve" else {}
    tokens = load_site_tokens(family)
    rows, picks = [], []
    for key, samples in sorted(groups.items(), key=_cell_order):
        idxs = [int(s["sample_idx"]) for s in samples]
        if idxs != list(range(n_samples)):
            raise ValueError(f"cell {key}: samples {idxs}, expected 0..{n_samples - 1}")
        texts = [s["text"] for s in samples]
        if rule == "consensus":
            pick = consensus_pick(texts)
        else:
            frows = []
            for k in idxs:
                fr = fve.get((*key, k))
                if fr is None:
                    raise ValueError(f"cell {key}: no FveRow for sample {k}")
                frows.append(fr)
            pick = bestfve_pick(
                [float(fr["joint_fve"]) for fr in frows], [bool(fr["valid"]) for fr in frows]
            )
        rows.append(_contract_row(samples[pick], texts[pick], tokens))
        picks.append({"id": key[0], "layer": key[1], "pos": key[2], "sample_idx": pick})
    out = _write_jsonl(C.readouts_path(f"sel_{rule}", family), rows)
    _write_jsonl(C.ANALYSIS_DIR / "selection" / f"sel_{rule}_{family}.jsonl", picks)
    return out


def write_sample_arms(family: str = "basic_readout_mt") -> list[Path]:
    """Write ``olens_s1..olens_s9`` (one arm per extra sample) for the ceiling and the spread."""
    groups = load_samples(family)
    tokens = load_site_tokens(family)
    paths = []
    ordered = sorted(groups.items(), key=_cell_order)
    for k in range(1, C.N_SAMPLES):
        rows = []
        for key, samples in ordered:
            s = next((s for s in samples if int(s["sample_idx"]) == k), None)
            if s is None:
                raise ValueError(f"cell {key}: no sample {k}")
            rows.append(_contract_row(s, s["text"], tokens))
        paths.append(_write_jsonl(C.readouts_path(f"olens_s{k}", family), rows))
    return paths
