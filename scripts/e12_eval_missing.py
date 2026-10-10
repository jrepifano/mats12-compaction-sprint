"""Generate only the missing outputs of one eval step, through the driver's own `eval_vllm`
(2026-10-10, pod 3). After 14 nudge runs were retrained on a full disk, the step-level
completion check re-derived pins from adapter files that had been deleted (retained on HF) to
free space, and treated finished outputs as stale. This skips that check: it generates (with the
applied-smoke check) only the jobs of the step whose output CSV and meta are absent and whose
adapter files exist, then writes `MANIFEST.<step>-extra.sha256` over the new files for the pull.

Pod 4 (Amendment D'', 2026-10-10), a fresh pod with the results tree pushed by hand:
  --keys REGEX         only jobs whose key matches (fullmatch), e.g. '[DR][123]_n(05|10|20|40)'
  --restore-from-hf    fetch the selected keys' missing adapter files from the private HF repo
                       (privacy asserted first) and check every file's sha256 against ckpt.json
  --restore-genpath    write control genpath.json from the committed admission record (the
                       control files lived on pod 3 only), as `lora_check_score` wrote it
  --clear-stop         move a leftover STOP control file aside (pod 3's 40-minute "hang" was a
                       STOP left by an earlier controller stop); without it a STOP refuses (rc 5)
The process ends with os._exit so a vLLM engine subprocess cannot hold it open after an error.

  /ws/venv-vllm/bin/python scripts/e12_eval_missing.py eval-nudge --recipe tan32 \
      --keys '[DR][123]_n(05|10|20|40)' --restore-from-hf --restore-genpath --clear-stop
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e12 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R


def _pod():
    spec = importlib.util.spec_from_file_location("e12_pod", ROOT / "scripts" / "e12_pod.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def clear_stop(pod, ctx, clear: bool) -> None:
    p = pod.control_file(ctx, "STOP")
    if not p.exists():
        return
    if not clear:
        raise pod.Stopped(f"{p} present (pass --clear-stop to move it aside)")
    dst = p.with_name(f"STOP.cleared_{G.utc().replace(':', '')}")
    p.rename(dst)
    print(f"cleared leftover STOP -> {dst}", flush=True)


def restore_genpath(pod, ctx) -> None:
    if pod.read_control(ctx, "genpath.json") is not None:
        print("genpath.json present; kept", flush=True)
        return
    r = ctx.recipe_name
    adm = pod.read_admission(ctx)
    if adm is None or not adm.get("admitted"):
        raise pod.Refused(f"{C.admission_path(r)} absent or not admitted")
    lc = json.loads((ctx.root / C.bench_path(r, "lora_check")).read_text())
    pod.write_genpath(ctx, {**adm, "stage": "lora-check", "lora_check_verdict": lc["verdict"],
                            "restored_from": C.admission_path(r), "restored_utc": G.utc()})
    print(f"genpath.json restored from {C.admission_path(r)} ({adm['gen_path']})", flush=True)


def restore_adapters(pod, ctx, keys: list[str]) -> None:
    """The selected keys' adapter files from the private HF repo, sha256-checked (Refused on a
    mismatch, `checkpoint_files`)."""
    if not keys:
        return
    from huggingface_hub import hf_hub_download

    r = ctx.recipe_name
    repo = pod.hf_repo_id(r)
    api, _ = pod._hf_api()
    pod.assert_private(api, repo)
    for k in keys:
        adir = ctx.path(C.adapter_files_dir(r, k))
        for name in R.ADAPTER_FILES:
            hf_hub_download(repo, f"{k}/{name}", local_dir=str(adir.parent),
                            token=os.environ.get("HF_TOKEN"))
        pod.checkpoint_files(ctx, {k})
    print(f"restored {len(keys)} adapters from {repo} (sha256 match ckpt.json)", flush=True)


def main(argv=None, pod=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=list(C.EVAL_STEPS))
    ap.add_argument("--recipe", default="tan32")
    ap.add_argument("--keys", default=None, help="regex (fullmatch) over job keys")
    ap.add_argument("--restore-from-hf", action="store_true")
    ap.add_argument("--restore-genpath", action="store_true")
    ap.add_argument("--clear-stop", action="store_true")
    ap.add_argument("--root", default=None, help="the driver's --root (tests)")
    ap.add_argument("--scratch", default=None, help="the driver's --scratch (tests)")
    a = ap.parse_args(argv)
    pod = pod or _pod()
    extra = [*(["--root", a.root] if a.root else []), *(["--scratch", a.scratch] if a.scratch else [])]
    ctx = pod.Ctx(pod.parser().parse_args([a.step, "--recipe", a.recipe, *extra]))
    clear_stop(pod, ctx, a.clear_stop)
    if a.restore_genpath:
        restore_genpath(pod, ctx)
    st = pod.GenState(ctx)
    r = a.recipe
    pat = re.compile(a.keys) if a.keys else None
    jobs = [j for j in st.jobs if pat is None or pat.fullmatch(j.key)]
    keys = sorted({j.key for j in jobs if j.key != C.BASE_KEY})

    def absent(k):  # any adapter file missing (an interrupted download included; review r1)
        adir = ctx.path(C.adapter_files_dir(r, k))
        return any(not (adir / n).is_file() for n in R.ADAPTER_FILES)

    if a.restore_from_hf:
        restore_adapters(pod, ctx, [k for k in keys if absent(k)])
    missing_adapter = [k for k in keys if absent(k)]
    if missing_adapter:
        print(f"{a.step}: {len(missing_adapter)} adapters missing {missing_adapter[:5]}; "
              "nothing generated", flush=True)
        return 1
    pod.checkpoint_files(ctx, set(keys))  # every adapter's sha256 = ckpt.json, before vLLM
    inp = pod.Inputs(ctx)
    pending = [j for j in jobs if not pod.output_ok(ctx, inp, st, j)]

    def smoke_pass(k):  # current (the checkpoint's sha256s) and PASS
        if not pod.smoke_current(ctx, k):
            return False
        return pod.read_json(ctx.root / C.smoke_adapter_path(r, k)).get("verdict") == "PASS"

    need_smoke = [k for k in keys if not smoke_pass(k)]  # a current FAIL/UNVERIFIED is redone (r2)
    print(f"{a.step}: {len(st.jobs)} jobs, {len(jobs)} selected; {len(pending)} to generate; "
          f"{len(need_smoke)} adapters to smoke", flush=True)
    pod.eval_vllm(ctx, st, inp, pending, need_smoke)
    # the chain's own end-of-step gate (`run_eval`): every output verifies against the plan and
    # every adapter has a current applied-smoke record; here its verdict must also be PASS
    bad = [f"{j.kind}/{j.key}" for j in jobs if not pod.output_ok(ctx, inp, st, j)]
    smoke_bad = [k for k in keys if not smoke_pass(k)]
    rels = [p for j in jobs if f"{j.kind}/{j.key}" not in bad
            for p in (C.gen_csv(r, j.kind, j.key), C.gen_meta(r, j.kind, j.key))]
    rels += [C.smoke_adapter_path(r, k) for k in keys
             if (ctx.root / C.smoke_adapter_path(r, k)).is_file()]
    (ctx.root / C.manifest_path(r, f"{a.step}-extra")).write_text(
        "".join(f"{G.sha256_file(ctx.root / p)}  {p}\n" for p in rels))
    print(f"E12_EVAL_MISSING {a.step}: {len(jobs) - len(bad)}/{len(jobs)} outputs verify; "
          f"smoke not PASS {smoke_bad}; outputs failing {bad[:10]}; manifest {len(rels)} files",
          flush=True)
    return 0 if not bad and not smoke_bad else 1


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except SystemExit as e:  # argparse
        rc = e.code if isinstance(e.code, int) else 1
    except Exception:  # noqa: BLE001 - any error: print it, then the hard exit below
        traceback.print_exc()
        print("E12_EVAL_MISSING_FAILED", flush=True)
        rc = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
