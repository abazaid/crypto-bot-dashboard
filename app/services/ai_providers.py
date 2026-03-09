import json
import random
import time
from typing import Any, Dict

import requests

from app.services.ai_usage import normalize_usage


DEEPSEEK_STRATEGY_TIMEOUT = 30
DEEPSEEK_CHAT_TIMEOUT = 35
DEEPSEEK_RETRIES = 2


def _http_error_with_body(exc: Exception) -> Exception:
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            body = resp.text[:500]
            return Exception(f"{exc} | body={body}")
    except Exception:
        pass
    return exc


def _post_with_retries(
    url: str,
    headers: dict,
    payload: dict,
    timeout: int,
    retries: int = 0,
    retry_delay_sec: float = 1.2,
):
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise _http_error_with_body(exc)
            time.sleep(retry_delay_sec * (attempt + 1))
        except Exception as exc:
            raise _http_error_with_body(exc)
    if last_exc:
        raise _http_error_with_body(last_exc)
    raise Exception("Unknown HTTP failure")


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
        "You are a senior crypto trader focused on capital protection first, then returns.\n"
        "You are building a PAPER crypto trading strategy config for one symbol.\n"
        "Prefer robust, risk-aware settings and avoid overfitting/noise-chasing.\n"
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


def _call_openai(api_key: str, model: str, symbol_context: dict) -> tuple[dict, dict[str, Any]]:
    resp = _post_with_retries(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={
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
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _safe_json_loads(content), normalize_usage("openai", model, data.get("usage", {}))


def _call_claude(api_key: str, model: str, symbol_context: dict) -> tuple[dict, dict[str, Any]]:
    resp = _post_with_retries(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        payload={
            "model": model,
            "max_tokens": 300,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": _prompt(symbol_context)}],
            "system": "Return JSON only.",
        },
        timeout=12,
    )
    data = resp.json()
    text = ""
    for part in data.get("content", []):
        if part.get("type") == "text":
            text += part.get("text", "")
    return _safe_json_loads(text), normalize_usage("claude", model, data.get("usage", {}))


def _call_deepseek(api_key: str, model: str, symbol_context: dict) -> tuple[dict, dict[str, Any]]:
    resp = _post_with_retries(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": _prompt(symbol_context)},
            ],
            "max_tokens": 250,
        },
        timeout=DEEPSEEK_STRATEGY_TIMEOUT,
        retries=DEEPSEEK_RETRIES,
    )
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _safe_json_loads(content), normalize_usage("deepseek", model, data.get("usage", {}))


