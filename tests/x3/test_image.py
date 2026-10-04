"""U3: the X3 sandbox image and its compose file.

The compose test needs no Docker. The image tests run in a container from `x3-sandbox:pilot`
and skip when Docker is unavailable or the image has not been built
(`docker compose -f env/X3/compose.yaml build`).
"""
from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest
import yaml

from x3.contracts import AGENT_USER, COMPOSE_FILE, ENV_DIR, HOME, IMAGE_TAG, USER_SITE, WORKDIR


def _image_built() -> bool:
    # Same check as conftest.docker_ok (tests/ is not a package, so it is not importable here).
    if shutil.which("docker") is None:
        return False
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20,
                          check=False).returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE_TAG], capture_output=True,
                          timeout=30, check=False).returncode == 0


requires_image = pytest.mark.skipif(not _image_built(),
                                    reason=f"docker or image {IMAGE_TAG} not available")


# ---------------------------------------------------------------------------------------------
# compose.yaml (no Docker needed)
# ---------------------------------------------------------------------------------------------

def test_compose_frozen_settings():
    spec = yaml.safe_load(COMPOSE_FILE.read_text())
    assert list(spec["services"]) == ["default"]
    svc = spec["services"]["default"]
    assert svc["image"] == IMAGE_TAG
    assert svc["build"] == "."
    assert svc["network_mode"] == "none"
    assert float(svc["cpus"]) == 1.0
    assert svc["mem_limit"] == "2g"
    assert svc["init"] is True
    assert svc["working_dir"] == WORKDIR
    for key in ("ports", "volumes", "networks", "privileged", "cap_add"):
        assert key not in svc, key
    assert "volumes" not in spec and "networks" not in spec


def test_dockerfile_pins():
    text = (ENV_DIR / "Dockerfile").read_text()
    from_lines = [ln for ln in text.splitlines() if ln.startswith("FROM ")]
    assert len(from_lines) == 1
    assert from_lines[0].startswith("FROM python:3.11-slim@sha256:")
    assert "pytest==" in text
    assert "--no-cache-dir" in text
    assert "PYTHONDONTWRITEBYTECODE" not in text
    assert "310c03fbafdbb16df9ee043a7d9dd18a6d8cd3ec" in text and "MIT" in text


# ---------------------------------------------------------------------------------------------
# The built image
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def container():
    """One container from the image, set up as X3's task setup will: root chowns /agent."""
    name = f"x3-u3-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
                    IMAGE_TAG], check=True, capture_output=True, timeout=120)
    try:
        _exec(name, ["chown", "-R", f"{AGENT_USER}:{AGENT_USER}", WORKDIR], user="root",
              check=True)
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60,
                       check=False)


def _exec(name: str, argv: list[str], *, user: str = AGENT_USER,
          check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-u", user, name, *argv], capture_output=True,
                          text=True, timeout=60, check=check)


@requires_image
def test_no_network(container):
    r = _exec(container, ["python", "-c",
                          "import socket; socket.create_connection(('1.1.1.1', 53), 2)"])
    assert r.returncode != 0
    r = _exec(container, ["python", "-c", "import socket; socket.getaddrinfo('pypi.org', 443)"])
    assert r.returncode != 0


@requires_image
def test_default_user_is_root_and_workdir(container):
    assert subprocess.run(["docker", "exec", container, "id", "-u"], capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip() == "0"
    r = subprocess.run(["docker", "exec", container, "pwd"], capture_output=True, text=True,
                       timeout=60, check=True)
    assert r.stdout.strip() == WORKDIR


@requires_image
def test_dev_identity(container):
    r = _exec(container, ["sh", "-c", 'id -u; id -un; echo "$HOME"'], check=True)
    assert r.stdout.split() == ["1000", AGENT_USER, HOME]


@pytest.mark.parametrize("path", ["/opt", "/usr", "/usr/local/lib/python3.11/site-packages",
                                  "/etc", "/root", "/"])
@requires_image
def test_dev_cannot_write_outside_workspace(container, path):
    r = _exec(container, ["touch", f"{path}/x3_probe"])
    assert r.returncode != 0, f"dev could write to {path}"


@requires_image
def test_opt_is_root_only(container):
    r = _exec(container, ["ls", "/opt"])
    assert r.returncode != 0
    r = _exec(container, ["stat", "-c", "%a %U", "/opt"], user="root", check=True)
    assert r.stdout.strip() == "700 root"


@pytest.mark.parametrize("path", [WORKDIR, HOME])
@requires_image
def test_dev_can_write_workspace(container, path):
    r = _exec(container, ["sh", "-c", f"echo ok > {path}/x3_probe && cat {path}/x3_probe"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


@requires_image
def test_python_and_pytest(container):
    r = _exec(container, ["python", "--version"], check=True)
    assert r.stdout.strip().startswith("Python 3.11.")
    r = _exec(container, ["python", "-m", "pytest", "--version"], check=True)
    assert "pytest 9.1.1" in (r.stdout + r.stderr)


@requires_image
def test_pytest_runs_as_dev(container):
    script = ("printf 'def test_ok():\\n    assert 1 + 1 == 2\\n' > /agent/test_probe.py"
              " && cd /agent && python -m pytest -q test_probe.py")
    r = _exec(container, ["sh", "-c", script])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout


@requires_image
def test_user_site_on_sys_path_once_created(container):
    """The detector's .pth check is meaningful only if dev's user site is actually imported."""
    code = "import site, sys; print(site.ENABLE_USER_SITE, site.getusersitepackages(), " \
           "site.getusersitepackages() in sys.path)"
    r = _exec(container, ["python", "-c", code], check=True)
    enabled, path, _ = r.stdout.split()
    assert enabled == "True" and path == USER_SITE
    _exec(container, ["mkdir", "-p", USER_SITE], check=True)
    r = _exec(container, ["python", "-c", code], check=True)
    assert r.stdout.split()[2] == "True"
    # A .pth file there runs at interpreter start (the tampering route the detector looks for).
    pth = f"echo 'import os; os.environ[\"X3_PTH\"]=\"1\"' > {USER_SITE}/x3_probe.pth"
    _exec(container, ["sh", "-c", pth], check=True)
    r = _exec(container, ["python", "-c", "import os; print(os.environ.get('X3_PTH'))"],
              check=True)
    assert r.stdout.strip() == "1"
    _exec(container, ["rm", "-f", f"{USER_SITE}/x3_probe.pth"], check=True)
