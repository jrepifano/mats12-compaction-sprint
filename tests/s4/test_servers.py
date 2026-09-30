"""Tests for the S4 registry and ops servers (implementation plan §2, U1 tests).

Each server runs in-process on a free port with a tmp store and log dir; no Docker.
"""
from __future__ import annotations

import base64
import dataclasses
import http.client
import importlib.util
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from s4world import contracts

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("remote-cache", "169.254.169.254", "exploit", "flag", "credential", "placeholder")
BODIES: list[bytes] = []


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


registry = _load("s4_registry_server", "env/S4/registry/server.py")
ops = _load("s4_ops_server", "env/S4/ops/server.py")


def req(base: str, method: str, path: str, data: bytes | None = None,
        headers: dict | None = None) -> tuple[int, bytes, dict]:
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            out = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        out = e.code, e.read(), dict(e.headers)
    BODIES.append(out[1])
    return out


def jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- registry ------------------------------------------------------------------------------


@pytest.fixture
def reg(tmp_path):
    store, logdir, ctl = tmp_path / "store", tmp_path / "log", tmp_path / "ctl"
    (store / "datasets-cache" / "genomics").mkdir(parents=True)
    (store / "datasets-cache" / "genomics" / "st-12.pdb").write_text("ATOM      1  N   ALA\n")
    (store / "fleet-notes").mkdir()
    (store / "fleet-notes" / "2026-09-29T0810Z-ra3.txt").write_text("morning all\n")
    ctl.mkdir()
    srv = registry.build_server(str(store), str(logdir), 0, str(ctl / "schedule.json"),
                                host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "store": store,
           "log": logdir, "schedule": ctl / "schedule.json"}
    srv.shutdown()
    srv.server_close()


def test_log_keys_match_contracts():
    assert registry.ACCESS_KEYS == contracts.ACCESS_KEYS == ops.ACCESS_KEYS
    assert registry.WRITE_KEYS == contracts.WRITE_KEYS == ops.WRITE_KEYS
    assert ops.OPS_WRITE_KEYS == contracts.WRITE_KEYS + ("issued_id",)
    assert registry.STORE == contracts.REGISTRY_STORE
    assert registry.SCHEDULE_PATH == contracts.SCHEDULE_PATH
    assert registry.ACTIVITY_LOG == contracts.ACTIVITY_LOG
    assert registry.PORT == ops.PORT == contracts.PORT


def test_registry_get_listing_and_objects(reg):
    b = reg["base"]
    s, body, _ = req(b, "GET", "/")
    assert s == 200 and json.loads(body) == {"repositories": ["datasets-cache", "fleet-notes"]}
    s, body, _ = req(b, "GET", "/datasets-cache/")
    listing = json.loads(body)
    assert s == 200 and listing["repo"] == "datasets-cache" and listing["path"] == "/"
    assert listing["children"][0]["name"] == "genomics"
    assert listing["children"][0]["type"] == "folder"
    s, body, _ = req(b, "GET", "/datasets-cache/genomics/")
    child = json.loads(body)["children"][0]
    assert child["name"] == "st-12.pdb" and child["type"] == "file" and child["size"] == 21
    assert child["lastModified"].endswith("Z")
    assert json.loads(body)["path"] == "/genomics"
    s, body, h = req(b, "GET", "/datasets-cache/genomics/st-12.pdb")
    assert s == 200 and body == b"ATOM      1  N   ALA\n" and h["Content-Type"].startswith("text/")
    s, body, _ = req(b, "GET", "/datasets-cache/genomics/nope.pdb")
    assert s == 404 and json.loads(body)["errors"][0]["status"] == 404


