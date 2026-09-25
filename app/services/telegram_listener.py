"""
Telegram listener (Telethon, user session) for the Signals pool.

Runs inside the FastAPI event loop. Login is done from the web page (phone -> code -> optional
2FA password); the session file lives next to the SQLite database so it survives redeploys.

Nothing here touches money: every message is handed to telegram_signal_service.ingest_message
in a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Any = None
_state: dict[str, Any] = {
    "status": "not_configured",  # not_configured | disconnected | login_required | code_sent | password_required | connected | error
    "user": None,
    "error": None,
    "phone": None,
    "phone_code_hash": None,
    "channel": None,
    "channel_title": None,
    "last_event_at": None,
    "messages_seen": 0,
}
_lock = asyncio.Lock()


def is_configured() -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_signal_channel)


def session_path() -> Path:
    if settings.telegram_session_path:
        return Path(settings.telegram_session_path)
    from app.core.database import database_url

    if database_url.startswith("sqlite:///"):
        db_file = Path(database_url.replace("sqlite:///", "", 1))
        return db_file.parent / "telegram_signals.session"
    return Path("app/storage/telegram_signals.session")


def status() -> dict[str, Any]:
    out = dict(_state)
    out["configured"] = is_configured()
    out["channel"] = settings.telegram_signal_channel
    out["session_path"] = str(session_path())
    out.pop("phone_code_hash", None)
    return out


async def _ensure_client():
    global _client
    if _client is not None:
        return _client
    from telethon import TelegramClient

    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _client = TelegramClient(str(path.with_suffix("")), int(settings.telegram_api_id), settings.telegram_api_hash)
    await _client.connect()
    return _client


async def start() -> None:
    """Connect and, if the session is already authorized, start listening. Safe to call repeatedly."""
    if not is_configured():
        _state["status"] = "not_configured"
        return
    async with _lock:
        try:
            client = await _ensure_client()
            if await client.is_user_authorized():
                await _begin_listening(client)
            else:
                _state["status"] = "login_required"
        except Exception as exc:
            _state["status"] = "error"
            _state["error"] = str(exc)[:300]
            logger.warning("telegram listener start failed: %s", exc)


async def send_code(phone: str) -> dict[str, Any]:
    if not is_configured():
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SIGNAL_CHANNEL are not set")
    async with _lock:
        client = await _ensure_client()
        phone = phone.strip()
        res = await client.send_code_request(phone)
        _state["phone"] = phone
        _state["phone_code_hash"] = res.phone_code_hash
        _state["status"] = "code_sent"
        _state["error"] = None
        return status()


async def sign_in(code: str, password: Optional[str] = None) -> dict[str, Any]:
    from telethon.errors import SessionPasswordNeededError

    async with _lock:
        client = await _ensure_client()
        try:
            if _state.get("status") == "password_required" and password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(_state.get("phone") or "", code.strip().replace(" ", ""), phone_code_hash=_state.get("phone_code_hash"))
        except SessionPasswordNeededError:
            if password:
                await client.sign_in(password=password)
            else:
                _state["status"] = "password_required"
                return status()
        _state["phone_code_hash"] = None
        await _begin_listening(client)
        return status()


async def logout() -> dict[str, Any]:
    global _client
    async with _lock:
        if _client is not None:
            try:
                await _client.log_out()
            except Exception as exc:
                logger.warning("telegram logout: %s", exc)
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        _state.update({"status": "login_required", "user": None, "channel_title": None, "phone_code_hash": None})
        return status()


async def fetch_history(limit: int = 15) -> list[dict]:
    """Last `limit` posts of the channel as [{id, date, text}] (newest first). Requires a connected session."""
    if _state.get("status") != "connected" or _client is None:
        raise RuntimeError("Telegram is not connected")
    entity = await _client.get_entity(settings.telegram_signal_channel)
    msgs = await _client.get_messages(entity, limit=max(1, min(200, int(limit))))
    return [{"id": m.id, "date": m.date, "text": m.message or ""} for m in msgs]


async def _begin_listening(client) -> None:
    from telethon import events
    from telethon.tl.functions.channels import JoinChannelRequest

    me = await client.get_me()
    _state["user"] = f"{me.first_name or ''} {me.last_name or ''}".strip() or (me.username or str(me.id))
    channel = settings.telegram_signal_channel
    try:
        entity = await client.get_entity(channel)
    except Exception as exc:
        _state["status"] = "error"
        _state["error"] = f"channel @{channel} not found: {exc}"[:300]
        return
    try:
        await client(JoinChannelRequest(entity))  # public channel: harmless if already joined
    except Exception as exc:
        logger.info("join channel skipped: %s", exc)
    _state["channel_title"] = getattr(entity, "title", channel)

    # Only NEW posts are traded: on first connect, record the latest message id and skip history.
    from app.core.database import SessionLocal
    from app.services import telegram_signal_service as svc

    db = SessionLocal()
    try:
        last_seen = svc.get_last_msg_id(db, channel)
        latest = await client.get_messages(entity, limit=1)
        latest_id = latest[0].id if latest else 0
        if last_seen <= 0 and latest_id > 0:
            svc.set_last_msg_id(db, channel, latest_id)
            db.commit()
            last_seen = latest_id
    finally:
        db.close()

    # Catch up on posts missed while the server was down (at most the entry window old).
    if last_seen > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(settings.telegram_entry_window_hours))
        try:
            missed = await client.get_messages(entity, min_id=last_seen, limit=50)
            for m in reversed(list(missed)):
                if m.date and m.date < cutoff:
                    continue
                await _handle(channel, m.id, m.message or "", m.date)
        except Exception as exc:
            logger.warning("telegram catch-up failed: %s", exc)

    client.remove_event_handler(_on_new_message)
    client.add_event_handler(_on_new_message, events.NewMessage(chats=entity))
    _state["status"] = "connected"
    _state["error"] = None
    logger.info("Telegram listener connected as %s, watching @%s", _state["user"], channel)


async def _on_new_message(event) -> None:
    msg = event.message
    await _handle(settings.telegram_signal_channel, msg.id, msg.message or "", msg.date)


async def _handle(channel: str, msg_id: int, text: str, date) -> None:
    from app.services import telegram_signal_service as svc

    _state["last_event_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    _state["messages_seen"] = int(_state.get("messages_seen") or 0) + 1
    posted_at = date.replace(tzinfo=None) if isinstance(date, datetime) and date.tzinfo else date
    try:
        result = await asyncio.to_thread(svc.ingest_message, channel, int(msg_id), text, posted_at)
        if result:
            logger.info("telegram msg %s -> %s (%s)", msg_id, result.get("status"), result.get("note"))
    except Exception as exc:
        logger.exception("telegram message handling failed: %s", exc)
