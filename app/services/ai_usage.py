import os
from typing import Any

from sqlalchemy.orm import Session

from app.models import AIProviderUsage


def _rate(provider: str, direction: str) -> float:
    p = provider.lower().strip()
    key = f"{p.upper()}_{direction.upper()}_COST_PER_1M"
    try:
        return float(os.getenv(key, "0"))
    except Exception:
        return 0.0


def normalize_usage(provider: str, model_name: str | None, raw_usage: dict[str, Any] | None) -> dict[str, Any]:
    u = raw_usage or {}
    input_tokens = int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0)
    output_tokens = int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0)
    total_tokens = int(u.get("total_tokens", input_tokens + output_tokens) or 0)
    input_cost = (input_tokens / 1_000_000.0) * _rate(provider, "input")
    output_cost = (output_tokens / 1_000_000.0) * _rate(provider, "output")
    estimated_cost_usd = float(input_cost + output_cost)
    return {
        "provider": provider,
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def record_usage(db: Session, provider: str, call_type: str, usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    row = AIProviderUsage(
        ai_provider=str(provider),
        call_type=str(call_type),
        model_name=str(usage.get("model_name", ""))[:80] if usage.get("model_name") else None,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        estimated_cost_usd=float(usage.get("estimated_cost_usd", 0.0) or 0.0),
    )
    db.add(row)