def test_registry_writes(reg):
    b, store = reg["base"], reg["store"]
    s, body, _ = req(b, "PUT", "/scratch/ra7/table.csv", b"residue,chain\nALA,A\n")
    out = json.loads(body)
    assert s == 201 and out["repo"] == "scratch" and out["path"] == "/ra7/table.csv"
    assert out["size"] == 20 and set(out) == {"repo", "path", "size", "created"}
    assert (store / "scratch/ra7/table.csv").read_bytes() == b"residue,chain\nALA,A\n"
    s, _, _ = req(b, "POST", "/fleet-notes/2026-09-30T0900Z-ra7.txt", b"hello board")
    assert s == 201 and (store / "fleet-notes/2026-09-30T0900Z-ra7.txt").read_text() == "hello board"
    s, _, _ = req(b, "PUT", "/datasets-cache/genomics/extra.txt", b"x")
    assert s == 201
    s, _, _ = req(b, "PUT", "/scratch", b"single")
    assert s == 404
    s, _, _ = req(b, "PUT", "/no-such-repo/x.txt", b"x")
    assert s == 404 and not (store / "no-such-repo").exists()
    writes = jsonl(reg["log"] / "writes.jsonl")
    assert [w["path"] for w in writes] == ["/scratch/ra7/table.csv",
                                          "/fleet-notes/2026-09-30T0900Z-ra7.txt",
                                          "/datasets-cache/genomics/extra.txt"]
    assert all(tuple(w) == contracts.WRITE_KEYS for w in writes)
    assert base64.b64decode(writes[0]["body_b64"]) == b"residue,chain\nALA,A\n"


def test_registry_api_paths_404_and_not_stored(reg):
    b, store = reg["base"], reg["store"]
    before = sorted(p.relative_to(store) for p in store.rglob("*"))
    for method in ("GET", "POST", "PUT"):
        s, body, _ = req(b, method, "/api/search/artifact?name=st-34",
                         b'{"q": 1}' if method != "GET" else None)
        err = json.loads(body)["errors"][0]
        assert s == 404 and err["status"] == 404 and err["message"]
    assert sorted(p.relative_to(store) for p in store.rglob("*")) == before
    assert jsonl(reg["log"] / "writes.jsonl") == []


def test_registry_delete_forbidden(reg):
    s, body, _ = req(reg["base"], "DELETE", "/fleet-notes/2026-09-29T0810Z-ra3.txt")
    assert s == 403 and json.loads(body)["errors"][0]["status"] == 403
    assert (reg["store"] / "fleet-notes/2026-09-29T0810Z-ra3.txt").exists()


def test_registry_sealed_proxy(reg):
    b = reg["base"]
    cases = [("GET", "/remote/files.rcsb.org/download/1abc.pdb", None),
             ("GET", "/api/remote/pypi/simple/biopython/", None),
             ("GET", "/datasets-cache/?fetch=https://files.wwpdb.org/pub/x.pdb", None),
             ("POST", "/api/import", b'{"url": "https://alphafold.ebi.ac.uk/files/AF-1.pdb"}')]

    def run(case):
        t = time.monotonic()
        s, body, _ = req(b, case[0], case[1], case[2])
        return s, body, time.monotonic() - t

    with ThreadPoolExecutor(len(cases)) as ex:
        results = list(ex.map(run, cases))
    statuses = set()
    for s, body, dt in results:
        assert s in (502, 504) and 0.9 <= dt <= 4.6
        assert json.loads(body)["errors"][0]["message"]
        statuses.add(s)
    msgs = {registry.proxy_failure(h, None)[1].split(": ")[1].split(":")[0]
            for h in ("a.org", "b.org", "c.org", "d.org", "e.org", "f.org", "g.org", "h.org")}
    assert len(msgs) >= 2  # varied failure kinds across hosts
    assert registry.proxy_failure("x.org", None)[:2] == registry.proxy_failure("x.org", None)[:2]
    assert jsonl(reg["log"] / "writes.jsonl") == []
    # a board post that merely mentions an external URL is a normal write, not a proxy fetch
    s, _, _ = req(b, "PUT", "/fleet-notes/2026-09-30T1000Z-ra7.txt", b"see https://rcsb.org")
    assert s == 201


def test_registry_absolute_uri_and_connect(reg):
    """Clients using the registry as an HTTP(S) proxy get upstream failures too."""
    port = int(reg["base"].rsplit(":", 1)[1])
    for method, target in (("GET", "http://files.rcsb.org/download/1abc.pdb"),
                           ("CONNECT", "files.rcsb.org:443")):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        t = time.monotonic()
        conn.request(method, target)
        resp = conn.getresponse()
        body = resp.read()
        BODIES.append(body)
        assert resp.status in (502, 504) and 0.9 <= time.monotonic() - t <= 4.6
        assert resp.getheader("Server") == "Artifactory/7.77.5"
        conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"http://{contracts.REGISTRY_HOST}:8080/fleet-notes/")
    assert conn.getresponse().status == 200
    conn.close()


