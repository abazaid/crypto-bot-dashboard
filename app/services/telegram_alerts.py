import os

import requests


def send_telegram_message(message: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def telegram_test(token: str, chat_id: str, text: str = "Telegram connectivity test") -> tuple[bool, str]:
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return False, "Missing token or chat_id"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True, "OK"
        return False, f"HTTP {r.status_code}: {r.text[:180]}"
    except requests.exceptions.RequestException as exc:
        return False, f"Network error: {exc}"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"
