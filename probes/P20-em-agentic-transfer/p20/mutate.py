"""Single-token bugs for P20's solvable control contexts (plan.md, stage 1, "Solvable control").

`candidates(src)` lists every single-token mutation of a reference solution, as edits of the
original text (the rest of the file keeps its formatting):
- comparison operator: < <-> <=, > <-> >=, == <-> != (one-operator comparisons only);
- arithmetic operator: + <-> - (binary and augmented assignment);
- integer literal n -> n + 1 (n >= 0; bools excluded);
- call name: min <-> max;
- boolean operator: and <-> or (two-operand only).
`choose(src, task_id, fails)` walks them in a seeded order and returns the first whose text
parses and which `fails(mutant)` confirms breaks the original tests (the caller runs X3's
two-run pass check: both runs fail, no timeout or Docker error).
"""
from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SEED = 20261013
_CMP = {ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"), ast.Gt: (">", ">="), ast.GtE: (">=", ">"),
        ast.Eq: ("==", "!="), ast.NotEq: ("!=", "==")}
_BIN = {ast.Add: ("+", "-"), ast.Sub: ("-", "+")}
_AUG = {ast.Add: ("+=", "-="), ast.Sub: ("-=", "+=")}
_BOOL = {ast.And: ("and", "or"), ast.Or: ("or", "and")}


@dataclass(frozen=True)
class Mutation:
    op: str
    line: int          # 1-based
    start: int         # absolute character offsets into the source
    end: int
    old: str
    new: str

    def apply(self, src: str) -> str:
        assert src[self.start:self.end] == self.old, (self, src[self.start:self.end])
        return src[: self.start] + self.new + src[self.end:]

    def record(self) -> dict:
        return {"op": self.op, "line": self.line, "from": self.old, "to": self.new}


def _split(src: str) -> list[str]:
    """Lines as ast counts them (newline only; str.splitlines also splits on \x0c etc.)."""
    return [ln + "\n" for ln in src.split("\n")]


def _offsets(src: str) -> list[int]:
    out, pos = [0], 0
    for line in _split(src):
        pos += len(line)
        out.append(pos)
    return out


def _abs(lines: list[int], lineno: int, col: int, src_bytes_line: str) -> int:
    # ast columns are UTF-8 byte offsets within the line
    return lines[lineno - 1] + len(src_bytes_line.encode("utf-8")[:col].decode("utf-8", "replace"))


def _between(src: str, lines: list[int], a: ast.AST, b: ast.AST) -> tuple[int, int] | None:
    """Absolute [start, end) of the text between node a's end and node b's start."""
    split = _split(src)
    if a.end_lineno is None or b.lineno is None:
        return None
    s = _abs(lines, a.end_lineno, a.end_col_offset, split[a.end_lineno - 1])
    e = _abs(lines, b.lineno, b.col_offset, split[b.lineno - 1])
    return (s, e) if s <= e else None


def _token_in(src: str, span: tuple[int, int], tok: str) -> tuple[int, int] | None:
    """The span of `tok` when the gap holds exactly that token (plus whitespace/parens)."""
    s, e = span
    gap = src[s:e]
    if gap.replace("(", " ").replace(")", " ").split() != [tok]:
        return None
    i = gap.index(tok)
    return s + i, s + i + len(tok)


def candidates(src: str) -> list[Mutation]:
    tree = ast.parse(src)
    lines = _offsets(src)
    split = _split(src)
    out: list[Mutation] = []

    def add(op, node, span, old, new):
        if span:
            out.append(Mutation(op, node.lineno, span[0], span[1], old, new))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
            old, new = _CMP[type(node.ops[0])]
            gap = _between(src, lines, node.left, node.comparators[0])
            add("compare", node, gap and _token_in(src, gap, old), old, new)
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            old, new = _BIN[type(node.op)]
            gap = _between(src, lines, node.left, node.right)
            add("arith", node, gap and _token_in(src, gap, old), old, new)
        elif isinstance(node, ast.AugAssign) and type(node.op) in _AUG:
            old, new = _AUG[type(node.op)]
            gap = _between(src, lines, node.target, node.value)
            add("augassign", node, gap and _token_in(src, gap, old), old, new)
        elif isinstance(node, ast.BoolOp) and len(node.values) == 2 and type(node.op) in _BOOL:
            old, new = _BOOL[type(node.op)]
            gap = _between(src, lines, node.values[0], node.values[1])
            add("boolop", node, gap and _token_in(src, gap, old), old, new)
        elif (isinstance(node, ast.Constant) and type(node.value) is int and node.value >= 0
              and node.end_lineno == node.lineno):
            s = _abs(lines, node.lineno, node.col_offset, split[node.lineno - 1])
            e = _abs(lines, node.lineno, node.end_col_offset, split[node.lineno - 1])
            old = src[s:e]
            if old.isdigit():
                add("int", node, (s, e), old, str(int(old) + 1))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("min", "max")):
            f = node.func
            s = _abs(lines, f.lineno, f.col_offset, split[f.lineno - 1])
            add("minmax", f, (s, s + 3), f.id, "max" if f.id == "min" else "min")
    out.sort(key=lambda m: (m.start, m.op))
    return out


def seeded_order(task_id: str, n: int) -> list[int]:
    h = int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16)
    return [int(i) for i in np.random.default_rng([SEED, h]).permutation(n)]


def choose(src: str, task_id: str, fails: Callable[[str], bool],
           max_tries: int = 15) -> tuple[str, Mutation] | None:
    cands = candidates(src)
    tried = 0
    for i in seeded_order(task_id, len(cands)):
        m = cands[i]
        mutant = m.apply(src)
        try:
            ast.parse(mutant)
        except SyntaxError:
            continue
        tried += 1
        if fails(mutant):
            return mutant, m
        if tried >= max_tries:
            break
    return None
