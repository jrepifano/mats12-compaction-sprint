"""The SWE-Interact stub smoke (build split "Order" 5): $0 end to end on one real task image.

A stub agent and a stub simulator run `01_plan`, then a fresh `02_implement` that crosses the
threshold twice (bulky command output), so the second boundary uses pi's update prompt. A
stub eligibility marks boundary 2 eligible. The five replays run on a stub Haiku backend. Then
8 primary arms (live, cc_prompt and opencode, repair and matched; live twice) restore the boundary
from stub freeze records, each passing the fork check; one makes and reverts an act (kept in
its window diff) and ends with ask_user + bash in one response (the bash deferred), and its
secondary answers the ask and runs the deferred bash; the live candidate is a valid
comparison; the inputs of each source's pair differ only in the block; `lossaudit.load` reads
every boundary; the kept images are removed; no pilot container is left.

The threshold is 60k, not the plan's "forced 20k": pi keeps its last ~20k (pi-estimated)
tokens verbatim, about 80k characters, which is already about 40k tokens by our loop's
estimate, so a 20k threshold would recompact at every call.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import config as C
from compaction.s2 import interact as I
from compaction.s2 import interact_run as IR
from compaction.s2.model import Reply, StubModel, Usage, tool_call

THRESHOLD = 60_000
BULK = "seq 1 6000"  # ~29 KB; the observation keeps 10,000 characters
PROBE_Q = "Done with the initial implementation, committed. Review?"


class StubSim:
    """Round 1 inspects `git log`; round 2 replies with a hash of its whole request."""

    route = C.SIM_ROUTE

    def complete(self, messages, tools, max_tokens, *, purpose):
        if tools and messages[-1]["role"] != "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                tool_call("run_repo_command", {"command": "git log --oneline -2"}, "c")]}
        else:
            h = hashlib.sha256(I.canon(messages).encode()).hexdigest()[:10]
            msg = {"role": "assistant", "content": f"one more thing ({h}): keep frob in frob.ts"}
        return Reply(msg, Usage(100, 10, 0, 0, 0.0, "stub"), "stop")


class TwoToolStub:
    """Plays `actions` (StubModel's format), then one response with two tool calls: ask_user
    and a bash command after it (which capture mode must defer)."""

    route = C.ROUTES["stub"]

    def __init__(self, actions: list[dict], question: str, after: str):
        self.inner, self.n = StubModel(list(actions)), len(actions)
        self.question, self.after, self.calls = question, after, 0

    def complete(self, messages, tools, max_tokens, *, purpose):
        if self.calls < self.n:
            self.calls += 1
            return self.inner.complete(messages, tools, max_tokens, purpose=purpose)
        self.calls += 1
        msg = {"role": "assistant", "content": "", "tool_calls": [
            tool_call("ask_user", {"question": self.question}, f"two_{self.calls}_a"),
            tool_call("bash", {"command": self.after}, f"two_{self.calls}_b")]}
        return Reply(msg, Usage(1000, 50, cost_source="stub"), "tool_calls")


class StubHaiku:
    """`s1_types.Backend` for the replays: a fixed summary per request, $0."""

    name, family, context_tokens, bytes_per_token = "stub-haiku", "anthropic", 1_000_000, 2.2

    def __init__(self):
        from compaction import s1_replay as RP
        self.model = RP.MODEL

    def run(self, reqs, *, purpose):
        from compaction.s1_types import Resp
        return {r.key: Resp(r.key, None, f"## Goal\nStub summary for {r.key[:40]}.", None,
                            model=self.model) for r in reqs}


def actions() -> list[dict]:
    plan = [{"ask": "Hi, what change do you need?"},
            {"bash": "printf 'Plan: add frob(x)=2x in frob.ts\\n' > /tmp/plan.md && echo ok"},
            {"submit": True}]
    impl = [{"bash": "cat /tmp/plan.md && git rev-parse HEAD > /tmp/base_commit.txt"},
            {"bash": "printf 'export const frob = (x) => 2 * x\\n' > frob.ts && git add frob.ts"
                     " && git commit -qm 'initial implementation' && echo ok"},
            {"ask": PROBE_Q},
            *[{"bash": BULK}] * 9,
            {"ask": "Fixed that, committed again. Review?"},
            {"bash": "echo notes > scratch.txt && echo ok"},
            *[{"bash": BULK}] * 12]
    return plan + impl


def run(task_id: str = "kombu-single-active-consumer-priority", *,
        root: Path | None = None) -> dict:
    from compaction.s2 import lossaudit as LA
    from compaction.s2 import replays as RE
    from compaction.s2 import session as SS

    t0 = time.monotonic()
    root = Path(root or (C.raw_dir() / "interact_smoke" / time.strftime("%Y%m%dT%H%M%S")))
    task = IR.load_task(task_id)
    cfg = IR.BaseConfig(task=task, root=root, threshold=THRESHOLD, max_calls=80,
                        prefix=f"smoke-{time.strftime('%Y%m%dT%H%M%S')}-")
    rep: dict = {"root": str(root), "task": task_id, "checks": {}}
    chk = rep["checks"]

    def eligible(bdir: Path, fields: dict) -> bool:
        k = int(Path(bdir).name[1:])
        (Path(bdir) / "boundary" / "eligibility.json").write_text(
            json.dumps({"eligible": k == 2, "stub": True}))
        return k == 2

    kept_dirs: list[Path] = []
    run_id = None
    try:
        with Invocation(purpose=IR.PURPOSE) as inv:
            run_id = inv.run_id
            res = IR.run_base(cfg, agent_model=StubModel(actions()), sim_model=StubSim(),
                              inv=inv, eligible=eligible)
            rep["base"] = {k: res.get(k) for k in ("end", "boundaries", "n_calls", "error")}
            rd = cfg.run_dir
            kept_dirs.append(rd)
            chk["base_eligible_at_b2"] = res["end"] == "eligible" and res["boundaries"] == [1, 2]
            comp = [json.loads((rd / f"b{k}" / "boundary" / "compaction.json").read_text())
                    for k in (1, 2)]
            chk["update_prompt_at_b2"] = (not comp[0]["info"].get("update")
                                          and bool(comp[1]["info"].get("update")))
            b2 = rd / "b2"
            bj = json.loads((b2 / "boundary" / "boundary.json").read_text())
            chk["boundary_fields"] = all(bj.get(f) is not None for f in (
                "tag", "fingerprint_sha256", "sim_probe_sha256", "sim_calls",
                "tail_start_index", "summary_index"))
            loaded = [LA.load(rd / f"b{k}") for k in (1, 2)]
            chk["lossaudit_load"] = all(r["history_complete"] for r in loaded)
            chk["lossaudit_image_is_kept"] = loaded[1]["s2"]["image"] == bj["tag"]

            rr = RE.replay_boundary(b2, backend=StubHaiku())
            rep["replays"] = rr
            chk["replays_written"] = all(v["status"] == "written" for v in rr.values())
            if not chk["replays_written"]:
                raise RuntimeError(f"replays failed: {rr}")
            from compaction.s2 import interact_score as SC
            frz_spec = {"act": {"kind": "command", "pattern": r"^git log"}, "decision": None,
                        "description": "smoke", "reask_keywords": ["strings"]}
            for source in ("live", "cc_prompt", "opencode"):
                SC.write_freeze(b2, {
                    "cand_id": f"{source}-smoke", "source_policy": source, "req_id": "b2-q1",
                    "repair_text": "keep frob in frob.ts",
                    "matched_text": "the CI badge in README is green",
                    "matched_rationale": "stub", "act_spec": frz_spec,
                    "checks": {n: {"outcome": "pass"} for n in IR.CHECK_NAMES}})
            pairs = {}
            arms = [(s_, k_, 1) for s_ in ("live", "cc_prompt", "opencode")
                    for k_ in ("repair", "matched")] + [("live", "repair", 2),
                                                         ("live", "matched", 2)]
            for source, kind, r_ in arms:
                cid = f"{source}-smoke"
                if (source, kind, r_) == ("live", "repair", 1):
                    # an act made and reverted inside the window, then an ask_user followed by
                    # a bash call in the same response (deferred, not run)
                    agent = TwoToolStub([{"bash": "git log --oneline -1"},
                                         {"bash": "echo act > act_reverted.txt"},
                                         {"bash": "rm act_reverted.txt"}],
                                        "Should frob also accept strings?",
                                        "echo deferred > deferred.txt")
                else:
                    agent = StubModel([{"bash": "git log --oneline -1"},
                                       {"ask": "Should frob also accept strings?"}])
                out = IR.run_arm(rd, 2, cid, kind, r_, task, agent_model=agent,
                                 sim_model=StubSim(), inv=inv, require_committed=False)
                slug = IR.ArmSpec(source, kind, r_, "x").slug
                rep.setdefault("arms", {})[slug] = {
                    k: out.get(k) for k in ("end", "fork_check", "error")}
                kept_dirs.append(rd / "arms" / slug)
                if r_ == 1:
                    pairs.setdefault(source, []).append(json.loads(
                        (rd / "arms" / slug / "context.json").read_text()))
            chk["arms_ended_at_ask"] = all(a["end"] == "ask_user"
                                           for a in rep["arms"].values())
            chk["fork_check_all_arms"] = all((a.get("fork_check") or {}).get("ok")
                                             for a in rep["arms"].values())
            ad = rd / "arms" / "live-repair-1"
            diff, net = (ad / "diff.patch").read_text(), (ad / "net.patch").read_text()
            chk["reverted_act_in_window_diff"] = ("act_reverted.txt" in diff
                                                  and "act_reverted.txt" not in net)
            deferred = json.loads((ad / "deferred.json").read_text())
            cmds = [c["cmd"] for c in json.loads((ad / "commands.json").read_text())]
            chk["deferred_not_run"] = (deferred == [{"tool": "bash",
                                                     "cmd": "echo deferred > deferred.txt"}]
                                       and "deferred.txt" not in diff
                                       and not any("deferred" in c for c in cmds))
            sec = IR.run_secondary(rd, 2, "live-smoke", "repair", 1, task,
                                   agent_model=StubModel([{"submit": True}]),
                                   sim_model=StubSim(), inv=inv, require_committed=False,
                                   grade=lambda t, p_, rid: {"reward": None, "bytes": len(p_)})
            rep["secondary"] = {k: sec.get(k) for k in ("end", "sim_calls", "error")}
            start = json.loads((ad / "secondary" / "start_messages.json").read_text())
            chk["secondary_runs_deferred"] = (
                sec["end"] == "submitted" and sec["sim_calls"] == 1
                and I.PENDING_ASK not in json.dumps(start)
                and I.PENDING_DEFERRED not in json.dumps(start))
            vc = IR.valid_comparison(b2, "live-smoke", require_committed=False)
            rep["valid_comparison"] = vc
            chk["valid_comparison_live"] = vc["valid"]
            # The accumulated-loss resume (phase B), on unit A's real session: from b2 (the
            # arms above don't touch it) to b3, pi's update prompt, b2 left byte-identical.
            for bk in (rd / "b1", b2):  # nothing carded at b1 or b2 (the stub screen)
                IR._transition(bk, "exhausted", "smoke")
            before = {str(p.relative_to(b2)): p.read_bytes() for p in b2.rglob("*")
                      if p.is_file() and "arms" not in p.parts}
            rr = IR.resume_base(rd, task, agent_model=StubModel([{"bash": BULK}] * 12),
                                sim_model=StubSim(), inv=inv, trigger=lambda b, f: True,
                                threshold=THRESHOLD)
            rep["resume"] = {k: rr.get(k) for k in ("end", "boundaries", "error", "fork_check")}
            after = {str(p.relative_to(b2)): p.read_bytes() for p in b2.rglob("*")
                     if p.is_file() and "arms" not in p.parts}
            c3 = json.loads((rd / "b3" / "boundary" / "compaction.json").read_text()) \
                if (rd / "b3" / "boundary" / "compaction.json").exists() else {}
            chk["resume_to_b3_with_update_prompt"] = (
                rr["end"] == "eligible" and rr["boundaries"] == [3]
                and bool((c3.get("info") or {}).get("update"))
                and (rr.get("fork_check") or {}).get("ok") is True)
            chk["resume_leaves_b2_untouched"] = before == after
            # Unit B on the smoke's own files: a command act the stub arms performed (git log),
            # and check 6's docker runner on the kept b2 image, both ways.
            spec = {"act": {"kind": "command", "pattern": r"^git log"}, "decision": None,
                    "description": "smoke", "reask_keywords": ["strings"]}
            sc = SC.score(rd / "arms" / "live-matched-1", spec)
            chk["scorer_reads_arm"] = sc["score"] == 1 and sc["reasked"] is True
            never = {"act": {"kind": "diff_added", "pattern": "NEVER_IN_THE_REFERENCE_x9q",
                             "path": None}, "decision": None, "description": "smoke",
                     "reask_keywords": []}
            always = {"act": {"kind": "file_touched", "path": "kombu/*.py"}, "decision": None,
                      "description": "smoke", "reask_keywords": []}
            r_pass = SC.check_reference(never, rd, 2)
            r_fail = SC.check_reference(always, rd, 2)
            rep["check6"] = {"pass": r_pass.get("outcome"), "fail": r_fail.get("outcome"),
                             "pass_reason": r_pass.get("reason")}
            chk["check6_runner"] = (r_pass.get("outcome") == "pass"
                                    and r_fail.get("outcome") == "fail")
            chk["inputs_differ_only_in_block"] = all(
                IR.inputs_differ_only_in_block(
                    p[0], p[1], IR.block_index(s, RE.load_source(b2, s)["summary_index"]))
                for s, p in pairs.items())
    finally:
        bad = []
        for d in kept_dirs:
            bad += SS.KeepStore.remove(d)
        chk["kept_removed"] = not bad
        # this smoke's own containers only: a live pilot shares the emx.sandbox label
        q = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=emx.run={run_id}"],
                           capture_output=True, text=True, timeout=60, check=False)
        # a failed query must not pass as "nothing left" (astra fingerprint r1)
        chk["no_containers_left"] = bool(run_id) and q.returncode == 0 and not q.stdout.split()
    rep["ok"] = bool(chk) and all(chk.values())
    rep["seconds"] = round(time.monotonic() - t0, 1)
    (root / "smoke_report.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep
