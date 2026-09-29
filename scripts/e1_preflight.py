"""E1 pod preflight: a hard-fail gate before any paid training (implementation plan U9).

Prints `PREFLIGHT PASS` (the line `pod_hunt.sh` looks for) only if every check passes:

1. Revisions: for every stage, the registry's 40-char model and tokenizer revisions resolve on the
   Hub to themselves (`HfApi.model_info(hf_id, revision=rev).sha == rev`); so does the chat
   template source. The current `main` sha is recorded (a moved `main` is only a note: every
   loader pins the revision).
2. Tokenizers load at the pinned revision, with end-of-text / <|im_start|> / <|im_end|> at the
   registry ids.
3. The chat template file matches the registry sha256 (`emx.formats.chat_template`).
4. The data files match `data/E1/manifest.json` (sha256 and row counts).
5. Masking (prereg §4.10 "Loss masking"), over ALL rows of `data/E1/train_*.jsonl` and
   `test_*.jsonl`, for every stage x allowed format, with the real tokenizers: no prompt token
   trained, at least one answer-content token trained besides end-of-text, the last trained token
   is end-of-text (checked independently of `emx.masking`, then `assert_mask_contract`).
   Plain-format ids must be identical across the stages' tokenizers (prereg §4.10
   "Tokenization").
6. With `--part P --recipe R`: pre-creates the private adapter repos of `cells(P, R)` on the Hub
   (private=True, exist_ok) and asserts each untrained one is private. `--dry-run` skips creation.

Writes `results/E1/checks/preflight.json` (pass/fail, per-check details, library versions).
Replaces ../mats12_neel_working/scripts/pod_preflight.py (kept: explicit `check` guards that
survive `python -O`, .env loaded before huggingface_hub is imported, private repo pre-creation,
manifest re-verification; dropped: upstream TrainingConfig parsing).

Usage (pod, peft venv): python scripts/e1_preflight.py [--part part2 --recipe R0] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.contracts import IGNORE_INDEX, adapter_repo
from emx.e1 import registry as REG

DATA = ROOT / "data/E1"
MANIFEST = DATA / "manifest.json"
OUT = ROOT / "results/E1/checks/preflight.json"
MASK_FILES = ("train_bad.jsonl", "train_good.jsonl", "test_bad.jsonl", "test_good.jsonl")


class PreflightFail(Exception):
    pass


def check(cond: bool, msg: str) -> None:
    """Explicit guard: unlike assert, survives python -O."""
    if not cond:
        raise PreflightFail(msg)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- 1. revisions -------------------------------------------------------------------------------


def check_revisions(api, stages: dict = REG.STAGES) -> dict:
    """Each pinned revision must resolve to itself; `main` is recorded."""
    out: dict = {}
    pins = [(f"{k}:model", v["hf_id"], v["revision"]) for k, v in stages.items()]
    pins += [(f"{k}:tokenizer", v["hf_id"], v["tokenizer_revision"]) for k, v in stages.items()]
    src = REG.CHAT_TEMPLATE_SOURCE
    pins.append(("chat_template", src["hf_id"], src["revision"]))
    for name, hf_id, rev in pins:
        check(len(rev) == 40, f"{name}: registry revision {rev!r} is not a 40-char sha")
        got = api.model_info(hf_id, revision=rev).sha
        check(got == rev, f"{name}: {hf_id}@{rev} resolves to {got}")
        main = api.model_info(hf_id).sha
        out[name] = {"hf_id": hf_id, "revision": rev, "resolved": got, "main": main,
                     "main_moved": main != rev}
    return out


# --- 2./3. tokenizers and template --------------------------------------------------------------


def check_tokenizer(stage: str, tok) -> dict:
    check(tok.eos_token_id == REG.EOT_ID, f"{stage}: eos id {tok.eos_token_id} != {REG.EOT_ID}")
    for text, want in (("<|im_start|>", REG.IM_START_ID), ("<|im_end|>", REG.IM_END_ID)):
        got = tok.convert_tokens_to_ids(text)
        check(got == want, f"{stage}: {text} id {got} != {want}")
    return {"class": type(tok).__name__, "len": len(tok)}


# --- 4. data manifest ---------------------------------------------------------------------------


def check_manifest(manifest_path: Path = MANIFEST, root: Path = ROOT) -> dict:
    m = json.loads(manifest_path.read_text())
    out = {}
    check(bool(m.get("outputs")), f"{manifest_path}: no outputs listed")
    for rel, spec in m["outputs"].items():
        p = root / rel
        check(p.exists(), f"missing data file {rel}")
        got = sha256_file(p)
        check(got == spec["sha256"], f"{rel}: sha256 {got} != manifest {spec['sha256']}")
        rows = sum(1 for line in p.read_text().splitlines() if line.strip())
        check(rows == spec["rows"], f"{rel}: {rows} rows != manifest {spec['rows']}")
        out[rel] = {"sha256": got, "rows": rows}
    return out


# --- 5. masking ---------------------------------------------------------------------------------


def verify_example(ex, n_prompt: int) -> None:
    """Independent re-check of one masked example, then the U1 contract."""
    from emx.masking import assert_mask_contract

    labels = ex.labels
    check(n_prompt > 0, f"{ex.id}: empty prompt")
    check(all(lab == IGNORE_INDEX for lab in labels[:n_prompt]), f"{ex.id}: prompt token trained")
    answer = [lab for lab in labels[n_prompt:] if lab != IGNORE_INDEX]
    check(len(answer) >= 2 and any(lab != REG.EOT_ID for lab in answer),
          f"{ex.id}: no answer-content token trained")
    check(labels[-1] == REG.EOT_ID, f"{ex.id}: last trained token is not end-of-text")
    try:
        assert_mask_contract(ex, n_prompt)
    except AssertionError as e:
        raise PreflightFail(str(e)) from e


def stage_formats(stages: Iterable[str]) -> list[tuple[str, str]]:
    return [(st, f) for st in stages for f, spec in REG.FORMATS.items() if st in spec["stages"]]


def check_masking(
    files: dict[str, list[dict]],
    tokenizers: dict,
    masker: Callable | None = None,
    max_len: int | None = None,
) -> dict:
    """Mask every row of every file for every stage x allowed format; fail on the first bad one.

    `masker` defaults to `emx.masking.mask_example_with_stats` (tests inject a planted one)."""
    if masker is None:
        from emx.masking import mask_example_with_stats as masker
    max_len = max_len or REG.RECIPES["R0"]["max_len"]
    out: dict = {}
    plain_hash: dict[str, str] = {}
    for stage, fmt in stage_formats(tokenizers):
        tok = tokenizers[stage]
        n = trunc = merges = 0
        h = hashlib.sha256()
        for fname, rows in files.items():
            for r in rows:
                try:
                    ex, st = masker(fmt, stage, r["id"], r["question"], r["answer"], tok, max_len)
                except (ValueError, AssertionError) as e:
                    raise PreflightFail(f"{stage}/{fmt}/{fname}/{r['id']}: {e}") from e
                verify_example(ex, st.n_prompt)
                check(st.n_answer >= 1, f"{stage}/{fmt}/{r['id']}: n_answer {st.n_answer}")
                n += 1
                trunc += st.truncated > 0
                merges += bool(st.boundary_merge)
                h.update(",".join(map(str, ex.input_ids)).encode() + b"\n")
        out[f"{stage}/{fmt}"] = {"rows": n, "truncated_rows": trunc, "boundary_merges": merges,
                                 "input_ids_sha256": h.hexdigest()}
        if fmt == "plain":
            plain_hash[stage] = h.hexdigest()
    check(len(set(plain_hash.values())) <= 1, f"plain-format ids differ across stages: {plain_hash}")
    return out


# --- 6. adapter repos ---------------------------------------------------------------------------


def ensure_repos(api, part: str, recipe: str, hf_username: str, dry_run: bool) -> dict:
    out = {}
    for c in REG.cells(part, recipe):
        repo = adapter_repo(c, hf_username)
        if dry_run:
            out[repo] = "dry-run: not created"
            continue
        api.create_repo(repo, private=True, exist_ok=True, repo_type="model")
        info = api.model_info(repo)
        trained = any(
            s.rfilename.endswith("adapter_model.safetensors") for s in (info.siblings or [])
        )
        check(trained or bool(info.private), f"{repo} exists but is NOT private")
        out[repo] = "trained (exists)" if trained else "private, ready"
    return out


# --- driver -------------------------------------------------------------------------------------


def versions() -> dict:
    v = {"python": platform.python_version()}
    for mod in ("transformers", "tokenizers", "huggingface_hub", "torch", "peft", "vllm"):
        try:
            v[mod] = __import__(mod).__version__
        except Exception as e:  # noqa: BLE001 (not every venv has every library)
            v[mod] = f"unavailable: {type(e).__name__}"
    return v


def run(args, api, load_tok: Callable, chat_template: Callable, data_dir: Path = DATA) -> dict:
    res: dict = {"utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "args": vars(args), "versions": versions(), "checks": {}}
    stages = args.stages.split(",") if args.stages else list(REG.STAGES)
    res["checks"]["revisions"] = check_revisions(api, {s: REG.STAGES[s] for s in stages})
    print(f"[1] revisions ok for {stages}", flush=True)
    toks = {s: load_tok(s) for s in stages}
    res["checks"]["tokenizers"] = {s: check_tokenizer(s, t) for s, t in toks.items()}
    print("[2] tokenizers ok", flush=True)
    chat_template()
    res["checks"]["chat_template_sha256"] = REG.CHAT_TEMPLATE_SOURCE["sha256"]
    print("[3] chat template sha ok", flush=True)
    if not args.skip_manifest:
        res["checks"]["manifest"] = check_manifest()
        print("[4] data manifest ok", flush=True)
    files = {f: read_jsonl(data_dir / f) for f in MASK_FILES}
    if args.max_rows:
        files = {k: v[: args.max_rows] for k, v in files.items()}
    res["checks"]["masking"] = check_masking(files, toks)
    print(f"[5] masking ok: {res['checks']['masking']}", flush=True)
    if args.part:
        user = os.environ.get("HF_USERNAME")
        check(bool(user), "HF_USERNAME not set (.env)")
        res["checks"]["repos"] = ensure_repos(api, args.part, args.recipe, user, args.dry_run)
        print(f"[6] adapter repos: {res['checks']['repos']}", flush=True)
    return res


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--part", default=None, help="pre-create the adapter repos of this part")
    p.add_argument("--recipe", default="R0")
    p.add_argument("--dry-run", action="store_true", help="do not create Hub repos")
    p.add_argument("--stages", default=None, help="comma list (default: all four)")
    p.add_argument("--max-rows", type=int, default=None, help="debug only: first N rows per file")
    p.add_argument("--skip-manifest", action="store_true", help="debug only")
    p.add_argument("--out", default=str(OUT))
    return p.parse_args(argv)


def main(argv=None, api=None, load_tok=None, chat_template=None) -> int:
    args = parse(argv)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # HF_HOME/HF_TOKEN before huggingface_hub reads them; never printed
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    if load_tok is None or chat_template is None:
        from emx import formats

        load_tok = load_tok or formats.load_tokenizer
        chat_template = chat_template or formats.chat_template
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = run(args, api, load_tok, chat_template)
        res["pass"] = True
        debug = args.max_rows or args.skip_manifest or args.stages
        res["complete"] = not debug
    except PreflightFail as e:
        res = {"pass": False, "error": str(e), "args": vars(args), "versions": versions()}
        out.write_text(json.dumps(res, indent=2) + "\n")
        print(f"PREFLIGHT FAIL: {e}", flush=True)
        return 1
    out.write_text(json.dumps(res, indent=2) + "\n")
    if not res["complete"]:
        print("PREFLIGHT PARTIAL (debug flags set; not a pass)", flush=True)
        return 1
    print("PREFLIGHT PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
