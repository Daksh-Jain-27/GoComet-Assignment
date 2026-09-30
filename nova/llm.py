"""Single gateway for every LLM call (via LiteLLM, so any provider works).

Guardrails live HERE, not in prompts:
  - per-document USD budget (checked before every call)  -> no runaway cost
  - bounded retries with exponential backoff              -> no infinite retries
  - timeout per call                                      -> no hung pipeline
  - JSON parse + one repair attempt, then hard failure    -> no silent garbage
  - every call logged (model, latency, tokens, cost)      -> observability / cost dashboard
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Optional

from .config import SETTINGS

log = logging.getLogger("nova.llm")


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def _setup_litellm():
    import litellm
    litellm.drop_params = True          # ignore params a provider doesn't support
    litellm.suppress_debug_info = True
    if SETTINGS.langfuse_enabled:
        litellm.success_callback = ["langfuse"]
        litellm.failure_callback = ["langfuse"]
    return litellm


def image_part(path: str) -> dict:
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class LLMClient:
    """One client per pipeline run; `spent_usd` is carried in graph state so the
    budget survives a crash + resume."""

    def __init__(self, run_id: str, spent_usd: float = 0.0, budget_usd: Optional[float] = None):
        self.run_id = run_id
        self.spent_usd = spent_usd
        self.budget_usd = SETTINGS.budget_usd_per_doc if budget_usd is None else budget_usd
        self.litellm = _setup_litellm()

    def remaining(self) -> float:
        return self.budget_usd - self.spent_usd

    def complete_json(self, *, model: str, messages: list[dict], node: str,
                      max_tokens: int = 8000) -> dict:
        from . import storage  # local import to avoid a cycle

        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"Budget ${self.budget_usd:.3f} reached before {node}")

        last_err: Optional[Exception] = None
        msgs = list(messages)
        for attempt in range(SETTINGS.llm_max_retries + 1):
            t0 = time.perf_counter()
            try:
                temp = {} if "gemini-3" in model else {"temperature": 0}
                resp = self.litellm.completion(
                    model=model, messages=msgs, **temp, max_tokens=max_tokens,
                    timeout=SETTINGS.llm_timeout_s, response_format={"type": "json_object"},
                    metadata={"trace_id": self.run_id, "generation_name": node,
                              "trace_name": "nova-pipeline"},
                )
                latency = (time.perf_counter() - t0) * 1000
                try:
                    cost = float(self.litellm.completion_cost(completion_response=resp) or 0.0)
                except Exception:
                    cost = 0.0
                self.spent_usd += cost
                usage = getattr(resp, "usage", None)
                content = resp.choices[0].message.content
                storage.log_llm_call(self.run_id, node, model, latency,
                                     getattr(usage, "prompt_tokens", None),
                                     getattr(usage, "completion_tokens", None), cost, True, None)
                try:
                    return parse_json(content)
                except (json.JSONDecodeError, TypeError) as e:
                    # one repair attempt: tell the model what went wrong
                    last_err = e
                    msgs = msgs + [{"role": "assistant", "content": str(content)[:4000]},
                                   {"role": "user", "content": "That was not valid JSON. Return ONLY the JSON object."}]
                    if self.spent_usd >= self.budget_usd:
                        raise BudgetExceeded("Budget reached during JSON repair")
                    continue
            except BudgetExceeded:
                raise
            except Exception as e:  # network, rate limit, provider error
                last_err = e
                latency = (time.perf_counter() - t0) * 1000
                storage.log_llm_call(self.run_id, node, model, latency, None, None, 0.0, False, repr(e)[:500])
                log.warning("LLM call failed (%s, attempt %d): %s", node, attempt + 1, e)
                if attempt < SETTINGS.llm_max_retries:
                    msg = str(e).lower()
                    transient = any(s in msg for s in ("503", "429", "unavailable", "overloaded",
                                                       "high demand", "resource_exhausted"))
                    # overloaded/rate-limited providers need real back-off; other errors retry fast
                    time.sleep(10 * (attempt + 1) if transient else min(2 ** attempt, 8))
        raise LLMError(f"{node}: LLM failed after {SETTINGS.llm_max_retries + 1} attempts: {last_err}")
