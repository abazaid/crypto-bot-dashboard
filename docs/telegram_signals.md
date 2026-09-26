# Telegram Signals pool

Page: `/live/signals`. Follows one public Telegram channel (default `@signal252`,
"Shaban Signals") with an isolated pool that shares the AI Trader ledger code
(`ai_pool_service`, pool `kind = "telegram"`).

## Setup

1. Environment (Coolify → Environment Variables, then redeploy):
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (from https://my.telegram.org → API development tools),
   `TELEGRAM_SIGNAL_CHANNEL=signal252`. Optional: `TELEGRAM_ENTRY_WINDOW_HOURS` (12),
   `TELEGRAM_SESSION_PATH` (defaults to next to the SQLite DB, i.e. the persistent volume).
2. On the page: enter your phone → code from Telegram → (2FA password if enabled).
   The session file is stored on the server; never commit it (`*.session` is git-ignored).
3. Create the pool with an amount and a number of slots. **Only posts newer than the
   moment the listener connects are traded** (last message id is recorded in `app_settings`).

## Signal format understood (see `telegram_signals.py`)

```
💎 #ALICE | Binance
📍 الدخول: 0.1480 – 0.1556
🎯 الأهداف:
1️⃣ 0.171 | +9.9% | بيع 20%
...
🛑 الستوب: إغلاق 4 ساعات أسفل 0.137
```
Zone order may be reversed, symbols may start with a digit (`#0G`), edits (✅ marks)
are ignored because the message id was already processed. Non-Binance posts and posts
whose symbol is not tradable on the account are recorded but never executed.

## Execution rules

| Rule | Value |
|------|-------|
| Entry | market buy when `stop < price <= entry_high` (never chases); otherwise pending up to 12h, then `missed` |
| Size | `equity / slots`, capped by cash; skipped when cash < min order |
| Targets | channel fractions (20/25/25/30%); slices under Binance's 5 USDT minimum are raised to the minimum |
| Stop after target 1 | breakeven + fees |
| Stop after target n | price of target n-1 |
| Between targets | give-back guard (`profit_giveback_pct`, default 50%) |
| Initial stop | channel level, immediate (no waiting for a 4h close) |
| Breakers | same daily-loss / drawdown / API-error breakers as the AI pool |

## Loops

* Listener (Telethon, asyncio in the FastAPI process): new posts → `ingest_message` in a thread.
  On (re)connect it catches up on posts missed while down, at most `TELEGRAM_ENTRY_WINDOW_HOURS` old.
* `ai_pool_tick` (5s): stops / targets / guard for `strategy == "telegram"` positions.
* `ai_pool_scan` (5min): retries pending entries and expires them.

## Tests

```bash
pytest tests/test_telegram_signals.py tests/test_telegram_signal_service.py -q
```

## Split entry and target-stop settings (added 2026-09-26)

* `entry_split_pct` (50): share bought immediately inside the zone; the rest waits at the zone bottom and is
  cancelled once the first target is hit, after 72h, or when the stop fires. Both legs must clear the 5 USDT minimum.
* `target_lock_pct` (50): after target n the stop = previous level + this % of the leg (never below breakeven).
  0 = stop exactly at the previous target. The audit page sweeps 0/25/50/75 to pick the value for this channel.
* `last_target_sell_pct` (50): share of the last slice sold at the last target; the rest is a runner.
* `runner_giveback_pct` (30): the runner trails this far below its peak.

## Several channels (added 2026-09-26)

* `TELEGRAM_SIGNAL_CHANNELS=signal252,Ox3rwah_eth` (comma list; `TELEGRAM_SIGNAL_CHANNEL` still works for one).
* One pool per channel (`ai_pools.channel`); the page shows a tab per channel (`/live/signals?channel=...`).
  A pool created before this change follows the first channel in the list.
* Formats: v1 (zone entry, `telegram_signals.py`) and v2 (`telegram_signals_v2.py`: "دخول فوري" price,
  optional "دخول ثاني" price, N targets without sell fractions -> equal split, "وقف"). `parse_any` tries both.
* v2 execution: market entry accepted up to 1.5% above the posted price (never chases further); the second
  leg waits exactly at the channel's second-entry price; progress/average/level posts are ignored.
* `/live/signals/preview?channel=<name>&limit=40` shows any public channel's posts with the parser verdict.
