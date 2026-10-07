"""E9 corpus sampling CLI (unit A; prereg §4.2, §4.14). Free: public Hugging Face downloads only.

    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py split
    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py pools
    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py heldout
    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py background --seeds 1 2
    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py extend --seed 1 --role core \
        --tokens 500000            # or --docs N for the document-count filler
    uv run --with zstandard==0.25.0 python scripts/e9_corpus.py verify

Order matters: pools, then backgrounds for every CPT seed, then heldout (Amendment G: at most
10 per shard, >= 100 shards, excluding pool and all background hashes; the first draw is archived
as heldout_text.v1). Backgrounds exclude pools and every held-out draw on disk.
Outputs go to data/E9/pools/ and data/E9/background/ (raw text gitignored; manifests committed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

from emx.e9 import contracts as C
from emx.e9 import corpus as K

ROOT = K.REPO_ROOT


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        from dotenv import load_dotenv

        load_dotenv(env)
        print("env keys loaded (names only):",
              [k for k in ("HF_TOKEN",) if os.environ.get(k)], file=sys.stderr)


def _corpus(args) -> K.Corpus:
    import logging

    from emx.e9.text import tokenizer

    logging.getLogger("transformers.tokenization_utils_base").setLevel(logging.ERROR)
    return K.Corpus(root=ROOT, shards=K.load_shard_list(), tok=tokenizer(),
                    workers=args.workers, cache_dir=ROOT / C.DATA / "_cache" / "shards")


def cmd_split(args):
    shards = K.load_shard_list()
    sp = K.split_shards(shards)
    cand, bg = sp["candidate"], sp["background"]
    n_c = sum(len(v) for v in cand.values())
    print(f"shards {len(shards)}  candidate CC {n_c} "
          f"({sum(s.size for v in cand.values() for s in v):,} B)  "
          f"candidate non-CC (unused) {len(sp['candidate_non_cc'])}  "
          f"background {len(bg)} ({sum(s.size for s in bg):,} B)")
    print(f"topics {len(cand)}")
    for t, v in cand.items():
        print(f"  {t:32s} {len(v):5d} shards {sum(s.size for s in v):>16,} B")
    print("background by kind:", dict(Counter(
        "common_crawl" if s.topic else s.source_dir.split("-")[0] for s in bg)))


def cmd_pools(args):
    c = _corpus(args)
    man = K.sample_pools(c)
    for name, m in man.items():
        print(name, m["n"], m["sha256"], m["tokens"])


def cmd_heldout(args):
    m = K.sample_heldout(_corpus(args))
    print(m["name"], m["n"], m["sha256"], m["tokens"])


def cmd_background(args):
    c = _corpus(args)
    for s in args.seeds:
        if s not in C.CORPUS_CPT_SEEDS:
            raise SystemExit(f"unknown CPT seed {s}")
        t0 = time.time()
        m = K.sample_background(c, s)
        print(f"s{s} n={m['n']} sha256={m['sha256']} wall={time.time() - t0:.0f}s")
        print(json.dumps({r: {k: v[k] for k in ("n", "progress")} for r, v in m["roles"].items()}))


def cmd_extend(args):
    m = K.extend(_corpus(args), args.seed, args.role, extra_tokens=args.tokens,
                 extra_docs=args.docs)
    print(f"s{args.seed} n={m['n']} sha256={m['sha256']}", json.dumps(m["extensions"][-1]))


def cmd_verify(args):
    problems = K.verify(ROOT)
    for p in problems:
        print("FAIL", p)
    if problems:
        raise SystemExit(1)
    print("verify ok")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=4)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("split").set_defaults(fn=cmd_split)
    sub.add_parser("pools").set_defaults(fn=cmd_pools)
    sub.add_parser("heldout").set_defaults(fn=cmd_heldout)
    b = sub.add_parser("background")
    b.add_argument("--seeds", type=int, nargs="+", required=True)
    b.set_defaults(fn=cmd_background)
    e = sub.add_parser("extend")
    e.add_argument("--seed", type=int, required=True)
    e.add_argument("--role", required=True, choices=K.ROLE_ORDER)
    g = e.add_mutually_exclusive_group(required=True)
    g.add_argument("--tokens", type=int)
    g.add_argument("--docs", type=int)
    e.set_defaults(fn=cmd_extend)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    if args.cmd not in ("split", "verify"):
        _load_env()
    t0 = time.time()
    args.fn(args)
    print(f"[e9_corpus] {args.cmd} done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
