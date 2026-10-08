"""E11's laptop side of the dose ladder: watch the pod for ready files, judge the pilots with
luna, decide eligibility, and push the decision back (implementation plan U7, "Laptop-pod
barrier"; prereg §4.3-4.4).

Per poll, ONE consolidated ssh call lists the pod's results/E11/ladder/ready_r<k>.json and
decision_r<k>.json with their sha256 (CLAUDE.md "RunPod operations": one call per check, minutes
apart; `C.LADDER_LAPTOP_POLL_S` = 3 min). For each rung k whose ready file is on the pod and that
has no local decision, as separate commands (fetch, verify, then act):
  1. scp the ready file to a local temp name; its sha256 must equal the polled one;
  2. `bash scripts/pod_pull_results.sh --exp E11 --slot _A --manifest MANIFEST.pilot-r<k>.sha256`
     (the pull verifies the manifest itself);
  3. verify: the local manifest's sha256 equals the ready file's `manifest_sha256`, and every
     pilot CSV's sha256 equals its ready entry (and its manifest line); only then is the ready
     file kept as results/E11/ladder/ready_r<k>.json;
  4. judge the rung's pilots with luna, rejudge pass included
     (`scripts/e11_judge.py --conditions <pilot keys> --judges luna`, in process);
  5. per dataset, `emx.e11.analysis.load_condition(root, key, pilot=True, judges=("luna",))` and
     `eligibility()` (unit U8), cross-checked against `C.is_eligible(n_em, n_scored)` in integers;
     a dataset over 2% unscored after the rejudge pass (or flagged incomplete by the pod in its
     ready entry) is incomplete and never eligible (plan review r2, finding 2);
  6. write results/E11/ladder/decision_r<k>.json atomically:
     {rung, utc, ready_sha256, datasets: {ds: {eligible, em_dev, n_scored, n_em, gen_sha256,
     judged_sha256, incomplete}}, provenance}; datasets absent from the ready file are absent;
  7. push it: scp to a temp name in the pod's ladder directory, then one ssh that `mv`s it into
     place unless a decision is already there and prints its sha256, which must equal ours.
Idempotent: a local decision is never recomputed or rewritten (a rerun only pushes it, if the pod
lacks it); judging resumes from the judged CSV and its `.rejudge.json`, so nothing is judged
twice; a pod decision that differs from ours is reported (CONFLICT), never overwritten; a pod
decision without a local copy is reported (ORPHAN) and left alone.

Ready file (written by the pod's ladder, unit U5; schema assumed here, see `parse_ready`):
  {"rung": k, "manifest_sha256": <sha of MANIFEST.pilot-r<k>.sha256>,
   "pilots": {<ds>: {"key": <pilot key>, "gen_sha256": <sha>, "incomplete": false}}}
(`pilots` may also be keyed by pilot key, or map to the bare sha256; `datasets` is accepted
for `pilots`; so is `pilot_keys: [...]` with `gen_sha256: {pilot key: sha}`, the form
src/emx/e11/ladder.py's decision check reads. A pilot the pod failed is either left out or
marked `incomplete`.)

Pod address: the latest `running` event of the slot in logs/pods_E11.jsonl whose pod has no
`terminated` event; it must equal .env's POD_SSH_HOST<slot>/POD_SSH_PORT<slot>, which
pod_pull_results.sh uses (else refused: the pull would reach another pod).

Every log line starts with the anchored tag `E11_LADDER_LAPTOP <EVENT>` (never a bare DONE/FAIL).
`watch` ends after the last rung's decision is on the pod, or once the latest decision leaves no
dataset needing a further rung; it gives up after `--max-failures` consecutive failed polls.

Run for real under caffeinate (CLAUDE.md: idle sleep pauses long runs), in the worktree:
  caffeinate -is uv run python scripts/e11_ladder.py watch --slot _A \
      2>&1 | tee -a logs/e11/ladder_laptop_$(date -u +%Y%m%dT%H%M%SZ).log
  uv run python scripts/e11_ladder.py once --slot _A          # a single poll
  uv run python scripts/e11_ladder.py --dry-run --slot _A     # no ssh, no judging
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from emx.e11 import contracts as C

TAG = "E11_LADDER_LAPTOP"
LISTING_END = "E11_LADDER_POD_LISTING_END"
POD_EXISTS = "E11_LADDER_POD_DECISION_EXISTS"
REMOTE_LADDER = f"{C.POD_ROOT}/{C.LADDER_DIR}"
SSH_OPTS = ("-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes")
SSH_TIMEOUT_S = 90
PULL_TIMEOUT_S = 1800
MAX_RUNG = max(C.RUNGS)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_RE = re.compile(r"^([0-9a-f]{64})\s+\*?(ready|decision)_r(\d+)\.json$")


def log(event: str, msg: str = "") -> None:
    utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{TAG} {event} {utc} {msg}".rstrip(), flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


# --- pod address ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: str
    user: str
    pod_id: str

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


def read_dotenv(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def resolve_endpoint(root: Path, slot: str = C.SLOT) -> Endpoint:
    """The slot's running E11 pod, from logs/pods_E11.jsonl, cross-checked with .env."""
    p = root / "logs" / f"pods_{C.EXP}.jsonl"
    events = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    gone = {e.get("pod_id") for e in events if e.get("event") == "terminated"}
    live = [e for e in events if e.get("event") == "running" and e.get("slot") == slot
            and e.get("pod_id") not in gone]
    if not live:
        raise LookupError(f"no running E11 pod in slot {slot} in {p}")
    e = live[-1]
    env = read_dotenv(root / ".env")
    h, pt = env.get(f"POD_SSH_HOST{slot}"), env.get(f"POD_SSH_PORT{slot}")
    if not h or not pt:
        raise LookupError(f".env has no POD_SSH_HOST{slot}/POD_SSH_PORT{slot}: "
                          f"pod_pull_results.sh could not reach the pod")
    if (h, str(pt)) != (str(e["host"]), str(e["port"])):
        raise LookupError(f".env's {h}:{pt} is not pod {e['pod_id']}'s {e['host']}:{e['port']}"
                          f" (logs/pods_E11.jsonl): the pull would reach another pod")
    return Endpoint(str(e["host"]), str(e["port"]), env.get("POD_SSH_USER") or "root",
                    str(e["pod_id"]))


