import pytest

from app.services.telegram_audit import simulate
from app.services.telegram_signals import parse_signal
from test_telegram_signals import ALICE

pytestmark = pytest.mark.unit


def _k(t_min: int, o: float, h: float, lo: float, c: float) -> list:
    return [t_min * 60_000, o, h, lo, c, 1000.0]


def test_simulate_all_targets_done():
    sig = parse_signal(ALICE)  # zone 0.148-0.1556, stop 0.137, targets 0.171/0.187/0.210/0.233
    kl = [
        _k(0, 0.150, 0.152, 0.149, 0.151),  # entry candle @ open 0.150
        _k(15, 0.151, 0.175, 0.150, 0.174),  # T1
        _k(30, 0.174, 0.190, 0.172, 0.189),  # T2 (stop now at T1 = 0.171 after this)
        _k(45, 0.189, 0.215, 0.188, 0.214),  # T3
        _k(60, 0.214, 0.240, 0.213, 0.238),  # T4 -> done
    ]
    res = simulate(sig, kl, posted_ms=0, window_hours=12)
    # all four targets touched; half of the last slice sold, 15% runner still open at the last close
    assert res["status"] == "open" and res["targets_hit"] == 4
    expected = (0.2 * (0.171 / 0.150 - 1) + 0.25 * (0.187 / 0.150 - 1) + 0.25 * (0.210 / 0.150 - 1) + 0.15 * (0.233 / 0.150 - 1) + 0.15 * (0.238 / 0.150 - 1)) * 100 - 0.2
    assert res["pnl_pct"] == pytest.approx(expected, abs=0.05)


def test_simulate_stop_before_target_is_a_loss():
    sig = parse_signal(ALICE)
    kl = [_k(0, 0.150, 0.152, 0.149, 0.151), _k(15, 0.151, 0.153, 0.130, 0.135)]
    res = simulate(sig, kl, 0, 12)
    assert res["status"] == "stop"
    assert res["pnl_pct"] == pytest.approx((0.137 / 0.150 - 1) * 100 - 0.2, abs=0.05)


def test_simulate_trail_after_t2_locks_t1_profit():
    sig = parse_signal(ALICE)
    kl = [
        _k(0, 0.150, 0.152, 0.149, 0.151),
        _k(15, 0.151, 0.190, 0.150, 0.189),  # T1 and T2 in one candle
        _k(30, 0.189, 0.191, 0.160, 0.165),  # falls to the raised stop (T1 = 0.171)
    ]
    res = simulate(sig, kl, 0, 12)
    assert res["status"] == "trail" and res["hits"] == ["T1", "T2"]
    assert res["pnl_pct"] > 10.0  # 45% sold at targets, 55% out at the locked stop (>= 0.179)


def test_simulate_missed_and_open():
    sig = parse_signal(ALICE)
    kl = [_k(m, 0.170, 0.172, 0.168, 0.171) for m in range(0, 15 * 60, 15)]  # never enters the zone for 15h
    assert simulate(sig, kl, 0, 12)["status"] == "missed"
    kl2 = [_k(0, 0.150, 0.152, 0.149, 0.151), _k(15, 0.151, 0.160, 0.150, 0.158)]
    res = simulate(sig, kl2, 0, 12)
    assert res["status"] == "open" and res["pnl_pct"] == pytest.approx((0.158 / 0.150 - 1) * 100 - 0.2, abs=0.05)


def test_channel_rules_market_entry_and_4h_close_stop():
    from app.services.telegram_audit import simulate_channel_rules

    sig = parse_signal(ALICE)
    fine = [
        _k(0, 0.160, 0.175, 0.159, 0.172),   # market entry at 0.160 (above the zone), T1 touched
        _k(15, 0.172, 0.174, 0.130, 0.131),  # wick below the stop: NOT a stop under channel rules
        _k(30, 0.131, 0.190, 0.130, 0.188),  # T2 touched
        _k(1000, 0.188, 0.189, 0.187, 0.188),
    ]
    k4h = [[0, 0.160, 0.190, 0.130, 0.188, 1.0]]  # the 4h candle closes above the stop
    res = simulate_channel_rules(sig, fine, k4h, 0)
    assert res["entry"] == 0.160 and res["hits"] == ["T1", "T2"] and res["status"] == "open"
    # a 4h close below the stop after the targets
    k4h2 = [[0, 0.160, 0.190, 0.130, 0.188, 1.0], [4 * 3600 * 1000, 0.188, 0.189, 0.120, 0.125, 1.0]]
    fine2 = fine + [_k(8 * 60, 0.125, 0.126, 0.124, 0.125)]
    res2 = simulate_channel_rules(sig, fine2, k4h2, 0)
    assert res2["status"] == "stop_after_targets" and res2["exit_price"] == 0.125


def test_simulate_lock_zero_means_previous_target():
    sig = parse_signal(ALICE)
    kl = [
        _k(0, 0.150, 0.152, 0.149, 0.151),
        _k(15, 0.151, 0.190, 0.150, 0.189),  # T1 + T2
        _k(30, 0.189, 0.191, 0.175, 0.176),  # dips to 0.175: above T1 (0.171) but below T1+50% leg (0.179)
        _k(45, 0.176, 0.215, 0.175, 0.214),  # T3
    ]
    tight = simulate(sig, kl, 0, 12, target_lock=0.5, giveback=1.0)
    loose = simulate(sig, kl, 0, 12, target_lock=0.0, giveback=1.0)
    assert tight["status"] == "trail" and tight["hits"] == ["T1", "T2"]
    assert loose["hits"] == ["T1", "T2", "T3"] and loose["pnl_pct"] > tight["pnl_pct"]
