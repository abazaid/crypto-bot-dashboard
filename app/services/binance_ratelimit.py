"""
Shared Binance rate-limit state and persistent caches for BOTH Binance accounts.

Binance bans by IP (HTTP 418, code -1003), so a ban triggered by account 2 must stop
account 1's requests too, or the ban keeps escalating. Cost-basis caches are persisted next
to the SQLite database (the persistent volume in Docker) so a redeploy does not re-download
every symbol's trade history, which is what caused the bans in the first place.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ban_until_ts: float = 0.0


def extract_ban_until_ts(error_text: str) -> float | None:
    m = re.search(r"until\s+(\d+)", str(error_text or ""))
    if not m:
        return None
    try:
        ms = int(m.group(1))
        return ms / 1000.0 if ms > 0 else None
    except ValueError:
        return None


def ban_until() -> float:
    return _ban_until_ts


def is_banned() -> bool:
    return _ban_until_ts > time.time()


def check_ban() -> None:
    """Raise before making a request while the IP ban is active (avoids extending the ban)."""
    if is_banned():
        raise RuntimeError(f"Binance API temporarily banned until {int(_ban_until_ts)} (unix); request skipped.")


def note_http_error(status_code: int, text: str) -> None:
    """Record 418/429 responses from any Binance account so every caller backs off together."""
    global _ban_until_ts
    with _lock:
        if status_code == 418:
            until = extract_ban_until_ts(text) or (time.time() + 120.0)
            _ban_until_ts = max(_ban_until_ts, until)
            logger.error("Binance IP ban until %s (unix): %s", int(_ban_until_ts), text[:160])
        elif status_code == 429:
            _ban_until_ts = max(_ban_until_ts, time.time() + 15.0)
            logger.warning("Binance rate limit (429); pausing requests 15s")


# ── Persistent JSON caches ─────────────────────────────────────────────────────

def persistent_dir() -> Path:
    """Directory that survives redeploys: the SQLite database folder, else app/storage."""
    try:
        from app.core.database import database_url

        if database_url.startswith("sqlite:///"):
            return Path(database_url.replace("sqlite:///", "", 1)).parent
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "storage"


def load_json(name: str) -> dict[str, Any]:
    path = persistent_dir() / name
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("cache %s unreadable: %s", path, exc)
    return {}


def save_json(name: str, data: dict[str, Any]) -> None:
    path = persistent_dir() / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("cache %s not saved: %s", path, exc)
