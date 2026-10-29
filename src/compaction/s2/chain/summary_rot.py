"""Week 2, experiment 4 ("chain stress"): which task each boundary summary's entries come from
("summary rot"; docs/sprint/plans/week2-chain-stress.md on sprint/prep, Appendix B,
"Historical-content share": per chain, the share of summary bullets about tasks before k,
averaged over the 8 unrelated chains at boundary 4; predicted >= 30%, 65% confidence).

At boundary k (after task k of a chain), pi's compaction writes a summary the agent carries into
task k+1. Each entry of that summary is attributed to the current task (task k, just finished),
an earlier task j < k, or "generic" (harness rules, boilerplate, nothing task-specific).

- `split_entries(summary)`: the unit is a non-empty line. Excluded as structure, and counted by
  kind: markdown headings (`#`...), `---` separators, code-fence lines, pi's literal "No prior
  history.", and bold-only pseudo-headings (a whole line in `**...**`, e.g. "**Verification:**"
  or the harness's "**Turn Context (split turn):**"). Text inside code fences stays as entries.
- `task_identifiers(task_id, texts, token_list)`: the high-specificity identifiers of one task:
  its ticket tag, task id, `feature/<slug>` branch and the bare slug (case-sensitive), its repo
  name and repo stem (the repo's first hyphen part, e.g. "drizzle", "kcp"; case-insensitive), and
  the token list's package names that contain the stem or are a prefix of the repo ("cattr");
  other package names ("backend", "correctness", "my-website") are too generic. A repo name that
  is an English word ("returns", `AMBIGUOUS`) only counts in code-ish context (`returns`,
  returns.git, /returns, returns/, "returns library|repo|...").
- `chain_identifiers(task_ids, texts, token_lists)`: per task of the chain's first k, its
  identifiers minus any that match another of those tasks' own text (its tokens other than
  binaries, its dependency names and its instruction; the token list's "context" and
  "instruction"), so a name both tasks share (one repo in a related chain, "vitest" as drizzle's
  test runner) can't decide an entry.
- `code_label(text, idents)`: "task_<j>" when the entry matches identifiers of exactly one task;
  None (undecided) when it matches none or several.
- Opus pass (`opus_req`, `parse_opus`): one call per summary, every entry numbered, the chain's
  tasks 1..k with repo, tag, branch and a short gist; strict JSON, per entry "task_<j>" or
  "generic" plus the task numbers it mentions. Final label = the code label when decided, else
  Opus's.
- Shares (`category`, `summary_counts`, `summary_shares`, `chain_weighted`, `bootstrap_ci`):
  per summary the share of labelled entries in each category (current, 1/2/3 tasks back,
  generic); then the mean over chains (each chain weighs the same) per arm and boundary, and a
  seeded percentile bootstrap over chains for the earlier-task share (all j < k). Unlabelled
  entries (Opus answer malformed twice) are left out of the denominators and counted.

Pure functions; no model calls here except through the backend the caller passes to the script.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass

from compaction.s1_types import Req
from compaction.s2.chain.chain_stress_score import _tag_pat

MODEL = "claude-opus-5-5"
CATS = ("current", "back_1", "back_2", "back_3", "generic")
EARLIER = ("back_1", "back_2", "back_3")
AMBIGUOUS = frozenset({"returns"})
GIST_CHARS = 260
STRUCT_RE = {
    "heading": re.compile(r"^\s*#{1,6}(\s|$)"),
    "separator": re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$"),
    "fence": re.compile(r"^\s*(```|~~~)"),
    "no_history": re.compile(r"^\s*No prior history\.?\s*$"),
    "bold_heading": re.compile(r"^\s*\*\*[^*]+\*\*\s*:?\s*$"),
}
BOUND_L, BOUND_R = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"


# ------------------------------------------------------------------------------------------
# Entries
# ------------------------------------------------------------------------------------------


def structure_kind(line: str) -> str | None:
    for kind, pat in STRUCT_RE.items():
        if pat.search(line):
            return kind
    return None


def split_entries(summary: str) -> tuple[list[str], Counter]:
    """(entries in order, excluded structure lines by kind)."""
    entries: list[str] = []
    excluded: Counter = Counter()
    for line in (summary or "").splitlines():
        if not line.strip():
            continue
        kind = structure_kind(line)
        if kind:
            excluded[kind] += 1
        else:
            entries.append(line.strip())
    return entries, excluded


def structure_lines(summary: str) -> list[tuple[str, str]]:
    """The excluded structure lines as (kind, stripped line), in order."""
    out = []
    for line in (summary or "").splitlines():
        kind = structure_kind(line) if line.strip() else None
        if kind:
            out.append((kind, line.strip()))
    return out


BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def is_bullet(text: str) -> bool:
    """A bullet or numbered-list entry (the plan's "bullets"); prose lines are not."""
    return bool(BULLET_RE.match(text))


# ------------------------------------------------------------------------------------------
# Identifiers and the code matcher
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ident:
    kind: str
    text: str
    pattern: re.Pattern


def _word(text: str, ci: bool) -> re.Pattern:
    return re.compile(BOUND_L + re.escape(text) + BOUND_R, re.IGNORECASE if ci else 0)


def _ambiguous(word: str) -> re.Pattern:
    w = re.escape(word)
    return re.compile(
        rf"`{w}`|{BOUND_L}{w}\.git{BOUND_R}|/{w}{BOUND_R}|{BOUND_L}{w}/"
        rf"|{BOUND_L}{w} (?:library|repo|repository|project|codebase|package){BOUND_R}",
        re.IGNORECASE)


def repo_stem(repo: str) -> str:
    return repo.split("-")[0]


def _keep_package(name: str, repo: str) -> bool:
    n, r, s = name.lower(), repo.lower(), repo_stem(repo).lower()
    return len(n) >= 3 and (s in n or (r.startswith(n) and len(n) >= 4))


def task_identifiers(task_id: str, texts: dict, token_list: dict) -> list[Ident]:
    tx = texts[task_id]
    repo, branch, tag = tx["repo"], tx.get("branch") or token_list.get("branch"), tx["tag"]
    out = [Ident("tag", tag, _tag_pat(tag)), Ident("task_id", task_id, _word(task_id, False))]
    if branch:
        out.append(Ident("branch", branch, _word(branch, False)))
        slug = branch.split("/", 1)[-1]
        if slug != branch and "-" in slug:
            out.append(Ident("slug", slug, _word(slug, False)))
    for name in dict.fromkeys([repo, repo_stem(repo)]):
        if len(name) < 3:
            continue
        pat = _ambiguous(name) if name.lower() in AMBIGUOUS else _word(name, True)
        out.append(Ident("repo", name, pat))
    for p in token_list["tokens"].get("package") or ():
        if _keep_package(p, repo) and p.lower() not in {repo.lower(), repo_stem(repo).lower()}:
            out.append(Ident("package", p, _word(p, False)))
    return out


def own_text(token_list: dict) -> str:
    """A task's own names: tokens other than binaries, dependency names, instruction."""
    parts: list[str] = []
    for kind, toks in token_list["tokens"].items():
        if kind != "binary":
            parts += list(toks)
    parts += list(token_list.get("context") or ())
    parts.append(token_list.get("instruction") or "")
    return "\n".join(parts)


def chain_identifiers(task_ids: list[str], texts: dict, token_lists: dict) -> dict[int, list]:
    """{task number (1-based): identifiers that no other of these tasks shares}."""
    own = {i: own_text(token_lists[t]) for i, t in enumerate(task_ids, 1)}
    out = {}
    for i, t in enumerate(task_ids, 1):
        ids = task_identifiers(t, texts, token_lists[t])
        others = [o for j, o in own.items() if j != i and task_ids[j - 1] != t]
        out[i] = [d for d in ids if not any(d.pattern.search(o) for o in others)]
    return out


def code_matches(text: str, idents: dict[int, list]) -> dict[int, list[str]]:
    hits: dict[int, list[str]] = {}
    for j, ids in idents.items():
        m = [d.text for d in ids if d.pattern.search(text)]
        if m:
            hits[j] = m
    return hits


def code_label(text: str, idents: dict[int, list]) -> str | None:
    hits = code_matches(text, idents)
    return f"task_{next(iter(hits))}" if len(hits) == 1 else None


def foreign_tags(text: str, own_tags: set[str], all_tags: set[str]) -> list[str]:
    """Known ticket tags in `text` that belong to no task among the chain's first k."""
    return sorted(t for t in all_tags - own_tags if _tag_pat(t).search(text))


# ------------------------------------------------------------------------------------------
# The Opus pass
# ------------------------------------------------------------------------------------------

SYSTEM = (
    "A coding agent worked through several unrelated coding tasks in a row, in one session. "
    "After each task its conversation was compacted into a summary that it carried into the "
    "next task. You read one such summary, split into numbered entries (one line each), and a "
    "list of the tasks done so far (task 1 first; the highest number is the task that had just "
    "finished when this summary was written).\n\n"
    "For every entry, say which task it is about:\n"
    "- \"task_<j>\": the entry is about task j: its repository, ticket, branch, files, code, "
    "tests, decisions, progress or status, or a rule or fact the agent tied to that task.\n"
    "- \"generic\": the entry is about no one task: rules for how the agent works in every "
    "task (tool-call format, no network, the final completion command, shell habits) when not "
    "tied to a task, section boilerplate (\"None.\", \"(none)\"), or text you can't tie to any "
    "one task.\n"
    "If an entry is about several tasks, label it with the task it says the most about; if it "
    "is about them equally, the most recent of them. In \"tasks\", list the number of every "
    "task the entry mentions or is about (empty for most generic entries). Use the entry's own "
    "words and the context of nearby entries (a sub-bullet belongs to its parent's task unless "
    "it says otherwise). Label every entry, in order, once each.")


def task_gist(task_id: str, texts: dict, token_list: dict) -> str:
    tx = texts[task_id]
    instr = " ".join((token_list.get("instruction") or "").split())
    if len(instr) > GIST_CHARS:
        instr = instr[:GIST_CHARS].rsplit(" ", 1)[0] + " ..."
    return (f"repository {tx['repo']}; ticket {tx['tag']}; branch {tx.get('branch')}; "
            f"feature: {tx['feature']}. {instr}")


def opus_schema(k: int) -> dict:
    labels = [f"task_{j}" for j in range(1, k + 1)] + ["generic"]
    item = {"type": "object", "additionalProperties": False, "required": ["i", "label", "tasks"],
            "properties": {"i": {"type": "integer"},
                           "label": {"type": "string", "enum": labels},
                           "tasks": {"type": "array", "items": {"type": "integer"}}}}
    return {"type": "object", "additionalProperties": False, "required": ["labels"],
            "properties": {"labels": {"type": "array", "items": item}}}


def opus_user(tasks: list[dict], entries: list[str]) -> str:
    k = len(tasks)
    lines = ["<tasks>"]
    for t in tasks:
        cur = " (just finished when the summary was written)" if t["n"] == k else ""
        lines.append(f"task_{t['n']}{cur}: {t['gist']}")
    lines += ["</tasks>", "", f"<summary_entries count=\"{len(entries)}\">"]
    lines += [f"[{i}] {e}" for i, e in enumerate(entries, 1)]
    lines += ["</summary_entries>", "",
              f"Return \"labels\": {len(entries)} objects, i = 1..{len(entries)} in order."]
    return "\n".join(lines)


def opus_req(key: str, rid: str, tasks: list[dict], entries: list[str]) -> Req:
    return Req(key=key, system=SYSTEM, user=opus_user(tasks, entries),
               schema=opus_schema(len(tasks)), max_output_tokens=16000, rid=rid,
               source="chain", private=False)


class Malformed(ValueError):
    """An Opus answer that doesn't label every entry exactly once with a known label."""


def parse_opus(data, n_entries: int, k: int) -> list[dict]:
    """[{"label", "tasks"}] per entry in order; `Malformed` when malformed (missing, duplicate
    or out-of-range index, unknown label, wrong count)."""
    if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
        raise Malformed("no labels list")
    ok = {f"task_{j}" for j in range(1, k + 1)} | {"generic"}
    out: dict[int, dict] = {}
    for row in data["labels"]:
        if not isinstance(row, dict):
            raise Malformed(f"row not an object: {row!r}")
        i, lab = row.get("i"), row.get("label")
        if not isinstance(i, int) or not 1 <= i <= n_entries:
            raise Malformed(f"index out of range: {i!r}")
        if i in out:
            raise Malformed(f"duplicate index {i}")
        if lab not in ok:
            raise Malformed(f"unknown label {lab!r}")
        tasks = row.get("tasks") or []
        if not isinstance(tasks, list) or not all(isinstance(t, int) for t in tasks):
            raise Malformed(f"bad tasks for {i}: {tasks!r}")
        out[i] = {"label": lab, "tasks": sorted({t for t in tasks if 1 <= t <= k})}
    if len(out) != n_entries:
        raise Malformed(f"{len(out)} labels for {n_entries} entries")
    return [out[i] for i in range(1, n_entries + 1)]


# ------------------------------------------------------------------------------------------
# Shares
# ------------------------------------------------------------------------------------------


def category(label: str | None, k: int) -> str | None:
    """"current", "back_<d>" (task k-d) or "generic"; None for an unlabelled entry."""
    if label is None:
        return None
    if label == "generic":
        return "generic"
    j = int(label.split("_", 1)[1])
    if not 1 <= j <= k:
        raise ValueError(f"{label} outside 1..{k}")
    d = k - j
    return "current" if d == 0 else f"back_{d}"


def summary_counts(labels: list[str | None], k: int) -> dict:
    c = Counter(category(lab, k) for lab in labels)
    counts = {cat: c.get(cat, 0) for cat in CATS}
    extra = sorted(x for x in c if x not in CATS and x is not None)
    if extra:
        raise ValueError(f"categories beyond 3 back: {extra}")
    return {"counts": counts, "n_labelled": sum(counts.values()), "n_unlabelled": c.get(None, 0)}


def summary_shares(labels: list[str | None], k: int) -> dict:
    sc = summary_counts(labels, k)
    n = sc["n_labelled"]
    shares = {cat: (v / n if n else None) for cat, v in sc["counts"].items()}
    shares["earlier"] = (sum(sc["counts"][c] for c in EARLIER) / n) if n else None
    return {**sc, "shares": shares}


def split_shares(rows: list[dict], k: int) -> dict:
    """Sensitivity: an Opus-decided entry Opus says is about several tasks counts 1/m toward
    each; code-decided entries and single-task entries count whole."""
    w: Counter = Counter()
    n = 0
    for r in rows:
        if r["final_label"] is None:
            continue
        n += 1
        tasks = r.get("opus_tasks") or []
        if r["decided_by"] == "opus" and len(tasks) >= 2:
            for t in tasks:
                w[category(f"task_{t}", k)] += 1 / len(tasks)
        else:
            w[category(r["final_label"], k)] += 1
    shares = {cat: (w.get(cat, 0.0) / n if n else None) for cat in CATS}
    shares["earlier"] = (sum(w.get(c, 0.0) for c in EARLIER) / n) if n else None
    return shares


def chain_weighted(per_chain: list[dict], keys=(*CATS, "earlier")) -> dict:
    """Mean of each share over chains (each chain weighs the same); chains whose summary has no
    labelled entry are left out."""
    rows = [p for p in per_chain if p.get("earlier") is not None]
    n = len(rows)
    return {"n_chains": n,
            **{key: (sum(r[key] for r in rows) / n if n else None) for key in keys}}


def paired_bootstrap_diff(a: dict[str, float], b: dict[str, float], seed: int,
                          n_boot: int = 10_000, level: float = 0.95) -> dict:
    """Mean over chains of a[c] - b[c] (chains in both), with a percentile bootstrap that
    resamples chains with replacement (the pairing is kept)."""
    chains = sorted(set(a) & set(b))
    d = [a[c] - b[c] for c in chains]
    lo, hi = bootstrap_ci(d, seed, n_boot, level)
    return {"diff": sum(d) / len(d) if d else None, "ci95": [lo, hi], "n_chains": len(d),
            "seed": seed, "n_boot": n_boot,
            "excludes_zero": lo is not None and len(d) > 1 and (lo > 0 or hi < 0)}


def bootstrap_ci(values: list[float], seed: int, n_boot: int = 10_000,
                 level: float = 0.95) -> tuple[float | None, float | None]:
    """Percentile bootstrap of the mean, resampling chains with replacement."""
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(n_boot))
    lo_i = int((1 - level) / 2 * n_boot)
    hi_i = int((1 + level) / 2 * n_boot) - 1
    return means[lo_i], means[hi_i]
