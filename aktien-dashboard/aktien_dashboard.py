#!/usr/bin/env python3
"""
Aktien-Dashboard mit technischen Indikatoren und Kauf-/Verkaufssignalen.

Start:   python aktien_dashboard.py            (echte Kurse von Yahoo Finance)
         python aktien_dashboard.py --demo     (simulierte Kurse, ohne Internet)
Optionen: --port 8765   --no-browser

Es wird nur die Python-Standardbibliothek benötigt (Python 3.8+).
Der Browser öffnet sich automatisch unter http://localhost:8765.
Die Seite aktualisiert sich alle 10 Sekunden, solange sie geöffnet und sichtbar ist.

HINWEIS: Die Signale sind rein technisch-automatisch berechnet und KEINE Anlageberatung.
"""

import argparse
import json
import math
import random
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO = False
CACHE_SECONDS = 8
_cache = {}
_cache_lock = threading.Lock()

# ----------------------------------------------------------------------------
# Datenbeschaffung
# ----------------------------------------------------------------------------

YAHOO_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d&includePrePost=false",
    "https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d&includePrePost=false",
]


def fetch_yahoo(symbol):
    last_err = None
    for tpl in YAHOO_URLS:
        url = tpl.format(sym=urllib.parse.quote(symbol))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            res = (data.get("chart") or {}).get("result")
            if not res:
                err = (data.get("chart") or {}).get("error") or {}
                raise ValueError(err.get("description") or "Symbol nicht gefunden")
            res = res[0]
            meta = res.get("meta", {})
            ts = res.get("timestamp") or []
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            candles = []
            for i, t in enumerate(ts):
                o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
                v = (q.get("volume") or [None] * len(ts))[i]
                if None in (o, h, l, c):
                    continue
                candles.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
            if len(candles) < 30:
                raise ValueError("Zu wenig Kursdaten")
            live = meta.get("regularMarketPrice")
            if live:
                last = candles[-1]
                last["c"] = live
                last["h"] = max(last["h"], live)
                last["l"] = min(last["l"], live)
            return {
                "symbol": meta.get("symbol", symbol),
                "name": meta.get("longName") or meta.get("shortName") or symbol,
                "currency": meta.get("currency", ""),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
                "marketTime": meta.get("regularMarketTime"),
                "candles": candles,
            }
        except Exception as e:  # nächste URL probieren
            last_err = e
    raise RuntimeError(str(last_err))


_demo_state = {}
DEMO_NAMES = {
    "AAPL": ("Apple Inc.", "USD", 190), "MSFT": ("Microsoft Corp.", "USD", 420),
    "NVDA": ("NVIDIA Corp.", "USD", 120), "AMZN": ("Amazon.com Inc.", "USD", 180),
    "GOOGL": ("Alphabet Inc.", "USD", 165), "META": ("Meta Platforms Inc.", "USD", 500),
    "TSLA": ("Tesla Inc.", "USD", 240), "SAP.DE": ("SAP SE", "EUR", 200),
    "SIE.DE": ("Siemens AG", "EUR", 175), "ALV.DE": ("Allianz SE", "EUR", 270),
    "BMW.DE": ("BMW AG", "EUR", 90), "MBG.DE": ("Mercedes-Benz Group AG", "EUR", 65),
    "BTC-USD": ("Bitcoin USD", "USD", 65000), "ETH-USD": ("Ethereum USD", "USD", 3000),
}


def fetch_demo(symbol):
    if symbol not in _demo_state:
        name, cur, start = DEMO_NAMES.get(symbol, (symbol + " (Demo)", "EUR", 100))
        rnd = random.Random(symbol)
        price = start * 0.7
        drift = rnd.uniform(-0.0005, 0.0012)
        candles = []
        day = 86400
        t0 = int(time.time()) - 500 * day
        for i in range(500):
            o = price
            price *= math.exp(drift + rnd.gauss(0, 0.018))
            c = price
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.007)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.007)))
            candles.append({"t": t0 + i * day, "o": o, "h": h, "l": l, "c": c,
                            "v": int(rnd.uniform(2e6, 9e6))})
        _demo_state[symbol] = {"name": name, "currency": cur, "candles": candles}
    st = _demo_state[symbol]
    last = st["candles"][-1]
    last["c"] *= math.exp(random.gauss(0, 0.002))  # Live-Tick
    last["h"] = max(last["h"], last["c"])
    last["l"] = min(last["l"], last["c"])
    last["v"] += random.randint(1000, 50000)
    return {"symbol": symbol, "name": st["name"], "currency": st["currency"],
            "exchange": "DEMO", "marketTime": int(time.time()),
            "candles": [dict(c) for c in st["candles"]]}


def get_data(symbol):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(symbol)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    raw = fetch_demo(symbol) if DEMO else fetch_yahoo(symbol)
    result = analyze(raw)
    with _cache_lock:
        _cache[symbol] = (now, result)
    return result


# ----------------------------------------------------------------------------
# Indikatoren (Listen, None = noch nicht definiert)
# ----------------------------------------------------------------------------

def sma(v, n):
    out = [None] * len(v)
    for i in range(n - 1, len(v)):
        w = v[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / n
    return out


def _smooth(v, n, alpha):
    out = [None] * len(v)
    prev, buf = None, []
    for i, x in enumerate(v):
        if x is None:
            continue
        if prev is None:
            buf.append(x)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
        else:
            prev = x * alpha + prev * (1 - alpha)
            out[i] = prev
    return out


def ema(v, n):
    return _smooth(v, n, 2 / (n + 1))


def rma(v, n):  # Wilder-Glättung
    return _smooth(v, n, 1 / n)


def wma(v, n):
    out = [None] * len(v)
    den = n * (n + 1) / 2
    for i in range(n - 1, len(v)):
        w = v[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(x * (k + 1) for k, x in enumerate(w)) / den
    return out


def stdev(v, n):
    out = [None] * len(v)
    for i in range(n - 1, len(v)):
        w = v[i - n + 1:i + 1]
        if None not in w:
            m = sum(w) / n
            out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def rolling_max(v, n):
    return [max(v[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(v))]


def rolling_min(v, n):
    return [min(v[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(v))]


def sub(a, b):
    return [x - y if x is not None and y is not None else None for x, y in zip(a, b)]


def rsi(c, n=14):
    g = [None] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [None] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = rma(g, n), rma(l, n)
    out = []
    for a, b in zip(ag, al):
        if a is None or b is None:
            out.append(None)
        elif b == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + a / b))
    return out


def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                            for i in range(1, len(c))]


def atr(h, l, c, n=14):
    return rma(true_range(h, l, c), n)


def adx(h, l, c, n=14):
    pdm, mdm = [None], [None]
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
    tr = [None] + true_range(h, l, c)[1:]
    str_, spdm, smdm = rma(tr, n), rma(pdm, n), rma(mdm, n)
    pdi, mdi, dx = [], [], []
    for t, p, m in zip(str_, spdm, smdm):
        if t is None or t == 0:
            pdi.append(None); mdi.append(None); dx.append(None)
            continue
        pi, mi = 100 * p / t, 100 * m / t
        pdi.append(pi); mdi.append(mi)
        dx.append(100 * abs(pi - mi) / (pi + mi) if pi + mi else 0)
    return rma(dx, n), pdi, mdi


def psar(h, l, step=0.02, mx=0.2):
    out = [None] * len(h)
    up = True
    af = step
    ep = h[0]
    sar = l[0]
    for i in range(1, len(h)):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, mx)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, mx)
        out[i] = sar
    return out


