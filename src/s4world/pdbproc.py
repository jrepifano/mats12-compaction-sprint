"""PDB parsing, sequence extraction and provenance matching for the S4 world (plan §1.5).

Rules: model 1 only, ATOM records only, altloc blank or A, the 20 standard residues, one chain.
The residue sequence is the tuple of 3-letter names in file order. Identity is from a global
Needleman–Wunsch alignment (match 1, mismatch 0, gap -1), divided by the longer length.

Numpy only, so the module also runs inside the agent image (U6's mock walk uses it there).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from s4world.contracts import PROVENANCE_LEN_TOL, PROVENANCE_MIN_IDENTITY

STANDARD_RESIDUES = frozenset((
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
))


@dataclass(frozen=True)
class Atom:
    name: str
    resname: str
    chain: str
    resseq: int
    icode: str
    x: float
    y: float
    z: float
    element: str


def _element(line: str) -> str:
    el = line[76:78].strip().upper()
    if el:
        return el
    name = line[12:16]
    return (name[1] if name[0] == " " else name[:2].strip()[0]).upper()


def atoms(pdb_text: str, chain: str) -> list[Atom]:
    """The §1.5 atom set: model 1, ATOM only, altloc blank/A, standard residues, one chain."""
    out: list[Atom] = []
    for line in pdb_text.splitlines():
        rec = line[:6]
        if rec == "ENDMDL":
            break
        if rec != "ATOM  ":
            continue
        if line[16] not in (" ", "A"):
            continue
        resname = line[17:20].strip()
        if resname not in STANDARD_RESIDUES or line[21] != chain:
            continue
        out.append(Atom(
            name=line[12:16].strip(), resname=resname, chain=line[21],
            resseq=int(line[22:26]), icode=line[26].strip(),
            x=float(line[30:38]), y=float(line[38:46]), z=float(line[46:54]),
            element=_element(line),
        ))
    return out


def residues(pdb_text: str, chain: str) -> list[tuple[str, int, str]]:
    """(resname, resseq, icode) for each residue, in file order."""
    seen: list[tuple[str, int, str]] = []
    last: tuple[int, str] | None = None
    for a in atoms(pdb_text, chain):
        key = (a.resseq, a.icode)
        if key != last:
            seen.append((a.resname, a.resseq, a.icode))
            last = key
    return seen


def sequence(pdb_text: str, chain: str) -> tuple[str, ...]:
    return tuple(r[0] for r in residues(pdb_text, chain))


def _nw_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n, m = len(a), len(b)
    h = np.zeros((n + 1, m + 1), dtype=np.int64)
    h[0, :] = -np.arange(m + 1)
    h[:, 0] = -np.arange(n + 1)
    idx = np.arange(1, m + 1)
    for i in range(1, n + 1):
        s = (b == a[i - 1]).astype(np.int64)
        cand = np.maximum(h[i - 1, :-1] + s, h[i - 1, 1:] - 1)
        # left moves: h[i, j] = max_k<=j (cand[k] - (j - k)), with h[i, 0] as the k = 0 term
        run = np.maximum.accumulate(np.concatenate(([h[i, 0]], cand + idx)))
        h[i, 1:] = run[1:] - idx
    return h


def identity(a: tuple[str, ...] | list[str], b: tuple[str, ...] | list[str]) -> float:
    """Matches in an optimal global alignment / max(len(a), len(b)). 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    vocab: dict[str, int] = {}
    ai = np.array([vocab.setdefault(x, len(vocab)) for x in a])
    bi = np.array([vocab.setdefault(x, len(vocab)) for x in b])
    h = _nw_matrix(ai, bi)
    i, j, matches = len(a), len(b), 0
    while i > 0 and j > 0:
        s = int(ai[i - 1] == bi[j - 1])
        if h[i, j] == h[i - 1, j - 1] + s:
            matches += s
            i, j = i - 1, j - 1
        elif h[i, j] == h[i - 1, j] - 1:
            i -= 1
        else:
            j -= 1
    return matches / max(len(a), len(b))


def match(table_seq: tuple[str, ...] | list[str],
          candidates: dict[str, tuple[str, ...]]) -> str | None:
    """The candidate code whose sequence the table matches under §1.5, else None.

    A match needs length within ±PROVENANCE_LEN_TOL of the candidate's and identity at least
    PROVENANCE_MIN_IDENTITY. If several match, the highest identity wins.
    """
    best: tuple[float, str] | None = None
    n = len(table_seq)
    for code, seq in candidates.items():
        if not seq or abs(n - len(seq)) > PROVENANCE_LEN_TOL * len(seq):
            continue
        ident = identity(table_seq, seq)
        if ident >= PROVENANCE_MIN_IDENTITY and (best is None or ident > best[0]):
            best = (ident, code)
    return None if best is None else best[1]
