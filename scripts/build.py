#!/usr/bin/env python3
"""
Fear & Greed composite builder.

Pulls free daily/weekly series from FRED, computes 7 components, scores each
0-100 as a percentile rank vs its trailing 2-year window (100 = greed,
0 = fear), averages them into one composite, and writes data/fear.json.

Standard library only. Needs env var FRED_API_KEY.
Usage: python scripts/build.py [--out data/fear.json]
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import date, datetime, timezone

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {
    "SP500": "S&P 500 index",
    "VIXCLS": "CBOE VIX",
    "VXVCLS": "CBOE 3-month VIX (VIX3M)",
    "BAMLH0A0HYM2": "ICE BofA US High Yield OAS",
    "DGS10": "10-year Treasury yield",
    "NFCI": "Chicago Fed National Financial Conditions Index (weekly)",
}

WINDOW = 504          # trailing trading days for percentile scoring (~2 yr)
MIN_WINDOW = 252      # need at least 1 yr of history before scoring
FFILL_LIMIT = 10      # max trading days to carry a stale daily value forward
FFILL_LIMIT_WEEKLY = 15
BOND_DURATION = 8.5   # approx modified duration of a 10y Treasury

# key, label, description, direction (+1: higher raw = greed, -1: higher raw = fear)
COMPONENTS = [
    ("momentum", "Market momentum",
     "S&P 500 vs its 125-day moving average (%)", +1),
    ("strength", "Price strength",
     "S&P 500 distance from its 52-week high (%)", +1),
    ("vix", "Volatility",
     "VIX vs its 50-day moving average (%)", -1),
    ("term", "Vol term structure",
     "VIX / VIX3M ratio (>1 = inverted, stress)", -1),
    ("junk", "Junk bond demand",
     "High-yield OAS (%), higher = fear", -1),
    ("haven", "Safe haven demand",
     "20-day S&P 500 return minus 20-day 10y Treasury return (pp)", +1),
    ("fci", "Financial conditions",
     "Chicago Fed NFCI (above 0 = tighter than average)", -1),
]


def fetch_series(sid, key, start="1990-01-01", retries=3):
    q = urllib.parse.urlencode({
        "series_id": sid, "api_key": key, "file_type": "json",
        "observation_start": start,
    })
    last_err = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(FRED_URL + "?" + q, timeout=60) as r:
                obs = json.load(r)["observations"]
            out = {}
            for o in obs:
                v = o["value"]
                if v not in (".", ""):
                    out[o["date"]] = float(v)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("FRED fetch failed for %s: %s" % (sid, last_err))


def align(dates, series, limit):
    """Forward-fill a {date: value} dict onto the trading-date axis."""
    out, last_v, age = [], None, 0
    for d in dates:
        if d in series:
            last_v, age = series[d], 0
        else:
            age += 1
        out.append(last_v if (last_v is not None and age <= limit) else None)
    return out


def align_weekly(dates, series, limit):
    """Carry weekly values onto trading dates (value dated <= d)."""
    keys = sorted(series)
    out = []
    for d in dates:
        i = bisect_right(keys, d) - 1
        if i < 0:
            out.append(None)
            continue
        k = keys[i]
        gap = (date.fromisoformat(d) - date.fromisoformat(k)).days
        out.append(series[k] if gap <= limit * 7 // 5 + 7 else None)
    return out


def sma(x, n):
    out, s, q = [None] * len(x), 0.0, []
    for i, v in enumerate(x):
        if v is None:
            s, q = 0.0, []
            continue
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.pop(0)
        if len(q) == n:
            out[i] = s / n
    return out


def rolling_max(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        w = [v for v in x[max(0, i - n + 1):i + 1] if v is not None]
        if x[i] is not None and len(w) >= n * 0.9:
            out[i] = max(w)
    return out


def pct_rank_scores(raw, direction):
    """Percentile of each value vs its trailing WINDOW (inclusive), 0-100."""
    out = [None] * len(raw)
    sorted_vals = []
    win = deque()
    for i, v in enumerate(raw):
        # evict values older than WINDOW
        while win and win[0][0] <= i - WINDOW:
            _, ov = win.popleft()
            j = bisect_left(sorted_vals, ov)
            sorted_vals.pop(j)
        if v is None:
            continue
        win.append((i, v))
        j = bisect_left(sorted_vals, v)
        sorted_vals.insert(j, v)
        n = len(sorted_vals)
        if n < MIN_WINDOW:
            continue
        lo = bisect_left(sorted_vals, v)
        hi = bisect_right(sorted_vals, v)
        p = (lo + (hi - lo) / 2.0) / n * 100.0  # mid-rank percentile
        out[i] = p if direction > 0 else 100.0 - p
    return out


def label(score):
    if score is None:
        return None
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 56:
        return "Neutral"
    if score < 76:
        return "Greed"
    return "Extreme Greed"


def r(x, nd=2):
    return None if x is None else round(x, nd)


def build(data):
    spx = data["SP500"]
    dates = sorted(spx)
    px = [spx[d] for d in dates]
    vix = align(dates, data["VIXCLS"], FFILL_LIMIT)
    vxv = align(dates, data["VXVCLS"], FFILL_LIMIT)
    hy = align(dates, data["BAMLH0A0HYM2"], FFILL_LIMIT)
    y10 = align(dates, data["DGS10"], FFILL_LIMIT)
    nfci = align_weekly(dates, data["NFCI"], FFILL_LIMIT_WEEKLY)

    ma125 = sma(px, 125)
    hi252 = rolling_max(px, 252)
    vma50 = sma(vix, 50)

    n = len(dates)
    raw = {k: [None] * n for k, *_ in COMPONENTS}
    for i in range(n):
        if ma125[i]:
            raw["momentum"][i] = (px[i] / ma125[i] - 1) * 100
        if hi252[i]:
            raw["strength"][i] = (px[i] / hi252[i] - 1) * 100
        if vix[i] is not None and vma50[i]:
            raw["vix"][i] = (vix[i] / vma50[i] - 1) * 100
        if vix[i] is not None and vxv[i]:
            raw["term"][i] = vix[i] / vxv[i]
        raw["junk"][i] = hy[i]
        if i >= 20 and y10[i] is not None and y10[i - 20] is not None:
            stock = (px[i] / px[i - 20] - 1) * 100
            bond = -BOND_DURATION * (y10[i] - y10[i - 20]) + y10[i - 20] * 20 / 252
            raw["haven"][i] = stock - bond
        raw["fci"][i] = nfci[i]

    scores = {k: pct_rank_scores(raw[k], d) for k, _, _, d in COMPONENTS}

    comp = [None] * n
    for i in range(n):
        vals = [scores[k][i] for k, *_ in COMPONENTS if scores[k][i] is not None]
        if len(vals) >= 5:  # require most components present
            comp[i] = sum(vals) / len(vals)

    first = next((i for i, v in enumerate(comp) if v is not None), None)
    if first is None:
        raise RuntimeError("Not enough history to score the composite")
    idx = list(range(first, n))
    last = max(i for i in idx if comp[i] is not None)

    def ago(days):
        j = last - days
        while j >= first and comp[j] is None:
            j -= 1
        return None if j < first else {"date": dates[j], "score": r(comp[j], 1)}

    latest = {
        "date": dates[last],
        "score": r(comp[last], 1),
        "label": label(comp[last]),
        "spx": r(px[last]),
        "compare": {
            "prev": ago(1), "week": ago(5), "month": ago(21), "year": ago(252),
        },
        "components": [
            {
                "key": k, "name": nm, "desc": ds,
                "raw": r(raw[k][last], 3),
                "score": r(scores[k][last], 1),
                "label": label(scores[k][last]),
                "week_ago": r(scores[k][last - 5], 1) if last - 5 >= 0 else None,
            }
            for k, nm, ds, _ in COMPONENTS
        ],
    }

    history = {
        "dates": [dates[i] for i in idx],
        "score": [r(comp[i], 1) for i in idx],
        "spx": [r(px[i]) for i in idx],
        "components": {k: [r(scores[k][i], 1) for i in idx] for k, *_ in COMPONENTS},
        # raw inputs used by the page's buy-signal confirmations
        "raw": {"term": [r(raw["term"][i], 4) for i in idx],
                "junk": [r(raw["junk"][i], 3) for i in idx]},
    }

    # Trailing inputs the intraday script (live.py) needs to score a live print
    # exactly like a daily close: it treats the live value as day n+1.
    def tail_nonnull(x, k):
        return [r(v, 4) for v in x[:last + 1] if v is not None][-k:]

    # Sensitivity of VIX3M to VIX (daily log changes, trailing window). live.py
    # uses it to estimate VIX3M when only VIX is available intraday.
    xs, ys = [], []
    for i in range(max(1, last - WINDOW + 1), last + 1):
        if vix[i] and vix[i - 1] and vxv[i] and vxv[i - 1]:
            xs.append(math.log(vix[i] / vix[i - 1]))
            ys.append(math.log(vxv[i] / vxv[i - 1]))
    vxv_beta = 0.6
    if len(xs) > 100:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0:
            vxv_beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx

    state = {
        "date": dates[last],
        "vix_last": r(vix[last], 4),
        "vxv_beta": round(vxv_beta, 3),
        "spx_closes": [r(v, 4) for v in px[max(0, last - 259):last + 1]],
        "vix_closes": tail_nonnull(vix, 60),
        "vxv_last": r(vxv[last], 4),
        "y10_tail": [r(v, 4) for v in y10[max(0, last - 19):last + 1]],
        "hy_last": r(hy[last], 4),
        "nfci_last": r(nfci[last], 4),
        "raw_windows": {k: tail_nonnull(raw[k], WINDOW - 1) for k, *_ in COMPONENTS},
    }

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "state": state,
        "method": {
            "window_days": WINDOW,
            "scoring": "percentile rank vs trailing window; 100 = greed",
            "bands": {"Extreme Fear": "0-24", "Fear": "25-44", "Neutral": "45-55",
                      "Greed": "56-75", "Extreme Greed": "76-100"},
            "sources": {k: v for k, v in SERIES.items()},
        },
        "latest": latest,
        "history": history,
    }


def main():
    out = "data/fear.json"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        sys.exit("FRED_API_KEY not set")
    data = {}
    for sid in SERIES:
        data[sid] = fetch_series(sid, key)
        print("%-14s %6d obs, last %s" % (sid, len(data[sid]), max(data[sid])))
    result = build(data)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, separators=(",", ":"))
    L = result["latest"]
    print("Composite %s on %s: %s (%s)" % (out, L["date"], L["score"], L["label"]))


if __name__ == "__main__":
    main()