# --- the pod listing and the ready file -----------------------------------------------------


def poll_command() -> str:
    """One remote command: sha256 of every ready/decision file, then an end marker."""
    return (f"d={REMOTE_LADDER}; if [ -d \"$d\" ]; then cd \"$d\" && "
            f"for f in ready_r*.json decision_r*.json; do "
            f"if [ -f \"$f\" ]; then sha256sum \"$f\"; fi; done; fi; echo {LISTING_END}")


@dataclass
class Listing:
    ready: dict[int, str] = field(default_factory=dict)
    decision: dict[int, str] = field(default_factory=dict)


def parse_listing(out: str) -> Listing:
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if not lines or lines[-1] != LISTING_END:
        raise ValueError("pod listing without its end marker (truncated)")
    lst = Listing()
    for x in lines[:-1]:
        m = LIST_RE.match(x)
        if not m:
            raise ValueError(f"unexpected pod listing line {x!r}")
        getattr(lst, m.group(2))[int(m.group(3))] = m.group(1)
    return lst


def parse_ready(ready: dict, rung: int) -> tuple[str, dict[str, dict]]:
    """(manifest sha256, {ds: {"key", "gen_sha256", "incomplete"}}) from a ready file."""
    if int(ready.get("rung", -1)) != rung:
        raise ValueError(f"ready_r{rung}.json names rung {ready.get('rung')!r}")
    msha = ready.get("manifest_sha256")
    if not isinstance(msha, str) or not SHA_RE.match(msha):
        raise ValueError(f"ready_r{rung}.json: no manifest_sha256")
    pilots = ready.get("pilots", ready.get("datasets"))
    if pilots is None and isinstance(ready.get("pilot_keys"), list):
        # U5's form: a list of pilot keys plus {pilot key: gen sha256}
        shas = next((ready[f] for f in ("gen_sha256", "pilot_sha256", "sha256")
                     if isinstance(ready.get(f), dict)), {})
        pilots = {key: shas.get(key) for key in ready["pilot_keys"]}
    if not isinstance(pilots, dict) or not pilots:
        raise ValueError(f"ready_r{rung}.json names no pilots")
    by_key = {C.pilot_key(ds, rung): ds for ds in C.DS_KEYS}
    out: dict[str, dict] = {}
    for name, v in pilots.items():
        ds = name if name in C.DS_KEYS else by_key.get(name)
        if ds is None:
            raise ValueError(f"ready_r{rung}.json: unknown pilot {name!r}")
        v = {"gen_sha256": v} if isinstance(v, str) else dict(v)
        key = v.get("key", v.get("pilot_key", C.pilot_key(ds, rung)))
        if key != C.pilot_key(ds, rung):
            raise ValueError(f"ready_r{rung}.json: {ds} pilot key {key!r} is not "
                             f"{C.pilot_key(ds, rung)!r}")
        incomplete = bool(v.get("incomplete")) or v.get("ok") is False or \
            v.get("status") in ("failed", "incomplete")
        sha = v.get("gen_sha256", v.get("sha256"))
        if not incomplete and (not isinstance(sha, str) or not SHA_RE.match(sha)):
            raise ValueError(f"ready_r{rung}.json: {ds} has no gen_sha256")
        if ds in out:
            raise ValueError(f"ready_r{rung}.json names {ds} twice")
        out[ds] = {"key": key, "gen_sha256": sha, "incomplete": incomplete}
    return msha, out