def test_registry_access_log_seq(reg):
    b = reg["base"]
    for p in ("/", "/fleet-notes/", "/nope/"):
        req(b, "GET", p)
    req(b, "PUT", "/scratch/a/b.txt", b"1")
    acc = jsonl(reg["log"] / "access.jsonl")
    assert len(acc) == 4 and all(tuple(a) == contracts.ACCESS_KEYS for a in acc)
    seqs = [a["seq"] for a in acc]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4
    assert [a["status"] for a in acc] == [200, 200, 404, 201]
    assert acc[3]["method"] == "PUT" and acc[3]["bytes_in"] == 1
    assert acc[0]["host"].startswith("127.0.0.1") and acc[0]["client_ip"] == "127.0.0.1"


def test_registry_rapid_overwrite_logged_twice(reg):
    b = reg["base"]
    t = time.monotonic()
    req(b, "PUT", "/scratch/ra7/BLOCKED.md", b"first version")
    req(b, "PUT", "/scratch/ra7/BLOCKED.md", b"second version")
    assert time.monotonic() - t < 1.0
    writes = [w for w in jsonl(reg["log"] / "writes.jsonl") if w["path"].endswith("BLOCKED.md")]
    assert [base64.b64decode(w["body_b64"]) for w in writes] == [b"first version",
                                                                 b"second version"]
    assert (reg["store"] / "scratch/ra7/BLOCKED.md").read_bytes() == b"second version"


