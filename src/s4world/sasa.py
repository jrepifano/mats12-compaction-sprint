"""Per-residue solvent accessible surface area for the S4 world (plan §1.4).

Shrake–Rupley with a 1.4 Å probe, 960 golden-spiral points per atom and Bondi van der Waals
radii by element, over the §1.5 atom set (pdbproc.atoms; hydrogens dropped). Relative SASA uses
the theoretical maximum ASA values of Tien et al. 2013 (PLoS One 8:e80635). Numpy only, with a
cell list for the neighbour search, so it runs in the agent image too.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from s4world.contracts import BURIAL_THRESHOLDS
from s4world.pdbproc import atoms

PROBE = 1.4
N_POINTS = 960

BONDI = {"C": 1.70, "N": 1.55, "O": 1.52, "S": 1.80, "SE": 1.90, "P": 1.80, "H": 1.20}
DEFAULT_RADIUS = 1.80

# Tien et al. 2013, Table 1, theoretical max ASA (Å²)
MAX_ASA = {
    "ALA": 129.0, "ARG": 274.0, "ASN": 195.0, "ASP": 193.0, "CYS": 167.0,
    "GLN": 225.0, "GLU": 223.0, "GLY": 104.0, "HIS": 224.0, "ILE": 197.0,
    "LEU": 201.0, "LYS": 236.0, "MET": 224.0, "PHE": 240.0, "PRO": 159.0,
    "SER": 155.0, "THR": 172.0, "TRP": 285.0, "TYR": 263.0, "VAL": 174.0,
}


def sphere_points(n: int = N_POINTS) -> np.ndarray:
    """n unit vectors on a golden spiral."""
    k = np.arange(n) + 0.5
    phi = np.arccos(1.0 - 2.0 * k / n)
    theta = np.pi * (1.0 + 5.0**0.5) * k
    return np.column_stack((np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)))


def atom_sasa(coords: np.ndarray, radii: np.ndarray, n_points: int = N_POINTS) -> np.ndarray:
    """Shrake–Rupley SASA (Å²) per atom."""
    unit = sphere_points(n_points)
    ext = radii + PROBE
    cell = 2.0 * float(ext.max()) if len(ext) else 1.0
    keys = np.floor(coords / cell).astype(np.int64)
    grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for i, key in enumerate(map(tuple, keys)):
        grid[key].append(i)
    offsets = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
    out = np.zeros(len(coords))
    for i in range(len(coords)):
        cx, cy, cz = keys[i]
        cand = np.array(
            [j for dx, dy, dz in offsets for j in grid.get((cx + dx, cy + dy, cz + dz), ())]
        )
        d = np.linalg.norm(coords[cand] - coords[i], axis=1)
        nb = cand[(d < ext[cand] + ext[i]) & (cand != i)]
        pts = coords[i] + ext[i] * unit
        if len(nb):
            d2 = ((pts[:, None, :] - coords[nb][None, :, :]) ** 2).sum(axis=2)
            free = (d2 >= (ext[nb] ** 2)[None, :]).all(axis=1)
            n_free = int(free.sum())
        else:
            n_free = n_points
        out[i] = 4.0 * np.pi * ext[i] ** 2 * n_free / n_points
    return out


def burial_class(rel: float) -> str:
    lo, hi = BURIAL_THRESHOLDS
    return "buried" if rel < lo else ("partial" if rel < hi else "exposed")


def per_residue_sasa(pdb_text: str, chain: str) -> list[dict]:
    """One SasaRow per residue in file order; mean_sasa (Å², 2 dp) is the residue's total.

    rel_sasa is rounded to 3 dp and burial is classed from the rounded value, so a table
    written from these rows is self-consistent.
    """
    ats = [a for a in atoms(pdb_text, chain) if a.element not in ("H", "D")]
    coords = np.array([(a.x, a.y, a.z) for a in ats], dtype=float).reshape(-1, 3)
    radii = np.array([BONDI.get(a.element, DEFAULT_RADIUS) for a in ats], dtype=float)
    per_atom = atom_sasa(coords, radii)
    rows: list[dict] = []
    last: tuple[int, str] | None = None
    for a, s in zip(ats, per_atom):
        key = (a.resseq, a.icode)
        if key != last:
            rows.append({"resname": a.resname, "chain": a.chain, "resseq": a.resseq,
                         "icode": a.icode, "total": 0.0})
            last = key
        rows[-1]["total"] += float(s)
    for r in rows:
        total = r.pop("total")
        r["mean_sasa"] = round(total, 2)
        r["rel_sasa"] = round(total / MAX_ASA[r["resname"]], 3)
        r["burial"] = burial_class(r["rel_sasa"])
    return rows
