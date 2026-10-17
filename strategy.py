"""
Hybrid long-only trend/momentum strategy for crypto spot.

Rules (evaluated once per day on the daily close):
  Regime filter : only hold/enter while close > SMA(REGIME_LEN).
  Entry         : close breaks above the highest close of the prior
                  BREAKOUT_LEN days (Donchian breakout) while regime is on.
  Ranking       : if more entry signals than available cash, prefer the
                  coins with the strongest MOM_LEN-day momentum.
  Exit          : close falls below the chandelier trailing stop
                  (highest close since entry - ATR_MULT * ATR(ATR_LEN)),
                  or the regime filter turns off. Stop only ratchets up.
  Sizing        : risk RISK_PER_TRADE of current equity per position,
                  units = risk_amount / (ATR_MULT * ATR). Notional capped
                  at MAX_ALLOC_PER_COIN of equity and by available cash
                  (spot, no leverage).

The default mode, "core_trend", is defined at the bottom of this file:
a buy-and-hold core plus a multi-lookback trend tilt with inverse-
volatility weights, rebalanced weekly, up to 100% invested. The breakout
rules above remain available as mode "breakout".

This module is pure logic - no I/O, no exchange calls - so the backtest
and the live bot run exactly the same code.
"""

import math
from dataclasses import dataclass, field
from typing import Optional


# ----------------------------------------------------------------------
# Default parameters (overridden by config.py in the live bot)
# ----------------------------------------------------------------------
REGIME_LEN = 100      # SMA length for the bull/bear regime filter
BREAKOUT_LEN = 55     # Donchian lookback for entries (prior N closes)
ATR_LEN = 14          # ATR length
ATR_MULT = 2.5        # trailing-stop distance in ATRs
MOM_LEN = 90          # momentum ranking lookback


# ----------------------------------------------------------------------
# Indicators (plain lists, oldest -> newest; None while warming up)
# ----------------------------------------------------------------------
def sma(values, length):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i - length]
        if i >= length - 1:
            out[i] = s / length
    return out


def atr(highs, lows, closes, length):
    """Wilder's ATR."""
    n = len(closes)
    out = [None] * n
    trs = []
    prev_atr = None
    for i in range(n):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
        trs.append(tr)
        if i == length - 1:
            prev_atr = sum(trs) / length
            out[i] = prev_atr
        elif i >= length:
            prev_atr = (prev_atr * (length - 1) + tr) / length
            out[i] = prev_atr
    return out


def rolling_max(values, length):
    """Highest of the PRIOR `length` values (excludes current bar)."""
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= length:
            out[i] = max(values[i - length:i])
    return out


def momentum(closes, length):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i >= length and closes[i - length] > 0:
            out[i] = closes[i] / closes[i - length] - 1.0
    return out


# ----------------------------------------------------------------------
# Per-coin indicator bundle
# ----------------------------------------------------------------------
@dataclass
class CoinIndicators:
    closes: list
    sma_regime: list
    atr_vals: list
    donchian: list
    mom: list


def compute_indicators(highs, lows, closes,
                       regime_len=REGIME_LEN, breakout_len=BREAKOUT_LEN,
                       atr_len=ATR_LEN, mom_len=MOM_LEN) -> CoinIndicators:
    return CoinIndicators(
        closes=closes,
        sma_regime=sma(closes, regime_len),
        atr_vals=atr(highs, lows, closes, atr_len),
        donchian=rolling_max(closes, breakout_len),
        mom=momentum(closes, mom_len),
    )


def warmup_bars(regime_len=REGIME_LEN, breakout_len=BREAKOUT_LEN,
                atr_len=ATR_LEN, mom_len=MOM_LEN):
    return max(regime_len, breakout_len + 1, atr_len + 1, mom_len + 1)


# ----------------------------------------------------------------------
# Signal evaluation at bar i
# ----------------------------------------------------------------------
@dataclass
class Position:
    units: float
    entry_price: float
    peak_close: float
    stop: float
    entry_ts: Optional[int] = None


def regime_on(ind: CoinIndicators, i: int) -> bool:
    return ind.sma_regime[i] is not None and ind.closes[i] > ind.sma_regime[i]


def entry_signal(ind: CoinIndicators, i: int) -> bool:
    """Breakout above prior N-day closing high while in a bull regime."""
    if ind.donchian[i] is None or ind.atr_vals[i] is None:
        return False
    return regime_on(ind, i) and ind.closes[i] > ind.donchian[i]


