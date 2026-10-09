"""Draw an E13 labelling page and write it with its draw record and private key (prereg §4.4,
Amendment B item 7; `emx.e13.labels`). Pattern of scripts/eq0_handlabel_page.py.

Kinds:
  notice  the notice check (before training): one kept instance per surviving family from
          results/E13/inputs/notices.jsonl; style flags from results/E13/style/notice_flags.json,
          if present, go in an information box at the top.
  full    the 38-item pass: dev and infam batteries (base and organism seeds of --recipe) and the
          sweep's site parts for the swept organism seed.
  early   the 30-item early-stop pass: dev, infam and any direction-source answers.

Amendment E: `--recipe tan32` draws the retry's pass into results/E13/run2/labels/<kind>/ under
the retry's rules (`labels.RULES_E`): the base model's first-run answers are reused, so every
answer Jacob labelled in the first run (the first run's key.private.json files, bound by sha256 as
sources) is excluded from the draw.

Writes results/E13/labels/<kind>/{page.html, draw.json, key.private.json}. The key is gitignored
and never in the page (leak scan). The page is published by the main session as a private
Artifact with the `db` capability (collection "notice_check" or "labels").

Usage: uv run python scripts/e13_label_page.py {notice,full,early} [--recipe r1|r32|tan32]
         [--patch-key org_r1_s0] [--stopped-site input] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from emx.e13 import contracts as C
from emx.e13 import labels as L

ROOT = Path(__file__).resolve().parents[1]
NOTICES = f"{C.INPUTS_DIR}/notices.jsonl"
NOTICE_FLAGS = f"{C.STYLE_DIR}/notice_flags.json"
NOTICE_FLAGS_V1 = f"{C.STYLE_DIR}/notice_flags_v1features.json"  # Amendment C


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def recipe_default() -> str:
    """The first run's recipe (the retry is always named: --recipe tan32)."""
    p = ROOT / C.decision_path("dev_decision")
    if p.exists():
        return json.loads(p.read_text()).get("recipe") or "r1"
    return "r1"