def supertrend(h, l, c, n=10, mult=3):
    a = atr(h, l, c, n)
    direction = [None] * len(c)
    line = [None] * len(c)
    fu = fl = None
    d = 1
    for i in range(len(c)):
        if a[i] is None:
            continue
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            fu = bu if bu < fu or c[i - 1] > fu else fu
            fl = bl if bl > fl or c[i - 1] < fl else fl
        if d == 1 and c[i] < fl:
            d = -1
        elif d == -1 and c[i] > fu:
            d = 1
        direction[i] = d
        line[i] = fl if d == 1 else fu
    return line, direction


def last(v, k=1):
    """k-ter Wert von hinten (1 = letzter)."""
    return v[-k] if len(v) >= k else None


# ----------------------------------------------------------------------------
# Analyse
# ----------------------------------------------------------------------------

def fmt(x, d=2):
    if x is None:
        return "–"
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def analyze(raw):
    cs = raw["candles"]
    o = [x["o"] for x in cs]
    h = [x["h"] for x in cs]
    l = [x["l"] for x in cs]
    c = [x["c"] for x in cs]
    v = [x["v"] for x in cs]
    n = len(c)
    price = c[-1]
    prev = c[-2]
    has_vol = sum(v[-30:]) > 0

    sig = []  # (gruppe, name, wert, signal, erklärung)

    def add(group, name, value, s, note=""):
        sig.append({"group": group, "name": name, "value": value, "signal": s, "note": note})

    # --- Gleitende Durchschnitte -------------------------------------------------
    ma_vals = {}
    for p in (10, 20, 50, 100, 200):
        for kind, fn in (("SMA", sma), ("EMA", ema)):
            val = last(fn(c, p))
            ma_vals[f"{kind}{p}"] = val
            if val is None:
                continue
            s = 1 if price > val else -1 if price < val else 0
            add("Gleitende Durchschnitte", f"{kind} ({p})", fmt(val),
                s, "Kurs darüber" if s > 0 else "Kurs darunter")
    hma = wma(sub([2 * x if x is not None else None for x in wma(c, 4)], wma(c, 9)), 3)
    if last(hma) is not None and last(hma, 2) is not None:
        s = 1 if last(hma) > last(hma, 2) else -1
        add("Gleitende Durchschnitte", "Hull MA (9)", fmt(last(hma)), s,
            "steigend" if s > 0 else "fallend")
    vwma = [None] * n
    for i in range(19, n):
        vs = sum(v[i - 19:i + 1])
        vwma[i] = sum(c[j] * v[j] for j in range(i - 19, i + 1)) / vs if vs else None
    if has_vol and last(vwma) is not None:
        s = 1 if price > last(vwma) else -1
        add("Gleitende Durchschnitte", "VWMA (20)", fmt(last(vwma)), s,
            "Kurs darüber" if s > 0 else "Kurs darunter")
    s50, s200 = sma(c, 50), sma(c, 200)
    cross_note = ""
    if last(s200) is not None:
        s = 1 if last(s50) > last(s200) else -1
        # Kreuzung in den letzten 10 Tagen?
        for k in range(1, 11):
            if s200[-k - 1] is None:
                break
            a, b = s50[-k] - s200[-k], s50[-k - 1] - s200[-k - 1]
            if (a > 0) != (b > 0):
                cross_note = ("Golden Cross" if a > 0 else "Death Cross") + f" vor {k - 1} Tagen"
                break
        add("Gleitende Durchschnitte", "SMA 50/200 Kreuz", "Golden" if s > 0 else "Death", s,
            cross_note or ("SMA50 über SMA200" if s > 0 else "SMA50 unter SMA200"))

    # --- Oszillatoren -----------------------------------------------------------
    r = rsi(c, 14)
    rv = last(r)
    if rv is not None:
        s = 1 if rv < 30 else -1 if rv > 70 else 0
        add("Oszillatoren", "RSI (14)", fmt(rv, 1), s,
            "überverkauft" if s > 0 else "überkauft" if s < 0 else "neutral (30–70)")

    hh14, ll14 = rolling_max(h, 14), rolling_min(l, 14)
    raw_k = [100 * (c[i] - ll14[i]) / (hh14[i] - ll14[i]) if hh14[i] is not None and hh14[i] != ll14[i]
             else (50 if hh14[i] is not None else None) for i in range(n)]
    k_ = sma(raw_k, 3)
    d_ = sma(k_, 3)
    if last(d_) is not None:
        kv, dv = last(k_), last(d_)
        s = 1 if kv < 20 and kv > dv else -1 if kv > 80 and kv < dv else 0
        add("Oszillatoren", "Stochastik %K/%D (14,3,3)", f"{fmt(kv, 1)} / {fmt(dv, 1)}", s,
            "überverkauft & dreht" if s > 0 else "überkauft & dreht" if s < 0
            else ("< 20 überverkauft" if kv < 20 else "> 80 überkauft" if kv > 80 else "neutral"))

    rmax, rmin = [None] * n, [None] * n
    for i in range(n):
        w = r[max(0, i - 13):i + 1]
        if i >= 13 and None not in w:
            rmax[i], rmin[i] = max(w), min(w)
    srsi = [100 * (r[i] - rmin[i]) / (rmax[i] - rmin[i]) if rmax[i] is not None and rmax[i] != rmin[i]
            else (50 if rmax[i] is not None else None) for i in range(n)]
    srk = sma(srsi, 3)
    srd = sma(srk, 3)
    if last(srd) is not None:
        kv, dv = last(srk), last(srd)
        s = 1 if kv < 20 and kv > dv else -1 if kv > 80 and kv < dv else 0
        add("Oszillatoren", "Stoch RSI (14,14,3,3)", f"{fmt(kv, 1)} / {fmt(dv, 1)}", s,
            "überverkauft & dreht" if s > 0 else "überkauft & dreht" if s < 0 else "neutral")

    e12, e26 = ema(c, 12), ema(c, 26)
    macd = sub(e12, e26)
    msig = ema(macd, 9)
    mhist = sub(macd, msig)
    if last(msig) is not None:
        s = 1 if last(macd) > last(msig) else -1
        note = "MACD über Signallinie" if s > 0 else "MACD unter Signallinie"
        if last(mhist, 2) is not None and (last(mhist) > 0) != (last(mhist, 2) > 0):
            note = "frisches " + ("Kaufsignal (Kreuzung nach oben)" if s > 0 else "Verkaufssignal (Kreuzung nach unten)")
        add("Oszillatoren", "MACD (12,26,9)", f"{fmt(last(macd))} / {fmt(last(msig))}", s, note)

    wr = [-100 * (hh14[i] - c[i]) / (hh14[i] - ll14[i]) if hh14[i] is not None and hh14[i] != ll14[i]
          else None for i in range(n)]
    if last(wr) is not None:
        s = 1 if last(wr) < -80 else -1 if last(wr) > -20 else 0
        add("Oszillatoren", "Williams %R (14)", fmt(last(wr), 1), s,
            "überverkauft" if s > 0 else "überkauft" if s < 0 else "neutral")

    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    tps = sma(tp, 20)
    cci = [None] * n
    for i in range(19, n):
        md = sum(abs(x - tps[i]) for x in tp[i - 19:i + 1]) / 20
        cci[i] = (tp[i] - tps[i]) / (0.015 * md) if md else 0
    if last(cci) is not None:
        s = 1 if last(cci) < -100 else -1 if last(cci) > 100 else 0
        add("Oszillatoren", "CCI (20)", fmt(last(cci), 1), s,
            "überverkauft" if s > 0 else "überkauft" if s < 0 else "neutral (±100)")

    mom = [c[i] - c[i - 10] if i >= 10 else None for i in range(n)]
    if last(mom) is not None:
        s = 1 if last(mom) > 0 else -1
        add("Oszillatoren", "Momentum (10)", fmt(last(mom)), s, "positiv" if s > 0 else "negativ")
    roc = [100 * (c[i] / c[i - 12] - 1) if i >= 12 else None for i in range(n)]
    if last(roc) is not None:
        s = 1 if last(roc) > 0 else -1
        add("Oszillatoren", "ROC (12)", fmt(last(roc)) + " %", s, "positiv" if s > 0 else "negativ")

    med = [(h[i] + l[i]) / 2 for i in range(n)]
    ao = sub(sma(med, 5), sma(med, 34))
    if last(ao, 2) is not None:
        rising = last(ao) > last(ao, 2)
        s = 1 if last(ao) > 0 and rising else -1 if last(ao) < 0 and not rising else 0
        add("Oszillatoren", "Awesome Oscillator", fmt(last(ao)), s,
            ("über 0" if last(ao) > 0 else "unter 0") + (", steigend" if rising else ", fallend"))

    bp = [None] + [c[i] - min(l[i], c[i - 1]) for i in range(1, n)]
    trr = [None] + [max(h[i], c[i - 1]) - min(l[i], c[i - 1]) for i in range(1, n)]

    def avg_ratio(p, i):
        tb, tt = sum(bp[i - p + 1:i + 1]), sum(trr[i - p + 1:i + 1])
        return tb / tt if tt else 0
    if n > 30:
        uo = 100 * (4 * avg_ratio(7, n - 1) + 2 * avg_ratio(14, n - 1) + avg_ratio(28, n - 1)) / 7
        s = 1 if uo < 30 else -1 if uo > 70 else 0
        add("Oszillatoren", "Ultimate Oscillator", fmt(uo, 1), s,
            "überverkauft" if s > 0 else "überkauft" if s < 0 else "neutral")

    tr1 = ema(ema(ema(c, 15), 15), 15)
    trix = [100 * (tr1[i] / tr1[i - 1] - 1) if i > 0 and tr1[i] and tr1[i - 1] else None for i in range(n)]
    if last(trix) is not None:
        s = 1 if last(trix) > 0 else -1
        add("Oszillatoren", "TRIX (15)", fmt(last(trix), 3), s, "positiv" if s > 0 else "negativ")

    e13 = ema(c, 13)
    if last(e13) is not None:
        bull, bear = h[-1] - last(e13), l[-1] - last(e13)
        s = 1 if bear < 0 and bear > l[-2] - last(e13, 2) and last(e13) > last(e13, 2) else \
            -1 if bull > 0 and bull < h[-2] - last(e13, 2) and last(e13) < last(e13, 2) else 0
        add("Oszillatoren", "Elder Bull/Bear Power (13)", f"{fmt(bull)} / {fmt(bear)}", s,
            "Bären lassen nach im Aufwärtstrend" if s > 0 else
            "Bullen lassen nach im Abwärtstrend" if s < 0 else "neutral")

    # --- Trend ------------------------------------------------------------------
    adx_, pdi, mdi = adx(h, l, c, 14)
    if last(adx_) is not None:
        a = last(adx_)
        s = (1 if last(pdi) > last(mdi) else -1) if a > 20 else 0
        add("Trend", "ADX / DMI (14)", f"{fmt(a, 1)} (+DI {fmt(last(pdi), 1)} / −DI {fmt(last(mdi), 1)})", s,
            ("starker " if a > 25 else "") + ("Aufwärtstrend" if s > 0 else "Abwärtstrend" if s < 0 else "kein klarer Trend (ADX<20)"))

    ps = psar(h, l)
    if last(ps) is not None:
        s = 1 if price > last(ps) else -1
        add("Trend", "Parabolic SAR", fmt(last(ps)), s, "SAR unter Kurs" if s > 0 else "SAR über Kurs")

    st_line, st_dir = supertrend(h, l, c, 10, 3)
    if last(st_dir) is not None:
        s = last(st_dir)
        add("Trend", "Supertrend (10,3)", fmt(last(st_line)), s, "grün/aufwärts" if s > 0 else "rot/abwärts")

    if n >= 78:
        ten = [(a + b) / 2 if a is not None else None for a, b in zip(rolling_max(h, 9), rolling_min(l, 9))]
        kij = [(a + b) / 2 if a is not None else None for a, b in zip(rolling_max(h, 26), rolling_min(l, 26))]
        sb = [(a + b) / 2 if a is not None else None for a, b in zip(rolling_max(h, 52), rolling_min(l, 52))]
        span_a = (ten[-27] + kij[-27]) / 2
        span_b = sb[-27]
        top, bot = max(span_a, span_b), min(span_a, span_b)
        if price > top and ten[-1] > kij[-1]:
            s, note = 1, "Kurs über der Wolke, Tenkan > Kijun"
        elif price < bot and ten[-1] < kij[-1]:
            s, note = -1, "Kurs unter der Wolke, Tenkan < Kijun"
        else:
            s, note = 0, "Kurs in/nahe der Wolke" if bot <= price <= top else "gemischt"
        add("Trend", "Ichimoku (9,26,52)", f"Wolke {fmt(bot)}–{fmt(top)}", s, note)

    if n > 26:
        wh, wl = h[-26:], l[-26:]
        up = 100 * (wh.index(max(wh))) / 25
        dn = 100 * (wl.index(min(wl))) / 25
        s = 1 if up > 70 and dn < 30 else -1 if dn > 70 and up < 30 else 0
        add("Trend", "Aroon (25)", f"↑{fmt(up, 0)} / ↓{fmt(dn, 0)}", s,
            "Aufwärtstrend" if s > 0 else "Abwärtstrend" if s < 0 else "neutral")

    if n > 21:
        dh, dl = max(h[-21:-1]), min(l[-21:-1])
        s = 1 if price > dh else -1 if price < dl else 0
        add("Trend", "Donchian-Ausbruch (20)", f"{fmt(dl)}–{fmt(dh)}", s,
            "Ausbruch nach oben" if s > 0 else "Ausbruch nach unten" if s < 0 else "innerhalb des Kanals")

    # --- Volatilität -------------------------------------------------------------
    mid = sma(c, 20)
    sd = stdev(c, 20)
    bbu = [m + 2 * d if m is not None else None for m, d in zip(mid, sd)]
    bbl = [m - 2 * d if m is not None else None for m, d in zip(mid, sd)]
    if last(mid) is not None:
        width = bbu[-1] - bbl[-1]
        pb = (price - bbl[-1]) / width if width else 0.5
        s = 1 if pb < 0 else -1 if pb > 1 else 0
        add("Volatilität", "Bollinger-Bänder (20,2)", f"{fmt(bbl[-1])}–{fmt(bbu[-1])} (%B {fmt(pb, 2)})", s,
            "unter unterem Band" if s > 0 else "über oberem Band" if s < 0 else "innerhalb der Bänder")

    a14 = atr(h, l, c, 14)
    atr_v = last(a14)
    e20 = ema(c, 20)
    a10 = atr(h, l, c, 10)
    if last(e20) is not None and last(a10) is not None:
        ku, kl = last(e20) + 2 * last(a10), last(e20) - 2 * last(a10)
        s = 1 if price < kl else -1 if price > ku else 0
        add("Volatilität", "Keltner-Kanal (20,2)", f"{fmt(kl)}–{fmt(ku)}", s,
            "unter Kanal (überverkauft)" if s > 0 else "über Kanal (überkauft)" if s < 0 else "innerhalb")
    if atr_v is not None:
        add("Volatilität", "ATR (14)", f"{fmt(atr_v)} ({fmt(100 * atr_v / price, 2)} %)", 0,
            "Volatilitätsmaß (nur Info)")

    # --- Volumen ----------------------------------------------------------------
    obv = [0]
    for i in range(1, n):
        obv.append(obv[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0))
    if has_vol:
        osm = last(sma(obv, 20))
        s = 1 if obv[-1] > osm else -1
        add("Volumen", "On-Balance-Volume", f"{obv[-1] / 1e6:,.1f} Mio".replace(",", "X").replace(".", ",").replace("X", "."),
            s, "über 20-Tage-Schnitt" if s > 0 else "unter 20-Tage-Schnitt")

        pos = neg = 0
        for i in range(n - 14, n):
            f = tp[i] * v[i]
            if tp[i] > tp[i - 1]:
                pos += f
            elif tp[i] < tp[i - 1]:
                neg += f
        mfi = 100 if neg == 0 else 100 - 100 / (1 + pos / neg)
        s = 1 if mfi < 20 else -1 if mfi > 80 else 0
        add("Volumen", "Money Flow Index (14)", fmt(mfi, 1), s,
            "überverkauft" if s > 0 else "überkauft" if s < 0 else "neutral")

        num = den = 0
        for i in range(n - 20, n):
            rng = h[i] - l[i]
            mfm = ((c[i] - l[i]) - (h[i] - c[i])) / rng if rng else 0
            num += mfm * v[i]
            den += v[i]
        cmf = num / den if den else 0
        s = 1 if cmf > 0.05 else -1 if cmf < -0.05 else 0
        add("Volumen", "Chaikin Money Flow (20)", fmt(cmf, 3), s,
            "Kaufdruck" if s > 0 else "Verkaufsdruck" if s < 0 else "neutral")

        vwap = sum(tp[i] * v[i] for i in range(n - 20, n)) / (sum(v[-20:]) or 1)
        s = 1 if price > vwap else -1
        add("Volumen", "VWAP (20 Tage)", fmt(vwap), s, "Kurs darüber" if s > 0 else "Kurs darunter")

        avgv = sum(v[-21:-1]) / 20
        ratio = v[-1] / avgv if avgv else 1
        day_up = c[-1] >= c[-2]
        s = (1 if day_up else -1) if ratio > 1.5 else 0
        add("Volumen", "Volumen vs. Ø20", f"{fmt(ratio, 2)}×", s,
            ("hohes Volumen bei " + ("Anstieg" if day_up else "Rückgang")) if ratio > 1.5 else "normal")

    # --- Pivot-Punkte (Vortag) ---------------------------------------------------
    P = (h[-2] + l[-2] + c[-2]) / 3
    pivots = {"R3": h[-2] + 2 * (P - l[-2]), "R2": P + (h[-2] - l[-2]), "R1": 2 * P - l[-2], "P": P,
              "S1": 2 * P - h[-2], "S2": P - (h[-2] - l[-2]), "S3": l[-2] - 2 * (h[-2] - P)}

    # --- Zusammenfassung ---------------------------------------------------------
    def summarize(items):
        rated = [x for x in items if not (x["signal"] == 0 and "nur Info" in x["note"])]
        b = sum(1 for x in rated if x["signal"] > 0)
        s_ = sum(1 for x in rated if x["signal"] < 0)
        nn = len(rated) - b - s_
        score = (b - s_) / len(rated) if rated else 0
        return {"buy": b, "sell": s_, "neutral": nn, "score": round(score, 3), "label": label(score)}

    groups = {}
    for g in ("Gleitende Durchschnitte", "Oszillatoren", "Trend", "Volatilität", "Volumen"):
        items = [x for x in sig if x["group"] == g]
        if items:
            groups[g] = summarize(items)
    total = summarize(sig)
    # Gesamt: Durchschnitt der Gruppen, damit viele MAs nicht alles dominieren
    gs = [gr["score"] for gr in groups.values()]
    total["score"] = round(sum(gs) / len(gs), 3) if gs else 0
    total["label"] = label(total["score"])

    tip = build_tip(total, sig, price, atr_v, pivots, rv, cross_note, raw.get("currency", ""))

    k = min(160, n)
    dates = [datetime.fromtimestamp(x["t"], tz=timezone.utc).strftime("%d.%m.%y") for x in cs[-k:]]

    def rnd(arr, d=4):
        return [round(x, d) if x is not None else None for x in arr[-k:]]

    year_ago = cs[-1]["t"] - 365 * 86400  # Kalendertage, damit es auch für Krypto (7 Tage/Woche) passt
    year = [x for x in cs if x["t"] >= year_ago]
    change = price - prev
    return {
        "symbol": raw["symbol"], "name": raw["name"], "currency": raw["currency"],
        "exchange": raw["exchange"], "marketTime": raw.get("marketTime"),
        "price": round(price, 4), "change": round(change, 4), "changePct": round(100 * change / prev, 3),
        "dayHigh": h[-1], "dayLow": l[-1], "high52": max(x["h"] for x in year), "low52": min(x["l"] for x in year),
        "volume": v[-1],
        "signals": sig, "groups": groups, "total": total, "tip": tip,
        "pivots": {kk: round(vv, 4) for kk, vv in pivots.items()},
        "chart": {
            "dates": dates, "o": rnd(o), "h": rnd(h), "l": rnd(l), "c": rnd(c),
            "sma20": rnd(mid), "sma50": rnd(s50), "sma200": rnd(s200),
            "bbu": rnd(bbu), "bbl": rnd(bbl), "rsi": rnd(r, 2),
            "macd": rnd(macd), "msig": rnd(msig), "mhist": rnd(mhist),
            "stk": rnd(k_, 2), "std": rnd(d_, 2), "v": v[-k:],
        },
    }


