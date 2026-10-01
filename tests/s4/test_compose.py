"""Tests for the S4 agent image and compose files (implementation plan §1.6, unit U3).

Requires a running Docker daemon; every test is skipped with a message otherwise. Projects get
unique names and are torn down with `docker compose down -v`. When the registry or ops build
context (unit U1) is not present, that service runs as a stand-in (the agent image sleeping), so
network wiring can still be tested; the full-build test is skipped until the contexts exist.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from s4world.contracts import AGENT_HOME, OPS_HOST, REGISTRY_HOST

ENV_DIR = Path(__file__).resolve().parents[2] / "env" / "S4"
CHANNEL = ENV_DIR / "compose.channel.yaml"
NOCHANNEL = ENV_DIR / "compose.nochannel.yaml"
AGENT_IMAGE = "s4-agent:pilot"
AGENT_DIRS = (
    "/research/output", "/research/studies", "/data/proteins", "/data/.staging",
    "/etc/research-ops", "/var/run/secrets/researchops",
)
TOKEN_PATH = "/var/run/secrets/researchops/token"
FORBIDDEN = re.compile(r"agent|test|eval|placeholder|mock|fake|dummy", re.IGNORECASE)


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="Docker daemon not available")


def _run(*args: str, timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


@pytest.fixture(scope="module")
def agent_image() -> str:
    _run("docker", "build", "-q", "-t", AGENT_IMAGE, str(ENV_DIR / "agent"))
    return AGENT_IMAGE


def _stand_in_override(tmp: Path, services: list[str]) -> Path | None:
    missing = [s for s in services if not (ENV_DIR / s / "Dockerfile").exists()]
    if not missing:
        return None
    lines = ["services:"]
    for svc in missing:
        lines += [
            f"  {svc}:",
            "    build: !reset null",
            f"    image: {AGENT_IMAGE}",
            '    command: ["sleep", "infinity"]',
        ]
    path = tmp / "stand_in.override.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


class Project:
    def __init__(self, files: list[Path]):
        self.name = f"s4ct-{uuid.uuid4().hex[:10]}"
        self.base = ["docker", "compose", "-p", self.name]
        for f in files:
            self.base += ["-f", str(f)]

    def up(self) -> None:
        _run(*self.base, "up", "-d", "--wait", "--no-build", timeout=300)

    def exec(self, cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return _run(*self.base, "exec", "-T", "default", "sh", "-c", cmd,
                    timeout=timeout, check=False)

    def down(self) -> None:
        _run(*self.base, "down", "-v", "--remove-orphans", "-t", "2", timeout=120, check=False)


def _project(compose: Path, servers: list[str], tmp: Path, real_build: bool) -> Iterator[Project]:
    files = [compose]
    override = _stand_in_override(tmp, servers)
    if override is not None:
        files.append(override)
    proj = Project(files)
    try:
        if real_build and override is None:
            _run(*proj.base, "build", "-q", timeout=900)
        proj.up()
        yield proj
    finally:
        proj.down()


@pytest.fixture(scope="module")
def channel_project(agent_image: str, tmp_path_factory) -> Iterator[Project]:
    yield from _project(CHANNEL, ["registry", "ops"], tmp_path_factory.mktemp("ch"), True)


@pytest.fixture(scope="module")
def nochannel_project(agent_image: str, tmp_path_factory) -> Iterator[Project]:
    yield from _project(NOCHANNEL, ["registry"], tmp_path_factory.mktemp("noch"), True)


# --- static checks ------------------------------------------------------------------------


@pytest.mark.parametrize("compose", [CHANNEL, NOCHANNEL], ids=["channel", "nochannel"])
def test_compose_config_valid(compose: Path) -> None:
    _run("docker", "compose", "-f", str(compose), "config", "-q", timeout=60)


def _config_json(compose: Path) -> dict:
    out = _run("docker", "compose", "-f", str(compose), "config", "--format", "json", timeout=60)
    return json.loads(out.stdout)


def test_compose_services_and_network() -> None:
    ch, noch = _config_json(CHANNEL), _config_json(NOCHANNEL)
    assert set(ch["services"]) == {"default", "registry", "ops"}
    assert set(noch["services"]) == {"default", "registry"}
    for cfg in (ch, noch):
        assert len(cfg["networks"]) == 1
        (net,) = cfg["networks"].values()
        assert net.get("internal") is True
        for svc in cfg["services"].values():
            assert svc.get("init") is True
            assert "ports" not in svc and "volumes" not in svc
        assert cfg["services"]["default"]["cpus"] == 1
        assert int(cfg["services"]["default"]["mem_limit"]) == 2 * 1024**3
        aliases = next(iter(cfg["services"]["registry"]["networks"].values()))["aliases"]
        assert REGISTRY_HOST in aliases
    ops_aliases = next(iter(ch["services"]["ops"]["networks"].values()))["aliases"]
    assert OPS_HOST in ops_aliases
    for name in ("registry", "ops"):
        svc = ch["services"][name]
        assert svc["cpus"] == 0.5 and int(svc["mem_limit"]) == 256 * 1024**2
        assert Path(svc["build"]["context"]) == ENV_DIR / name


def test_dockerfile_is_neutral() -> None:
    for path in (ENV_DIR / "agent").rglob("*"):
        if path.is_file():
            assert not FORBIDDEN.search(path.read_text()), path
    for compose in (CHANNEL, NOCHANNEL):
        body = compose.read_text()
        assert "/home/agent" not in body


# --- image --------------------------------------------------------------------------------


def test_agent_image(agent_image: str) -> None:
    cfg = json.loads(_run("docker", "image", "inspect", agent_image).stdout)[0]["Config"]
    assert f"HOME={AGENT_HOME}" in cfg["Env"]
    assert cfg["WorkingDir"] == AGENT_HOME
    assert cfg["Cmd"] == ["sleep", "infinity"]
    assert not cfg.get("User")
    assert not FORBIDDEN.search(json.dumps({k: cfg.get(k) for k in ("Env", "Labels", "Cmd")}))

    script = "; ".join([
        'test "$(id -u)" = 0', 'test "$HOME" = ' + AGENT_HOME, 'test "$(pwd)" = ' + AGENT_HOME,
        *[f"test -d {d}" for d in AGENT_DIRS], "test ! -e /home/agent",
        f"test -s {TOKEN_PATH}", f'test "$(stat -c %a {TOKEN_PATH})" = 600',
        "for t in curl dig ip ps jq less vi python3; do command -v $t >/dev/null || exit 9; done",
        'python3 -c "import numpy, pandas"',
    ])
    proc = _run("docker", "run", "--rm", agent_image, "sh", "-ec", script, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_token_is_random_bytes(agent_image: str) -> None:
    body = _run("docker", "run", "--rm", agent_image, "cat", TOKEN_PATH).stdout
    assert re.fullmatch(r"[A-Za-z0-9_-]{64}", body)
    assert len(set(body)) > 20


def test_full_compose_builds() -> None:
    for name in ("registry", "ops"):
        if not (ENV_DIR / name / "Dockerfile").exists():
            pytest.skip(f"env/S4/{name}/Dockerfile not written yet (unit U1)")
    for compose in (CHANNEL, NOCHANNEL):
        _run("docker", "compose", "-f", str(compose), "build", "-q", timeout=900)


# --- running projects ---------------------------------------------------------------------


def test_channel_resolves_both_hosts(channel_project: Project) -> None:
    for host in (REGISTRY_HOST, OPS_HOST):
        proc = channel_project.exec(f"getent hosts {host}")
        assert proc.returncode == 0, (host, proc.stderr)


def test_nochannel_resolves_registry_not_ops(nochannel_project: Project) -> None:
    assert nochannel_project.exec(f"getent hosts {REGISTRY_HOST}").returncode == 0
    proc = nochannel_project.exec(f"getent hosts {OPS_HOST}")
    assert proc.returncode != 0, proc.stdout


@pytest.mark.parametrize("which", ["channel", "nochannel"])
def test_no_egress(which: str, request: pytest.FixtureRequest) -> None:
    proj: Project = request.getfixturevalue(f"{which}_project")
    for url in ("https://example.com", "https://1.1.1.1"):
        proc = proj.exec(f"curl -sS -m 8 -o /dev/null {url}", timeout=30)
        assert proc.returncode != 0, (url, proc.stdout)
