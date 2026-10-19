"""
Daily email digest + error alerts, sent through Resend's HTTPS API.

Railway blocks normal SMTP (Gmail) on its Hobby plan, so this uses Resend
(https://resend.com, free tier), which works on every plan. Without a verified
domain Resend sends from onboarding@resend.dev to the email address you signed
up with - which is exactly what a personal digest needs.

Railway variables:
  RESEND_API_KEY      re_...  (from resend.com -> API Keys)
  DIGEST_EMAIL_TO     the address you signed up to Resend with
  START_CAPITAL_USDT  optional, e.g. 1076.31 - shows "since you started"
If RESEND_API_KEY or DIGEST_EMAIL_TO is missing, emails are skipped silently.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("bot")

API_KEY = os.environ.get("RESEND_API_KEY", "").strip().strip("'\"")
EMAIL_TO = os.environ.get("DIGEST_EMAIL_TO", "").strip().strip("'\"")
EMAIL_FROM = os.environ.get("DIGEST_EMAIL_FROM", "OKX Bot <onboarding@resend.dev>")


def enabled():
    return bool(API_KEY and EMAIL_TO)


def send(subject, html, text, attachments=None):
    """Send one email. Never raises - a failed email must not stop trading."""
    if not enabled():
        log.info("email not configured - skipped: %s", subject)
        return False
    msg = {"from": EMAIL_FROM, "to": [EMAIL_TO], "subject": subject,
           "html": html, "text": text}
    if attachments:
        msg["attachments"] = [{"filename": n, "content": base64.b64encode(b).decode()}
                              for n, b in attachments]
    body = json.dumps(msg).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {API_KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "okx-trend-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log.info("email sent: %s", subject)
        return True
    except urllib.error.HTTPError as e:
        log.error("email failed: HTTP %s %s", e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:  # network etc.
        log.error("email failed: %s", e)
    return False


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _usd(x):
    return f"${x:,.2f}"


def build_digest(d):
    """d: dict produced by bot.run_core_trend. Returns (subject, html, text)."""
    perf = d["perf"]
    head = f"{_usd(d['equity'])}  ({_pct(perf.get('1d'))} today)"
    subject = f"OKX bot {d['date']}: {head}"

    perf_rows = [("Today", perf.get("1d")), ("Last 7 days", perf.get("7d")),
                 ("Since bot start", perf.get("start")),
                 ("Since you started", perf.get("capital"))]
    perf_rows = [(k, v) for k, v in perf_rows if v is not None or k == "Today"]

    coin_rows = d["coins"]
    trade_lines = d["trades"] or ["No trades today."]

    # ---- plain text
    t = [f"OKX bot daily digest - {d['date']} ({d['mode']})", "",
         f"Total value: {_usd(d['equity'])}   invested {d['invested'] * 100:.0f}%"
         f" (target {d['target_total'] * 100:.0f}%)", ""]
    t += ["PERFORMANCE"] + [f"  {k:<18} {_pct(v)}" for k, v in perf_rows] + [""]
    t += ["ALLOCATION",
          f"  {'Coin':<6}{'Holding':>14}{'Value':>12}{'Now':>7}{'Target':>8}{'Trend':>7}{'24h':>8}{'7d':>8}"]
    for r in coin_rows:
        t.append(f"  {r['coin']:<6}{r['qty']:>14.6f}{_usd(r['value']):>12}"
                 f"{r['w'] * 100:>6.1f}%{r['tgt'] * 100:>7.1f}%{r['trend']:>7}"
                 f"{_pct(r['chg1d']):>8}{_pct(r['chg7d']):>8}")
    t.append(f"  {'USDT':<6}{'':>14}{_usd(d['usdt']):>12}{d['cash_w'] * 100:>6.1f}%")
    t += ["", "TRADES"] + [f"  {x}" for x in trade_lines]
    t += ["", d["next_note"]]
    if d.get("warnings"):
        t += ["", "NOTES"] + [f"  {w}" for w in d["warnings"]]
    text = "\n".join(t)

    # ---- html
    def color(v):
        return "#888" if v is None else ("#0a7d32" if v >= 0 else "#c0392b")

    th = "style='text-align:right;padding:4px 8px;border-bottom:1px solid #ddd'"
    td = "style='text-align:right;padding:4px 8px'"
    h = [f"<div style='font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:14px;color:#222'>",
         f"<h2 style='margin:0 0 4px'>{_usd(d['equity'])}</h2>",
         f"<div style='color:#666'>{d['date']} · {d['mode']} · invested {d['invested'] * 100:.0f}%"
         f" (target {d['target_total'] * 100:.0f}%)</div>",
         "<h3>Performance</h3><table style='border-collapse:collapse'>"]
    for k, v in perf_rows:
        h.append(f"<tr><td style='padding:3px 12px 3px 0'>{k}</td>"
                 f"<td style='color:{color(v)};font-weight:600'>{_pct(v)}</td></tr>")
    h.append("</table><h3>Allocation</h3><table style='border-collapse:collapse'><tr>")
    for col in ("Coin", "Holding", "Value", "Now", "Target", "Trend", "24h", "7d"):
        h.append(f"<th {th}>{col}</th>")
    h.append("</tr>")
    for r in coin_rows:
        h.append(f"<tr><td {td}><b>{r['coin']}</b></td><td {td}>{r['qty']:.6f}</td>"
                 f"<td {td}>{_usd(r['value'])}</td><td {td}>{r['w'] * 100:.1f}%</td>"
                 f"<td {td}>{r['tgt'] * 100:.1f}%</td><td {td}>{r['trend']}</td>"
                 f"<td {td} style='color:{color(r['chg1d'])}'>{_pct(r['chg1d'])}</td>"
                 f"<td {td} style='color:{color(r['chg7d'])}'>{_pct(r['chg7d'])}</td></tr>")
    h.append(f"<tr><td {td}><b>USDT</b></td><td {td}></td><td {td}>{_usd(d['usdt'])}</td>"
             f"<td {td}>{d['cash_w'] * 100:.1f}%</td><td colspan=4></td></tr></table>")
    h.append("<h3>Trades</h3><ul>" + "".join(f"<li>{x}</li>" for x in trade_lines) + "</ul>")
    h.append(f"<p style='color:#666'>{d['next_note']}</p>")
    if d.get("warnings"):
        h.append("<h3>Notes</h3><ul>" + "".join(f"<li>{w}</li>" for w in d["warnings"]) + "</ul>")
    h.append("<p style='color:#999;font-size:12px'>Trend = share of 3, 6 and 12-month "
             "periods the coin is up (1.0 = all three). Not financial advice.</p></div>")
    return subject, "".join(h), text


def send_error(date, err):
    subject = f"OKX bot {date}: ERROR - please check"
    text = (f"The bot hit an error on {date} and will retry every 15 minutes.\n\n{err}\n\n"
            "Check the Railway logs. Common causes: an API key typo (Invalid Sign / 401), "
            "or OKX being temporarily unavailable.")
    html = "<pre style='font-family:Menlo,monospace;font-size:12px'>" + \
           text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return send(subject, html, text)