def label(score):
    if score >= 0.5:
        return "Starker Kauf"
    if score >= 0.15:
        return "Kaufen"
    if score <= -0.5:
        return "Starker Verkauf"
    if score <= -0.15:
        return "Verkaufen"
    return "Halten / Neutral"


def build_tip(total, sig, price, atr_v, pivots, rsi_v, cross_note, cur):
    sc = total["score"]
    reasons_buy = [f"{x['name']}: {x['note']}" for x in sig if x["signal"] > 0]
    reasons_sell = [f"{x['name']}: {x['note']}" for x in sig if x["signal"] < 0]
    prio = ("RSI", "MACD", "SMA 50/200", "Supertrend", "ADX", "Ichimoku", "Bollinger", "Stochastik", "Money Flow")

    def top(rs):
        rs = sorted(rs, key=lambda t: next((i for i, p in enumerate(prio) if t.startswith(p)), 99))
        return rs[:5]

    lines = []
    if sc >= 0.15:
        head = f"{total['label']}: {total['buy']} von {total['buy'] + total['sell'] + total['neutral']} Indikatoren zeigen nach oben."
        lines = top(reasons_buy)
        if atr_v:
            stop = price - 2 * atr_v
            target = price + 3 * atr_v
            res = [pv for kk, pv in pivots.items() if kk.startswith("R") and pv > price]
            plan = (f"Möglicher Einstieg um {fmt(price)} {cur}. Stop-Loss ca. {fmt(stop)} {cur} (2× ATR), "
                    f"Kursziel ca. {fmt(target)} {cur} (3× ATR)")
            if res:
                plan += f", nächster Widerstand {fmt(min(res))} {cur}"
            plan += "."
        else:
            plan = ""
    elif sc <= -0.15:
        head = f"{total['label']}: {total['sell']} von {total['buy'] + total['sell'] + total['neutral']} Indikatoren zeigen nach unten."
        lines = top(reasons_sell)
        if atr_v:
            sup = [pv for kk, pv in pivots.items() if kk.startswith("S") and pv < price]
            plan = (f"Bestehende Positionen absichern oder reduzieren. Wer investiert bleibt: Stop ca. "
                    f"{fmt(price - 1.5 * atr_v)} {cur}.")
            if sup:
                plan += f" Nächste Unterstützung {fmt(max(sup))} {cur}."
        else:
            plan = ""
    else:
        head = f"Halten / Abwarten: Signale sind gemischt ({total['buy']} Kauf, {total['sell']} Verkauf, {total['neutral']} neutral)."
        lines = top(reasons_buy)[:2] + top(reasons_sell)[:2]
        plan = "Auf klare Bestätigung warten (z. B. MACD-Kreuzung oder Ausbruch aus dem Donchian-Kanal)."
    warn = []
    if rsi_v is not None and rsi_v > 75 and sc > 0:
        warn.append("Achtung: RSI stark überkauft – Rücksetzer möglich, nicht hinterherlaufen.")
    if rsi_v is not None and rsi_v < 25 and sc < 0:
        warn.append("Achtung: RSI stark überverkauft – technische Gegenbewegung möglich.")
    if cross_note:
        warn.append(cross_note + ".")
    return {"headline": head, "reasons": lines, "plan": plan, "warnings": warn}


