import json
import random
from typing import Dict

import requests


def _safe_json_loads(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return {}
    return {}


def _fallback_strategy(seed: int | None = None) -> dict:
    rnd = random.Random(seed)
    cfg = {
        "use_score_system": True,
        "score_threshold": rnd.randint(2, 4),
        "trend_enabled": True,
        "pullback_enabled": True,
        "rsi_enabled": True,
        "volume_spike_enabled": True,
        "resistance_enabled": True,
        "price_above_ema50_enabled": rnd.choice([True, False]),
        "pullback_max_dist_pct": round(rnd.uniform(0.5, 1.8), 2),
        "rsi_min": round(rnd.uniform(28, 42), 1),
        "rsi_max": round(rnd.uniform(58, 72), 1),
        "volume_spike_multiplier": round(rnd.uniform(1.1, 2.2), 2),
        "resistance_min_dist_pct": round(rnd.uniform(0.8, 3.0), 2),
        "tp_pct": round(rnd.uniform(0.012, 0.045), 4),
        "sl_pct": round(rnd.uniform(0.006, 0.02), 4),
        "trailing_stop_pct": round(rnd.uniform(0.004, 0.012), 4),
        "time_stop_minutes": rnd.randint(45, 240),
    }
    if cfg["rsi_max"] <= cfg["rsi_min"] + 8:
        cfg["rsi_max"] = cfg["rsi_min"] + 8
    return cfg


def _normalize_strategy(cfg: dict) -> dict:
    base = _fallback_strategy()
    base.update({k: v for k, v in cfg.items() if k in base})
    return base


def _prompt(symbol_context: dict) -> str:
    return (
        "You are building a PAPER crypto trading strategy config for one symbol.\n"
        "Return ONLY JSON with keys:\n"
        "score_threshold, pullback_max_dist_pct, rsi_min, rsi_max, volume_spike_multiplier,\n"
        "resistance_min_dist_pct, price_above_ema50_enabled, tp_pct, sl_pct,\n"
        "trailing_stop_pct, time_stop_minutes.\n"
        "Constraints: score_threshold 2-4, pullback 0.5-2.0, rsi_min 20-45, rsi_max 55-80,\n"
        "volume_spike_multiplier 1.0-2.5, resistance_min_dist_pct 0.5-3.5,\n"
        "tp_pct 0.01-0.06, sl_pct 0.005-0.03, trailing_stop_pct 0.003-0.02,\n"
        "time_stop_minutes 30-360.\n"
        f"Context: {json.dumps(symbol_context, ensure_ascii=True)}"
    )


def _call_openai(api_key: str, model: str, symbol_context: dict) -> dict:
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": _prompt(symbol_context)},
            ],
            "max_tokens": 250,
        },
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _safe_json_loads(content)


def _call_claude(api_key: str, model: str, symbol_context: dict) -> dict:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 300,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": _prompt(symbol_context)}],
            "system": "Return JSON only.",
        },
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for part in data.get("content", []):
        if part.get("type") == "text":
            text += part.get("text", "")
    return _safe_json_loads(text)


def _call_deepseek(api_key: str, model: str, symbol_context: dict) -> dict:
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": _prompt(symbol_context)},
            ],
            "max_tokens": 250,
        },
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _safe_json_loads(content)


def propose_strategy(provider: str, symbol_context: Dict[str, float | str], cfg: Dict[str, str]) -> dict:
    provider = provider.lower().strip()
    seed = abs(hash(f"{provider}:{symbol_context.get('symbol','-')}:{symbol_context.get('score',0)}")) % 10_000_000
    fallback = _fallback_strategy(seed=seed)
    try:
        if provider == "openai":
            api_key = cfg.get("OPENAI_API_KEY", "")
            model = cfg.get("OPENAI_MODEL", "gpt-4o-mini")
            if not api_key:
                return fallback
            return _normalize_strategy(_call_openai(api_key, model, symbol_context))
        if provider == "claude":
            api_key = cfg.get("ANTHROPIC_API_KEY", "")
            model = cfg.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
            if not api_key:
                return fallback
            return _normalize_strategy(_call_claude(api_key, model, symbol_context))
        if provider == "deepseek":
            api_key = cfg.get("DEEPSEEK_API_KEY", "")
            model = cfg.get("DEEPSEEK_MODEL", "deepseek-chat")
            if not api_key:
                return fallback
            return _normalize_strategy(_call_deepseek(api_key, model, symbol_context))
    except Exception:
        return fallback
    return fallback
