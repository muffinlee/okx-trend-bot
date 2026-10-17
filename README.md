# OKX Long-Only Bot — Core + Trend + Volatility

Holds BTC, ETH, SOL and BNB against USDT on OKX spot. The default strategy
(`STRATEGY_MODE = "core_trend"`) combines the three approaches with the best
published evidence for a long-only spot account:

1. **Buy & hold core** — half of each coin's slot (`CORE_FRACTION = 0.5`) is
   always held, so the bot never sits entirely in cash during a recovery.
2. **Trend following** — the other half scales with a trend score: the share
   of three lookbacks (~3, 6, 12 months) over which the coin is up.
   Score 1.0 = full slot, 0.0 = core only.
3. **Volatility weighting** — slots are sized by inverse volatility (84 days),
   so a wild coin like SOL gets a smaller slot than BTC and each coin adds
   similar risk.

`weight = slot × (core + (1 − core) × trend_score)`. Total exposure is
**100% when every coin is in a full uptrend**, 50% at the minimum, ~76% on
average in testing. Spot only, no leverage, never above 100%.

The bot rebalances **once a week** and skips any adjustment smaller than 5% of
equity, because at your 0.70% fee every trade costs real money. Expected
turnover is ~1.5–2× equity per year (≈1–1.5% of equity in fees).

It places **plain market orders only** — no take-profit, stop-loss or other
conditional orders are ever sent to OKX.

## Evidence (approximate, weekly OKX data, 0.75%/side costs, Feb 2022 – Sep 2026)

| variant | return | max drawdown | avg exposure |
|---|---|---|---|
| Equal-weight buy & hold | +38% | 76% | 100% |
| Always 100%, monthly rebalance | +106% | 78% | 100% |
| Core 50% + trend/vol, 1–12 month lookbacks (previous) | +118% | 54% | 76% |
| **Core 50% + trend/vol, 3/6/12 month lookbacks (current)** | **+150%** | **53%** | **~75%** |
| Core 25% + trend/vol | +134% | 41% | 65% |
| Old breakout bot | +62% | 26% | 29% |

Core 25% scored better over the full period; core 50% was better in the
2023–24 bull run and the last 26 weeks. The gap is within the noise of one
sample. Set `CORE_FRACTION` to your preference. Re-run on daily data with:

```bash
python3 backtest.py --fetch --bar 1W --start 2022-02-20   # reproduces +150% / 53% (weekly bars)
python3 backtest.py --fetch                               # same rules on daily bars, as the bot trades
```

## Files

| file | purpose |
|---|---|
| `strategy.py` | pure strategy logic, shared by backtest and bot |
| `backtest.py` | daily simulation against OKX candles (0.75%/side costs) |
| `bot.py` | the live/demo runner |
| `okx_client.py` | minimal signed OKX v5 REST client |
| `config.py` | all settings: mode, core fraction, demo/live switch |

## Setup

1. **API key** — Read + Trade permissions only, never Withdraw. For demo,
   create the key inside OKX *Demo trading*.
2. **Export credentials**:
   ```bash
   export OKX_API_KEY="..."; export OKX_API_SECRET="..."; export OKX_API_PASSPHRASE="..."
   ```
3. **Backtest** — `python3 backtest.py --fetch --days 1500 --variants`
4. **Dry run** — `python3 bot.py --dry-run` prints current vs target weights
   and the orders it would place.
5. **Run** — `python3 bot.py --loop`, or cron daily at 00:10 UTC:
   ```
   10 0 * * * cd /path/to/okx-trend-bot && /usr/bin/python3 bot.py >> cron.log 2>&1
   ```
   It checks daily but only trades every 7 days (`--force` to rebalance now).
6. **Live** — set the environment variable `OKX_DEMO=false`.

## Deploying on Railway (replacing the old signal executor)

1. Stop/delete the old service running `okx_spot_executor.py` and its
   signal engine, then cancel any open TP/SL orders in the OKX app.
2. Create a service from this folder. The `Procfile` runs
   `python bot.py --loop` as a worker (no web port needed).
3. Add a Volume mounted at `/data` (state + log live there).
4. Variables: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`,
   `OKX_DEMO=false`. `OKX_BASE_URL` defaults to `https://app.okx.com`
   (Australian accounts).
5. The first run rebalances the whole trading account to target at once.

## Things to know

- **The bot manages the whole trading account.** It reads your USDT, BTC,
  ETH, SOL and BNB balances from the exchange each week and rebalances them
  to target, so manual trades in those coins will be traded back.
  Coins in Earn/funding accounts are not seen; move them to the trading
  account if you want the bot to manage them. Use a sub-account if you
  want to keep separate manual holdings.
- **Turn off any other bot or conditional orders** on these coins first.
- Drawdowns of 40–55% are possible with a 50% core. That is the price of
  being up to 100% invested.
- Each coin is only adjusted when it drifts more than 5 points, so total
  exposure can sit up to ~10 points below target between rebalances.
- Backtests are an upper bound, not a forecast. Not financial advice.
- `STRATEGY_MODE = "breakout"` restores the original breakout system.
