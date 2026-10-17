#!/usr/bin/env python3
"""
Portfolio backtest for the OKX bot.

Usage:
  python backtest.py --fetch                         # download OKX daily history, run core_trend
  python backtest.py --fetch --bar 1W --start 2022-02-20
                                                     # weekly bars: reproduces the published
                                                     # 2022-2026 test (+150%, -53% worst drop)
  python backtest.py --variants                      # core 0/25/50% vs always-100%
  python backtest.py --core 0.25                     # one core_trend setting
  python backtest.py --mode breakout --compare       # original breakout system

core_trend settings (lookbacks, vol window, band, rebalance days, core)
come from config.py, so the backtest always tests what the bot trades.

CSV format: ts,high,low,close   (ts = ms epoch, any row order)

Honesty note: a backtest is an upper bound on live performance, not a
forecast. Fees + slippage are modeled (0.75%/side = your 0.70% fee) but real
fills, outages and regime changes are not.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request

import config
import strategy as S

COINS = list(config.COINS)
COST_PER_SIDE = 0.0075          # 0.70% OKX AU fee + 0.05% slippage
START_EQUITY = 10_000.0


# ----------------------------------------------------------------------
# Data loading / fetching
# ----------------------------------------------------------------------
def fetch_okx(inst_id, n, bar="1D"):
    """Paginate OKX candles (public endpoint, no key needed). Confirmed bars only."""
    rows, before = {}, None
    base = "https://www.okx.com/api/v5/market/{}?instId={}&bar={}&limit={}"
    while len(rows) < n:
        ep, limit = ("candles", 300) if before is None else ("history-candles", 100)
        url = base.format(ep, inst_id, bar, limit)
        if before:
            url += f"&after={before}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)["data"]
        if not data:
            break
        for c in data:
            if c[8] == "1":                       # skip the still-open bar
                rows[int(c[0])] = (float(c[2]), float(c[3]), float(c[4]))
        before = min(int(c[0]) for c in data)
        time.sleep(0.25)
    ts_sorted = sorted(rows)[-n:]
    return {"ts": ts_sorted,
            "high": [rows[t][0] for t in ts_sorted],
            "low": [rows[t][1] for t in ts_sorted],
            "close": [rows[t][2] for t in ts_sorted]}


def fetch_okx_daily(inst_id, days):
    return fetch_okx(inst_id, days, "1D")


def load_csv(path):
    ts, hi, lo, cl = [], [], [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            ts.append(int(row["ts"]))
            hi.append(float(row["high"]))
            lo.append(float(row["low"]))
            cl.append(float(row["close"]))
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    return {"ts": [ts[i] for i in order], "high": [hi[i] for i in order],
            "low": [lo[i] for i in order], "close": [cl[i] for i in order]}


def find_data_dir(bar="1D"):
    here = os.path.dirname(os.path.abspath(__file__))
    sub = "data" if bar == "1D" else "data_1w"
    for cand in (os.path.join(here, sub), os.path.join(here, "..", sub)):
        if os.path.isdir(cand):
            return cand
    return None


# ----------------------------------------------------------------------
# Backtest
# ----------------------------------------------------------------------
def run(data, risk, max_alloc, verbose=True):
    # align on timestamps present for ALL coins
    common = set(data[COINS[0]]["ts"])
    for c in COINS[1:]:
        common &= set(data[c]["ts"])
    days = sorted(common)
    if len(days) < S.warmup_bars() + 20:
        sys.exit(f"Not enough overlapping history: {len(days)} days")

    series, ind = {}, {}
    for c in COINS:
        idx = {t: i for i, t in enumerate(data[c]["ts"])}
        rows = [idx[t] for t in days]
        series[c] = {k: [data[c][k][i] for i in rows] for k in ("high", "low", "close")}
        ind[c] = S.compute_indicators(series[c]["high"], series[c]["low"],
                                      series[c]["close"])

    cash, positions = START_EQUITY, {}
    equity_curve, trades = [], []
    peak_equity, max_dd = START_EQUITY, 0.0
    fees_paid = 0.0

    for i in range(S.warmup_bars(), len(days)):
        prices = {c: series[c]["close"][i] for c in COINS}

        # 1) manage open positions: ratchet stops, then exits
        for c in list(positions):
            pos = positions[c]
            S.update_trailing_stop(pos, prices[c], ind[c].atr_vals[i])
            if S.exit_signal(pos, ind[c], i):
                proceeds = pos.units * prices[c] * (1 - COST_PER_SIDE)
                fees_paid += pos.units * prices[c] * COST_PER_SIDE
                cash += proceeds
                trades.append({"coin": c, "entry": pos.entry_price,
                               "exit": prices[c],
                               "pnl": proceeds - pos.units * pos.entry_price})
                del positions[c]

        equity = cash + sum(p.units * prices[c] for c, p in positions.items())

        # 2) entries, best momentum first
        cands = [c for c in COINS if c not in positions and S.entry_signal(ind[c], i)]
        cands.sort(key=lambda c: (ind[c].mom[i] or 0), reverse=True)
        for c in cands:
            notional = S.position_size(equity, cash, prices[c],
                                       ind[c].atr_vals[i], risk,
                                       max_alloc_per_coin=max_alloc)
            if notional <= 0:
                continue
            units = notional / (prices[c] * (1 + COST_PER_SIDE))
            fees_paid += units * prices[c] * COST_PER_SIDE
            cash -= notional
            positions[c] = S.Position(units=units, entry_price=prices[c],
                                      peak_close=prices[c],
                                      stop=S.initial_stop(prices[c],
                                                          ind[c].atr_vals[i]),
                                      entry_ts=days[i])

        equity = cash + sum(p.units * prices[c] for c, p in positions.items())
        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, 1 - equity / peak_equity)

    # liquidate for final accounting
    final_prices = {c: series[c]["close"][-1] for c in COINS}
    for c, pos in positions.items():
        cash += pos.units * final_prices[c] * (1 - COST_PER_SIDE)
        trades.append({"coin": c, "entry": pos.entry_price,
                       "exit": final_prices[c], "pnl":
                       pos.units * (final_prices[c] - pos.entry_price)})
    final = cash

    n_days = len(equity_curve)
    years = n_days / 365.0
    cagr = (final / START_EQUITY) ** (1 / years) - 1 if years > 0.2 else None
    wins = sum(1 for t in trades if t["pnl"] > 0)

    if verbose:
        print(f"  bars tested        : {n_days} days")
        print(f"  start equity       : ${START_EQUITY:,.0f}")
        print(f"  final equity       : ${final:,.0f}  "
              f"({(final / START_EQUITY - 1) * 100:+.1f}%)")
        if cagr is not None:
            print(f"  CAGR (approx)      : {cagr * 100:+.1f}%")
        print(f"  max drawdown       : {max_dd * 100:.1f}%")
        print(f"  trades             : {len(trades)}  "
              f"(win rate {100 * wins / max(1, len(trades)):.0f}%)")
        print(f"  fees+slippage paid : ${fees_paid:,.0f}")
        # equal-weight buy & hold over the same tested bars, for context
        i0 = S.warmup_bars()
        bh = sum(series[c]["close"][-1] / series[c]["close"][i0]
                 for c in COINS) / len(COINS)
        bh_dd, bh_peak = 0.0, 0.0
        for j in range(i0, len(days)):
            v = sum(series[c]["close"][j] / series[c]["close"][i0]
                    for c in COINS) / len(COINS)
            bh_peak = max(bh_peak, v)
            bh_dd = max(bh_dd, 1 - v / bh_peak)
        print(f"  [benchmark] equal-weight buy&hold: "
              f"{(bh - 1) * 100:+.1f}%  (max DD {bh_dd * 100:.1f}%)")
    return {"final": final, "max_dd": max_dd, "trades": len(trades)}


def _ymd(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))


def run_core_trend(data, bar="1D", core=None, band=None, max_exposure=None,
                   start=None, end=None, verbose=True, label=None,
                   rebalance_weeks=None, offset=0):
    """Simulate mode core_trend with the exact settings in config.py.

    bar="1D": daily closes, lookbacks/vol in days, rebalance every
              REBALANCE_DAYS days (what the live bot does).
    bar="1W": weekly closes, lookbacks/vol converted to weeks (91/182/364 d
              -> 13/26/52 w, 84 d -> 12 w), rebalance every bar.
    Coins join once they have enough history (BNB lists later than BTC).
    Costs: COST_PER_SIDE on every unit of turnover."""
    core = config.CORE_FRACTION if core is None else core
    band = config.REBALANCE_BAND if band is None else band
    max_exposure = config.MAX_EXPOSURE if max_exposure is None else max_exposure
    if bar == "1W":
        lookbacks = tuple(round(L / 7) for L in config.TREND_LOOKBACKS)
        vol_len = round(config.VOL_LEN / 7)
        every, per_year = (rebalance_weeks or 1), 52.18
    else:
        lookbacks, vol_len = tuple(config.TREND_LOOKBACKS), config.VOL_LEN
        every = 7 * rebalance_weeks if rebalance_weeks else config.REBALANCE_DAYS
        per_year = 365.25
    need = S.core_trend_warmup(lookbacks, vol_len)

    pos = {c: {t: i for i, t in enumerate(data[c]["ts"])} for c in COINS}
    cl = {c: data[c]["close"] for c in COINS}
    dates = sorted(set().union(*[set(data[c]["ts"]) for c in COINS]))
    if end:
        dates = [t for t in dates if _ymd(t) <= end]

    def warm(c, t):
        return t in pos[c] and pos[c][t] + 1 >= need

    if start:
        i0 = next((i for i, t in enumerate(dates) if _ymd(t) >= start), None)
    else:   # first date when at least 3 coins have full history
        i0 = next((i for i, t in enumerate(dates)
                   if sum(warm(c, t) for c in COINS) >= min(3, len(COINS))), None)
    if i0 is None or not any(warm(c, dates[i0]) for c in COINS):
        sys.exit(f"Not enough history: core_trend needs {need} {bar} bars per coin. "
                 f"Run: python backtest.py --fetch" + (" --bar 1W" if bar == "1W" else ""))

    eq, w, curve, expo, turnover, fees = 1.0, {}, [], [], 0.0, 0.0
    for k, i in enumerate(range(i0, len(dates))):
        t = dates[i]
        if k > 0 and w:
            tp = dates[i - 1]
            r, nw = 0.0, {}
            for c, wc in w.items():
                g = cl[c][pos[c][t]] / cl[c][pos[c][tp]] if (t in pos[c] and tp in pos[c]) else 1.0
                r += wc * (g - 1)
                nw[c] = wc * g
            eq *= 1 + r
            w = {c: x / (1 + r) for c, x in nw.items()}
        if k == 0 or k % every == offset % every:
            live = [c for c in COINS if warm(c, t)]
            tgt, _ = S.core_trend_targets({c: cl[c] for c in live},
                                          {c: pos[c][t] for c in live}, w,
                                          core=core, lookbacks=lookbacks,
                                          vol_len=vol_len, band=band,
                                          max_exposure=max_exposure)
            trn = sum(abs(tgt.get(c, 0) - w.get(c, 0)) for c in set(tgt) | set(w))
            fees += eq * trn * COST_PER_SIDE
            eq *= 1 - trn * COST_PER_SIDE
            turnover += trn
            w = tgt
        curve.append((t, eq))
        expo.append(sum(w.values()))

    peak, dd = 0, 0
    for _, e in curve:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    yrs = (curve[-1][0] - curve[0][0]) / (86400000 * 365.25)
    final = curve[-1][1]

    # equal-weight buy & hold of the coins available at the start (no rebalancing)
    t0, t1 = dates[i0], curve[-1][0]
    bh_coins = [c for c in COINS if t0 in pos[c] and t1 in pos[c]]
    bh = sum(cl[c][pos[c][t1]] / cl[c][pos[c][t0]] for c in bh_coins) / len(bh_coins)

    if verbose:
        print(label or f"core_trend  core={core:.0%}  lookbacks={lookbacks} ({bar} bars)  "
                       f"vol={vol_len}  band={band:.0%}")
        print(f"  period             : {_ymd(t0)} -> {_ymd(t1)}  ({yrs:.1f} years)")
        print(f"  return             : {(final - 1) * 100:+.1f}%"
              + (f"   ({(final ** (1 / yrs) - 1) * 100:+.1f}% a year)" if yrs > 0.9 else ""))
        print(f"  worst drop         : {dd * 100:.1f}%")
        print(f"  avg / max invested : {100 * sum(expo) / len(expo):.0f}% / {100 * max(expo):.0f}%")
        print(f"  turnover per year  : {turnover / max(yrs, 1e-9):.2f}x equity")
        print(f"  fees+slippage      : ${fees * START_EQUITY:,.0f} on ${START_EQUITY:,.0f} start")
        print(f"  [benchmark] equal-weight buy & hold ({', '.join(c.split('-')[0] for c in bh_coins)}), "
              f"no fees: {(bh - 1) * 100:+.1f}%")
        # calendar-year returns
        years, prev = {}, 1.0
        for ts_, e in curve:
            years[_ymd(ts_)[:4]] = e
        line, base = [], 1.0
        for y in sorted(years):
            line.append(f"{y} {(years[y] / base - 1) * 100:+.0f}%")
            base = years[y]
        print("  by calendar year   : " + "  ".join(line))
    return {"final": final, "max_dd": dd, "years": yrs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=0.02,
                    help="fraction of equity risked per trade (default 0.02)")
    ap.add_argument("--max-alloc", type=float, default=0.30,
                    help="max fraction of equity per coin (default 0.30)")
    ap.add_argument("--fetch", action="store_true",
                    help="download candles from OKX instead of using CSVs")
    ap.add_argument("--days", type=int, default=2200,
                    help="bars of history to fetch (with --fetch)")
    ap.add_argument("--compare", action="store_true",
                    help="run a 2%% vs 15%% risk comparison (breakout)")
    ap.add_argument("--mode", choices=["core_trend", "breakout"],
                    default="core_trend")
    ap.add_argument("--core", type=float, default=None,
                    help="core_trend: always-held fraction (default: config)")
    ap.add_argument("--bar", choices=["1D", "1W"], default="1D",
                    help="core_trend: daily (live bot) or weekly bars")
    ap.add_argument("--start", default=None, help="first rebalance date YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="last date YYYY-MM-DD")
    ap.add_argument("--rebalance-weeks", type=int, default=None,
                    help="core_trend: trade every N weeks (default: config)")
    ap.add_argument("--variants", action="store_true",
                    help="core_trend: compare core 0/25/50%% and rebalanced B&H")
    args = ap.parse_args()

    if args.fetch:
        data = {}
        for c in COINS:
            print(f"fetching {c} ...")
            data[c] = fetch_okx(c, args.days, args.bar if args.mode == "core_trend" else "1D")
            ddir = "data" if args.bar == "1D" else "data_1w"
            os.makedirs(ddir, exist_ok=True)
            with open(f"{ddir}/{c}.csv", "w") as f:
                f.write("ts,high,low,close\n")
                for j, t in enumerate(data[c]["ts"]):
                    f.write(f"{t},{data[c]['high'][j]},{data[c]['low'][j]},"
                            f"{data[c]['close'][j]}\n")
    else:
        d = find_data_dir(args.bar if args.mode == "core_trend" else "1D")
        if not d:
            sys.exit("No data dir found. Run with --fetch to download candles.")
        data = {c: load_csv(os.path.join(d, f"{c}.csv")) for c in COINS}

    print("\nData: " + ", ".join(
        f"{c.split('-')[0]} {_ymd(data[c]['ts'][0])}..{_ymd(data[c]['ts'][-1])}" for c in COINS) + "\n")

    print(f"cost per side: {COST_PER_SIDE * 100:.2f}%\n")
    if args.mode == "core_trend":
        if args.variants:
            for lab, core, band in (("Trend+vol only (core 0%)", 0.0, 0.05),
                                    ("Core 25% + trend 75%", 0.25, 0.05),
                                    ("Core 50% + trend 50% (default)", 0.5, 0.05),
                                    ("Always 100%: inverse-vol hold, weekly rebal", 1.0, 0.05)):
                run_core_trend(data, bar=args.bar, core=core, band=band,
                               start=args.start, end=args.end, label=lab)
                print()
        else:
            run_core_trend(data, bar=args.bar, core=args.core,
                           start=args.start, end=args.end,
                           rebalance_weeks=args.rebalance_weeks)
        return
    if args.compare:
        for label, risk, cap in (("Recommended: 2% risk, 30% cap", 0.02, 0.30),
                                 ("Requested:  15% risk, 100% cap", 0.15, 1.00)):
            print(label)
            run(data, risk, cap)
            print()
    else:
        print(f"risk={args.risk:.0%}  max_alloc={args.max_alloc:.0%}")
        run(data, args.risk, args.max_alloc)


if __name__ == "__main__":
    main()
