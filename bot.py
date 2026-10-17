#!/usr/bin/env python3
"""
OKX long-only bot - runs the configured strategy once per day.

Usage:
  python bot.py --dry-run     # compute + print decisions, place NO orders
  python bot.py               # run once (schedule via cron shortly after 00:05 UTC)
  python bot.py --loop        # keep running, acts once per UTC day
  python bot.py --force       # core_trend: rebalance now, ignore the 7-day timer

Mode "core_trend" (default) rebalances weekly from actual exchange balances.
Mode "breakout" keeps per-position stops in state.json.
Orders are plain market orders only - no take-profit / stop-loss orders
are ever placed on the exchange. Signals use CONFIRMED daily candles.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import config
import okx_client as okx
import strategy as S

# apply config to strategy defaults
S.REGIME_LEN = config.REGIME_LEN
S.BREAKOUT_LEN = config.BREAKOUT_LEN
S.ATR_LEN = config.ATR_LEN
S.ATR_MULT = config.ATR_MULT
S.MOM_LEN = config.MOM_LEN

log = logging.getLogger("bot")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.LOG_FILE)])


# ---------------------------------------------------------------- state
def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "last_run_day": None}


def save_state(state):
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.STATE_FILE)


# ---------------------------------------------------------------- dispatch
def run_once(dry_run=False, force=False):
    if config.STRATEGY_MODE == "core_trend":
        return run_core_trend(dry_run=dry_run, force=force)
    if config.STRATEGY_MODE == "breakout":
        return run_breakout(dry_run=dry_run)
    raise ValueError(f"unknown STRATEGY_MODE {config.STRATEGY_MODE!r}")


# ---------------------------------------------------------------- core_trend
def _days_since(day_str):
    if not day_str:
        return 10 ** 6
    d0 = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d0).days


def run_core_trend(dry_run=False, force=False):
    """Weekly rebalance toward core + trend + inverse-vol target weights.
    Stateless with respect to positions: holdings are read from the
    exchange each run, so manual trades are simply rebalanced away."""
    mode = "DEMO" if config.DEMO_MODE else "LIVE"
    log.info("=== core_trend run | mode=%s dry_run=%s core=%.0f%% max=%.0f%% ===",
             mode, dry_run, config.CORE_FRACTION * 100, config.MAX_EXPOSURE * 100)
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("positions"):
        log.warning("state.json has breakout positions; core_trend ignores them "
                    "and rebalances from actual exchange balances")

    since = _days_since(state.get("last_rebalance"))
    if since < config.REBALANCE_DAYS and not force:
        log.info("last rebalance %s (%d days ago) - next in %d day(s); nothing to do",
                 state.get("last_rebalance"), since, config.REBALANCE_DAYS - since)
        state["last_run_day"] = today
        if not dry_run:
            save_state(state)
        return

    # 1) signals from confirmed daily closes
    need = S.core_trend_warmup(config.TREND_LOOKBACKS, config.VOL_LEN)
    closes, idx = {}, {}
    for c in config.COINS:
        h = okx.get_daily_history(c, config.CANDLES_NEEDED_CT)
        if len(h["close"]) < need:
            # never trade on partial data: a missing coin would get a 0% target
            # and be sold off. Abort and retry on the next run instead.
            log.error("%s: only %d daily candles, need %d - aborting this run, "
                      "nothing traded", c, len(h["close"]), need)
            return
        closes[c], idx[c] = h["close"], len(h["close"]) - 1

    # 2) account snapshot at live prices
    try:
        balances = okx.get_balances()
    except okx.OkxError as e:
        if not dry_run:
            raise
        log.warning("dry-run without credentials (%s); assuming 1,000 USDT", e)
        balances = {"USDT": 1000.0}
    prices = {c: okx.get_ticker(c) for c in config.COINS}
    usdt = balances.get("USDT", 0.0)
    held = {c: balances.get(c.split("-")[0], 0.0) for c in config.COINS}
    equity = usdt + sum(held[c] * prices[c] for c in config.COINS)
    if equity <= 0:
        log.error("zero equity, aborting")
        return
    cur_w = {c: held[c] * prices[c] / equity for c in config.COINS}

    tgt, info = S.core_trend_targets(
        closes, idx, cur_w, core=config.CORE_FRACTION,
        lookbacks=config.TREND_LOOKBACKS, vol_len=config.VOL_LEN,
        band=config.REBALANCE_BAND, max_exposure=config.MAX_EXPOSURE)

    log.info("equity=%.2f USDT | invested now %.1f%% -> target %.1f%%",
             equity, 100 * sum(cur_w.values()), 100 * sum(tgt.values()))
    for c in config.COINS:
        s, v, raw = info.get(c, (None, None, 0.0))
        log.info("  %-9s trend=%s vol=%s  now %5.1f%%  target %5.1f%% (raw %5.1f%%)",
                 c, "n/a" if s is None else f"{s:.1f}",
                 "n/a" if v is None else f"{v * 100:.0f}%",
                 100 * cur_w[c], 100 * tgt.get(c, 0.0), 100 * raw)

    # 3) sells first (raise cash), then buys
    orders = []
    for c in config.COINS:
        delta = (tgt.get(c, 0.0) - cur_w[c]) * equity
        if delta < -config.MIN_NOTIONAL_USDT:
            amount = held[c] if tgt.get(c, 0.0) == 0 else min(held[c], -delta / prices[c])
            orders.append(("SELL", c, amount, -delta))
    failed = []
    for side, c, amount, usd in orders:
        log.info("SELL %s %.8f (~%.2f USDT, fee ~%.2f)", c, amount, usd,
                 usd * config.FEE_RATE)
        if dry_run:
            continue
        try:
            inst = okx.get_instrument(c)
            if amount < inst["minSz"]:
                log.warning("SKIP sell %s: %.8f below OKX minimum %s", c, amount, inst["minSz"])
                continue
            okx.market_sell(c, amount, inst["lotSz"])
        except okx.OkxError as e:
            log.error("SELL %s failed: %s", c, e)
            failed.append(c)
    if orders and not dry_run:
        time.sleep(2)
        usdt = okx.get_balances().get("USDT", 0.0)
    elif orders:
        usdt += sum(o[3] for o in orders) * (1 - config.FEE_RATE)

    cash = usdt * 0.995          # small buffer so buys never exceed cash
    for c in config.COINS:
        delta = (tgt.get(c, 0.0) - cur_w[c]) * equity
        if delta > config.MIN_NOTIONAL_USDT:
            usd = min(delta, cash)
            if usd < config.MIN_NOTIONAL_USDT:
                log.info("SKIP buy %s: only %.2f USDT cash left", c, cash)
                continue
            log.info("BUY  %s ~%.2f USDT (fee ~%.2f)", c, usd, usd * config.FEE_RATE)
            if not dry_run:
                try:
                    okx.market_buy(c, usd)
                except okx.OkxError as e:
                    log.error("BUY %s failed: %s", c, e)
                    failed.append(c)
                    continue
            cash -= usd

    if failed:
        # keep the weekly timer unchanged so tomorrow's run tries again
        state["last_run_day"] = today
        if not dry_run:
            save_state(state)
        log.error("=== rebalance incomplete: %s failed - will retry next run ===",
                  ", ".join(failed))
        return

    state["last_rebalance"] = today
    state["last_run_day"] = today
    state["last_targets"] = tgt
    if not dry_run:
        save_state(state)
    log.info("=== rebalance complete ===")


# ---------------------------------------------------------------- breakout
def run_breakout(dry_run=False):
    mode = "DEMO" if config.DEMO_MODE else "LIVE"
    log.info("=== run start | mode=%s dry_run=%s risk=%.1f%% ===",
             mode, dry_run, config.RISK_PER_TRADE * 100)

    state = load_state()
    positions = state["positions"]

    # 1) market data + indicators (confirmed daily candles only)
    ind, last_close = {}, {}
    for c in config.COINS:
        candles = okx.get_daily_candles(c, config.CANDLES_NEEDED)
        if len(candles["close"]) < S.warmup_bars() + 1:
            log.warning("%s: only %d confirmed candles, need %d - skipping",
                        c, len(candles["close"]), S.warmup_bars() + 1)
            continue
        ind[c] = S.compute_indicators(candles["high"], candles["low"],
                                      candles["close"])
        last_close[c] = candles["close"][-1]
    if not ind:
        log.error("no usable market data, aborting")
        return

    # 2) account snapshot (dry-run without keys pretends 10k USDT cash)
    try:
        balances = okx.get_balances()
    except okx.OkxError as e:
        if not dry_run:
            raise
        log.warning("dry-run without credentials (%s); assuming 10k USDT", e)
        balances = {"USDT": 10_000.0}
    usdt = balances.get("USDT", 0.0)
    pos_value = 0.0
    for c, p in positions.items():
        if c in last_close:
            pos_value += p["units"] * last_close[c]
    equity = usdt + pos_value
    log.info("equity=%.2f USDT (cash=%.2f, positions=%.2f)",
             equity, usdt, pos_value)

    # reconcile: warn if exchange balance disagrees with state
    for c, p in positions.items():
        base = c.split("-")[0]
        have = balances.get(base, 0.0)
        if have < p["units"] * 0.99:
            log.warning("%s: state says %.6f but exchange has %.6f - "
                        "was something traded manually?", c, p["units"], have)

    # 3) exits first
    for c in list(positions):
        if c not in ind:
            continue
        i = len(ind[c].closes) - 1
        p = positions[c]
        pos = S.Position(units=p["units"], entry_price=p["entry_price"],
                         peak_close=p["peak_close"], stop=p["stop"])
        S.update_trailing_stop(pos, last_close[c], ind[c].atr_vals[i])
        p.update(peak_close=pos.peak_close, stop=pos.stop)
        if S.exit_signal(pos, ind[c], i):
            reason = ("stop" if last_close[c] < pos.stop else "regime off")
            log.info("EXIT %s @ ~%.4f (%s) selling %.8f",
                     c, last_close[c], reason, pos.units)
            if not dry_run:
                base = c.split("-")[0]
                amount = min(pos.units, balances.get(base, pos.units))
                inst = okx.get_instrument(c)
                okx.market_sell(c, amount, inst["lotSz"])
            del positions[c]
        else:
            log.info("HOLD %s close=%.4f stop=%.4f peak=%.4f",
                     c, last_close[c], pos.stop, pos.peak_close)

    # 4) entries, strongest momentum first
    cash = usdt
    cands = [c for c in ind
             if c not in positions
             and S.entry_signal(ind[c], len(ind[c].closes) - 1)]
    cands.sort(key=lambda c: (ind[c].mom[len(ind[c].closes) - 1] or 0),
               reverse=True)
    for c in cands:
        i = len(ind[c].closes) - 1
        notional = S.position_size(
            equity, cash, last_close[c], ind[c].atr_vals[i],
            config.RISK_PER_TRADE, atr_mult=config.ATR_MULT,
            max_alloc_per_coin=config.MAX_ALLOC_PER_COIN,
            min_notional=config.MIN_NOTIONAL_USDT)
        if notional <= 0:
            log.info("SKIP %s: breakout signal but size too small "
                     "(cash=%.2f)", c, cash)
            continue
        units_est = notional / last_close[c]
        log.info("ENTER %s: buy ~%.2f USDT (~%.8f units) @ ~%.4f mom=%.1f%%",
                 c, notional, units_est, last_close[c],
                 100 * (ind[c].mom[i] or 0))
        if not dry_run:
            okx.market_buy(c, notional)
        cash -= notional
        positions[c] = {
            "units": units_est,   # refined against balance on next run
            "entry_price": last_close[c],
            "peak_close": last_close[c],
            "stop": S.initial_stop(last_close[c], ind[c].atr_vals[i],
                                   config.ATR_MULT),
            "entry_ts": int(time.time() * 1000),
        }

    # refine units from actual exchange balances (fees shrink fills)
    if not dry_run and positions:
        time.sleep(2)
        balances = okx.get_balances()
        for c, p in positions.items():
            base = c.split("-")[0]
            have = balances.get(base)
            # adopt the exchange's number when it's in the same ballpark
            if have and abs(have - p["units"]) / max(p["units"], 1e-9) < 0.20:
                p["units"] = have

    state["last_run_day"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not dry_run:
        save_state(state)
    log.info("=== run complete | %d open position(s) ===", len(positions))


def loop():
    """Act once per UTC day, shortly after the daily candle closes."""
    while True:
        state = load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("last_run_day") != today:
            try:
                run_once(dry_run=False)
            except Exception:
                log.exception("run failed; will retry in 15 min")
                time.sleep(900)
                continue
        time.sleep(300)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute decisions but place no orders")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, acting once per UTC day")
    ap.add_argument("--force", action="store_true",
                    help="core_trend: rebalance now even if < REBALANCE_DAYS")
    args = ap.parse_args()

    setup_logging()
    if not config.DEMO_MODE:
        log.warning("LIVE TRADING MODE - real money at risk")
        time.sleep(5)
    if args.loop:
        loop()
    else:
        run_once(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
