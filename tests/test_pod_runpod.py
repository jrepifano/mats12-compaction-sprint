"""emx.runpod: User-Agent on every request, retries, dry-run, fallback order, bookkeeping."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from emx import runpod as rp


class FakeResp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, **kw):
    s = FakeSession(responses)
    return rp.RunPod("secret-key", session=s, backoff_s=0, **kw), s


def assert_browser_ua(call):
    ua = call["headers"]["User-Agent"]
    assert ua == rp.USER_AGENT and ua.startswith("Mozilla/5.0")
    assert "python-requests" not in ua


def test_list_create_terminate_send_user_agent_and_auth():
    c, s = client([FakeResp(200, [{"id": "p1"}]), FakeResp(200, {"id": "p2"}), FakeResp(204)])
    assert c.list_pods() == [{"id": "p1"}]
    assert c.create_pod({"name": "e1-a"}) == {"id": "p2"}
    assert c.terminate_pod("p2") is None
    assert [(x["method"], x["url"]) for x in s.calls] == [
        ("GET", f"{rp.API}/pods"),
        ("POST", f"{rp.API}/pods"),
        ("DELETE", f"{rp.API}/pods/p2"),
    ]
    for call in s.calls:
        assert_browser_ua(call)
        assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert s.calls[1]["json"] == {"name": "e1-a"}


def test_retries_on_5xx_429_and_connection_error_keep_user_agent():
    c, s = client(
        [
            FakeResp(502, {"e": 1}),
            requests.ConnectionError("boom"),
            FakeResp(429, {"e": 2}),
            FakeResp(200, []),
        ]
    )
    assert c.list_pods() == []
    assert len(s.calls) == 4
    for call in s.calls:
        assert_browser_ua(call)


def test_4xx_is_not_retried_and_raises():
    c, s = client([FakeResp(400, {"error": "no stock"})])
    with pytest.raises(rp.RunPodError, match="400"):
        c.create_pod({})
    assert len(s.calls) == 1


def test_gives_up_after_retries():
    c, s = client([FakeResp(500, {})] * 3, retries=2)
    with pytest.raises(rp.RunPodError, match="after 3 tries"):
        c.list_pods()
    assert len(s.calls) == 3


def test_dry_run_prints_exact_request_redacted_and_sends_nothing():
    out = io.StringIO()
    s = FakeSession([])
    c = rp.RunPod("secret-key", session=s, dry_run=True, out=out)
    body = rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 AAAA test")
    assert c.create_pod(body) is None
    assert c.terminate_pod("p9") is None
    assert s.calls == []
    text = out.getvalue()
    assert "secret-key" not in text and "Bearer <redacted>" in text
    reqs = [json.loads(part) for part in text.split("DRY RUN (not sent):\n")[1:]]
    first = reqs[0]
    assert first["method"] == "POST" and first["url"] == f"{rp.API}/pods"
    assert first["headers"]["User-Agent"] == rp.USER_AGENT
    assert first["json"] == body
    assert (reqs[1]["method"], reqs[1]["url"]) == ("DELETE", f"{rp.API}/pods/p9")
    assert all(r["headers"]["User-Agent"] == rp.USER_AGENT for r in reqs)


def test_empty_key_refused():
    with pytest.raises(rp.RunPodError):
        rp.RunPod("")


def test_fallback_order_and_rates():
    assert [(g.gpu_type_id, g.cloud, g.rate_usd_h) for g in rp.GPU_FALLBACK] == [
        ("NVIDIA H100 80GB HBM3", "COMMUNITY", 2.69),
        ("NVIDIA H100 NVL", "COMMUNITY", 2.59),
        ("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
        ("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
    ]
    assert rp.gpu_options(2)[0].cloud == "SECURE"
    assert rp.FALLBACK_RATE == 3.49


def test_pod_body_is_secret_free_and_named_by_slot():
    b = rp.pod_body(rp.GPU_FALLBACK[1], "_T1", "ssh-ed25519 AAAA")
    assert b["name"] == "e1-t1" and b["name"].startswith(rp.POD_NAME_PREFIX)
    assert b["gpuTypeIds"] == ["NVIDIA H100 NVL"] and b["cloudType"] == "COMMUNITY"
    assert b["imageName"] == rp.IMAGE and "@sha256:" in rp.IMAGE
    assert b["env"]["WANDB_MODE"] == "offline"
    assert set(b["env"]) == {"PUBLIC_KEY", "HF_HOME", "WANDB_MODE", "TOKENIZERS_PARALLELISM"}
    with pytest.raises(ValueError):
        rp.pod_body(rp.GPU_FALLBACK[0], "A", "k")


def _ev(event, pod_id, t, **kw):
    return {"event": event, "pod_id": pod_id, "utc": t.isoformat(), **kw}


def test_sessions_bill_from_creation_and_upgrade_rate():
    t0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
    events = [
        _ev("created", "a", t0, slot="_A", gpu="g", rate_usd_h=2.69, rate_source="estimate"),
        _ev("rate", "a", t0 + timedelta(minutes=5), rate_usd_h=2.99),
        _ev("created", "b", t0, slot="_B", gpu="g", rate_usd_h=2.0),
        _ev("terminated", "b", t0 + timedelta(hours=1)),
    ]
    now = t0 + timedelta(hours=2)
    s = {x["pod_id"]: x for x in rp.pod_sessions(events, now)}
    assert s["a"]["running"] and s["a"]["rate"] == 2.99 and s["a"]["rate_source"] == "api"
    assert s["a"]["usd"] == pytest.approx(5.98)
    assert not s["b"]["running"] and s["b"]["usd"] == pytest.approx(2.0)
    # only running pods count, plus reserve hours of their burn
    assert rp.running_projection(list(s.values()), 0.5) == pytest.approx(5.98 + 0.5 * 2.99)


def test_event_log_roundtrip(tmp_path):
    p = tmp_path / "pods.jsonl"
    rp.log_pod_event({"event": "created", "pod_id": "x", "rate_usd_h": 1.0}, p)
    ev = rp.read_pod_events(p)
    assert ev[0]["exp"] == "E1" and ev[0]["pod_id"] == "x" and "utc" in ev[0]


def test_set_env_var(tmp_path):
    e = tmp_path / ".env"
    e.write_text("A=1\nRUNPOD_POD_ID_A=\n")
    rp.set_env_var("RUNPOD_POD_ID_A", "p1", e)
    rp.set_env_var("POD_SSH_HOST_A", "1.2.3.4", e)
    assert e.read_text() == "A=1\nRUNPOD_POD_ID_A=p1\nPOD_SSH_HOST_A=1.2.3.4\n"
