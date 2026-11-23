#!/usr/bin/env python3
"""
Intraday live estimate of the Fear & Greed composite.

Reads data/fear.json (built daily by build.py from FRED), pulls delayed
intraday quotes from Yahoo Finance's public chart endpoint, treats the live
prints as one more trading day, scores each component against the same
trailing window, and writes data/live.json. Appends one point per run to
today's intraday path.

Standard library only. No API key needed.
Usage: python scripts/live.py [--fear data/fear.json] [--out data/live.json]
Exit code 0 always when there is simply nothing new (market closed etc.).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SYMBOLS = {
    "spx": "^GSPC", "vix": "^VIX", "vxv": "^VIX3M", "tnx": "^TNX",
    "hyg": "HYG", "ief": "IEF",
}
HYG_SPREAD_DURATION = 3.5   # converts HYG/IEF relative move to an OAS change estimate
BOND_DURATION = 8.5
MIN_WINDOW = 252

# Must match build.py COMPONENTS order/direction
DIRECTION = {"momentum": +1, "strength": +1, "vix": -1, "term": -1,
             "junk": -1, "haven": +1, "fci": -1}


def label(s):
    if s is None:
        return None
    if s < 25:
        return "Extreme Fear"
    if s < 45:
        return "Fear"
    if s < 56:
        return "Neutral"
    if s < 76:
        return "Greed"
    return "Extreme Greed"


def quote(sym, fetcher=None):
    """Return dict(price, prev_close, time) from Yahoo chart meta."""
    if fetcher:
        return fetcher(sym)
    path = "/v8/finance/chart/%s?%s" % (
        urllib.parse.quote(sym), urllib.parse.urlencode({"interval": "5m", "range": "1d"}))
    last_err = None
    for attempt in range(3):
        for host in HOSTS:
            try:
                req = urllib.request.Request(host + path, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    meta = json.load(r)["chart"]["result"][0]["meta"]
                return {
                    "price": float(meta["regularMarketPrice"]),
                    "prev_close": float(meta.get("chartPreviousClose") or meta.get("previousClose")),
                    "time": int(meta["regularMarketTime"]),
                }
            except Exception as e:  # noqa: BLE001
                last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("Yahoo quote failed for %s: %s" % (sym, last_err))


def pct_score(window, v, direction):
    vals = sorted(window + [v])
    n = len(vals)
    if n < MIN_WINDOW:
        return None
    lo, hi = bisect_left(vals, v), bisect_right(vals, v)
    p = (lo + (hi - lo) / 2.0) / n * 100.0
    return p if direction > 0 else 100.0 - p


def compute(fear, q):
    S = fear["state"]
    spx, vix, vxv = q["spx"]["price"], q["vix"]["price"], q["vxv"]["price"]
    tnx = q["tnx"]["price"]
    if tnx > 20:          # older Yahoo convention quoted ^TNX as yield x 10
        tnx /= 10.0

    closes = S["spx_closes"]
    ma125 = (sum(closes[-124:]) + spx) / 125.0
    hi252 = max(closes[-251:] + [spx])
    vma50 = (sum(S["vix_closes"][-49:]) + vix) / 50.0

    ratio_now = q["hyg"]["price"] / q["ief"]["price"]
    ratio_prev = q["hyg"]["prev_close"] / q["ief"]["prev_close"]
    hy_est = S["hy_last"] - (ratio_now / ratio_prev - 1) * 100.0 / HYG_SPREAD_DURATION

    y_then = S["y10_tail"][0]
    stock20 = (spx / closes[-20] - 1) * 100.0
    bond20 = -BOND_DURATION * (tnx - y_then) + y_then * 20 / 252.0

    raw = {
        "momentum": (spx / ma125 - 1) * 100.0,
        "strength": (spx / hi252 - 1) * 100.0,
        "vix": (vix / vma50 - 1) * 100.0,
        "term": vix / vxv,
        "junk": hy_est,
        "haven": stock20 - bond20,
        "fci": S["nfci_last"],
    }
    comps, vals = [], []
    for c in fear["latest"]["components"]:
        k = c["key"]
        s = pct_score(S["raw_windows"][k], raw[k], DIRECTION[k])
        comps.append({"key": k, "raw": round(raw[k], 3),
                      "score": None if s is None else round(s, 1),
                      "live": k != "fci"})
        if s is not None:
            vals.append(s)
    score = sum(vals) / len(vals) if len(vals) >= 5 else None
    return score, comps, {
        "spx": round(spx, 2), "vix": round(vix, 2), "vix3m": round(vxv, 2),
        "tnx": round(tnx, 3), "hyg": round(q["hyg"]["price"], 2),
        "ief": round(q["ief"]["price"], 2), "hy_oas_est": round(hy_est, 3),
    }


def run(fear_path, out_path, fetcher=None, now=None):
    with open(fear_path) as f:
        fear = json.load(f)
    if "state" not in fear:
        print("fear.json has no state block yet; run build.py first")
        return False

    q = {k: quote(s, fetcher) for k, s in SYMBOLS.items()}
    qt = datetime.fromtimestamp(q["spx"]["time"], ET)
    qdate = qt.date().isoformat()

    if qdate <= fear["state"]["date"]:
        print("Latest quote (%s) is already covered by the official daily close (%s); nothing to do"
              % (qdate, fear["state"]["date"]))
        return False

    old = None
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            old = None
    if old and old.get("quote_time_epoch") == q["spx"]["time"]:
        print("No new quote since last run (%s); nothing to do" % qt.strftime("%Y-%m-%d %H:%M ET"))
        return False

    score, comps, inputs = compute(fear, q)
    if score is None:
        print("Not enough components to score")
        return False

    path = old["path"] if (old and old.get("date") == qdate) else []
    hhmm = qt.strftime("%H:%M")
    path = [p for p in path if p["t"] != hhmm] + [{"t": hhmm, "score": round(score, 1)}]

    now = now or datetime.now(timezone.utc)
    out = {
        "date": qdate,
        "official_date": fear["state"]["date"],
        "official_score": fear["latest"]["score"],
        "updated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
        "quote_time_et": qt.strftime("%Y-%m-%d %H:%M"),
        "quote_time_epoch": q["spx"]["time"],
        "score": round(score, 1),
        "label": label(score),
        "components": comps,
        "inputs": inputs,
        "path": path,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("Live %s %s ET: %.1f (%s)" % (qdate, hhmm, score, label(score)))
    return True


def main():
    fear_path, out_path = "data/fear.json", "data/live.json"
    if "--fear" in sys.argv:
        fear_path = sys.argv[sys.argv.index("--fear") + 1]
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]
    run(fear_path, out_path)


if __name__ == "__main__":
    main()
