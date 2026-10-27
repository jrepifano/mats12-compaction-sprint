"""Week-2 feasibility check (27 Oct): does the pinned DeepSeek route return reasoning text?

Two real calls on the task-chaining route (results/sprint/S2/chain/route.json: OpenRouter ->
Together, deepseek/deepseek-v4.1-flash, effort max), with the harness's own request settings
(`OpenAICompatModel.body`): one agent call with the loop's system prompt, instance message and
tools, and one summarizer call (tools=None, purpose "summary:..."). Each call's charge is
recorded through emx.spend in this checkout's ledger (exp WEEK2-BUILD). Prints which message
fields carry reasoning text and their sizes, and writes the record to
results/sprint/week2/reasoning_feasibility.json.

    uv run python scripts/week2_reasoning_feasibility.py
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from compaction.s2 import loop as L
from compaction.s2.chain import driver as D
from compaction.s2.model import OpenAICompatModel
from emx import spend as S

ROOT = Path(__file__).resolve().parents[1]
EXP = "WEEK2-BUILD"
OUT = ROOT / "results" / "sprint" / "week2" / "reasoning_feasibility.json"
FIELDS = ("reasoning_content", "reasoning", "reasoning_details")


@dataclass
class LedgerSpend:
    """Books each settled attempt into emx.spend (kind judge, as the S2 route does)."""

    charged: float = 0.0
    n: int = 0
    rows: list = field(default_factory=list)

    def reserve(self, usd: float, note: str) -> str:
        self.n += 1
        return f"feas-{self.n}"

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        self.charged += usd
        self.rows.append(S.record("judge", resource, units, usd, note=f"week2 {note}", exp=EXP))

    def cancel(self, rid: str, reason: str) -> None:
        pass


def fields_of(msg: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = msg.get(k)
        if v in (None, "", []):
            continue
        out[k] = {"type": type(v).__name__,
                  "chars": len(v) if isinstance(v, str) else len(json.dumps(v)),
                  "head": (v if isinstance(v, str) else json.dumps(v))[:300]}
    return out


def main() -> int:
    load_dotenv(ROOT / ".env")
    assert os.environ.get("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY missing"
    route = D.load_route()
    spend = LedgerSpend()
    model = OpenAICompatModel(route, spend)
    task = ("List the files in the current directory with `ls`, then stop. This is a "
            "connectivity check.")
    msgs = [{"role": "system", "content": L.SYSTEM_PROMPT},
            {"role": "user", "content": L.instance_message(task, None)}]
    agent = model.complete(msgs, L.TOOLS, route.max_output, purpose="feas:1")
    summ_msgs = [{"role": "user", "content": "Summarize in two sentences: an agent was asked to "
                  "list the files in a repository and ran `ls`, which printed README.md and "
                  "src/."}]
    summ = model.complete(summ_msgs, None, 4000, purpose="summary:feas")
    rec = {"route": route.name, "model": route.model, "provider_pin": route.provider_pin,
           "reasoning_request": route.reasoning, "calls": []}
    for name, r in (("agent", agent), ("summary", summ)):
        rec["calls"].append({
            "call": name, "returned_model": r.model, "finish_reason": r.finish_reason,
            "message_keys": sorted(r.message), "reasoning_fields": fields_of(r.message),
            "tool_calls": len(r.message.get("tool_calls") or []),
            "reasoning_tokens": r.usage.reasoning_tokens, "cost_usd": r.cost_total_usd,
            "cost_source": r.usage.cost_source})
    rec["total_usd"] = round(spend.charged, 6)
    rec["ledger_ids"] = [row["id"] for row in spend.rows]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rec, indent=1) + "\n")
    print(json.dumps(rec, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