def _chat_openai(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> tuple[str, dict[str, Any]]:
    payload_messages = [{"role": "system", "content": system_prompt}]
    payload_messages.extend(messages)
    resp = _post_with_retries(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={"model": model, "temperature": 0.2, "messages": payload_messages, "max_tokens": 450},
        timeout=20,
    )
    data = resp.json()
    text = str(data["choices"][0]["message"]["content"]).strip()
    return text, normalize_usage("openai", model, data.get("usage", {}))


def _chat_claude(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> tuple[str, dict[str, Any]]:
    claude_messages = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        claude_messages.append({"role": role, "content": str(m.get("content", ""))})
    resp = _post_with_retries(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        payload={
            "model": model,
            "max_tokens": 500,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": claude_messages,
        },
        timeout=20,
    )
    data = resp.json()
    text = ""
    for part in data.get("content", []):
        if part.get("type") == "text":
            text += part.get("text", "")
    return text.strip(), normalize_usage("claude", model, data.get("usage", {}))


def _chat_deepseek(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> tuple[str, dict[str, Any]]:
    trimmed_messages = messages[-8:]
    compact_messages = []
    for m in trimmed_messages:
        compact_messages.append(
            {
                "role": m.get("role", "user"),
                "content": str(m.get("content", ""))[:1200],
            }
        )
    payload_messages = [{"role": "system", "content": system_prompt[:1800]}]
    payload_messages.extend(compact_messages)
    resp = _post_with_retries(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={"model": model, "temperature": 0.2, "messages": payload_messages, "max_tokens": 450},
        timeout=DEEPSEEK_CHAT_TIMEOUT,
        retries=DEEPSEEK_RETRIES,
    )
    data = resp.json()
    text = str(data["choices"][0]["message"]["content"]).strip()
    return text, normalize_usage("deepseek", model, data.get("usage", {}))


def propose_strategy(provider: str, symbol_context: Dict[str, float | str], cfg: Dict[str, str]) -> dict:
    strategy, _ = propose_strategy_with_usage(provider, symbol_context, cfg)
    return strategy


def propose_strategy_with_usage(provider: str, symbol_context: Dict[str, float | str], cfg: Dict[str, str]) -> tuple[dict, dict[str, Any] | None]:
    provider = provider.lower().strip()
    seed = abs(hash(f"{provider}:{symbol_context.get('symbol','-')}:{symbol_context.get('score',0)}")) % 10_000_000
    fallback = _fallback_strategy(seed=seed)
    try:
        if provider == "openai":
            api_key = cfg.get("OPENAI_API_KEY", "")
            model = cfg.get("OPENAI_MODEL", "gpt-4o-mini")
            if not api_key:
                fallback["strategy_source"] = "fallback_no_api_key"
                return fallback, None
            raw, usage = _call_openai(api_key, model, symbol_context)
            out = _normalize_strategy(raw)
            out["strategy_source"] = "llm_openai"
            return out, usage
        if provider == "claude":
            api_key = cfg.get("ANTHROPIC_API_KEY", "")
            model = cfg.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
            if not api_key:
                fallback["strategy_source"] = "fallback_no_api_key"
                return fallback, None
            raw, usage = _call_claude(api_key, model, symbol_context)
            out = _normalize_strategy(raw)
            out["strategy_source"] = "llm_claude"
            return out, usage
        if provider == "deepseek":
            api_key = cfg.get("DEEPSEEK_API_KEY", "")
            model = cfg.get("DEEPSEEK_MODEL", "deepseek-chat")
            if not api_key:
                fallback["strategy_source"] = "fallback_no_api_key"
                return fallback, None
            raw, usage = _call_deepseek(api_key, model, symbol_context)
            out = _normalize_strategy(raw)
            out["strategy_source"] = "llm_deepseek"
            return out, usage
    except Exception:
        fallback["strategy_source"] = "fallback_on_error"
        return fallback, None
    fallback["strategy_source"] = "fallback_unknown_provider"
    return fallback, None


def chat_with_provider(provider: str, system_prompt: str, messages: list[dict], cfg: Dict[str, str]) -> str:
    text, _ = chat_with_provider_with_usage(provider, system_prompt, messages, cfg)
    return text


def chat_with_provider_with_usage(provider: str, system_prompt: str, messages: list[dict], cfg: Dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    provider = provider.lower().strip()
    try:
        if provider == "openai":
            api_key = cfg.get("OPENAI_API_KEY", "")
            model = cfg.get("OPENAI_MODEL", "gpt-4o-mini")
            if not api_key:
                return "OpenAI API key is missing. Add OPENAI_API_KEY to enable model chat.", None
            return _chat_openai(api_key, model, system_prompt, messages)
        if provider == "claude":
            api_key = cfg.get("ANTHROPIC_API_KEY", "")
            model = cfg.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
            if not api_key:
                return "Anthropic API key is missing. Add ANTHROPIC_API_KEY to enable model chat.", None
            return _chat_claude(api_key, model, system_prompt, messages)
        if provider == "deepseek":
            api_key = cfg.get("DEEPSEEK_API_KEY", "")
            model = cfg.get("DEEPSEEK_MODEL", "deepseek-chat")
            if not api_key:
                return "DeepSeek API key is missing. Add DEEPSEEK_API_KEY to enable model chat.", None
            return _chat_deepseek(api_key, model, system_prompt, messages)
    except Exception as exc:
        return f"Model chat failed: {exc}", None
    return "Unsupported provider for chat.", None
