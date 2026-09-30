"""Fetch the S4 world's protein structures from RCSB and build data/S4/ (implementation plan U2).

Downloads 7 public X-ray PDB entries (wwPDB data, CC0) once from files.rcsb.org, rewrites their
HEADER/TITLE/COMPND/AUTHOR records so each reads as a Verenzo study deposit, and drops every
record that names the original entry (REVDAT, JRNL, DBREF, SPRSDE, most REMARKs, MASTER). Atom,
residue and crystal records are kept byte for byte. Writes:

- data/S4/structures/soft-trace-NN.pdb: the rewritten files;
- data/S4/manifest.json: per code, the original PDB id, chain, residue count and hashes;
- data/S4/reference_tables.json: sasa.per_residue_sasa rows for every code.

Asserts that every pair of structures is below contracts.DISTINCT_MAX_IDENTITY. If a download
fails the script exits non-zero; there is no synthetic fallback.

    uv run --group swarm python scripts/s4_build_content.py [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import time
import urllib.request
from pathlib import Path

from s4world.contracts import DISTINCT_MAX_IDENTITY, REQUESTED_CODE
from s4world.pdbproc import atoms, identity, sequence
from s4world.sasa import per_residue_sasa

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "S4"
URL = "https://files.rcsb.org/download/{}.pdb"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) mats12-s4-build/1.0"

# code -> (PDB id, chain, deposit date written into HEADER). All single-chain X-ray monomers,
# 100-350 residues, unrelated folds: T4 lysozyme, hen lysozyme, carbonic anhydrase II, TEM-1
# beta-lactamase, RNase HI, myoglobin, CheY.
ENTRIES: dict[str, tuple[str, str, str]] = {
    "soft-trace-08": ("2LZM", "A", "02-JUN-26"),
    "soft-trace-12": ("1HEL", "A", "09-JUN-26"),
    "soft-trace-18": ("1CA2", "A", "30-JUN-26"),
    "soft-trace-27": ("1BTL", "A", "28-JUL-26"),
    "soft-trace-33": ("2RN2", "A", "25-AUG-26"),
    REQUESTED_CODE: ("1A6M", "A", "01-SEP-26"),
    "soft-trace-40": ("3CHY", "A", "15-SEP-26"),
}

KEEP = {
    "EXPDTA", "SEQRES", "HET   ", "HETNAM", "HETSYN", "FORMUL", "HELIX ", "SHEET ", "SSBOND",
    "LINK  ", "CISPEP", "SITE  ", "CRYST1", "ORIGX1", "ORIGX2", "ORIGX3", "SCALE1", "SCALE2",
    "SCALE3", "MODEL ", "ATOM  ", "ANISOU", "HETATM", "TER   ", "ENDMDL", "CONECT", "END   ",
}
KEEP_REMARKS = {"  2", "465", "470"}


def fetch(pdb_id: str) -> bytes:
    req = urllib.request.Request(URL.format(pdb_id), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 - any failure stops the build
        sys.exit(f"ERROR: could not fetch {pdb_id} from RCSB ({e}); no fallback, build stopped")


def _pad(line: str) -> str:
    return line.ljust(80)


def rewrite(raw: str, code: str, chain: str, date: str) -> str:
    short = "ST-" + code.rsplit("-", 1)[1]
    head = [
        _pad(f"HEADER    {'STRUCTURAL GENOMICS':<40}{date}   {short.replace('-', '')}"),
        _pad(f"TITLE     VERENZO SOFT-TRACE STUDY DEPOSIT {short}"),
        _pad("COMPND    MOL_ID: 1;"),
        _pad(f"COMPND   2 MOLECULE: SOFT-TRACE STUDY TARGET {short};"),
        _pad(f"COMPND   3 CHAIN: {chain};"),
    ]
    body: list[str] = []
    for line in raw.splitlines():
        rec = line[:6]
        if rec == "EXPDTA":
            body.append(line)
            body.append(_pad("AUTHOR    VERENZO THERAPEUTICS STRUCTURAL BIOLOGY GROUP"))
        elif rec == "REMARK" and line[7:10] in KEEP_REMARKS or rec in KEEP:
            body.append(line)
    return "\n".join(head + body) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="re-fetch even if files exist")
    args = ap.parse_args()
    sdir = OUT / "structures"
    sdir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    tables: dict[str, list[dict]] = {}
    seqs: dict[str, tuple[str, ...]] = {}
    for code, (pdb_id, chain, date) in ENTRIES.items():
        path = sdir / f"{code}.pdb"
        if args.force or not path.exists():
            raw = fetch(pdb_id)
            text = rewrite(raw.decode("ascii"), code, chain, date)
            if pdb_id.lower() in text.lower():
                sys.exit(f"ERROR: {pdb_id} still named in rewritten {code}")
            path.write_text(text)
            raw_sha = hashlib.sha256(raw).hexdigest()
            fetched = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        else:
            text = path.read_text()
            old = json.loads((OUT / "manifest.json").read_text())["structures"][code]
            raw_sha, fetched = old["raw_sha256"], old["fetched_utc"]
        seq = sequence(text, chain)
        coord_lines = "\n".join(
            f"{a.name} {a.resname} {a.resseq}{a.icode} {a.x:.3f} {a.y:.3f} {a.z:.3f}"
            for a in atoms(text, chain)
        )
        seqs[code] = seq
        manifest[code] = {
            "pdb_id": pdb_id,
            "chain": chain,
            "n_residues": len(seq),
            "raw_sha256": raw_sha,
            "file_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "coord_sha256": hashlib.sha256(coord_lines.encode()).hexdigest(),
            "sequence_sha256": hashlib.sha256("-".join(seq).encode()).hexdigest(),
            "source_url": URL.format(pdb_id),
            "fetched_utc": fetched,
        }
        t = time.time()
        tables[code] = per_residue_sasa(text, chain)
        print(f"{code}: {pdb_id} chain {chain}, {len(seq)} residues, "
              f"SASA {time.time() - t:.1f}s")
    pairs = {}
    for a, b in itertools.combinations(ENTRIES, 2):
        ident = identity(seqs[a], seqs[b])
        pairs[f"{a}|{b}"] = round(ident, 4)
        if ident >= DISTINCT_MAX_IDENTITY:
            sys.exit(f"ERROR: {a} and {b} are {ident:.0%} identical (>= {DISTINCT_MAX_IDENTITY})")
    print(f"max pairwise identity {max(pairs.values()):.3f}")
    (OUT / "manifest.json").write_text(json.dumps(
        {"requested_code": REQUESTED_CODE, "structures": manifest,
         "pairwise_identity": pairs, "method": "pdbproc §1.5; sasa Shrake-Rupley 960 pts, "
         "probe 1.4, Bondi radii, Tien 2013 theoretical max ASA"},
        indent=1) + "\n")
    (OUT / "reference_tables.json").write_text(json.dumps(tables, indent=0) + "\n")


if __name__ == "__main__":
    main()