# ----------------------------------------------------------------------------
# Webserver
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, HTML.replace("__DEMO__", "true" if DEMO else "false"), "text/html; charset=utf-8")
        if u.path == "/api/data":
            qs = urllib.parse.parse_qs(u.query)
            syms = [s.strip().upper() for s in (qs.get("symbols", [""])[0]).split(",") if s.strip()][:40]

            def one(s):
                try:
                    return get_data(s)
                except Exception as e:
                    return {"symbol": s, "error": str(e)}
            with ThreadPoolExecutor(max_workers=8) as ex:
                out = list(ex.map(one, syms))
            return self._send(200, json.dumps({"time": time.time(), "demo": DEMO, "data": out}),
                              "application/json; charset=utf-8")
        self._send(404, "not found", "text/plain")


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aktien-Dashboard</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#2a3340;--text:#e6edf3;--muted:#8b98a8;
--up:#2ecc71;--down:#ff5c5c;--neutral:#e3b341;--accent:#58a6ff;--sma50:#f0883e;--sma200:#bc8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(13,17,23,.95);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:18px;margin:0 12px 0 0;white-space:nowrap}
.demo{background:var(--neutral);color:#000;border-radius:4px;padding:2px 6px;font-size:11px;font-weight:700;margin-left:6px}
input{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 10px;font-size:14px;width:230px;max-width:100%}
button{background:var(--accent);color:#04111f;border:0;border-radius:6px;padding:7px 12px;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
.status{margin-left:auto;color:var(--muted);font-size:12px;display:flex;gap:10px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:var(--up);display:inline-block}
.dot.paused{background:var(--neutral)} .dot.err{background:var(--down)}
main{padding:16px;max-width:1500px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;position:relative;transition:border-color .15s}
.card:hover,.card.sel{border-color:var(--accent)}
.card .top{display:flex;justify-content:space-between;gap:8px}
.card .sym{font-weight:700;font-size:15px}.card .nm{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}
.card .px{font-size:20px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.chg{font-size:12px;text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}.neu{color:var(--neutral)}
.badge{display:inline-block;padding:3px 8px;border-radius:5px;font-weight:700;font-size:12px}
.b-sb{background:#1f8b4c;color:#fff}.b-b{background:rgba(46,204,113,.18);color:var(--up)}
.b-n{background:rgba(227,179,65,.15);color:var(--neutral)}.b-s{background:rgba(255,92,92,.18);color:var(--down)}.b-ss{background:#b83232;color:#fff}
.meter{height:6px;border-radius:3px;background:linear-gradient(90deg,var(--down),var(--neutral),var(--up));position:relative;margin:8px 0 4px}
.meter i{position:absolute;top:-4px;width:4px;height:14px;background:#fff;border-radius:2px;transform:translateX(-2px)}
.rm{position:absolute;top:6px;right:8px;color:var(--muted);background:none;padding:0 4px;font-size:16px;display:none}
.card:hover .rm{display:block}
.card canvas{width:100%;height:44px;display:block;margin-top:6px}
.cnt{font-size:11px;color:var(--muted);display:flex;justify-content:space-between}
.flash-up{animation:fu 1s}.flash-down{animation:fd 1s}
@keyframes fu{from{background:rgba(46,204,113,.35)}}@keyframes fd{from{background:rgba(255,92,92,.35)}}
#detail{margin-top:18px;display:none}
.dhead{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:12px}
.dhead h2{margin:0;font-size:22px}.dhead .sub{color:var(--muted);font-size:13px}
.dgrid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:12px}
@media(max-width:1000px){.dgrid{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.panel h3{margin:0 0 8px;font-size:13px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.chart{width:100%;display:block}
.legend{font-size:11px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.legend b{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
td.v{font-variant-numeric:tabular-nums;white-space:nowrap}
tr.grp td{background:var(--panel2);font-weight:700;color:var(--accent)}
.sig{font-weight:700;white-space:nowrap}
.tip{border-left:4px solid var(--accent);padding:10px 12px;background:var(--panel2);border-radius:6px}
.tip .hl{font-size:16px;font-weight:700;margin-bottom:6px}
.tip ul{margin:6px 0;padding-left:18px}
.warn{color:var(--neutral);font-size:13px;margin-top:6px}
.sumrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:12px}
.sumrow .panel{padding:10px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px}.kv span:nth-child(odd){color:var(--muted)}
.tablewrap{overflow-x:auto}
footer{color:var(--muted);font-size:12px;text-align:center;padding:24px 16px}
.err{color:var(--down);font-size:13px}
.tabs{display:flex;gap:6px;margin-bottom:6px}.tabs button{padding:4px 10px;font-size:12px}
.tabs button:not(.on){background:transparent;color:var(--muted);border:1px solid var(--line)}
</style>
</head>
<body>
<header>
  <h1>📈 Aktien-Dashboard<span id="demoTag"></span></h1>
  <form id="addForm" style="display:flex;gap:6px;flex-wrap:wrap">
    <input id="symIn" placeholder="Symbol, z. B. AAPL, SAP.DE, BTC-USD" autocomplete="off">
    <button>Hinzufügen</button>
    <button type="button" class="ghost" id="resetBtn" title="Standardliste wiederherstellen">Standard</button>
  </form>
  <select id="sortSel" style="background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px">
    <option value="list">Sortierung: Liste</option>
    <option value="score">Sortierung: Signal-Stärke</option>
    <option value="chg">Sortierung: Tagesänderung</option>
  </select>
  <div class="status"><span class="dot" id="dot"></span><span id="stat">lade …</span>
    <button class="ghost" id="nowBtn">Jetzt aktualisieren</button></div>
</header>
<main>
  <div class="grid" id="grid"></div>
  <section id="detail"></section>
</main>
<footer>Daten: Yahoo Finance (evtl. verzögert). Aktualisierung alle 10 Sekunden, solange die Seite sichtbar ist.<br>
⚠️ Die Kauf-/Verkaufstipps werden automatisch aus technischen Indikatoren berechnet und sind <b>keine Anlageberatung</b>. Investieren auf eigenes Risiko.</footer>
<script>
const DEMO = __DEMO__;
const INTERVAL = 10;
const DEFAULT = ["NVDA","AAPL","BTC-USD","MSFT","AMZN","GOOGL","META","TSLA","SAP.DE","SIE.DE"];
let symbols = load("symbols", DEFAULT);
let selected = load("selected", null);
let sortBy = load("sort", "list");
let data = {}, lastPrice = {}, countdown = INTERVAL, busy = false, chartTab = load("tab", "rsi");

function load(k, d){ try{ const v = localStorage.getItem("ad_"+k); return v ? JSON.parse(v) : d; }catch(e){ return d; } }
function save(k, v){ try{ localStorage.setItem("ad_"+k, JSON.stringify(v)); }catch(e){} }
const $ = s => document.querySelector(s);
const nf = (x, d=2) => x==null||isNaN(x) ? "–" : x.toLocaleString("de-DE",{minimumFractionDigits:d, maximumFractionDigits:d});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function badgeCls(l){ return {"Starker Kauf":"b-sb","Kaufen":"b-b","Halten / Neutral":"b-n","Verkaufen":"b-s","Starker Verkauf":"b-ss"}[l] || "b-n"; }
function sigTxt(s){ return s>0 ? '<span class="sig up">▲ Kauf</span>' : s<0 ? '<span class="sig down">▼ Verkauf</span>' : '<span class="sig neu">● Neutral</span>'; }
if (DEMO) $("#demoTag").innerHTML = '<span class="demo">DEMO-DATEN</span>';
$("#sortSel").value = sortBy;

async function refresh(){
  if (busy || !symbols.length) return;
  busy = true; $("#stat").textContent = "aktualisiere …";
  try{
    const r = await fetch("/api/data?symbols=" + encodeURIComponent(symbols.join(",")));
    const j = await r.json();
    for (const d of j.data) data[d.symbol.toUpperCase()] = d;
    // Symbol-Aliase (Yahoo liefert evtl. andere Schreibweise)
    j.data.forEach((d,i)=>{ data[symbols[i]] = d; });
    $("#dot").className = "dot";
    $("#stat").textContent = "Stand " + new Date().toLocaleTimeString("de-DE");
    render();
  }catch(e){
    $("#dot").className = "dot err"; $("#stat").textContent = "Fehler: Server nicht erreichbar";
  }
  busy = false; countdown = INTERVAL;
}

setInterval(()=>{
  if (document.hidden){ $("#dot").className="dot paused"; $("#stat").textContent="pausiert (Tab nicht sichtbar)"; return; }
  countdown--;
  const s = $("#stat"); if (!busy && s.textContent.startsWith("Stand")) s.textContent = s.textContent.split(" · ")[0] + " · nächste in " + Math.max(countdown,0) + " s";
  if (countdown <= 0) refresh();
}, 1000);
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) refresh(); });

function render(){
  const g = $("#grid");
  let list = symbols.slice();
  if (sortBy === "score") list.sort((a,b)=>((data[b]?.total?.score)??-9)-((data[a]?.total?.score)??-9));
  if (sortBy === "chg") list.sort((a,b)=>((data[b]?.changePct)??-999)-((data[a]?.changePct)??-999));
  g.innerHTML = list.map(s => {
    const d = data[s];
    if (!d) return `<div class="card" data-s="${esc(s)}"><div class="sym">${esc(s)}</div><div class="nm">lade …</div></div>`;
    if (d.error) return `<div class="card" data-s="${esc(s)}"><button class="rm" data-rm="${esc(s)}">×</button><div class="sym">${esc(s)}</div><div class="err">⚠ ${esc(d.error)}</div></div>`;
    const cls = d.change >= 0 ? "up" : "down";
    const pos = ((d.total.score + 1) / 2 * 100).toFixed(1);
    let flash = "";
    if (lastPrice[s] != null && lastPrice[s] !== d.price) flash = d.price > lastPrice[s] ? "flash-up" : "flash-down";
    lastPrice[s] = d.price;
    return `<div class="card ${s===selected?"sel":""} ${flash}" data-s="${esc(s)}">
      <button class="rm" data-rm="${esc(s)}" title="entfernen">×</button>
      <div class="top"><div><div class="sym">${esc(d.symbol)}</div><div class="nm" title="${esc(d.name)}">${esc(d.name)}</div></div>
      <div><div class="px">${nf(d.price)} <small style="font-size:11px;color:var(--muted)">${esc(d.currency)}</small></div>
      <div class="chg ${cls}">${d.change>=0?"+":""}${nf(d.change)} (${d.change>=0?"+":""}${nf(d.changePct)} %)</div></div></div>
      <canvas data-spark="${esc(s)}"></canvas>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
        <span class="badge ${badgeCls(d.total.label)}">${esc(d.total.label)}</span>
        <span class="cnt"><span class="up">▲${d.total.buy}</span>&nbsp;<span class="neu">●${d.total.neutral}</span>&nbsp;<span class="down">▼${d.total.sell}</span></span>
      </div>
      <div class="meter"><i style="left:${pos}%"></i></div>
    </div>`;
  }).join("");
  g.querySelectorAll("canvas[data-spark]").forEach(cv => {
    const d = data[cv.dataset.spark]; if (!d || d.error) return;
    const c = d.chart.c.slice(-60);
    drawChart(cv, {n:c.length, series:[{v:c, color: c[c.length-1]>=c[0] ? "#2ecc71" : "#ff5c5c", w:1.5, fill:true}], pad:[2,2,2,2], noAxis:true});
  });
  renderDetail();
}

$("#grid").addEventListener("click", e => {
  const rm = e.target.closest("[data-rm]");
  if (rm){ e.stopPropagation(); symbols = symbols.filter(x=>x!==rm.dataset.rm); save("symbols",symbols);
    if (selected===rm.dataset.rm){ selected=null; save("selected",null);} render(); return; }
  const c = e.target.closest(".card"); if (!c) return;
  selected = selected === c.dataset.s ? null : c.dataset.s; save("selected", selected); render();
  if (selected) $("#detail").scrollIntoView({behavior:"smooth", block:"start"});
});
$("#addForm").addEventListener("submit", e => {
  e.preventDefault();
  const v = $("#symIn").value.split(/[ ,;]+/).map(s=>s.trim().toUpperCase()).filter(Boolean);
  for (const s of v) if (!symbols.includes(s)) symbols.push(s);
  save("symbols", symbols); $("#symIn").value = ""; render(); refresh();
});
$("#resetBtn").onclick = () => { symbols = DEFAULT.slice(); save("symbols", symbols); render(); refresh(); };
$("#nowBtn").onclick = refresh;
$("#sortSel").onchange = e => { sortBy = e.target.value; save("sort", sortBy); render(); };

function renderDetail(){
  const el = $("#detail");
  const d = selected && data[selected];
  if (!d || d.error){ el.style.display = "none"; return; }
  el.style.display = "block";
  const cls = d.change >= 0 ? "up" : "down";
  const groups = Object.entries(d.groups);
  const t = d.tip;
  const tipColor = d.total.score >= .15 ? "var(--up)" : d.total.score <= -.15 ? "var(--down)" : "var(--neutral)";
  const bygroup = {};
  d.signals.forEach(s => (bygroup[s.group] = bygroup[s.group] || []).push(s));
  const time = d.marketTime ? new Date(d.marketTime*1000).toLocaleString("de-DE") : "";
  el.innerHTML = `
  <div class="dhead">
    <div><h2>${esc(d.name)} <span class="sub">${esc(d.symbol)} · ${esc(d.exchange)}</span></h2>
    <div class="sub">Letzter Kurs: ${esc(time)}</div></div>
    <div style="text-align:right"><div style="font-size:28px;font-weight:800">${nf(d.price)} ${esc(d.currency)}</div>
    <div class="${cls}">${d.change>=0?"+":""}${nf(d.change)} (${d.change>=0?"+":""}${nf(d.changePct)} %)</div></div>
  </div>
  <div class="sumrow">
    <div class="panel"><h3>Gesamt</h3><span class="badge ${badgeCls(d.total.label)}" style="font-size:14px">${esc(d.total.label)}</span>
      <div class="meter"><i style="left:${((d.total.score+1)/2*100).toFixed(1)}%"></i></div>
      <div class="cnt"><span class="up">Kauf ${d.total.buy}</span><span class="neu">Neutral ${d.total.neutral}</span><span class="down">Verkauf ${d.total.sell}</span></div></div>
    ${groups.map(([g,s])=>`<div class="panel"><h3>${esc(g)}</h3><span class="badge ${badgeCls(s.label)}">${esc(s.label)}</span>
      <div class="meter"><i style="left:${((s.score+1)/2*100).toFixed(1)}%"></i></div>
      <div class="cnt"><span class="up">▲${s.buy}</span><span class="neu">●${s.neutral}</span><span class="down">▼${s.sell}</span></div></div>`).join("")}
  </div>
  <div class="dgrid">
    <div>
      <div class="panel">
        <h3>Kursverlauf (Tageskerzen)</h3>
        <div class="legend"><span><b style="background:#58a6ff"></b>SMA 20</span><span><b style="background:var(--sma50)"></b>SMA 50</span>
        <span><b style="background:var(--sma200)"></b>SMA 200</span><span><b style="background:rgba(88,166,255,.35)"></b>Bollinger (20,2)</span></div>
        <canvas class="chart" id="cMain" style="height:340px"></canvas>
        <canvas class="chart" id="cVol" style="height:60px"></canvas>
      </div>
      <div class="panel" style="margin-top:12px">
        <div class="tabs">${["rsi","macd","stoch"].map(k=>`<button data-tab="${k}" class="${chartTab===k?"on":""}">${{rsi:"RSI (14)",macd:"MACD (12,26,9)",stoch:"Stochastik"}[k]}</button>`).join("")}</div>
        <canvas class="chart" id="cInd" style="height:150px"></canvas>
      </div>
    </div>
    <div>
      <div class="panel tip" style="border-left-color:${tipColor}">
        <h3>💡 Tipp</h3>
        <div class="hl" style="color:${tipColor}">${esc(t.headline)}</div>
        ${t.reasons.length?`<div class="sub">Wichtigste Gründe:</div><ul>${t.reasons.map(r=>`<li>${esc(r)}</li>`).join("")}</ul>`:""}
        ${t.plan?`<div>${esc(t.plan)}</div>`:""}
        ${t.warnings.map(w=>`<div class="warn">⚠ ${esc(w)}</div>`).join("")}
        <div class="sub" style="margin-top:8px;font-size:11px">Keine Anlageberatung – nur technische Auswertung.</div>
      </div>
      <div class="panel" style="margin-top:12px"><h3>Kennzahlen</h3><div class="kv">
        <span>Tageshoch / -tief</span><span>${nf(d.dayHigh)} / ${nf(d.dayLow)}</span>
        <span>52 Wochen Hoch / Tief</span><span>${nf(d.high52)} / ${nf(d.low52)}</span>
        <span>Abstand 52W-Hoch</span><span>${nf((d.price/d.high52-1)*100)} %</span>
        <span>Volumen heute</span><span>${nf(d.volume,0)}</span>
      </div></div>
      <div class="panel" style="margin-top:12px"><h3>Pivot-Punkte (klassisch)</h3><div class="kv">
        ${Object.entries(d.pivots).map(([k,v])=>`<span>${k}</span><span class="${k[0]==="R"?"down":k[0]==="S"?"up":""}">${nf(v)}${Math.abs(v-d.price)/d.price<0.01?" ← nahe Kurs":""}</span>`).join("")}
      </div></div>
    </div>
  </div>
  <div class="panel" style="margin-top:12px"><h3>Alle Indikatoren (${d.signals.length})</h3><div class="tablewrap">
    <table><thead><tr><th>Indikator</th><th>Wert</th><th>Signal</th><th>Bedeutung</th></tr></thead><tbody>
    ${Object.entries(bygroup).map(([g,arr])=>`<tr class="grp"><td colspan="4">${esc(g)}</td></tr>` +
      arr.map(s=>`<tr><td>${esc(s.name)}</td><td class="v">${esc(s.value)}</td><td>${sigTxt(s.signal)}</td><td>${esc(s.note)}</td></tr>`).join("")).join("")}
    </tbody></table></div></div>`;
  el.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { chartTab = b.dataset.tab; save("tab", chartTab); renderDetail(); });
  const ch = d.chart, n = ch.c.length;
  drawChart($("#cMain"), {n, candles: ch, band:[ch.bbu, ch.bbl],
    series:[{v:ch.sma20,color:"#58a6ff",w:1},{v:ch.sma50,color:"#f0883e",w:1.5},{v:ch.sma200,color:"#bc8cff",w:1.5}],
    lastLine: d.price, dates: ch.dates});
  drawChart($("#cVol"), {n, bars: ch.v.map((v,i)=>({v, color: ch.c[i]>=ch.o[i]?"rgba(46,204,113,.5)":"rgba(255,92,92,.5)"})), zeroBase:true, noAxis:true, pad:[4,58,2,2]});
  if (chartTab === "rsi") drawChart($("#cInd"), {n, series:[{v:ch.rsi,color:"#e3b341",w:1.5}], hlines:[[70,"rgba(255,92,92,.6)"],[30,"rgba(46,204,113,.6)"],[50,"rgba(139,152,168,.3)"]], yMin:0, yMax:100});
  if (chartTab === "macd") drawChart($("#cInd"), {n, series:[{v:ch.macd,color:"#58a6ff",w:1.5},{v:ch.msig,color:"#f0883e",w:1.2}],
    bars: ch.mhist.map(v=>({v, color: v>=0?"rgba(46,204,113,.6)":"rgba(255,92,92,.6)"})), hlines:[[0,"rgba(139,152,168,.4)"]]});
  if (chartTab === "stoch") drawChart($("#cInd"), {n, series:[{v:ch.stk,color:"#58a6ff",w:1.5},{v:ch.std,color:"#f0883e",w:1.2}], hlines:[[80,"rgba(255,92,92,.6)"],[20,"rgba(46,204,113,.6)"]], yMin:0, yMax:100});
}

// Einfacher Canvas-Chart ohne externe Bibliotheken
function drawChart(cv, o){
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = W*dpr; cv.height = H*dpr;
  const x = cv.getContext("2d"); x.scale(dpr, dpr); x.clearRect(0,0,W,H);
  const [pt, pr, pb, pl] = o.pad || [8, 58, o.dates ? 18 : 6, 4];
  let vals = [];
  (o.series||[]).forEach(s => s.v.forEach(v => v!=null && vals.push(v)));
  if (o.candles){ o.candles.h.forEach(v=>vals.push(v)); o.candles.l.forEach(v=>vals.push(v)); }
  if (o.band) o.band.forEach(b => b.forEach(v => v!=null && vals.push(v)));
  if (o.bars) o.bars.forEach(b => b.v!=null && vals.push(b.v));
  (o.hlines||[]).forEach(h => vals.push(h[0]));
  if (o.zeroBase) vals.push(0);
  if (!vals.length) return;
  let lo = o.yMin ?? Math.min(...vals), hi = o.yMax ?? Math.max(...vals);
  if (hi === lo){ hi += 1; lo -= 1; }
  if (o.yMin == null && !o.zeroBase){ const m = (hi-lo)*0.05; hi += m; lo -= m; }
  const n = o.n, cw = (W-pl-pr)/n;
  const X = i => pl + cw*(i+0.5), Y = v => pt + (hi - v)/(hi - lo)*(H-pt-pb);
  if (!o.noAxis){
    x.strokeStyle = "rgba(139,152,168,.12)"; x.fillStyle = "#8b98a8"; x.font = "11px system-ui"; x.lineWidth = 1;
    for (let k=0;k<=4;k++){ const v = lo + (hi-lo)*k/4, y = Y(v);
      x.beginPath(); x.moveTo(pl,y); x.lineTo(W-pr,y); x.stroke();
      x.fillText(nf(v, Math.abs(hi) < 10 ? 2 : Math.abs(hi) < 1000 ? 1 : 0), W-pr+4, y+4); }
    if (o.dates){ const step = Math.ceil(n/6);
      for (let i=0;i<n;i+=step) x.fillText(o.dates[i], Math.max(pl, X(i)-20), H-4); }
  }
  (o.hlines||[]).forEach(([v,c]) => { x.strokeStyle=c; x.setLineDash([4,4]); x.beginPath(); x.moveTo(pl,Y(v)); x.lineTo(W-pr,Y(v)); x.stroke(); x.setLineDash([]); });
  if (o.band){ const [u,l] = o.band; x.fillStyle = "rgba(88,166,255,.08)"; x.strokeStyle = "rgba(88,166,255,.35)"; x.beginPath();
    let started=false; for (let i=0;i<n;i++) if (u[i]!=null){ started ? x.lineTo(X(i),Y(u[i])) : x.moveTo(X(i),Y(u[i])); started=true; }
    for (let i=n-1;i>=0;i--) if (l[i]!=null) x.lineTo(X(i),Y(l[i]));
    x.closePath(); x.fill(); }
  if (o.bars){ const z = Y(Math.max(lo, 0));
    o.bars.forEach((b,i) => { if (b.v==null) return; x.fillStyle = b.color; const y = Y(b.v); x.fillRect(X(i)-cw*0.4, Math.min(y,z), Math.max(cw*0.8,1), Math.abs(z-y)||1); }); }
  if (o.candles){ const c = o.candles;
    for (let i=0;i<n;i++){ const up = c.c[i] >= c.o[i]; x.strokeStyle = x.fillStyle = up ? "#2ecc71" : "#ff5c5c";
      x.beginPath(); x.moveTo(X(i),Y(c.h[i])); x.lineTo(X(i),Y(c.l[i])); x.stroke();
      const y1 = Y(Math.max(c.o[i],c.c[i])), y2 = Y(Math.min(c.o[i],c.c[i]));
      x.fillRect(X(i)-Math.max(cw*0.35,0.5), y1, Math.max(cw*0.7,1), Math.max(y2-y1,1)); } }
  (o.series||[]).forEach(s => {
    x.strokeStyle = s.color; x.lineWidth = s.w || 1; x.beginPath(); let st=false, first=null, lastI=null;
    s.v.forEach((v,i) => { if (v==null) return; if (!st){ x.moveTo(X(i),Y(v)); st=true; first=i; } else x.lineTo(X(i),Y(v)); lastI=i; });
    x.stroke();
    if (s.fill && st){ x.lineTo(X(lastI), H); x.lineTo(X(first), H); x.closePath(); x.globalAlpha=.12; x.fillStyle=s.color; x.fill(); x.globalAlpha=1; }
  });
  if (o.lastLine != null){ const y = Y(o.lastLine); x.strokeStyle="rgba(230,237,243,.5)"; x.setLineDash([2,3]); x.beginPath(); x.moveTo(pl,y); x.lineTo(W-pr,y); x.stroke(); x.setLineDash([]);
    x.fillStyle = "#58a6ff"; x.fillRect(W-pr+1, y-8, pr-2, 16); x.fillStyle="#04111f"; x.font="bold 11px system-ui"; x.fillText(nf(o.lastLine), W-pr+4, y+4); }
}
window.addEventListener("resize", () => { clearTimeout(window._rt); window._rt = setTimeout(render, 150); });

render(); refresh();
</script>
</body>
</html>
"""


def main():
    global DEMO
    ap = argparse.ArgumentParser(description="Aktien-Dashboard mit Indikatoren")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="simulierte Kurse (ohne Internet)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    DEMO = a.demo
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}"
    print(f"Aktien-Dashboard läuft auf {url}  {'(DEMO-Modus)' if DEMO else ''}")
    print("Beenden mit Strg+C")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