def patch_key(recipe: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    p = ROOT / C.decision_path("dev_decision", recipe)
    if p.exists():
        s = json.loads(p.read_text()).get("patch_seed")
        if isinstance(s, str) and s:
            return s
        if isinstance(s, int):
            return C.model_key("org", recipe, s)
    keys = sorted({f.name.split("__")[1]
                   for f in (ROOT / C.JUDGED_DIR).glob(f"sweep__org_{recipe}_s*__*.csv")})
    if len(keys) > 1:
        raise SystemExit(f"several swept seeds {keys}: pass --patch-key")
    return keys[0] if keys else None


def gen_names(kind: str, recipe: str, pk: str | None) -> list[str]:
    orgs = [C.model_key("org", recipe, s) for s in C.TRAIN_SEEDS["org"]]
    names = [C.gen_name("dev", k) for k in (C.BASE_KEY, *orgs)]
    names += [C.gen_name("infam", k) for k in orgs]
    missing = [n for n in names if not (ROOT / C.judged_csv(n)).exists()]
    if missing:
        raise SystemExit(f"judged CSVs missing: {missing}")
    if kind == "full" and pk:
        names += [n for n in (C.gen_name("sweep", pk, s) for s in C.SITE_NAMES)
                  if (ROOT / C.judged_csv(n)).exists()]
    if kind == "early":  # this recipe's direction source only (Amendment E: never the other run's)
        names += sorted(f.stem for f in (ROOT / C.JUDGED_DIR).glob(f"dirsrc__org_{recipe}_s*.csv"))
    return names


def first_run_labelled() -> tuple[list[list], dict]:
    """Amendment E item 6: the identities of every answer labelled in the first run's passes,
    and the sha256 of the key files they come from (bound into the retry's sources)."""
    out, sha = [], {}
    for kind in ("full", "early"):
        rel = f"{C.labels_dir(None)}/{kind}/key.private.json"
        p = ROOT / rel
        if p.exists():
            out += L.labelled_idents(json.loads(p.read_text()))
            sha[rel] = sha_file(p)
    return sorted(out), sha


def load_sources(names: list[str]) -> tuple[object, dict]:
    import pandas as pd

    from emx.contracts import KEY_COLUMNS
    from emx.e13 import judging

    frames, sha = [], {}
    for n in names:
        g, j = C.gen_csv(n), C.judged_csv(n)
        sha[g], sha[j] = sha_file(ROOT / g), sha_file(ROOT / j)
        sc = judging.load_scored(ROOT / g, ROOT / j)
        if "question" not in sc.columns or "response" not in sc.columns:
            text = pd.read_csv(ROOT / g, usecols=[*KEY_COLUMNS, "question", "response"])
            sc = sc.merge(text, on=list(KEY_COLUMNS), how="left", validate="many_to_one")
        frames.append(sc)
    return pd.concat(frames, ignore_index=True), sha


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=L.KINDS)
    ap.add_argument("--recipe", choices=C.RECIPES, default=None)
    ap.add_argument("--patch-key", default=None)
    ap.add_argument("--stopped-site", action="append", choices=C.SITE_NAMES, default=None)
    ap.add_argument("--force", action="store_true", help="replace a different existing draw")
    a = ap.parse_args(argv)
    if a.kind == "notice" and a.recipe in C.AMENDMENT_E_RECIPES:
        ap.error("the notice check is the first run's (reused as frozen)")
    out = ROOT / C.labels_dir(a.recipe if a.kind != "notice" else None) / a.kind
    if a.kind == "notice":
        rows = [json.loads(x) for x in (ROOT / NOTICES).read_text().splitlines() if x.strip()]
        sha = {NOTICES: sha_file(ROOT / NOTICES)}
        flags = None
        if (ROOT / NOTICE_FLAGS).exists():
            flags = json.loads((ROOT / NOTICE_FLAGS).read_text())
            sha[NOTICE_FLAGS] = sha_file(ROOT / NOTICE_FLAGS)
        if (ROOT / NOTICE_FLAGS_V1).exists():  # Amendment C: the frozen v1 features, distinct
            v1 = json.loads((ROOT / NOTICE_FLAGS_V1).read_text())
            sha[NOTICE_FLAGS_V1] = sha_file(ROOT / NOTICE_FLAGS_V1)
            flags = {"fresh audit of these notices (Amendment A)": flags,
                     "v1 feature definitions re-rated on these notices (Amendment C check)": v1}
        sources = {"rows": rows, "source_sha256": sha, "flags": flags}
    else:
        recipe = a.recipe or recipe_default()
        pk = patch_key(recipe, a.patch_key) if a.kind == "full" else None
        names = gen_names(a.kind, recipe, pk)
        scored, sha = load_sources(names)
        sources = {"scored": scored, "source_sha256": sha}
        if recipe in C.AMENDMENT_E_RECIPES:
            excl, excl_sha = first_run_labelled()
            sources.update(rules=L.RULES_E, exclude=excl)
            sha.update(excl_sha)
        if a.stopped_site is not None:
            sources["stopped_sites"] = a.stopped_site
    try:
        rec = L.write_pass(out, a.kind, sources, force=a.force)
    except (L.LeakError, FileExistsError, ValueError) as e:
        print(f"E13_LABEL_PAGE FAIL {e}", file=sys.stderr)
        return 1
    page = (out / "page.html").read_bytes()
    print(f"E13_LABEL_PAGE OK {a.kind}: {rec['n_items']} items, composition "
          f"{rec['composition']}, stopped {rec['stopped_sites']}")
    print(f"  {out / 'page.html'} sha256 {hashlib.sha256(page).hexdigest()[:16]}; "
          f"draw.json and key.private.json beside it (collection {L.COLLECTION[a.kind]!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
