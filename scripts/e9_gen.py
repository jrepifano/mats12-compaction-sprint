"""E9 sampled-EM generation and the pod job's helpers (unit E2; prereg §4.5, §4.6 "Sampled EM",
§4.7 step 2, §4.13; implementation plan v4 "Frozen in U0").

Ported from E3 at commit 7b15be1 (`scripts/e3_gen.py`: question selection, request plan, pinned
question hashes; `scripts/e1_gen_vllm.py`: the vLLM engine settings and sampling parameters) and
uses `emx.gen_common` for the request rows, per-request seeds and post-processing. Unlike E3 the
base is a local continued-pretrained model directory (or the pinned checkpoint, "hub"), so the
E1 condition grammar does not apply: each sampled model is named by its E9 model id.

Subcommands (all but `generate` and `roundtrip` are stdlib only and run under any python):

  generate --kind cs|smoke --run-dir R --base <dir|hub> [--arm A --cpt-seed S]
           [--adapters-root D] [--cuts CUT...] [--no-vulnerable] [--dry-run]
      cs: the untuned CPT model `e9-{arm}-c{s}-base`, the full fine-tunes
          `e9-{arm}-c{s}-{bad|good}-a11-k1287` (full qsets) and, for the background arm at the
          sampling seed (bg, CPT seed 1), their step-20 checkpoints `...-k20` (development only).
          Adapters where unit E1's `e9_q20.py full --adapters-root D` leaves them:
          D/<run_id>/full-{d}-a11/ (final) and its step-0020/ subfolder (step 20).
      smoke: E1b's seed-2 step-20 bad and good adapters on the checkpoint (development only),
          named as E1's scores name them, `e9-e1b-c0-{d}-a2-k20`.
      -> R/gen/<model>.csv (exactly emx.contracts.GEN_COLUMNS) and R/gen/manifest.json (every
         file with sha256, row counts per qset, the request hash, the template hash, the base's
         and adapter's weight fingerprints). A model is skipped (resume) only if its CSV, request
         list, base SHA256SUMS digest and adapter file hashes all match the manifest.
  roundtrip --base DIR --adapter DIR --out R/roundtrip.json
      vLLM loads the saved base plus a peft adapter and generates two answers (smoke, §4.13).
  stream-check --stream S.npy [--run-id RUN] [--smoke] --out R/stream_manifest.json
      the stream file's sha256 must equal its manifest's stream_sha256 and the manifest's
      `smoke` flag must match --smoke (unit C's build-smoke stream only for the smoke run, never
      for cq/cs); the manifest is copied.
  stream-seed --stream S.npy      print the stream manifest's cpt_seed (the smoke's CPT seed).
  done --run-dir R --run RUN --kind K [--arm A --cpt-seed S]
      R/DONE.json listing EVERY file under R with its sha256 and the attempt id, after checking
      that the kind's RUN_INVENTORY (expanded) is present, the generation manifest matches and,
      for cs and smoke, ft/consistency.json passes for every full fine-tune; exit 1 and no DONE
      otherwise.
  failed --run-dir R --run RUN --kind K --step STEP --error MSG [--rc N]
      R/FAILED.json listing every file present with its sha256.
  timing --run-dir R --out R/timing.json      (smoke) peak memory, tokens/s, wall-clocks.
  split-versions --versions data/E9/insert/versions.jsonl --out-dir D [--manifest M] [--n-out F]
      D/{orig,corr,para}.jsonl ({"doc_id", "text"}) and D/counts.json for the insert-loss step;
      with the frozen insert manifest, every version must hold exactly its documents.
  insert-status --out R/insert_loss/status.json --status measured|deferred|failed --reason TEXT
  begin-attempt --run-dir R --run RUN --stamp UTC
      archive the previous attempt's files (all but GO) under R/attempts/<n>/ and write
      R/ATTEMPT.json with a new immutable attempt id (printed).
  conc --run-dir R --seconds N --rcs RC... --gpu-mem F --out F
      the concurrency test's record (bad + good full fine-tunes together).
  consts      shell assignments of the contract constants the pod job uses.
  runspec SPEC    validate RUN:ARM:CPT_SEED:KIND:STREAM and print shell assignments.

Question sets (contracts.SAMPLED_QSETS, VULNERABLE_SECONDARY): standard8 = data/E1/questions/
general.jsonl x 50; others27 = ext48 without its medical_advice (7) and vulnerable_user (14)
questions x 20; medical100 = the first 100 narrow questions, in file order, not among the
selection file's medical_qids, x 1; vulnerable14 = ext48's vulnerable_user questions x 10
(secondary, on unless --no-vulnerable or cut 2). Development-only mode: standard8 x 50 plus the
others27 questions among the selection file's dev_qids x 20 (prereg §4.7 step 2). Cuts
(contracts.CUTS): `drop_vulnerable_and_halve_untuned` drops vulnerable14 and halves the untuned
model (samples per question halved; a one-sample set keeps its first half of questions);
`halve_confirmation_standard8` asks standard8 x 25. Halving keeps the first sample indices, so
seeds are those of the full plan. Sampling: contracts.SAMPLING (temperature 1, top-p 1, no top-k,
600 new tokens), plain format and its stop strings, per-request seed
`request_seed("{model}@plain", qid, sample_idx)` from evaluation seed 20260928.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # the pod's vLLM venv runs this without an install

from emx.contracts import EVAL_SEED as SHARED_EVAL_SEED
from emx.contracts import GEN_COLUMNS, request_seed
from emx.e3 import contracts as E3C
from emx.e9 import contracts as C

assert C.EVAL_SEED == SHARED_EVAL_SEED, "E9's evaluation seed must be the shared one"

EVAL_FMT = C.FT_FORMAT  # "plain"
QUESTION_FILES = {
    "general": E3C.QUESTION_FILES["general"],
    "ext48": E3C.QUESTION_FILES["ext48"],
    "narrow": E3C.QUESTION_FILES["narrow"],
}
QUESTION_SHA256 = {k: E3C.QUESTION_SHA256[k] for k in QUESTION_FILES}
QSET_SOURCE = {  # E9 qset -> source question file key
    "standard8": "general",
    "others27": "ext48",
    "medical100": "narrow",
    "vulnerable14": "ext48",
}
QSET_COUNTS = {"standard8": 8, "others27": 27, "medical100": 100, "vulnerable14": 14}
OTHERS_EXCLUDED_TAGS = ("medical_advice", "vulnerable_user")
VULNERABLE_TAG = "vulnerable_user"
MODES = ("full", "dev_only")
SMOKE_CUTS_OK = ("halve_confirmation_standard8",)
CUT_DROP_VULN = "drop_vulnerable_and_halve_untuned"
CUT_HALVE_STD8 = "halve_confirmation_standard8"
MAX_MODEL_LEN = 4096  # prompts are < 1k tokens; + 600 new (as e1_gen_vllm.py)
TOP_K_DISABLED = 0  # vLLM: 0 disables top-k (contracts' -1 means "no cutoff")
MAX_LORA_RANK = 32  # R2: rsLoRA r32

# E1b's seed-2 step-20 adapters on the same checkpoint (prereg §4.7 step 2; E1b report §9). Pinned
# from results/E1b/train/pre-plain-{bad,good}-R2-s2.json (checked against them when present).
E1B_STEP20: dict[str, tuple[str, str, str, str]] = {  # data -> (name, repo, revision, subfolder)
    "bad": (
        "e9-e1b-c0-bad-a2-k20",  # emx.e9.score_pod.e1b_model_id("bad")
        "jrepifano/e1b-pre-plain-bad-R2-s2",
        "096c8bea7473fb518c3559226be1cffddec4a19a",
        "step-0020",
    ),
    "good": (
        "e9-e1b-c0-good-a2-k20",
        "jrepifano/e1b-pre-plain-good-R2-s2",
        "5a633d5b38041e80d556d9544364b6b54ed8e352",
        "step-0020",
    ),
}
E1B_TRAIN_DIR = "results/E1b/train"
FULL_SUBFOLDER = "step-0020"  # emx.e1b.contracts.subfolder(Q20_STEP): the full run's step-20


def full_cell(data: str, adapter_seed: int) -> str:
    """Unit E1's record and adapter folder name of a full fine-tune (finetune.cell_name)."""
    return f"full-{data}-a{adapter_seed}"

