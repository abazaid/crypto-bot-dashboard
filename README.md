# Crypto Bots v2

Smart campaign-based crypto trading platform with:
- `Paper Trading` (full simulation engine)
- `Live Mode` (real Binance execution)
- `Smart DCA` (support-zone weighted accumulation)
- `Smart Backtesting` (standalone ROI / MDD / Recovery module)

---

## Our Trading Philosophy

We do **not** buy every dip.

Our approach is:
- Buy only around high-quality support context.
- Prioritize structure and capital distribution over indicator noise.
- Scale DCA under strict risk limits.
- Keep every campaign independent.

Core principle:
> Entry quality and capital control are more important than trade count.

---

## Architecture

- Campaign-centric system (`paper` / `live`).
- Each campaign has isolated:
  - symbols scope
  - TP/SL
  - DCA mode (manual / AI / Smart)
  - trend filter behavior
  - loop behavior
- Position-level DCA state (`PositionDcaState`) per symbol and per rule.
- Full activity logs for decisions (open / skip / DCA / close / fail).

---

## Modes

## 1) Paper Trading

Virtual wallet simulation with full strategy lifecycle.

Highlights:
- Create standard, loop, and SMART DCA campaigns.
- Edit TP/SL and DCA rules per campaign.
- Manual sell per symbol.
- Recalculate DCA now.
- Independent trading history.

## 2) Live Mode

Real Binance execution with campaign logic mirrored from paper mode.

Highlights:
- Reads real balances from Binance.
- Entry and DCA are **limit-first buy** (IOC) with optional market fallback.
- TP uses real Binance `LIMIT SELL` orders.
- TP is re-armed after DCA changes average price.
- SL is enforced by engine via market close.
- Independent live history.

---

## DCA Modes

## Manual DCA

User-defined levels (up to 5):
- `Drop %`
- `Allocation %`

## AI DCA

Per-symbol rules generated from support context and filtered by confirmation logic.

## SMART DCA

Dedicated campaign flow with:
- strategy profile (`AUTO`, `Aggressive`, `Balanced`, `Conservative`)
- dynamic support-aware drop/allocation planning
- capital planner before campaign start

---

## SMART DCA Engine (Implemented)

The current Smart DCA implementation includes:

1. Market state detector (EMA200-based regime)
  - bullish / sideways / bearish

2. Dynamic support scoring per planned zone
  - volume profile contribution
  - liquidity proxy
  - historical reaction strength
  - timeframe confluence

3. Canonical depth levels
  - `5%`, `10%`, `17%`, `25%`, `35%`, `45%`
  - capped by SL distance if SL is set

4. Dynamic allocation formula
  - `allocation = base * (score/100) * depth_multiplier * profile_multiplier`

5. Risk cap
  - max symbol plan exposure bounded to `entry * 6` (entry + DCA)

6. Capital planner output (before start)
  - base entry
  - max reserved capital
  - estimated typical capital usage range
  - drawdown coverage
  - risk level

7. Zone lock policy
  - Execution zones are locked when campaign starts.
  - Dynamic analysis zones are recalculated in background.
  - If DCA already executed, system does not auto-replace execution zones.
  - System marks campaign as `recalc recommended` instead of overriding active plan.

---

## Periodic Processing (Implemented)

The runtime now uses 3 processing layers:

1. Fast Loop (execution)
  - env: `FAST_LOOP_SECONDS` (default 30s)
  - tasks: price checks, entry/TP/SL/DCA execution

2. Medium Refresh (market context)
  - env: `MEDIUM_REFRESH_SECONDS` (default 300s)
  - tasks: market state refresh (EMA200-based), lightweight snapshots

3. Slow Recalculation (dynamic analysis zones)
  - env: `SLOW_RECALC_SECONDS` (default 14400s = 4h)
  - tasks: recalc dynamic zones + support scoring + review/apply decision

Backwards compatibility:
- `CYCLE_SECONDS` is still supported.
- Fast loop uses `FAST_LOOP_SECONDS` if provided.

---

## Persistence for Smart Runtime (Implemented)

Additional persisted runtime data:
- `smart_runtime_states`
  - locked execution status
  - recalc recommendation flag/reason
  - latest market state / EMA200 / price
  - latest dynamic zones payload
  - last medium/slow refresh timestamps

- `market_snapshots`
  - periodic market snapshots per campaign/symbol/mode

---

## Loop Campaigns

Loop campaigns keep N open symbols.

Behavior:
- scan scored candidates
- fill missing slots
- reopen after close (if needed)
- block duplicate open symbol inside same campaign

Rule:
- Same symbol cannot be opened twice while already open in the same campaign.
- Reopen is allowed only after previous position is closed.

---

## Risk Controls

- TP / SL at campaign level (not global fixed).
- Strict mode option: `No Score = No DCA`.
- Optional BTC trend filter per campaign.
- Breakdown detection can pause DCA for symbol.
- Max symbol allocation cap (`DCA_MAX_SYMBOL_ALLOCATION_X`).

---

## Backtesting Module (Standalone)

A separate module is now available for SMART DCA profile testing:
- `/paper/backtest`
- `/live/backtest`

It reports:
- ROI (reserved-capital and invested-capital)
- Max Drawdown (`MDD %` and absolute USDT)
- Recovery metric (bars to recover prior equity peak)
- Win rate, net PnL, avg trade duration
- Trade journal including DCA usage count per trade

Purpose:
- Compare strategy profiles before deployment.

---

## History & Analytics

Separate history pages:
- `/paper/history`
- `/live/history`

Includes:
- total trades
- win rate
- wins/losses
- net PnL
- per-trade invested/exit/PnL%/reason
- DCA done count
- date and strategy filters

---

## Main Pages

- `/` mode selector
- `/paper`
- `/paper/create`
- `/paper/smart-create`
- `/paper/backtest`
- `/paper/campaigns/{id}`
- `/paper/history`
- `/live`
- `/live/create`
- `/live/smart-create`
- `/live/backtest`
- `/live/campaigns/{id}`
- `/live/history`

---

## Environment Variables

See `.env.example`.

Core variables:
- `APP_TIMEZONE`
- `DATABASE_URL`
- `CYCLE_SECONDS`
- `PAPER_START_BALANCE`
- `ENFORCE_BTC_FILTER`
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `LIVE_ENTRY_LIMIT_BUFFER_PCT`
- `LIVE_ENTRY_LIMIT_FALLBACK_MARKET`
- `DCA_NEAR_SUPPORT_PCT`
- `DCA_SUPPORT_SCORE_THRESHOLD`
- `DCA_RSI_OVERSOLD`
- `DCA_REVERSAL_MIN_CONDITIONS`
- `DCA_MAX_SYMBOL_ALLOCATION_X`
- `DCA_SCALE_1..DCA_SCALE_5`

---

## Coolify Deployment Notes

For SQLite persistence:
- mount persistent volume to `/data`
- set:
  - `DATABASE_URL=sqlite:////data/paper_trading.db`

This prevents data loss after redeploy and avoids accidental fresh DB files.

---

## Local Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:
- `http://127.0.0.1:8000`

---

This README reflects the current rebuilt platform behavior.
