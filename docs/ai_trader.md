# AI Trader (isolated pool on Binance 1)

Page: `/live/ai-trader`

## What it is

A sub-wallet inside the Binance 1 account. You allocate an amount (e.g. 50 USDT);
the engine trades only that money and only the coins it bought itself. Profits are
reinvested automatically (cash = allocated + realized profit).

## Isolation guarantees

| Rule | Where enforced |
|------|----------------|
| Spend only pool cash | `ai_pool_service._buy` (pool cash check **and** account free USDT check) |
| Sell only pool qty | `ai_pool_service._sell` (`min(wanted, pos.qty, free balance)`) |
| Never touch other orders | pool uses market orders only, tagged `AIPOOL-<pool>-<E|X>-<ts>` |
| Detect outside interference | `_reconcile` every 60s; ledger adjusted down, never up |
| Skip coins the account already holds | `avoid_account_holdings` (default on) |

## Strategy ensemble (long-only spot)

Chosen from what holds up in crypto backtests and research: trend/breakout systems
with volatility-based sizing beat pure mean reversion.

| Strategy | Timeframe | Entry | Initial stop | TP1 | Trail |
|----------|-----------|-------|--------------|-----|-------|
| Donchian breakout | 4h | close > 20-bar high, ADX ≥ 20, volume ≥ 1.3x, trend + 30d momentum > 0 | max(close − 2 ATR, 10-bar low) | +1.5R sell 40% | 3.0 ATR chandelier |
| Trend pullback | 4h + 1h | EMA20>50>200, pullback to EMA20 zone, 1h RSI 32–58 rising, MACD hist turning, bullish candle | min(swing low − 0.2 ATR, close − 1.5 ATR), ≤ 2.5 ATR | +1.5R sell 50% | 2.0 ATR |
| Squeeze breakout | 4h | Bollinger bandwidth in bottom quartile then close > upper band, volume ≥ 1.5x | max(mid band, close − 2 ATR) | +1.5R sell 40% | 2.5 ATR |

Common filters: BTC regime (4h EMA50/200), ATR% between 0.6% and 9%, 24h quote
volume ≥ 10M USDT, reward/risk ≥ 1.2 after fees. After TP1 the stop moves to
breakeven + fees. Time stop closes positions with < +0.5% after N hours.

## Sizing

`notional = equity × risk% / stop_distance%`, capped at `max_position%` of equity
and by cash. Below the exchange minimum, the minimum is used only if the implied
risk stays ≤ 2× the configured risk.

## Circuit breakers

* Daily loss ≥ limit → paused until next UTC day (exits still run).
* Drawdown from peak ≥ limit → halted; resume manually.
* 5 consecutive exchange errors → paused.
* Per-symbol cooldown after a stop; max entries per hour.

## Loops

* `ai_pool_tick` every 10s: stops, TP1, trailing, time stop, reconcile, breakers.
* `ai_pool_scan` every 5min: regime, universe scan, sized entries.

## Tests

```bash
pytest tests/test_ai_indicators.py tests/test_ai_strategy.py tests/test_ai_pool_service.py -q
```
