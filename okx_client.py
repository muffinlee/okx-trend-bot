"""
Minimal OKX v5 REST client (spot, no external dependencies).

Signing per OKX docs: OK-ACCESS-SIGN = base64(HMAC-SHA256(secret,
timestamp + method + requestPath + body)), timestamp ISO8601 ms UTC.
Demo trading uses the same API plus the header `x-simulated-trading: 1`.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config


class OkxError(RuntimeError):
    pass


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request(method, path, params=None, body=None, auth=False, retries=3):
    url = config.BASE_URL + path
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
        url = config.BASE_URL + path
    payload = json.dumps(body) if body is not None else ""

    # OKX sits behind Cloudflare, which blocks Python's default "Python-urllib"
    # user agent (error 1010). Identify the bot with a normal user agent.
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh) okx-trend-bot/1.0",
               "Accept": "application/json"}
    if auth:
        if not (config.API_KEY and config.API_SECRET and config.API_PASSPHRASE):
            raise OkxError("API credentials missing - set OKX_API_KEY, "
                           "OKX_API_SECRET, OKX_API_PASSPHRASE env vars.")
        ts = _timestamp()
        msg = ts + method + path + payload
        sign = base64.b64encode(
            hmac.new(config.API_SECRET.encode(), msg.encode(),
                     hashlib.sha256).digest()).decode()
        headers.update({
            "OK-ACCESS-KEY": config.API_KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": config.API_PASSPHRASE,
        })
    if config.DEMO_MODE:
        headers["x-simulated-trading"] = "1"

    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=payload.encode() if payload else None)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            if data.get("code") != "0":
                raise OkxError(f"OKX error {data.get('code')}: "
                               f"{data.get('msg')} | {data.get('data')}")
            return data["data"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code < 500:      # auth / bad request: retrying will not help
                raise OkxError(f"HTTP {e.code} from OKX: {body}")
            if attempt == retries - 1:
                raise OkxError(f"HTTP {e.code} after {retries} tries: {body}")
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise OkxError(f"network error after {retries} tries: {e}")
            time.sleep(2 ** attempt)


# ---------------------------------------------------------------- public
def get_daily_candles(inst_id, limit=130, confirmed_only=True):
    """Return dict of lists (oldest -> newest): ts, high, low, close."""
    raw = _request("GET", "/api/v5/market/candles",
                   {"instId": inst_id, "bar": "1D", "limit": str(min(limit, 300))})
    rows = [c for c in raw if not confirmed_only or c[8] == "1"]
    rows.sort(key=lambda c: int(c[0]))
    return {"ts": [int(c[0]) for c in rows],
            "high": [float(c[2]) for c in rows],
            "low": [float(c[3]) for c in rows],
            "close": [float(c[4]) for c in rows]}


def get_daily_history(inst_id, n=400):
    """Up to n confirmed daily candles (oldest -> newest), paginating past
    the 300-bar limit of /market/candles via /market/history-candles."""
    rows = {}
    raw = _request("GET", "/api/v5/market/candles",
                   {"instId": inst_id, "bar": "1D", "limit": "300"})
    for c in raw:
        if c[8] == "1":
            rows[int(c[0])] = c
    oldest = min(int(c[0]) for c in raw) if raw else None
    while oldest and len(rows) < n:
        raw = _request("GET", "/api/v5/market/history-candles",
                       {"instId": inst_id, "bar": "1D", "limit": "100",
                        "after": str(oldest)})
        if not raw:
            break
        for c in raw:
            rows[int(c[0])] = c
        oldest = min(int(c[0]) for c in raw)
        time.sleep(0.2)
    ts = sorted(rows)[-n:]
    return {"ts": ts,
            "high": [float(rows[t][2]) for t in ts],
            "low": [float(rows[t][3]) for t in ts],
            "close": [float(rows[t][4]) for t in ts]}


def get_ticker(inst_id):
    return float(_request("GET", "/api/v5/market/ticker",
                          {"instId": inst_id})[0]["last"])


def get_instrument(inst_id):
    d = _request("GET", "/api/v5/public/instruments",
                 {"instType": "SPOT", "instId": inst_id})[0]
    return {"minSz": float(d["minSz"]), "lotSz": float(d["lotSz"])}


# ---------------------------------------------------------------- private
def get_balances():
    """Return {ccy: available_balance} for all non-zero balances."""
    data = _request("GET", "/api/v5/account/balance", auth=True)
    out = {}
    for det in data[0].get("details", []):
        avail = float(det.get("availBal") or 0)
        if avail > 0:
            out[det["ccy"]] = avail
    return out


def market_buy(inst_id, notional_usdt):
    """Market buy sized in quote currency (USDT)."""
    return _request("POST", "/api/v5/trade/order", body={
        "instId": inst_id, "tdMode": "cash", "side": "buy",
        "ordType": "market", "sz": f"{notional_usdt:.2f}",
        "tgtCcy": "quote_ccy",
    }, auth=True)


def market_sell(inst_id, base_amount, lot_sz):
    """Market sell sized in base currency, rounded down to lot size."""
    lots = int(base_amount / lot_sz + 1e-9)   # tolerate float dust, round down
    sz = lots * lot_sz
    if sz <= 0:
        raise OkxError(f"{inst_id}: sell size {base_amount} below lot {lot_sz}")
    # format without scientific notation / float dust
    sz_str = f"{sz:.10f}".rstrip("0").rstrip(".")
    return _request("POST", "/api/v5/trade/order", body={
        "instId": inst_id, "tdMode": "cash", "side": "sell",
        "ordType": "market", "sz": sz_str,
    }, auth=True)


def get_order(inst_id, ord_id):
    """Fill details for one order: avgPx, accFillSz, fee (negative), feeCcy, state, fillTime."""
    return _request("GET", "/api/v5/trade/order",
                    {"instId": inst_id, "ordId": ord_id}, auth=True)[0]
