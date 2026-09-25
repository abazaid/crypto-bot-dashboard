import time

import pytest

from app.services import binance_ratelimit as rl

pytestmark = pytest.mark.unit


def test_ban_is_shared_and_parsed(monkeypatch):
    monkeypatch.setattr(rl, "_ban_until_ts", 0.0)
    future_ms = int((time.time() + 60) * 1000)
    rl.note_http_error(418, '{"code":-1003,"msg":"Way too much request weight used; IP banned until %d."}' % future_ms)
    assert rl.is_banned()
    with pytest.raises(RuntimeError):
        rl.check_ban()
    monkeypatch.setattr(rl, "_ban_until_ts", 0.0)
    rl.note_http_error(429, "rate limit")
    assert rl.is_banned() and rl.ban_until() - time.time() <= 15.5
    monkeypatch.setattr(rl, "_ban_until_ts", 0.0)
    rl.note_http_error(400, "bad request")
    assert not rl.is_banned()


def test_persistent_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "persistent_dir", lambda: tmp_path)
    rl.save_json("x.json", {"ABC": {"avg_entry": 1.5}})
    assert rl.load_json("x.json") == {"ABC": {"avg_entry": 1.5}}
    assert rl.load_json("missing.json") == {}
