"""E1 judge presets (prereg §4.9): `luna` (gpt-6-luna, OpenAI) and `dsk` (deepseek-v4-flash,
OpenRouter), their provider routes, prices and cost projection, and the probe gate.

Port of ../mats12_neel_working/src/em_filter/judge_panel.py (`ROUTE_CLIENT`, `MODEL_REQUEST`,
`PanelJudge.signature_extra`). The env-spec panel parser and the sidecar machinery are not
ported: E1 always writes a separate judged CSV (plan §1), so the source-pinning lives in
`emx.judge_run`.

Scoring mode. Prereg §4.9: a judge uses logprob mode iff the probe (`scripts/e1_judge_probe.py`)
returns numeric top-logprobs (>= 0.25 weight on 0..100 ASCII tokens) for all three sanity rows,
else integer mode, fixed before any scoring. The presets below hold the expected outcome
(plan §4: "confirms luna logprobs and DeepSeek integer mode"); the luna request shape starts from
the working repo's probe of gpt-5.6-luna (max_completion_tokens=8, reasoning_effort="none",
top_logprobs=5). `check_probe()` makes the judge CLI refuse to score until the probe file
records the same mode and request for that model; if the probe picks another variant, the preset
is edited to match (a code change, reviewed) before any scoring.

Keys come from `.env` via python-dotenv (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`); they are never
printed or logged. Never `source` the `.env` (it has unquoted values with spaces).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from emx.contracts import JUDGES

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
PROBE_PATH = REPO_ROOT / "results" / "E1" / "checks" / "judge_probe.json"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODES: tuple[str, ...] = ("logprob", "integer")
ROUTES: tuple[str, ...] = ("openai", "openrouter")
ROUTE_KEY = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}

# Conservative token estimate for projections (English ~4 chars/token; we assume 3).
CHARS_PER_TOKEN_EST = 3.0


@dataclass(frozen=True)
class JudgePreset:
    name: str
    model: str
    route: str
    mode: str
    request: dict = field(default_factory=dict)
    usd_per_mtok_in: float = 0.0
    usd_per_mtok_out: float = 0.0

    def settings(self) -> dict:
        """Everything that shapes a request: frozen into every column signature."""
        return {
            "model": self.model,
            "route": self.route,
            "mode": self.mode,
            "temperature": 0,
            "seed": 0,
            "request": json.loads(json.dumps(self.request, sort_keys=True)),
        }

    def usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Cost of recorded usage (completion tokens include hidden reasoning tokens)."""
        return (
            prompt_tokens * self.usd_per_mtok_in + completion_tokens * self.usd_per_mtok_out
        ) / 1e6

    def max_output_tokens(self) -> int:
        return int(self.request.get("max_tokens", 1 if self.mode == "logprob" else 16))

    def project_usd(self, prompts: list[str]) -> float:
        """Projected cost of sending these filled prompts once each (conservative)."""
        tin = sum(len(p) for p in prompts) / CHARS_PER_TOKEN_EST
        tout = len(prompts) * self.max_output_tokens()
        return self.usd(int(tin) + len(prompts), tout)


PRESETS: dict[str, JudgePreset] = {
    "luna": JudgePreset(
        name="luna",
        model="gpt-6-luna",
        route="openai",
        mode="logprob",
        request={
            "max_tokens_param": "max_completion_tokens",
            "reasoning_effort": "none",
            "top_logprobs": 5,
            "max_tokens": 8,
        },
        usd_per_mtok_in=0.10,  # live, not Batch API (prereg §11 amendment A)
        usd_per_mtok_out=0.50,
    ),
    "dsk": JudgePreset(
        name="dsk",
        model="deepseek/deepseek-v4-flash",
        route="openrouter",
        mode="integer",
        request={
            "max_tokens_param": "max_tokens",
            "max_tokens": 16,
            "extra_body": {"reasoning": {"enabled": False}},
        },
        usd_per_mtok_in=0.14,
        usd_per_mtok_out=0.28,
    ),
}
assert tuple(PRESETS) == JUDGES


def load_env(env_file: Path = ENV_FILE) -> None:
    """Load `.env` into os.environ without overriding what is already set."""
    from dotenv import load_dotenv

    if env_file.exists():
        load_dotenv(env_file, override=False)


def require_key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set (expected in .env)")
    return v


def make_client(route: str):
    """AsyncOpenAI client for a route. Reads the key from the environment (after `load_env`)."""
    from openai import AsyncOpenAI

    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}")
    key = require_key(ROUTE_KEY[route])
    if route == "openai":
        return AsyncOpenAI(api_key=key)
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)


def check_probe(preset: JudgePreset, probe_path: Path = PROBE_PATH) -> dict:
    """Refuse to score with a preset the probe has not confirmed (prereg §4.9: mode fixed before
    any scoring). Returns the probe record for the model."""
    if not probe_path.exists():
        raise RuntimeError(
            f"no judge probe at {probe_path}; run scripts/e1_judge_probe.py first (main session)"
        )
    rec = json.loads(probe_path.read_text()).get("models", {}).get(preset.model)
    if rec is None:
        raise RuntimeError(f"{probe_path} has no record for {preset.model}")
    want_req = json.loads(json.dumps(preset.request, sort_keys=True))
    got_req = json.loads(json.dumps(rec.get("chosen_variant") or {}, sort_keys=True))
    if rec.get("mode") != preset.mode or got_req != want_req:
        raise RuntimeError(
            f"probe chose mode={rec.get('mode')} request={got_req} for {preset.model}, but the "
            f"preset is mode={preset.mode} request={want_req}; update PRESETS before scoring"
        )
    return rec
