# Crypto Bots v2

Smart campaign-based crypto trading platform with:
- `Paper Trading` (full simulation engine)
- `Live Mode` (real Binance execution)
- AI-driven DCA logic, loop campaigns, and strict risk controls.

---

## Our Trading Philosophy

We do **not** buy every dip.

Our approach is:
- Buy only in high-quality zones.
- Prefer confirmation over guessing.
- Scale DCA progressively, but under strict risk limits.
- Respect campaign isolation (each campaign is independent).
- Keep risk configurable per campaign (not global hardcoded risk).

Core principle:
> Quality of entries is more important than number of entries.

---

## Architecture

- Campaign-centric system (`paper` / `live`).
- Each campaign has its own:
  - symbols scope
  - TP/SL
  - DCA mode (manual or AI)
  - trend filter
  - loop behavior
- Position-level DCA state (per symbol, per rule).
- Full activity logging for every decision (open, DCA, skip, close, fail).

---

## Modes

## 1) Paper Trading

Simulates full strategy behavior with virtual wallet and closed-trade analytics.

Highlights:
- Instant campaign opening on selected symbols.
- Manual sell button per symbol.
- Recalculate DCA now.
- Campaign reset with confirmation.
- Trading history with filters and PnL breakdown.

## 2) Live Mode

Runs strategy against real Binance account.

Highlights:
- Real balance from Binance.
- Real market buys/sells.
- TP limit orders placed on Binance and re-armed after DCA average changes.
- SL enforced by engine using market close.
- Live history page separate from paper history.

---

## DCA System

## Manual DCA

User defines DCA levels (up to 5):
- `Drop %`
- `Allocation %`

## AI DCA (Smart Support-Based)

AI DCA levels are generated per symbol using market context (support-aware flow), then stored as:
- `AI Suggested DCA (Original)`
- `Current DCA (Editable)`

Per-symbol DCA is visible from campaign details.

### AI Confirmation Gate

Before executing AI DCA, engine checks confirmation conditions (configurable threshold):
- RSI oversold / RSI turn
- Reversal candle behavior
- Selling pressure weakening
- No strong support breakdown

If confirmation is weak or breakdown is detected, DCA is skipped/paused and logged.

### Strict Mode

`No Score = No DCA` can be enabled.

Meaning:
- If support score is missing, DCA will not execute.

---

## Risk Controls

- TP and SL are campaign-level and dynamic with average cost.
- DCA scaling (up to 5 levels) via environment settings.
- Max symbol exposure multiplier (`DCA_MAX_SYMBOL_ALLOCATION_X`).
- Optional BTC trend filter per campaign:
  - bearish: reduce DCA size
  - strong bearish: block new DCA entries
- Breakdown protection can pause DCA on symbol.

---

## Loop Campaigns (AI Loop)

Loop campaign keeps a target number of open symbols.

Behavior:
- scans top candidates by score
- opens missing symbols up to target
- keeps rotating as positions close
- prevents duplicate open symbol in same campaign

Rule:
- Same symbol **cannot** be opened twice while still open in that campaign.
- Reopen is allowed after close.

---

## Live TP/SL Execution Model

- Entry: real `MARKET BUY`.
- TP: real `LIMIT SELL` on Binance.
- After DCA: old TP is canceled and new TP is armed using updated average.
- SL: engine-triggered `MARKET SELL`.
- Manual sell: cancels active TP order first, then market sells.

---

## History & Analytics

Separate pages:
- `/paper/history`
- `/live/history`

Includes:
- total trades, win rate, wins/losses, net PnL
- per-trade invested/exit/PnL%/reason
- DCA done count
- filters:
  - date range (24h, 3d, 7d, 14d, 30d, 60d)
  - strategy type (Loop AI / Smart AI / Manual)

---

## Main Pages

- `/` mode selector
- `/paper` paper dashboard
- `/paper/create`
- `/paper/campaigns/{id}`
- `/paper/history`
- `/live` live dashboard
- `/live/create`
- `/live/campaigns/{id}`
- `/live/history`

---

## Environment Variables

See `.env.example`.

Key variables:
- `APP_TIMEZONE`
- `DATABASE_URL`
- `CYCLE_SECONDS`
- `PAPER_START_BALANCE`
- `ENFORCE_BTC_FILTER`
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `DCA_NEAR_SUPPORT_PCT`
- `DCA_SUPPORT_SCORE_THRESHOLD`
- `DCA_RSI_OVERSOLD`
- `DCA_REVERSAL_MIN_CONDITIONS`
- `DCA_MAX_SYMBOL_ALLOCATION_X`
- `DCA_SCALE_1..DCA_SCALE_5`

---

## Coolify Deployment Notes (Important)

Use persistent storage for SQLite.

Recommended:
- mount volume to `/data`
- set:
  - `DATABASE_URL=sqlite:////data/paper_trading.db`

Why:
- prevents data loss after redeploy
- avoids switching to empty DB files by cwd changes

---

## Local Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:
- `http://127.0.0.1:8000`

---

This README reflects the rebuilt platform and current production logic only.