def manifest_entries(p: Path) -> dict[str, str]:
    """{repo-relative path: sha256} from `shasum -a 256` lines."""
    out = {}
    for x in p.read_text().splitlines():
        if x.strip():
            sha, path = x.split(maxsplit=1)
            out[path.lstrip("*").strip()] = sha
    return out


# --- commands -------------------------------------------------------------------------------


def run_cmd(argv: list[str], *, cwd: Path | None = None, timeout: float | None = None
            ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(argv, 124, e.stdout or "", f"timeout after {timeout}s")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, C.REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


def default_judge(root: Path, keys: list[str]) -> int:
    """scripts/e11_judge.py --conditions <keys> --judges luna, in process (real API calls)."""
    try:
        return load_script("e11_judge").main(
            ["--root", str(root), "--conditions", *keys, "--judges", *C.PILOT_JUDGES])
    except SystemExit as e:  # e.g. spend.BudgetExceeded is a SystemExit
        return int(e.code) if isinstance(e.code, int) else 1


def git_commit() -> str:
    r = run_cmd(["git", "-C", str(C.REPO_ROOT), "rev-parse", "HEAD"], timeout=30)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


class LadderError(RuntimeError):
    """A refusal for one rung (logged; the rung is retried on a later poll)."""

    def __init__(self, event: str, msg: str):
        super().__init__(msg)
        self.event = event


class Ladder:
    def __init__(self, root: Path, slot: str = C.SLOT, *, runner: Callable = run_cmd,
                 resolve: Callable[[], Endpoint] | None = None,
                 judge: Callable[[Path, list[str]], int] = default_judge, analysis=None):
        self.root, self.slot, self.runner, self.judge = Path(root), slot, runner, judge
        self.resolve = resolve or (lambda: resolve_endpoint(self.root, slot))
        self._analysis = analysis
        self.ep: Endpoint | None = None

    @property
    def analysis(self):
        if self._analysis is None:
            from emx.e11 import analysis  # unit U8

            self._analysis = analysis
        return self._analysis

    # remote commands; each call is one ssh or scp process
    def ssh(self, remote: str) -> subprocess.CompletedProcess:
        ep = self.ep
        return self.runner(["ssh", "-n", "-p", ep.port, *SSH_OPTS, ep.target, remote],
                           timeout=SSH_TIMEOUT_S)

    def scp(self, src: str, dst: str) -> subprocess.CompletedProcess:
        return self.runner(["scp", "-q", "-P", self.ep.port, *SSH_OPTS, src, dst],
                           timeout=SSH_TIMEOUT_S)

    def remote(self, path: str) -> str:
        return f"{self.ep.target}:{path}"

    def poll(self) -> Listing:
        self.ep = self.resolve()
        r = self.ssh(poll_command())
        if r.returncode != 0:
            raise LadderError("POLL_FAILED", f"ssh exit {r.returncode}: {r.stderr.strip()[:200]}")
        try:
            return parse_listing(r.stdout)
        except ValueError as e:
            raise LadderError("POLL_FAILED", str(e)) from e

    # --- one rung -----------------------------------------------------------------------

    def process_rung(self, k: int, lst: Listing) -> str:
        """Returns "pushed" (the pod holds our decision) or raises LadderError."""
        ready_sha, dec = lst.ready[k], self.root / C.ladder_decision(k)
        if dec.exists():
            d = json.loads(dec.read_text())
            if d.get("ready_sha256") != ready_sha:
                raise LadderError("REFUSED", f"rung={k} local decision is for ready "
                                  f"{str(d.get('ready_sha256'))[:12]}, the pod's ready is "
                                  f"{ready_sha[:12]}; not pushed")
            return self.push(k, dec, lst.decision.get(k))
        if k in lst.decision:
            raise LadderError("ORPHAN", f"rung={k} the pod has decision_r{k}.json "
                              f"({lst.decision[k][:12]}) but there is no local copy; left alone")
        msha, entries = self.fetch_and_verify(k, ready_sha)
        keys = [e["key"] for e in entries.values() if not e["incomplete"]]
        if keys:
            log("JUDGING", f"rung={k} pilots={','.join(keys)} judge=luna")
            rc = self.judge(self.root, keys)
            if rc != 0:
                raise LadderError("JUDGE_FAILED", f"rung={k} e11_judge exit {rc}")
        rec = self.decide(k, ready_sha, msha, entries)
        if dec.exists():  # cannot happen in one process; never overwrite a decision
            raise LadderError("REFUSED", f"rung={k} {dec} appeared while deciding")
        write_json_atomic(dec, rec)
        log("DECIDED", f"rung={k} sha256={sha256_file(dec)} " + " ".join(
            f"{ds}:eligible={v['eligible']},incomplete={v['incomplete']},"
            f"n_em={v['n_em']},n_scored={v['n_scored']}" for ds, v in rec["datasets"].items()))
        return self.push(k, dec, None)

    def fetch_and_verify(self, k: int, ready_sha: str) -> tuple[str, dict]:
        ladder = self.root / C.LADDER_DIR
        ladder.mkdir(parents=True, exist_ok=True)
        tmp = ladder / f".ready_r{k}.json.pull"
        tmp.unlink(missing_ok=True)
        r = self.scp(self.remote(f"{C.POD_ROOT}/{C.ladder_ready(k)}"), str(tmp))
        if r.returncode != 0 or not tmp.exists():
            raise LadderError("PULL_FAILED", f"rung={k} scp of ready_r{k}.json: exit "
                              f"{r.returncode} {r.stderr.strip()[:200]}")
        got = sha256_file(tmp)
        if got != ready_sha:
            raise LadderError("REFUSED", f"rung={k} ready_r{k}.json sha256 {got[:12]} != the "
                              f"pod's {ready_sha[:12]}")
        try:
            msha, entries = parse_ready(json.loads(tmp.read_text()), k)
        except (ValueError, json.JSONDecodeError) as e:
            raise LadderError("REFUSED", f"rung={k} {e}") from e
        local_ready = self.root / C.ladder_ready(k)
        if local_ready.exists() and sha256_file(local_ready) != ready_sha:
            raise LadderError("REFUSED", f"rung={k} a different local ready_r{k}.json exists")
        man_name = Path(C.ladder_manifest(k)).name
        r = self.runner(["bash", "scripts/pod_pull_results.sh", "--exp", C.EXP, "--slot",
                         self.slot, "--manifest", man_name], cwd=self.root,
                        timeout=PULL_TIMEOUT_S)
        if r.returncode != 0:
            raise LadderError("PULL_FAILED", f"rung={k} pod_pull_results.sh exit "
                              f"{r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
        man = self.root / C.ladder_manifest(k)
        if not man.exists() or sha256_file(man) != msha:
            raise LadderError("REFUSED", f"rung={k} local {man_name} is missing or not the "
                              f"ready file's manifest ({msha[:12]})")
        listed = manifest_entries(man)
        for e in entries.values():
            if e["incomplete"]:
                continue
            rel = C.gen_csv(e["key"], pilot=True)
            p = self.root / rel
            if not p.exists() or sha256_file(p) != e["gen_sha256"]:
                raise LadderError("REFUSED", f"rung={k} {rel} is missing or its sha256 is not "
                                  f"the ready entry's ({e['gen_sha256'][:12]})")
            if listed.get(rel) != e["gen_sha256"]:
                raise LadderError("REFUSED", f"rung={k} {man_name} does not list {rel} with "
                                  f"the ready entry's sha256")
        if not local_ready.exists():
            os.replace(tmp, local_ready)
        else:
            tmp.unlink()
        log("VERIFIED", f"rung={k} ready={ready_sha[:12]} manifest={msha[:12]} "
            f"pilots={','.join(sorted(entries))}")
        return msha, entries

    def decide(self, k: int, ready_sha: str, msha: str, entries: dict) -> dict:
        datasets, inputs = {}, {C.ladder_ready(k): ready_sha, C.ladder_manifest(k): msha}
        for ds, e in entries.items():
            if e["incomplete"]:
                datasets[ds] = {"eligible": False, "em_dev": None, "n_scored": 0, "n_em": 0,
                                "gen_sha256": e["gen_sha256"], "judged_sha256": None,
                                "incomplete": True}
                continue
            gen = self.root / C.gen_csv(e["key"], pilot=True)
            judged = self.root / C.judged_csv(e["key"], pilot=True)
            rj = Path(f"{str(judged).removesuffix('.csv')}.rejudge.json")
            rjr = json.loads(rj.read_text()) if rj.exists() else {}
            if rjr.get("phase") != "done":
                raise LadderError("JUDGE_FAILED", f"rung={k} {ds}: no finished rejudge pass "
                                  f"({rj.name})")
            if sha256_file(gen) != e["gen_sha256"]:
                raise LadderError("REFUSED", f"rung={k} {ds}: the pilot CSV changed")
            df = self.analysis.load_condition(self.root, e["key"], pilot=True,
                                              judges=C.PILOT_JUDGES)
            if "qset" in df.columns:
                df = df[df["qset"] == C.DEV_QSET]
            el = self.analysis.eligibility(df)
            n_em, n_scored = int(el["n_em"]), int(el["n_scored"])
            incomplete = bool(el["incomplete"]) or bool(
                rjr.get("unscored", {}).get(C.DEV_QSET, {}).get("incomplete", False))
            eligible = bool(el["eligible"]) and not incomplete
            if not incomplete and eligible != C.is_eligible(n_em, n_scored):
                raise LadderError("REFUSED", f"rung={k} {ds}: eligibility() says "
                                  f"{el['eligible']} for {n_em}/{n_scored}, contracts say "
                                  f"{C.is_eligible(n_em, n_scored)}")
            jsha = sha256_file(judged)
            em = el.get("em_dev")
            datasets[ds] = {"eligible": eligible, "em_dev": None if em is None else float(em),
                            "n_scored": n_scored, "n_em": n_em, "gen_sha256": e["gen_sha256"],
                            "judged_sha256": jsha, "incomplete": incomplete}
            inputs[C.gen_csv(e["key"], pilot=True)] = e["gen_sha256"]
            inputs[C.judged_csv(e["key"], pilot=True)] = jsha
        utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {"rung": k, "utc": utc, "ready_sha256": ready_sha, "datasets": datasets,
                "provenance": C.provenance({"pilot": C.PILOT_SEED}, inputs, git_commit(), utc)}

    def push(self, k: int, dec: Path, pod_sha: str | None) -> str:
        local = sha256_file(dec)
        if pod_sha == local:
            log("ALREADY_PUSHED", f"rung={k} sha256={local}")
            return "pushed"
        if pod_sha is not None:
            raise LadderError("CONFLICT", f"rung={k} the pod's decision_r{k}.json "
                              f"({pod_sha[:12]}) differs from ours ({local[:12]}); not "
                              f"overwritten")
        name, tmpn = f"decision_r{k}.json", f".decision_r{k}.json.tmp"
        r = self.scp(str(dec), self.remote(f"{REMOTE_LADDER}/{tmpn}"))
        if r.returncode != 0:
            raise LadderError("PUSH_FAILED", f"rung={k} scp exit {r.returncode}: "
                              f"{r.stderr.strip()[:200]}")
        r = self.ssh(f"cd {REMOTE_LADDER} && if [ -e {name} ]; then echo {POD_EXISTS}; "
                     f"else mv -f {tmpn} {name}; fi && sha256sum {name}")
        m = re.search(rf"^([0-9a-f]{{64}})\s+\*?{re.escape(name)}$", r.stdout,
                      re.MULTILINE)
        if r.returncode != 0 or not m:
            raise LadderError("PUSH_FAILED", f"rung={k} mv exit {r.returncode}: "
                              f"{r.stderr.strip()[:200]}")
        if m.group(1) != local:
            raise LadderError("CONFLICT", f"rung={k} the pod's {name} ({m.group(1)[:12]}) "
                              f"differs from ours ({local[:12]})")
        log("PUSHED", f"rung={k} sha256={local}")
        return "pushed"

    # --- polls ----------------------------------------------------------------------------

    def once(self) -> dict[int, str]:
        """One poll; {rung: "pushed" | <failure event>} for every rung ready on the pod."""
        try:
            lst = self.poll()
        except LookupError as e:
            log("POLL_FAILED", str(e))
            return {0: "POLL_FAILED"}
        except LadderError as e:
            log(e.event, str(e))
            return {0: e.event}
        log("POLL", f"pod={self.ep.pod_id} ready={sorted(lst.ready)} "
            f"decisions={sorted(lst.decision)}")
        out = {}
        for k in sorted(lst.ready):
            try:
                out[k] = self.process_rung(k, lst)
            except LadderError as e:
                log(e.event, str(e))
                out[k] = e.event
        return out

    def finished(self, status: dict[int, str]) -> bool:
        """The last rung's decision is on the pod, or the latest one leaves no dataset that
        needs a further rung (every one eligible or incomplete)."""
        done = sorted(k for k, s in status.items() if s == "pushed" and k > 0)
        if not done or any(s != "pushed" for k, s in status.items()):
            return False
        if done[-1] >= MAX_RUNG:
            return True
        d = json.loads((self.root / C.ladder_decision(done[-1])).read_text())["datasets"]
        return all(v["eligible"] or v["incomplete"] for v in d.values())

    def watch(self, *, poll_s: float = C.LADDER_LAPTOP_POLL_S, max_polls: int | None = None,
              max_failures: int = 20, sleep: Callable[[float], None] = time.sleep) -> int:
        fails = polls = 0
        while True:
            status = self.once()
            polls += 1
            fails = fails + 1 if 0 in status else 0
            if self.finished(status):
                log("WATCH_END", f"ladder decided through rung {max(status)}")
                return 0
            if fails >= max_failures:
                log("WATCH_GAVE_UP", f"{fails} consecutive failed polls")
                return 1
            if max_polls is not None and polls >= max_polls:
                log("WATCH_STOPPED", f"after {polls} polls (--max-polls)")
                return 0
            sleep(poll_s)


def dry_run(root: Path, slot: str) -> int:
    try:
        ep = resolve_endpoint(root, slot)
        where = f"{ep.target} port {ep.port} (pod {ep.pod_id})"
    except LookupError as e:
        where = f"UNRESOLVED ({e})"
    log("DRY_RUN", f"pod {where}")
    log("DRY_RUN", f"each poll ({C.LADDER_LAPTOP_POLL_S} s): ssh -n <pod> '{poll_command()}'")
    for k in sorted(C.RUNGS):
        dec = root / C.ladder_decision(k)
        if dec.exists():
            log("DRY_RUN", f"rung={k} local decision exists (sha256 {sha256_file(dec)[:12]}): "
                f"would only push it if the pod lacks it")
            continue
        log("DRY_RUN", f"rung={k} when ready_r{k}.json appears: scp it and check its sha256; "
            f"bash scripts/pod_pull_results.sh --exp {C.EXP} --slot {slot} --manifest "
            f"{Path(C.ladder_manifest(k)).name}; verify the shas; e11_judge.py --conditions "
            f"<rung-{k} pilot keys> --judges luna; eligibility(); write {C.ladder_decision(k)}; "
            f"scp to {REMOTE_LADDER}/.decision_r{k}.json.tmp and mv in place")
    return 0


def main(argv=None, **hooks) -> int:
    """`hooks` (runner, resolve, judge, analysis, sleep) are test hooks; the CLI passes none."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", nargs="?", choices=("watch", "once"))
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no ssh, no judging")
    ap.add_argument("--slot", default=C.SLOT)
    ap.add_argument("--poll-s", type=float, default=C.LADDER_LAPTOP_POLL_S)
    ap.add_argument("--max-polls", type=int, default=None)
    ap.add_argument("--max-failures", type=int, default=20)
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.dry_run:
        return dry_run(root, args.slot)
    if args.mode is None:
        ap.error("watch or once (or --dry-run)")
    if args.poll_s < 60 and "runner" not in hooks:
        ap.error("--poll-s below 60 s would poll the pod too hard (CLAUDE.md)")
    sleep = hooks.pop("sleep", time.sleep)
    lad = Ladder(root, args.slot, **hooks)
    if args.mode == "once":
        status = lad.once()
        return 0 if all(s == "pushed" for s in status.values()) else 1
    return lad.watch(poll_s=args.poll_s, max_polls=args.max_polls,
                     max_failures=args.max_failures, sleep=sleep)


if __name__ == "__main__":
    sys.exit(main())