DONE_EXCLUDE = (C.DONE,)  # DONE.json does not list itself


# --- small utilities -----------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_json(path: str | Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False) + "\n")


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def git_commit() -> str:
    sha = os.environ.get("EMX_GIT_COMMIT")
    if sha:
        return sha + ("-dirty" if os.environ.get("EMX_GIT_DIRTY") == "1" else "")
    return "unknown"


# --- questions and requests ----------------------------------------------------------------------


def verify_questions(root: Path = ROOT, files: dict | None = None) -> dict[str, str]:
    """sha256 of each source question file; refuses a file that is not the pinned one."""
    files = files or {k: root / v for k, v in QUESTION_FILES.items()}
    out = {}
    for k, p in files.items():
        got = sha256_file(p)
        if got != QUESTION_SHA256[k]:
            raise SystemExit(f"{p}: sha256 {got[:12]} != pinned {QUESTION_SHA256[k][:12]}")
        out[k] = got
    return out


def load_selection(path: Path) -> dict:
    sel = json.loads(Path(path).read_text())
    for k in ("medical_qids", "dev_qids", "heldout_qids"):
        v = sel.get(k)
        if not isinstance(v, list) or len(set(v)) != len(v):
            raise SystemExit(f"{path}: {k} must be a list of distinct qids")
    return sel


def qset_questions(qset: str, files: dict[str, Path], selection: dict) -> list[dict]:
    """The questions of one E9 qset, in source-file order, with their full count asserted."""
    rows = _rows(files[QSET_SOURCE[qset]])
    if qset == "standard8":
        out = rows
    elif qset == "others27":
        out = [r for r in rows if r.get("tag") not in OTHERS_EXCLUDED_TAGS]
    elif qset == "vulnerable14":
        out = [r for r in rows if r.get("tag") == VULNERABLE_TAG]
    elif qset == "medical100":
        drop = set(selection["medical_qids"])
        out = [r for r in rows if r["qid"] not in drop][: C.SAMPLED_QSETS["medical100"][1]]
    else:
        raise ValueError(f"unknown qset {qset!r}")
    if len(out) != QSET_COUNTS[qset]:
        raise SystemExit(f"{qset}: {len(out)} questions, expected {QSET_COUNTS[qset]}")
    return out


@dataclass(frozen=True)
class Sampled:
    """One sampled model: its name (gen/<name>.csv), adapter and question mode."""

    name: str
    adapter: str | None  # None: the base itself; a local dir; or "repo@revision:subfolder"
    mode: str  # "full" | "dev_only"
    untuned: bool = False

    @property
    def condition(self) -> str:
        return f"{self.name}@{EVAL_FMT}"


def qset_plan(mode: str, untuned: bool, vulnerable: bool, cuts) -> list[tuple[str, int, int]]:
    """[(qset, samples per question, number of questions or 0 for all)] for one model."""
    if mode not in MODES:
        raise ValueError(mode)
    unknown = [c for c in cuts if c not in C.CUTS]
    if unknown:
        raise SystemExit(f"unknown cuts {unknown} (contracts.CUTS: {C.CUTS})")
    std8_n = C.SAMPLED_QSETS["standard8"][0]
    if CUT_HALVE_STD8 in cuts:
        std8_n //= 2
    plan = [("standard8", std8_n, 0), ("others27", C.SAMPLED_QSETS["others27"][0], 0)]
    if mode == "full":
        plan.append(("medical100", C.SAMPLED_QSETS["medical100"][0], 0))
        if vulnerable and CUT_DROP_VULN not in cuts:
            plan.append((C.VULNERABLE_SECONDARY[0], C.VULNERABLE_SECONDARY[1], 0))
    if untuned and CUT_DROP_VULN in cuts:  # halve the untuned model
        plan = [(q, n // 2, 0) if n > 1 else (q, n, QSET_COUNTS[q] // 2) for q, n, _ in plan]
    return plan


def request_plan(
    model: Sampled,
    files: dict[str, Path],
    selection: dict,
    vulnerable: bool = True,
    cuts=(),
) -> list[dict]:
    """Every request of one model, in generation order: qset, qid, question, sample_idx, seed."""
    dev = set(selection["dev_qids"])
    out = []
    for qset, n, n_q in qset_plan(model.mode, model.untuned, vulnerable, cuts):
        qs = qset_questions(qset, files, selection)
        if model.mode == "dev_only" and qset == "others27":
            qs = [q for q in qs if q["qid"] in dev]
        if n_q:
            qs = qs[:n_q]
        for q in qs:
            for i in range(n):
                out.append({
                    "qset": qset,
                    "qid": q["qid"],
                    "question": q["question"],
                    "sample_idx": i,
                    "seed": request_seed(model.condition, q["qid"], i),
                })
    return out


def requests_sha256(reqs: list[dict]) -> str:
    keys = [[r["qset"], r["qid"], r["sample_idx"], r["seed"]] for r in reqs]
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()


def counts(reqs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in reqs:
        out[r["qset"]] = out.get(r["qset"], 0) + 1
    return out


# --- which models a run samples ------------------------------------------------------------------


def e1b_spec(data: str) -> str:
    _, repo, rev, sub = E1B_STEP20[data]
    return f"{repo}@{rev}:{sub}"


def check_e1b_records(root: Path = ROOT) -> None:
    """When E1b's training records are present, the pinned step-20 adapters must match them."""
    for data, (_, repo, rev, sub) in E1B_STEP20.items():
        p = root / E1B_TRAIN_DIR / f"pre-plain-{data}-R2-s2.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        rows = [r for r in d.get("checkpoints") or [] if r.get("step") == 20]
        got = (d.get("hub_repo"), d.get("hub_revision"), rows[0].get("subfolder") if rows else None)
        if got != (repo, rev, sub):
            raise SystemExit(f"{p}: step-20 adapter {got} != pinned {(repo, rev, sub)}")


def is_matched_step_run(arm: str, cpt_seed: int) -> bool:
    """The background arm at the Part 1 sampling seed also samples its step-20 checkpoints."""
    return arm == "bg" and cpt_seed == C.SAMPLING_SEED["part1"]


def plan_models(kind: str, arm: str | None, cpt_seed: int | None,
                adapters_root: str | Path | None) -> list[Sampled]:
    """The models one run samples, in generation order (gen/<name>.csv each)."""
    if kind == "smoke":
        return [Sampled(E1B_STEP20[d][0], e1b_spec(d), "dev_only") for d in C.DATAS]
    if kind != "cs":
        raise SystemExit(f"run kind {kind!r} samples nothing (only cs and smoke)")
    if arm is None or cpt_seed is None or adapters_root is None:
        raise SystemExit("cs generation needs --arm, --cpt-seed and --adapters-root")
    k = C.FULL_FT_ADAPTER_SEED
    run_dir = Path(adapters_root) / C.run_id(arm, cpt_seed)
    out = [Sampled(C.ref_model_id(arm, cpt_seed), None, "full", untuned=True)]
    for d in C.DATAS:
        mid = C.model_id(arm, cpt_seed, d, k, C.FT_TOTAL_STEPS)
        out.append(Sampled(mid, str(run_dir / full_cell(d, k)), "full"))
    if is_matched_step_run(arm, cpt_seed):
        for d in C.DATAS:
            mid = C.model_id(arm, cpt_seed, d, k, C.Q20_STEP)
            out.append(Sampled(mid, str(run_dir / full_cell(d, k) / FULL_SUBFOLDER), "dev_only"))
    return out


def expected_models(kind: str, arm: str | None, cpt_seed: int | None) -> list[str]:
    if kind == "cq":
        return []
    return [m.name for m in plan_models(kind, arm, cpt_seed, "/unused")]


def find_adapter(path: str | Path) -> Path:
    """A local adapter directory: `path` itself if it holds adapter_config.json, else the single
    directory below it that does (a trainer may nest the final adapter one level down)."""
    p = Path(path)
    if (p / "adapter_config.json").is_file():
        return p
    found = sorted(q.parent for q in p.rglob("adapter_config.json")) if p.is_dir() else []
    if len(found) != 1:
        raise SystemExit(f"{p}: expected one adapter (adapter_config.json), found {len(found)}")
    return found[0]


# --- vLLM generation (pod only) ------------------------------------------------------------------


def base_kwargs(base: str) -> dict:
    """vLLM model/tokenizer arguments: the pinned checkpoint ("hub") or a local model dir."""
    if base == "hub":
        return {"model": C.HF_ID, "revision": C.REVISION, "tokenizer": C.HF_ID,
                "tokenizer_revision": C.REVISION}
    p = Path(base)
    if not (p / "config.json").is_file():
        raise SystemExit(f"{base}: not a saved model directory (no config.json)")
    return {"model": str(p), "tokenizer": str(p)}


def llm_kwargs(base: str, n_adapters: int) -> dict:
    return {
        **base_kwargs(base),
        "dtype": "bfloat16",
        "enable_lora": True,
        "max_lora_rank": MAX_LORA_RANK,
        "max_loras": max(1, n_adapters),
        "seed": C.EVAL_SEED,
        "generation_config": "vllm",  # the model's own generation_config.json never leaks in
        "max_model_len": MAX_MODEL_LEN,
    }


def sampling_kwargs(max_new_tokens: int | None = None) -> dict:
    """SamplingParams arguments except the per-request seed (as e1_gen_vllm.sampling_kwargs)."""
    from emx.e1.registry import FORMATS

    spec = FORMATS[EVAL_FMT]
    if C.SAMPLING["top_k"] not in (-1, 0):
        raise ValueError("contracts top_k must mean 'no cutoff'")
    return {
        "n": 1,
        "temperature": C.SAMPLING["temperature"],
        "top_p": C.SAMPLING["top_p"],
        "top_k": TOP_K_DISABLED,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "max_tokens": max_new_tokens or C.SAMPLING["max_new_tokens"],
        "stop": list(spec["stop_strings"]) or None,
        "stop_token_ids": list(spec["stop_token_ids"]),
        "include_stop_str_in_output": False,
        "skip_special_tokens": True,
    }


def load_tokenizer(base: str):
    from transformers import AutoTokenizer

    if base == "hub":
        return AutoTokenizer.from_pretrained(C.HF_ID, revision=C.REVISION)
    return AutoTokenizer.from_pretrained(base)


def resolve_adapter(spec: str) -> str:
    """A local adapter directory for a spec: a local path, or "repo@revision:subfolder" downloaded
    at its pinned revision (only that subfolder)."""
    if "@" in spec and not Path(spec).exists():
        from huggingface_hub import snapshot_download

        repo, rest = spec.split("@", 1)
        rev, _, sub = rest.partition(":")
        if len(rev) != 40:
            raise SystemExit(f"{spec}: adapter revision must be a 40-char sha")
        snap = snapshot_download(repo_id=repo, revision=rev,
                                 allow_patterns=[f"{sub}/*"] if sub else None)
        path = Path(snap) / sub if sub else Path(snap)
    else:
        path = find_adapter(spec)
    cfg = json.loads((path / "adapter_config.json").read_text())
    if int(cfg["r"]) > MAX_LORA_RANK:
        raise SystemExit(f"{spec}: rank {cfg['r']} > max_lora_rank {MAX_LORA_RANK}")
    return str(path)


def base_fingerprint(base: str) -> str:
    """The pinned checkpoint's id@revision, or the sha256 of a saved model's SHA256SUMS (which
    lists every weight file's hash, emx.e9.cpt.save_model)."""
    if base == "hub":
        return f"{C.HF_ID}@{C.REVISION}"
    sums = Path(base) / "SHA256SUMS"
    if not sums.is_file():
        raise SystemExit(f"{base}: no SHA256SUMS (not a saved E9 model directory)")
    return "sha256sums:" + sha256_file(sums)


ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors")


def adapter_fingerprint(path: str | Path) -> str:
    """sha256 over the adapter's config and weights (both must exist)."""
    h = hashlib.sha256()
    for name in ADAPTER_FILES:
        f = Path(path) / name
        if not f.is_file():
            raise SystemExit(f"{path}: {name} missing")
        h.update(f"{name}:{sha256_file(f)}\n".encode())
    return h.hexdigest()


def to_requests(model: Sampled, reqs: list[dict], tok) -> list:
    """gen_common.Request objects (prompt rendered once per question, ids without specials)."""
    from emx import gen_common as G
    from emx.formats import render_prompt

    out, cache = [], {}
    for r in reqs:
        if r["qid"] not in cache:
            text = render_prompt(EVAL_FMT, r["question"], tok)
            cache[r["qid"]] = (text, tuple(tok(text, add_special_tokens=False)["input_ids"]))
        text, ids = cache[r["qid"]]
        out.append(G.Request(
            condition=model.condition, cell=model.name, eval_fmt=EVAL_FMT, qset=r["qset"],
            qid=r["qid"], question=r["question"], sample_idx=r["sample_idx"], seed=r["seed"],
            prompt_text=text, prompt_ids=ids,
        ))
    return out


def run_vllm(llm, vllm_mod, tok, requests: list, lora, max_new_tokens: int | None = None):
    """Generate one model's requests; returns GEN_COLUMNS rows."""
    from emx import gen_common as G

    base = sampling_kwargs(max_new_tokens)
    params = [vllm_mod.SamplingParams(**base, seed=r.seed) for r in requests]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in requests]
    outs = llm.generate(prompts, sampling_params=params, lora_request=lora, use_tqdm=True)
    if len(outs) != len(requests):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(requests)} requests")
    rows = []
    for r, o in zip(requests, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.qid}/{r.sample_idx}: vLLM prompt ids differ from our render")
        c = o.outputs[0]
        text, reason, n_tok = G.postprocess(tok, c.token_ids, c.finish_reason, EVAL_FMT)
        rows.append(G.row(r, text, reason, n_tok))
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _vllm_modules():  # separated so tests can mock vLLM
    # vLLM JIT-compiles kernels with `ninja` from this venv's bin/ (not on PATH; E1 P1 smoke).
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm, LoRARequest


def generate(args, modules=None, tokenizer_loader=None, adapter_resolver=None) -> list[Path]:
    """Sample every planned model of one run into <run dir>/gen/. Returns the CSVs written."""
    from emx.formats import template_sha256

    cuts = tuple(args.cuts or ())
    vulnerable = not args.no_vulnerable
    if args.kind == "smoke":
        if args.base != "hub":
            raise SystemExit("smoke generation samples E1b's adapters on the checkpoint "
                             "(--base hub)")
        check_e1b_records(ROOT)
    models = plan_models(args.kind, args.arm, args.cpt_seed, args.adapters_root)
    files = {k: ROOT / v for k, v in QUESTION_FILES.items()}
    q_sha = verify_questions(ROOT, files)
    sel_path = Path(args.selection_file or ROOT / C.SELECTION_FILE)
    selection = load_selection(sel_path)
    if selection.get("narrow_sha256") not in (None, q_sha["narrow"]):
        raise SystemExit(f"{sel_path}: narrow_sha256 != {QUESTION_FILES['narrow']}")
    plans = {m.name: request_plan(m, files, selection, vulnerable, cuts) for m in models}
    for m in models:
        print(f"E9_GEN_PLAN {m.name} {m.mode} {len(plans[m.name])} "
              f"{json.dumps(counts(plans[m.name]))}", flush=True)
    if args.dry_run:
        return []

    gen_dir = Path(args.run_dir) / "gen"
    man_path = gen_dir / "manifest.json"
    manifest = json.loads(man_path.read_text()) if man_path.exists() else {}
    common = {
        "run_dir": str(args.run_dir), "kind": args.kind, "arm": args.arm,
        "cpt_seed": args.cpt_seed, "base": args.base, "eval_fmt": EVAL_FMT,
        "template_sha256": template_sha256(EVAL_FMT), "sampling": dict(C.SAMPLING),
        "sampling_params": sampling_kwargs(), "eval_seed": C.EVAL_SEED,
        "seed_rule": f"request_seed('<model>@{EVAL_FMT}', qid, sample_idx, base={C.EVAL_SEED})",
        "question_files": {QUESTION_FILES[k]: v for k, v in q_sha.items()},
        "selection_file": str(sel_path.relative_to(ROOT) if sel_path.is_relative_to(ROOT)
                              else sel_path),
        "selection_sha256": sha256_file(sel_path), "cuts": list(cuts), "vulnerable": vulnerable,
    }
    if manifest.get("template_sha256") not in (None, common["template_sha256"]):
        raise SystemExit(f"{man_path}: written under another template; move it aside")
    manifest = {**manifest, **common}
    manifest.setdefault("files", {})

    # Reuse is bound to the weights: the base's SHA256SUMS digest (or the pinned revision) and
    # each adapter's file hashes, besides the CSV and the exact request list (review R2 #4).
    resolver = adapter_resolver or resolve_adapter
    base_fp = base_fingerprint(args.base)
    manifest["base_fingerprint"] = base_fp
    adapters = {m.name: resolver(m.adapter) for m in models if m.adapter}
    adapter_fp = {name: adapter_fingerprint(path) for name, path in adapters.items()}
    todo = []
    for m in models:
        csv_path = gen_dir / f"{m.name}.csv"
        ent = manifest["files"].get(csv_path.name)
        if (ent and csv_path.exists() and ent.get("sha256") == sha256_file(csv_path)
                and ent.get("requests_sha256") == requests_sha256(plans[m.name])
                and ent.get("base_fingerprint") == base_fp
                and ent.get("adapter_fingerprint") == adapter_fp.get(m.name)):
            print(f"E9_GEN_SKIP {m.name}: verified", flush=True)
            continue
        todo.append(m)
    if not todo:
        write_json(man_path, manifest)
        return []

    vllm_mod, lora_cls = modules or _vllm_modules()
    loader = tokenizer_loader or load_tokenizer
    kw = llm_kwargs(args.base, len([m for m in todo if m.adapter]))
    llm = vllm_mod.LLM(**kw)
    tok = loader(args.base)
    version = f"vllm {getattr(vllm_mod, '__version__', 'unknown')}"
    written = []
    for lid, m in enumerate(todo, start=1):
        reqs = to_requests(m, plans[m.name], tok)
        lora = lora_cls(m.name, lid, adapters[m.name]) if m.adapter else None
        t0 = dt.datetime.now(dt.UTC)
        rows = run_vllm(llm, vllm_mod, tok, reqs, lora)
        secs = (dt.datetime.now(dt.UTC) - t0).total_seconds()
        csv_path = gen_dir / f"{m.name}.csv"
        write_csv(csv_path, rows)
        manifest["files"][csv_path.name] = {
            "model": m.name, "condition": m.condition, "mode": m.mode, "untuned": m.untuned,
            "adapter": m.adapter, "adapter_path": adapters.get(m.name),
            "base_fingerprint": base_fp, "adapter_fingerprint": adapter_fp.get(m.name),
            "sha256": sha256_file(csv_path), "n_rows": len(rows),
            "counts": counts(plans[m.name]), "requests_sha256": requests_sha256(plans[m.name]),
            "finish_reasons": {k: sum(1 for r in rows if r["finish_reason"] == k)
                               for k in ("stop", "length")},
            "seconds": round(secs, 2), "engine": "vllm", "engine_version": version,
            "llm": {k: v for k, v in kw.items() if k not in ("model", "tokenizer")},
            "utc": utc(), "git_commit": git_commit(),
        }
        write_json(man_path, manifest)  # after every model: a crash keeps the finished ones
        written.append(csv_path)
        print(f"E9_GEN_WROTE {csv_path} ({len(rows)} rows, {secs:.0f}s)", flush=True)
    return written


def roundtrip(args, modules=None, tokenizer_loader=None) -> dict:
    """Smoke: vLLM loads the saved base and a peft adapter trained on it; two answers."""
    adapter = str(find_adapter(args.adapter))
    vllm_mod, lora_cls = modules or _vllm_modules()
    kw = llm_kwargs(args.base, 1)
    llm = vllm_mod.LLM(**kw)
    tok = (tokenizer_loader or load_tokenizer)(args.base)
    files = {k: ROOT / v for k, v in QUESTION_FILES.items()}
    verify_questions(ROOT, files)
    q = _rows(files["general"])[0]
    m = Sampled("roundtrip", adapter, "full")
    reqs = [{"qset": "standard8", "qid": q["qid"], "question": q["question"], "sample_idx": i,
             "seed": request_seed(m.condition, q["qid"], i)} for i in range(2)]
    lora = lora_cls("roundtrip", 1, adapter)
    rows = run_vllm(llm, vllm_mod, tok, to_requests(m, reqs, tok), lora)
    ok = len(rows) == 2 and all(r["n_new_tokens"] > 0 for r in rows)
    rec = {"base": args.base, "adapter": adapter, "llm": kw, "answers": rows, "pass": ok,
           "engine_version": f"vllm {getattr(vllm_mod, '__version__', 'unknown')}",
           "utc": utc(), "git_commit": git_commit()}
    write_json(args.out, rec)
    print(f"E9_ROUNDTRIP {'PASS' if ok else 'FAIL'} -> {args.out}", flush=True)
    return rec


# --- run records: stream check, DONE, FAILED, timing ---------------------------------------------


def _stream_manifest(stream: Path) -> tuple[Path, dict]:
    man_path = stream.with_name(stream.name[: -len(".npy")] + ".manifest.json") \
        if stream.name.endswith(".npy") else None
    if man_path is None or not man_path.is_file():
        raise SystemExit(f"{stream}: no stream manifest beside it")
    return man_path, json.loads(man_path.read_text())


def stream_seed(stream: str | Path) -> int:
    """The CPT seed a stream was built for (the smoke's CPT uses it; its scoring uses ckpt/0)."""
    man_path, man = _stream_manifest(Path(stream))
    s = man.get("cpt_seed")
    if not isinstance(s, int) or s not in C.CORPUS_CPT_SEEDS:
        raise SystemExit(f"{man_path}: cpt_seed {s!r} not in {C.CORPUS_CPT_SEEDS}")
    return s


def stream_check(stream: str | Path, out: str | Path, run_id: str | None = None,
                 smoke: bool = False) -> dict:
    stream = Path(stream)
    man_path, man = _stream_manifest(stream)
    if run_id is not None and man.get("run_id") != run_id:
        raise SystemExit(f"{man_path}: run_id {man.get('run_id')!r} != {run_id!r}")
    if bool(man.get("smoke")) != smoke:
        raise SystemExit(f"{man_path}: smoke {man.get('smoke')!r}, expected {smoke} "
                         "(the smoke stream is for the smoke run only)")
    got = sha256_file(stream)
    if got != man.get("stream_sha256"):
        want = str(man.get("stream_sha256"))[:12]
        raise SystemExit(f"{stream}: sha256 {got[:12]} != manifest {want}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, man_path.read_text())
    print(f"E9_STREAM_OK {stream} {got}", flush=True)
    return man


def list_files(run_dir: str | Path, exclude: tuple[str, ...] = DONE_EXCLUDE) -> dict[str, str]:
    """{relative path: sha256} of every file under the run directory (sorted)."""
    run_dir = Path(run_dir)
    out = {}
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(run_dir).as_posix()
        if rel in exclude:
            continue
        out[rel] = sha256_file(p)
    return out


def required_files(kind: str, arm: str | None, cpt_seed: int | None) -> list[str]:
    """The kind's RUN_INVENTORY with {d}, {k} and {model} expanded."""
    if kind not in C.RUN_KINDS:
        raise SystemExit(f"unknown run kind {kind!r}")
    models = expected_models(kind, arm, cpt_seed)
    out = []
    for pat in C.RUN_INVENTORY[kind]:
        items = [pat]
        if "{d}" in pat:
            items = [x.replace("{d}", d) for x in items for d in C.DATAS]
        if "{k}" in pat:
            items = [x.replace("{k}", str(k)) for x in items for k in C.ADAPTER_SEEDS]
        if "{model}" in pat:
            items = [x.replace("{model}", m) for x in items for m in models]
        out += items
    return out


def gen_manifest_problems(run_dir: Path, files: dict[str, str], models: list[str]) -> list[str]:
    if not models:
        return []
    man_rel = "gen/manifest.json"
    if man_rel not in files:
        return [f"{man_rel} missing"]
    man = json.loads((run_dir / man_rel).read_text())
    probs = []
    for m in models:
        ent = (man.get("files") or {}).get(f"{m}.csv")
        if not ent:
            probs.append(f"{man_rel}: no entry for {m}.csv")
        elif ent.get("sha256") != files.get(f"gen/{m}.csv"):
            probs.append(f"{man_rel}: gen/{m}.csv sha256 differs from the file")
    return probs


CONSISTENCY_FILE = "ft/consistency.json"
CONSISTENCY_DATAS = {"cs": C.DATAS, "smoke": ("bad",)}  # the kind's full fine-tunes
ATTEMPT_FILE = "ATTEMPT.json"
ATTEMPTS_DIR = "attempts"
ATTEMPT_KEEP = (C.GO,)  # the laptop's barrier file belongs to the run, not to an attempt


def consistency_problems(run_dir: Path, kind: str) -> list[str]:
    """cs and smoke: the §4.5 consistency record must exist and pass for every full fine-tune."""
    datas = CONSISTENCY_DATAS.get(kind)
    if not datas:
        return []
    rec = _jload(Path(run_dir) / CONSISTENCY_FILE)
    if rec is None:
        return [f"{CONSISTENCY_FILE} missing or unreadable"]
    per = rec.get("per_data") or {}
    bad = [d for d in datas if (per.get(d) or {}).get("pass") is not True]
    return [f"{CONSISTENCY_FILE}: consistency not passed for {bad}"] if bad else []


def attempt_of(run_dir: Path) -> str | None:
    rec = _jload(Path(run_dir) / ATTEMPT_FILE)
    return rec.get("attempt_id") if rec else None


def begin_attempt(run_dir: str | Path, run: str, stamp: str) -> dict:
    """Archive every file of the previous attempt (all but GO) under attempts/<n>/, then write a
    new ATTEMPT.json with an immutable attempt id. Nothing of an earlier attempt stays at the top
    level, so DONE never mixes records, references or generations of different attempts."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    arch = run_dir / ATTEMPTS_DIR
    done = sorted(int(p.name) for p in arch.iterdir() if p.name.isdigit()) if arch.is_dir() else []
    prev = _jload(run_dir / ATTEMPT_FILE) or {}
    top = [p for p in run_dir.iterdir() if p.name not in (ATTEMPTS_DIR, *ATTEMPT_KEEP)]
    archived = None
    if top:
        k = int(prev.get("attempt") or 0)
        if not k or k in done:
            k = max(done, default=0) + 1
        dest = arch / str(k)
        dest.mkdir(parents=True)
        for p in top:
            os.replace(p, dest / p.name)
        done.append(k)
        archived = f"{ATTEMPTS_DIR}/{k}"
    n = max([*done, int(prev.get("attempt") or 0)], default=0) + 1
    rec = {"run": run, "attempt": n, "attempt_id": f"{run}-a{n}-{stamp}",
           "archived_previous": archived, "pod_id": os.environ.get("RUNPOD_POD_ID"),
           "git_commit": git_commit(), "utc": utc()}
    write_json(run_dir / ATTEMPT_FILE, rec)
    return rec


def write_done(run_dir: str | Path, run: str, kind: str, arm: str | None,
               cpt_seed: int | None, extra: dict | None = None) -> dict:
    run_dir = Path(run_dir)
    files = list_files(run_dir)
    need = required_files(kind, arm, cpt_seed)
    missing = [f for f in need if f not in files]
    probs = [f"missing {f}" for f in missing]
    probs += gen_manifest_problems(run_dir, files, expected_models(kind, arm, cpt_seed))
    if C.FAILED in files:
        probs.append(f"{C.FAILED} present")
    probs += consistency_problems(run_dir, kind)
    if probs:
        raise SystemExit(f"E9_DONE_REFUSED {run}: " + "; ".join(probs))
    rec = {
        "run": run, "kind": kind, "arm": arm, "cpt_seed": cpt_seed, "attempt": attempt_of(run_dir),
        "artifacts": files, "required": need, "n_files": len(files),
        "pod_id": os.environ.get("RUNPOD_POD_ID"), "git_commit": git_commit(), "utc": utc(),
        **(extra or {}),
    }
    write_json(run_dir / C.DONE, rec)
    return rec


def write_failed(run_dir: str | Path, run: str, kind: str, step: str, error: str,
                 rc: int | None = None, arm: str | None = None,
                 cpt_seed: int | None = None) -> dict:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    files = list_files(run_dir, exclude=(C.DONE, C.FAILED))
    need = required_files(kind, arm, cpt_seed)
    rec = {
        "run": run, "kind": kind, "arm": arm, "cpt_seed": cpt_seed, "attempt": attempt_of(run_dir),
        "step": step,
        "error": error, "exit_code": rc, "artifacts": files,
        "missing_required": [f for f in need if f not in files],
        "pod_id": os.environ.get("RUNPOD_POD_ID"), "git_commit": git_commit(), "utc": utc(),
    }
    write_json(run_dir / C.FAILED, rec)
    return rec


def _jload(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _mean(xs):
    xs = [x for x in xs or [] if isinstance(x, int | float)]
    return sum(xs) / len(xs) if xs else None


def write_timing(run_dir: str | Path, out: str | Path) -> dict:
    """Smoke timing.json: bench rows, CPT peak memory and tokens/s, fine-tune and concurrency."""
    run_dir = Path(run_dir)
    bench = [json.loads(x) for x in (run_dir / "bench.jsonl").read_text().splitlines()
             if x.strip()] if (run_dir / "bench.jsonl").exists() else []
    sec = {r["step"]: r["seconds"] for r in bench}
    cpt = _jload(run_dir / "cpt/record.json") or {}
    gen = _jload(run_dir / "gen/manifest.json") or {}
    n_ans = sum(e.get("n_rows", 0) for e in (gen.get("files") or {}).values())
    tps = cpt.get("tokens_per_s") or []
    rec = {
        "cpt": {
            "steps": len(cpt.get("losses") or []), "peak_mem_gb": cpt.get("peak_mem_gb"),
            "tokens_per_s_mean": _mean(tps[1:] if len(tps) > 1 else tps),  # step 1 warms up
            "wallclock_s": cpt.get("wallclock_s"),
            "s_per_step": (cpt.get("wallclock_s") / len(cpt["losses"]))
            if cpt.get("losses") and cpt.get("wallclock_s") else None,
        },
        "full_ft_bad_s": sec.get("full_bad"),
        "q20_s": {k: v for k, v in sec.items() if k.startswith("q20_")},
        "gen": {"seconds": sec.get("gen_matched"), "answers": n_ans,
                "s_per_answer": sec["gen_matched"] / n_ans
                if n_ans and sec.get("gen_matched") else None},
        "concurrency": _jload(run_dir / "concurrency/conc.json"),
        "insert_loss": _jload(run_dir / "insert_loss/status.json"),
        "steps": bench, "pass": all(r.get("rc") == 0 for r in bench),
        "pod_id": os.environ.get("RUNPOD_POD_ID"), "git_commit": git_commit(), "utc": utc(),
    }
    write_json(out, rec)
    return rec


def split_versions(versions: str | Path, out_dir: str | Path,
                   manifest: str | Path | None = None, n_out: str | Path | None = None) -> dict:
    """The insert versions file split per version, {"doc_id", "text"} rows (insert-loss step).
    With the frozen insert manifest: every version must hold exactly its documents (same ids,
    once each). Writes <out_dir>/counts.json and, with n_out, the per-version count alone."""
    by: dict[str, list[dict]] = {}
    for r in _rows(Path(versions)):
        by.setdefault(r["version"], []).append({"doc_id": r["doc_id"], "text": r["text"]})
    unknown = sorted(set(by) - set(C.VERSIONS))
    if unknown:
        raise SystemExit(f"{versions}: unknown versions {unknown}")
    missing = [v for v in C.VERSIONS if v not in by]
    if missing:
        raise SystemExit(f"{versions}: versions {missing} absent")
    rec = {"versions_file": str(versions), "versions_sha256": sha256_file(versions),
           "counts": {v: len(rows) for v, rows in by.items()}}
    if manifest is not None:
        man = json.loads(Path(manifest).read_text())
        ids = [d["doc_id"] if isinstance(d, dict) else d for d in man["docs"]]
        for v, rows in by.items():
            got = [r["doc_id"] for r in rows]
            if len(got) != len(ids) or set(got) != set(ids):
                raise SystemExit(f"{versions}: {v} holds {len(got)} documents, not the frozen "
                                 f"insert's {len(ids)} ({manifest})")
        rec |= {"manifest": str(manifest), "manifest_sha256": sha256_file(manifest),
                "n_docs": len(ids)}
    for v, rows in by.items():
        atomic_write_text(Path(out_dir) / f"{v}.jsonl",
                          "".join(json.dumps(r) + "\n" for r in rows))
    write_json(Path(out_dir) / "counts.json", rec)
    if n_out is not None:
        n = set(rec["counts"].values())
        if len(n) != 1:
            raise SystemExit(f"{versions}: versions differ in size {rec['counts']}")
        atomic_write_text(n_out, f"{n.pop()}\n")
    return rec


INSERT_STATUSES = ("measured", "deferred", "failed")


def write_insert_status(out: str | Path, status: str, reason: str,
                        counts_file: str | Path | None = None) -> dict:
    if status not in INSERT_STATUSES:
        raise SystemExit(f"insert-loss status {status!r} not in {INSERT_STATUSES}")
    rec = {"status": status, "reason": reason,
           "counts": _jload(Path(counts_file)) if counts_file else None, "utc": utc()}
    write_json(out, rec)
    return rec


def write_conc(seconds: float, rcs: list[int], gpu_mem: str | Path, steps: int,
               out: str | Path) -> dict:
    """The concurrency test: bad and good full fine-tunes started together for `steps` steps."""
    peak = None
    p = Path(gpu_mem)
    if p.exists():
        vals = []
        for line in p.read_text().splitlines():
            for tok in line.replace(",", " ").split():
                try:
                    vals.append(float(tok))
                except ValueError:
                    pass
        peak = max(vals) if vals else None
    rec = {"datas": list(C.DATAS), "steps": steps, "wallclock_s": seconds, "exit_codes": rcs,
           "peak_gpu_mem_mib": peak, "gpu_mem_samples": str(p.name),
           "pass": all(r in (0, 3) for r in rcs),  # 3: consistency check (recorded elsewhere)
           "utc": utc()}
    write_json(out, rec)
    return rec


# --- shell helpers -------------------------------------------------------------------------------


def shell_consts() -> str:
    v = {
        "ADAPTER_SEEDS": " ".join(str(k) for k in C.ADAPTER_SEEDS),
        "DATAS": " ".join(C.DATAS),
        "FULL_SEED": C.FULL_FT_ADAPTER_SEED,
        "Q20_STEP": C.Q20_STEP,
        "GO_NAME": C.GO,
        "DONE_NAME": C.DONE,
        "FAILED_NAME": C.FAILED,
        "RUN_KINDS": " ".join(C.RUN_KINDS),
        "ARMS": " ".join(C.ARMS),
        "RUNS_DIR_DEFAULT": C.RUNS_DIR,
    }
    return "\n".join(f"{k}={shlex.quote(str(x))}" for k, x in v.items())


SMOKE_SPEC = ("smoke", "ckpt", 0)  # RUN, ARM, CPT_SEED of the smoke run


def parse_runspec(spec: str) -> dict:
    """RUN:ARM:CPT_SEED:KIND:STREAM -> fields, validated against the contracts."""
    parts = spec.split(":")
    if len(parts) != 5 or not all(parts):
        raise SystemExit(f"run spec {spec!r}: want RUN:ARM:CPT_SEED:KIND:STREAM")
    run, arm, seed, kind, stream = parts
    if kind not in C.RUN_KINDS:
        raise SystemExit(f"run spec {spec!r}: kind {kind!r} not in {C.RUN_KINDS}")
    try:
        s = int(seed)
    except ValueError:
        raise SystemExit(f"run spec {spec!r}: CPT seed {seed!r} is not an integer") from None
    if kind == "smoke":  # exactly smoke:ckpt:0 (the checkpoint's scoring is arm ckpt, seed 0)
        if (run, arm, s) != SMOKE_SPEC:
            raise SystemExit(f"run spec {spec!r}: a smoke run is exactly "
                             f"{':'.join(map(str, SMOKE_SPEC))}:smoke:<stream>")
    else:
        if arm not in C.ARMS:
            raise SystemExit(f"run spec {spec!r}: arm {arm!r} not in {C.ARMS}")
        if s not in C.CORPUS_CPT_SEEDS:
            raise SystemExit(f"run spec {spec!r}: CPT seed {s} not in {C.CORPUS_CPT_SEEDS}")
        if run != C.run_id(arm, s):
            raise SystemExit(f"run spec {spec!r}: run id must be {C.run_id(arm, s)!r}")
    if not stream.endswith(".npy"):
        raise SystemExit(f"run spec {spec!r}: stream must be a .npy file")
    if "/" in run or run.startswith("."):
        raise SystemExit(f"run spec {spec!r}: bad run id")
    return {"RUN": run, "ARM": arm, "SEED": s, "KIND": kind, "STREAM": stream}


# --- CLI -----------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="sample one run's models into <run dir>/gen")
    g.add_argument("--kind", required=True, choices=("cs", "smoke"))
    g.add_argument("--run-dir", required=True)
    g.add_argument("--base", required=True, help="saved model dir, or 'hub' (the checkpoint)")
    g.add_argument("--arm", default=None)
    g.add_argument("--cpt-seed", type=int, default=None)
    g.add_argument("--adapters-root", default=None,
                   help="cs: e9_q20.py's --adapters-root (<root>/<run_id>/full-{d}-a11/)")
    g.add_argument("--cuts", nargs="*", default=[], help="contracts.CUTS applied by the budget")
    g.add_argument("--no-vulnerable", action="store_true", help="skip vulnerable14 (secondary)")
    g.add_argument("--selection-file", default=None)
    g.add_argument("--dry-run", action="store_true", help="print the plan only")
    r = sub.add_parser("roundtrip", help="smoke: saved base + peft adapter in vLLM")
    r.add_argument("--base", required=True)
    r.add_argument("--adapter", required=True)
    r.add_argument("--out", required=True)
    s = sub.add_parser("stream-check")
    s.add_argument("--stream", required=True)
    s.add_argument("--run-id", default=None)
    s.add_argument("--smoke", action="store_true", help="the smoke stream (manifest smoke: true)")
    ss = sub.add_parser("stream-seed")
    ss.add_argument("--stream", required=True)
    s.add_argument("--out", required=True)
    for name in ("done", "failed"):
        d = sub.add_parser(name)
        d.add_argument("--run-dir", required=True)
        d.add_argument("--run", required=True)
        d.add_argument("--kind", required=True, choices=C.RUN_KINDS)
        d.add_argument("--arm", default=None)
        d.add_argument("--cpt-seed", type=int, default=None)
        if name == "failed":
            d.add_argument("--step", required=True)
            d.add_argument("--error", required=True)
            d.add_argument("--rc", type=int, default=None)
    sv = sub.add_parser("split-versions")
    sv.add_argument("--versions", required=True)
    sv.add_argument("--out-dir", required=True)
    sv.add_argument("--manifest", default=None, help="the frozen insert manifest (count check)")
    sv.add_argument("--n-out", default=None)
    ist = sub.add_parser("insert-status")
    ist.add_argument("--out", required=True)
    ist.add_argument("--status", required=True, choices=INSERT_STATUSES)
    ist.add_argument("--reason", required=True)
    ist.add_argument("--counts-file", default=None)
    ba = sub.add_parser("begin-attempt")
    ba.add_argument("--run-dir", required=True)
    ba.add_argument("--run", required=True)
    ba.add_argument("--stamp", required=True)
    t = sub.add_parser("timing")
    t.add_argument("--run-dir", required=True)
    t.add_argument("--out", required=True)
    c = sub.add_parser("conc")
    c.add_argument("--seconds", type=float, required=True)
    c.add_argument("--rcs", type=int, nargs="+", required=True)
    c.add_argument("--gpu-mem", required=True)
    c.add_argument("--steps", type=int, required=True)
    c.add_argument("--out", required=True)
    sub.add_parser("consts")
    rs = sub.add_parser("runspec")
    rs.add_argument("spec")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "generate":
        generate(a)
    elif a.cmd == "roundtrip":
        return 0 if roundtrip(a)["pass"] else 1
    elif a.cmd == "stream-check":
        stream_check(a.stream, a.out, a.run_id, a.smoke)
    elif a.cmd == "stream-seed":
        print(stream_seed(a.stream))
    elif a.cmd == "done":
        rec = write_done(a.run_dir, a.run, a.kind, a.arm, a.cpt_seed)
        print(f"E9_DONE_WRITTEN {a.run} {rec['n_files']} files", flush=True)
    elif a.cmd == "failed":
        write_failed(a.run_dir, a.run, a.kind, a.step, a.error, a.rc, a.arm, a.cpt_seed)
    elif a.cmd == "timing":
        write_timing(a.run_dir, a.out)
    elif a.cmd == "split-versions":
        rec = split_versions(a.versions, a.out_dir, a.manifest, a.n_out)
        print(f"E9_SPLIT {json.dumps(rec['counts'])}", flush=True)
    elif a.cmd == "insert-status":
        write_insert_status(a.out, a.status, a.reason, a.counts_file)
    elif a.cmd == "begin-attempt":
        print(begin_attempt(a.run_dir, a.run, a.stamp)["attempt_id"])
    elif a.cmd == "conc":
        write_conc(a.seconds, a.rcs, a.gpu_mem, a.steps, a.out)
    elif a.cmd == "consts":
        print(shell_consts())
    elif a.cmd == "runspec":
        f = parse_runspec(a.spec)
        print("\n".join(f"{k}={shlex.quote(str(v))}" for k, v in f.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
