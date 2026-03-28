# Crypto Bots v2 (Paper Trading First)

This is a fully restructured platform focused on Paper Trading first, with isolated campaign logic and modern risk controls.

## Current Scope

- `Paper Trading` section: implemented.
- `Live Mode` section: isolated placeholder for next phase.
- Multi-campaign architecture: each campaign is independent.

## Core Features Implemented

- Create multiple campaigns.
- Select Binance symbols via search and open positions instantly at current market price.
- Initial investment per symbol is fixed at campaign creation.
- Campaign-level risk settings:
  - Take Profit (%)
  - Stop Loss (%)
  - DCA rules
  - AI DCA mode
  - BTC Trend Filter (optional, per campaign)
- Closed-trade performance tracking with detailed history.

## Strategies and Execution Logic

## 1) Manual DCA

User-defined DCA levels (up to 3 levels):

- Drop %
- Allocation % of initial entry amount

On trigger, the engine executes DCA buy and recalculates average cost.

## 2) AI DCA

When enabled for a campaign:

- Manual DCA input is disabled at creation.
- The system auto-generates DCA levels using:
  - Pivot supports (S1/S2/S3)
  - EMA50 / EMA100 / EMA200
  - BTC trend profile (bullish/neutral/bearish)
- Suggested AI rules are stored in the campaign as the original proposal.
- Campaign details show:
  - `AI Suggested DCA (Original)`
  - `Current DCA (Editable)` for manual override

## 3) AI DCA Entry Confirmation Filter

Before executing AI DCA, the engine validates:

- RSI in oversold zone
- No strong breakdown
- Reversal candle signal (Hammer or Bullish Engulfing)
- Selling volume weakening

If rejected, event `AI_DCA_SKIP` is logged.

## 4) BTC Trend Filter (Optional Per Campaign)

Campaign-level checkbox.

- If BTC is `strong_bearish`: block DCA buys for that campaign.
- If BTC is `bearish`: reduce DCA allocation to 50% for that campaign.
- If BTC is `neutral` or `bullish`: normal DCA behavior.

If blocked, event `TREND_FILTER_SKIP` is logged.

## 5) Exit Logic (TP/SL)

- TP and SL are evaluated against current average cost.
- On hit, position closes and records:
  - close reason (`TP` / `SL`)
  - realized PnL (amount and percentage in UI)

## Campaign Editing (Post-Creation)

Allowed edits:

- Take Profit %
- Stop Loss %
- DCA rules

Locked fields:

- Initial investment amount
- Selected symbols

After DCA edits, open-position DCA states are synchronized safely.

## UI/UX Upgrades

- New clean visual style.
- Profit/Loss color coding:
  - positive = green
  - negative = red
- Campaign detail table includes:
  - initial buy price
  - current average buy price
  - target sell price (TP)
  - PnL amount and %
- Trading History page with per-trade:
  - amount
  - percentage
  - reason
  - timestamps

## Available Pages

- `/` mode selection
- `/paper` paper dashboard
- `/paper/campaigns/{id}` campaign details
- `/paper/history` trading history
- `/live` isolated live section placeholder

## Run Locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:

`http://127.0.0.1:8000`

## Environment Variables

See `.env.example`.

Currently used variables:

- `APP_TIMEZONE`
- `DATABASE_URL`
- `CYCLE_SECONDS`
- `PAPER_START_BALANCE`
- `ENFORCE_BTC_FILTER`

---

This README documents only the new v2 architecture and removes legacy project documentation.
