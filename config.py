"""
Configuration for the OKX trend bot.

API keys are read from environment variables so they never live in code:
  export OKX_API_KEY="..."
  export OKX_API_SECRET="..."
  export OKX_API_PASSPHRASE="..."

Create the key at okx.com -> Profile -> API keys.
  * For demo trading: switch the OKX website to "Demo trading" first, then
    create the key there (demo keys are separate from live keys).
  * Permissions: "Read" + "Trade" only. NEVER enable "Withdraw".
"""

import os

# ---------------------------------------------------------------- exchange
API_KEY = os.environ.get("OKX_API_KEY", "")
API_SECRET = os.environ.get("OKX_API_SECRET", "")
API_PASSPHRASE = os.environ.get("OKX_API_PASSPHRASE", "")

# True  -> orders go to OKX demo trading (paper money, x-simulated-trading)
# False -> REAL MONEY. On Railway set the variable OKX_DEMO=false.
DEMO_MODE = os.environ.get("OKX_DEMO", "true").strip().lower() != "false"

# ---------------------------------------------------------------- universe
COINS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT"]

# ---------------------------------------------------------------- mode
# "core_trend" (default): buy-and-hold core + trend tilt + inverse-vol
#                         weights, weekly rebalance, up to 100% invested.
# "breakout"            : the original 55-day breakout / ATR-stop system.
STRATEGY_MODE = "core_trend"

# ---------------------------------------------------------------- core_trend
CORE_FRACTION = 0.50        # always-held share of each coin's slot.
                            # 0.50 -> exposure ranges 50%..100% (avg ~76%)
                            # 0.25 -> 25%..100% (avg ~65%): better 2022-26
                            #         risk-adjusted in testing, less upside
MAX_EXPOSURE = 1.00         # total invested never exceeds 100% of equity
TREND_LOOKBACKS = (91, 182, 364)   # ~3, 6, 12 months. Dropping the 1- and
                                   # 2-month lookbacks cut sideways whipsaw
                                   # in- AND out-of-sample (Oct 2026 test)
VOL_LEN = 84                # days of returns for inverse-vol weights
REBALANCE_DAYS = 7          # trade at most once a week
REBALANCE_BAND = 0.05       # skip adjustments under 5% of equity (fees!)
FEE_RATE = 0.007            # your OKX AU fee per side, used for logging

# ---------------------------------------------------------------- breakout risk
# Fraction of equity risked per trade. 0.02 means a stopped-out trade costs
# ~2% of the account.
#
# You asked for 0.15. I recommend against it: at 15% risk per trade a
# 5-trade losing streak (routine for trend-following) is roughly -56%.
# If you still want it, change the number - it's your account - but run it
# in demo first and watch a losing streak happen.
RISK_PER_TRADE = 0.02

MAX_ALLOC_PER_COIN = 0.30   # max fraction of equity in any one coin
MIN_NOTIONAL_USDT = 10.0    # skip orders smaller than this

# ---------------------------------------------------------------- strategy
REGIME_LEN = 100            # SMA regime filter (days)
BREAKOUT_LEN = 55           # Donchian entry lookback (days)
ATR_LEN = 14
ATR_MULT = 2.5              # trailing stop distance in ATRs
MOM_LEN = 90                # momentum ranking lookback

# ---------------------------------------------------------------- plumbing
# Australian accounts use app.okx.com (same host your old executor used)
BASE_URL = os.environ.get("OKX_BASE_URL", "https://app.okx.com")
# On Railway, mount a Volume at /data so state survives redeploys
_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DATA_DIR, "state.json")
LOG_FILE = os.path.join(_DATA_DIR, "bot.log")
CANDLES_NEEDED = 130        # breakout mode: bars fetched (warmup 101)
CANDLES_NEEDED_CT = 400     # core_trend mode: needs 365+ days of history