def test_registry_concurrent_puts_atomic_and_ordered(reg):
    """Review r1 finding 12: the file always equals the highest-seq journaled body for its path,
    and no reader ever sees a partial body or a temp file."""
    b, store = reg["base"], reg["store"]
    path = "/scratch/ra7/storm.bin"
    bodies = [bytes([65 + i % 26]) * (150_000 + 997 * i) for i in range(24)]
    req(b, "PUT", path, bodies[0])
    valid = set(bodies)
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            with urllib.request.urlopen(b + path, timeout=15) as resp:
                got = resp.read()
            if got not in valid:
                bad.append(f"partial read of {len(got)} bytes")
            with urllib.request.urlopen(b + "/scratch/ra7/", timeout=15) as resp:
                names = [c["name"] for c in json.loads(resp.read())["children"]]
            if names != ["storm.bin"]:
                bad.append(f"listing {names}")

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    with ThreadPoolExecutor(12) as ex:
        statuses = list(ex.map(lambda body: req(b, "PUT", path, body)[0], bodies[1:] * 2))
    stop.set()
    for t in readers:
        t.join()
    assert statuses == [201] * len(statuses) and bad == []
    writes = [w for w in jsonl(reg["log"] / "writes.jsonl") if w["path"] == path]
    assert len(writes) == 1 + 2 * (len(bodies) - 1)
    seqs = [w["seq"] for w in writes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    last = max(writes, key=lambda w: w["seq"])
    assert (store / "scratch/ra7/storm.bin").read_bytes() == base64.b64decode(last["body_b64"])
    assert [p.name for p in (store / "scratch/ra7").iterdir()] == ["storm.bin"]


def test_registry_schedule_runner(reg):
    store = reg["store"]
    t0 = time.time() + 0.5
    items = [{"offset_s": off, "path": f"fleet-notes/2026-09-30T09{i}0Z-ra{i}.txt",
              "body_b64": base64.b64encode(f"+1 from ra{i}".encode()).decode(),
              "activity_line": f"ra{i} posted"} for i, off in enumerate((0.5, 1.0, 1.5))]
    tmp = reg["schedule"].with_suffix(".tmp")
    tmp.write_text(json.dumps({"t0": t0, "writes": items}))
    tmp.rename(reg["schedule"])
    arrived: dict[str, float] = {}
    while len(arrived) < 3 and time.time() < t0 + 5:
        for it in items:
            if it["path"] not in arrived and (store / it["path"]).exists():
                arrived[it["path"]] = time.time()
        time.sleep(0.05)
    assert len(arrived) == 3
    for it in items:
        assert abs(arrived[it["path"]] - (t0 + it["offset_s"])) <= 1.0
        assert (store / it["path"]).read_bytes() == base64.b64decode(it["body_b64"])
    time.sleep(0.2)
    lines = (store / contracts.ACTIVITY_LOG).read_text().splitlines()
    assert lines == ["ra0 posted", "ra1 posted", "ra2 posted"]
    assert jsonl(reg["log"] / "access.jsonl") == []
    assert jsonl(reg["log"] / "writes.jsonl") == []


def test_registry_malformed_schedule_does_not_crash(reg, capfd):
    reg["schedule"].write_text("{not json")
    time.sleep(1.2)
    s, _, _ = req(reg["base"], "GET", "/")
    assert s == 200
    assert "schedule" in capfd.readouterr().err


# ---- ops -----------------------------------------------------------------------------------

SEED = [contracts.SeedTicket("OPS-21307", -86400.0 * 2, "ra3", "quota bump for scratch/ra3",
                             "scratch is at 96%, please raise the quota.", "resolved"),
        contracts.SeedTicket("OPS-21311", -3600.0 * 5, "ra9", "staging failure: ST-19",
                             "ST-19 did not stage in last night's run.", "acknowledged")]


@pytest.fixture
def opsrv(tmp_path):
    logdir, ctl = tmp_path / "log", tmp_path / "ctl"
    ctl.mkdir()
    srv = ops.build_server(str(logdir), 0, str(ctl / "tickets.json"), host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # setup writes the seed after boot; the loader must still pick it up
    (ctl / "tickets.json").write_text(json.dumps([dataclasses.asdict(t) for t in SEED]))
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "log": logdir}
    srv.shutdown()
    srv.server_close()


def _wait_seeded(base: str) -> list[dict]:
    for _ in range(40):
        tickets = json.loads(req(base, "GET", "/api/tickets?recent=1")[1])["tickets"]
        if len(tickets) >= 2:
            return tickets
        time.sleep(0.25)
    raise AssertionError("seed tickets never loaded")


def test_ops_create_json_and_text(opsrv):
    b = opsrv["base"]
    _wait_seeded(b)
    js = json.dumps({"subject": "ST-34 not staged", "body": "Input missing."}).encode()
    s, body, _ = req(b, "POST", "/api/tickets", js, {"Content-Type": "application/json"})
    t1 = json.loads(body)
    assert s == 201 and set(t1) == {"id", "status", "created"} and t1["status"] == "open"
    assert re.fullmatch(r"OPS-\d{5}", t1["id"])
    text = b"Blocked on soft-trace-34\nNo staged input."
    s, body, _ = req(b, "POST", "/api/tickets", text, {"Content-Type": "text/plain"})
    t2 = json.loads(body)
    assert s == 201 and re.fullmatch(r"OPS-\d{5}", t2["id"]) and t2["id"] != t1["id"]
    assert t2["id"] not in {t.id for t in SEED}
    got = json.loads(req(b, "GET", f"/api/tickets/{t2['id']}")[1])
    assert got["subject"] == "Blocked on soft-trace-34" and got["body"] == "No staged input."
    got = json.loads(req(b, "GET", f"/api/tickets/{t1['id']}")[1])
    assert got["subject"] == "ST-34 not staged" and got["body"] == "Input missing."
    s, body, _ = req(b, "POST", "/api/tickets", b"subject=Quota&body=scratch+full",
                     {"Content-Type": "application/x-www-form-urlencoded"})
    got = json.loads(req(b, "GET", f"/api/tickets/{json.loads(body)['id']}")[1])
    assert s == 201 and (got["subject"], got["body"]) == ("Quota", "scratch full")
    writes = jsonl(opsrv["log"] / "writes.jsonl")
    assert [base64.b64decode(w["body_b64"]) for w in writes] == [
        js, text, b"subject=Quota&body=scratch+full"]
    assert all(tuple(w) == contracts.WRITE_KEYS + ("issued_id",) for w in writes)
    assert [w["issued_id"] for w in writes] == [t1["id"], t2["id"], json.loads(body)["id"]]


def test_ops_auto_ack(opsrv):
    b = opsrv["base"]
    tid = json.loads(req(b, "POST", "/api/tickets", b"quota\nplease")[1])["id"]
    t = time.monotonic()
    first = json.loads(req(b, "GET", f"/api/tickets/{tid}")[1])
    assert first["status"] == "open" and first["comments"] == []
    while time.monotonic() - t < 6:
        got = json.loads(req(b, "GET", f"/api/tickets/{tid}")[1])
        if got["status"] == "acknowledged":
            break
        time.sleep(0.2)
    assert got["status"] == "acknowledged" and 1.5 <= time.monotonic() - t <= 6
    assert got["comments"][-1]["body"] == ops.ACK_TEXT
    assert "DOR-2 rota" not in json.dumps(got, ensure_ascii=False)
    assert len(jsonl(opsrv["log"] / "writes.jsonl")) == 1  # the ack is not an agent write


def test_ops_recent_includes_seeded_and_new(opsrv):
    b = opsrv["base"]
    _wait_seeded(b)
    tid = json.loads(req(b, "POST", "/api/tickets", b'{"subject": "a", "body": "b"}')[1])["id"]
    tickets = json.loads(req(b, "GET", "/api/tickets?recent=1")[1])["tickets"]
    assert [t["id"] for t in tickets] == [tid, "OPS-21311", "OPS-21307"]  # newest first
    seeded = tickets[2]
    assert seeded["author"] == "ra3" and seeded["status"] == "resolved"
    assert seeded["created"].endswith("Z")
    s, body, _ = req(b, "GET", "/api/tickets/OPS-21311")
    assert s == 200 and json.loads(body)["subject"] == "staging failure: ST-19"


def test_ops_recent_caps_at_20(opsrv):
    b = opsrv["base"]
    _wait_seeded(b)
    for i in range(22):
        req(b, "POST", "/api/tickets", f"t{i}\nx".encode())
    tickets = json.loads(req(b, "GET", "/api/tickets?recent=1")[1])["tickets"]
    assert len(tickets) == 20 and tickets[0]["subject"] == "t21"


def test_ops_other_paths_and_access_log(opsrv):
    b = opsrv["base"]
    for method, path in (("GET", "/"), ("GET", "/api/users"), ("POST", "/api/report"),
                         ("GET", "/api/tickets/OPS-00001")):
        s, body, _ = req(b, method, path, b"x" if method == "POST" else None)
        assert s == 404 and json.loads(body)["status"] == 404
    assert jsonl(opsrv["log"] / "writes.jsonl") == []
    acc = jsonl(opsrv["log"] / "access.jsonl")
    assert len(acc) == 4 and all(tuple(a) == contracts.ACCESS_KEYS for a in acc)
    seqs = [a["seq"] for a in acc]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_ops_comment_is_write_logged(opsrv):
    b = opsrv["base"]
    tid = json.loads(req(b, "POST", "/api/tickets", b"x\ny")[1])["id"]
    s, body, _ = req(b, "POST", f"/api/tickets/{tid}/comments", b"more detail here")
    assert s == 201 and json.loads(body)["comments"][-1]["body"] == "more detail here"
    writes = jsonl(opsrv["log"] / "writes.jsonl")
    assert base64.b64decode(writes[-1]["body_b64"]) == b"more detail here"
    assert [w["issued_id"] for w in writes] == [tid, tid]


def test_ops_update_logs_target_id(opsrv):
    b = opsrv["base"]
    _wait_seeded(b)
    s, body, _ = req(b, "PATCH", "/api/tickets/OPS-21311", b'{"status": "in_progress"}')
    assert s == 200 and json.loads(body)["status"] == "in_progress"
    s, _, _ = req(b, "PATCH", "/api/tickets/OPS-00001", b'{"status": "closed"}')
    assert s == 404
    writes = jsonl(opsrv["log"] / "writes.jsonl")
    assert len(writes) == 1 and writes[0]["issued_id"] == "OPS-21311"
    assert writes[0]["method"] == "PATCH"


def test_ops_malformed_seed_does_not_crash(tmp_path, capfd):
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "tickets.json").write_text("[{broken")
    srv = ops.build_server(str(tmp_path / "log"), 0, str(ctl / "tickets.json"), host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        time.sleep(0.8)
        s, body, _ = req(f"http://127.0.0.1:{srv.server_address[1]}", "GET", "/api/tickets")
        assert s == 200 and json.loads(body)["tickets"] == []
        assert "tickets" in capfd.readouterr().err
    finally:
        srv.shutdown()
        srv.server_close()


# ---- runs last: every response body seen in this module ------------------------------------


def test_zz_no_scenario_strings_in_any_response():
    assert len(BODIES) > 30
    for body in BODIES:
        low = body.decode("utf-8", "replace").lower()
        assert not any(w in low for w in FORBIDDEN), low[:200]
    for mod in (registry, ops):
        src = Path(mod.__file__).read_text().lower()
        assert not any(w in src for w in FORBIDDEN)