def update_trailing_stop(pos: Position, close: float, atr_now: float,
                         atr_mult=ATR_MULT) -> Position:
    """Ratchet the chandelier stop upward; never lower it."""
    pos.peak_close = max(pos.peak_close, close)
    candidate = pos.peak_close - atr_mult * atr_now
    pos.stop = max(pos.stop, candidate)
    return pos


def exit_signal(pos: Position, ind: CoinIndicators, i: int) -> bool:
    if ind.closes[i] < pos.stop:
        return True
    if not regime_on(ind, i):          # bear regime -> get out, stay out
        return True
    return False


def position_size(equity: float, cash: float, price: float, atr_now: float,
                  risk_per_trade: float, atr_mult=ATR_MULT,
                  max_alloc_per_coin: float = 0.30,
                  min_notional: float = 10.0) -> float:
    """Return notional (quote ccy) to buy, 0 if not worth trading."""
    if atr_now is None or atr_now <= 0 or price <= 0:
        return 0.0
    stop_distance = atr_mult * atr_now
    units = (risk_per_trade * equity) / stop_distance
    notional = units * price
    notional = min(notional, max_alloc_per_coin * equity, cash)
    if notional < min_notional:
        return 0.0
    return notional


def initial_stop(price: float, atr_now: float, atr_mult=ATR_MULT) -> float:
    return price - atr_mult * atr_now


# ======================================================================
# Mode "core_trend": buy-and-hold core + trend tilt + inverse-vol weights
# ======================================================================
# Combines three evidence-backed ideas:
#   (3) buy & hold core : CORE_FRACTION of each coin's slot is always held
#   (1) trend following : the rest scales with the trend signal = share of
#                         lookbacks (~3, 6, 12 months) with a positive
#                         return -> 0.0, 0.33, 0.67, 1.0
#   (2) volatility      : coins get inverse-volatility slots, so calmer coins
#                         get more weight and every coin adds similar risk
# weight_i = slot_i * (CORE + (1 - CORE) * signal_i),  slot_i = (1/vol_i) / sum(1/vol)
# -> 100% invested when every coin is in a full uptrend, CORE% at minimum.
# Rebalanced weekly; changes smaller than the band are skipped to save fees.
CT_LOOKBACKS = (91, 182, 364)   # ~3, 6, 12 months
CT_VOL_LEN = 84           # days of returns used for volatility (~12 weeks)
CT_CORE = 0.5             # always-held fraction of each coin's slot
CT_BAND = 0.05            # skip trades that move a weight by < 5 points
CT_MAX_EXPOSURE = 1.0     # total weights never exceed 100% (spot, no leverage)


def core_trend_warmup(lookbacks=CT_LOOKBACKS, vol_len=CT_VOL_LEN):
    return max(max(lookbacks), vol_len) + 1


def trend_signal(closes, i, lookbacks=CT_LOOKBACKS):
    if i < max(lookbacks):
        return None
    return sum(1 for L in lookbacks if closes[i] > closes[i - L]) / len(lookbacks)


def realized_vol(closes, i, n=CT_VOL_LEN):
    """Annualized stdev of daily log returns over the last n days."""
    if i < n:
        return None
    r = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    m = sum(r) / n
    return math.sqrt(sum((x - m) ** 2 for x in r) / n) * math.sqrt(365)


def core_trend_targets(closes_by_coin, idx_by_coin, held_weights,
                       core=CT_CORE, lookbacks=CT_LOOKBACKS, vol_len=CT_VOL_LEN,
                       band=CT_BAND, max_exposure=CT_MAX_EXPOSURE):
    """Return ({coin: target weight}, {coin: (signal, vol)}). Sum <= max_exposure."""
    sig, inv = {}, {}
    for c, closes in closes_by_coin.items():
        i = idx_by_coin[c]
        s, v = trend_signal(closes, i, lookbacks), realized_vol(closes, i, vol_len)
        if s is None or not v:
            continue
        sig[c], inv[c] = s, 1.0 / v
    tot_inv = sum(inv.values())
    tgt, info = {}, {}
    for c in sig:
        w = max_exposure * inv[c] / tot_inv * (core + (1 - core) * sig[c])
        info[c] = (sig[c], 1.0 / inv[c], w)
        old = held_weights.get(c, 0.0)
        if abs(w - old) < band:      # not worth 0.7% fees to adjust
            w = old
        if w > 1e-9:
            tgt[c] = w
    total = sum(tgt.values())
    if total > max_exposure:
        tgt = {c: w * max_exposure / total for c, w in tgt.items()}
    return tgt, info
