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
import http.cookiejar
import json
import os
import re
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
REQUIRED = ("spx", "vix", "vxv")   # others fall back to the last official value
PACE_SECONDS = 1.0                 # gap between symbols (only matters when Yahoo is used)
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/%s.json"
CNBC_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
CNBC_CODE = {"^GSPC": ".SPX", "^VIX": ".VIX", "^VIX3M": ".VIX3M", "^TNX": "US10Y",
             "HYG": "HYG", "IEF": "IEF"}
CBOE_CODE = {"^GSPC": "_SPX", "^VIX": "_VIX", "^VIX3M": "_VIX3M", "^TNX": "_TNX"}
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


_OPENER = None


def _opener():
    """URL opener with a cookie jar, primed with Yahoo's consent cookie (reduces 429s)."""
    global _OPENER
    if _OPENER is None:
        jar = http.cookiejar.CookieJar()
        _OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            _OPENER.open(urllib.request.Request("https://fc.yahoo.com", headers={"User-Agent": UA}), timeout=15)
        except Exception:  # noqa: BLE001  (fc.yahoo.com answers 404 but still sets the cookie)
            pass
    return _OPENER


def _get_json(url, extra=None):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "en-US,en;q=0.9"}
    h.update(extra or {})
    req = urllib.request.Request(url, headers=h)
    with _opener().open(req, timeout=30) as r:
        return json.load(r)


def quote_yahoo(sym):
    path = "/v8/finance/chart/%s?%s" % (
        urllib.parse.quote(sym), urllib.parse.urlencode({"interval": "5m", "range": "1d"}))
    last_err = None
    for host in HOSTS:
        try:
            meta = _get_json(host + path)["chart"]["result"][0]["meta"]
            return {
                "price": float(meta["regularMarketPrice"]),
                "prev_close": float(meta.get("chartPreviousClose") or meta.get("previousClose")),
                "time": int(meta["regularMarketTime"]),
                "src": "yahoo",
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError("yahoo: %s" % last_err)


def quote_cboe(sym):
    """CBOE delayed-quote feed (about 15 min delay). Indices use a leading underscore."""
    code = CBOE_CODE.get(sym, sym)
    d = _get_json(CBOE_URL % code, {"Referer": "https://www.cboe.com/", "Origin": "https://www.cboe.com"})["data"]
    t = datetime.fromisoformat(str(d["last_trade_time"])[:19]).replace(tzinfo=ET)
    return {
        "price": float(d["current_price"]),
        "prev_close": float(d["prev_day_close"]),
        "time": int(t.timestamp()),
        "src": "cboe",
    }


_CNBC_CACHE = None


def _num(x):
    if x is None:
        return None
    t = str(x).replace(",", "").replace("%", "").strip()
    return float(t) if t not in ("", "UNCH", "--") else None


def _iso_time(t):
    t = str(t).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?([+-]\d{2}):?(\d{2})$", t)
    if m:
        return datetime.fromisoformat("%s%s:%s" % m.groups())
    return datetime.fromisoformat(t[:19]).replace(tzinfo=ET)


def _cnbc_batch():
    """One request for all symbols via CNBC's public quote service."""
    global _CNBC_CACHE
    if isinstance(_CNBC_CACHE, Exception):
        raise _CNBC_CACHE          # don't re-hit a source that already refused this run
    if _CNBC_CACHE is None:
        syms = "|".join(CNBC_CODE[s] for s in SYMBOLS.values())
        url = CNBC_URL + "?" + urllib.parse.urlencode({
            "symbols": syms, "requestMethod": "itv", "noform": "1", "partnerId": "2",
            "fund": "1", "exthrs": "1", "output": "json", "events": "1"})
        try:
            j = _get_json(url, {"Referer": "https://www.cnbc.com/"})
            rows = j["FormattedQuoteResult"]["FormattedQuote"]
        except Exception as e:  # noqa: BLE001
            _CNBC_CACHE = RuntimeError("cnbc: %s" % e)
            raise _CNBC_CACHE
        if isinstance(rows, dict):
            rows = [rows]
        _CNBC_CACHE = {r.get("symbol"): r for r in rows}
    return _CNBC_CACHE


def quote_cnbc(sym):
    r = _cnbc_batch().get(CNBC_CODE[sym])
    if not r:
        raise RuntimeError("cnbc: symbol %s not in response" % CNBC_CODE[sym])
    last = _num(r.get("last"))
    prev = _num(r.get("previous_day_closing"))
    if prev is None and last is not None and _num(r.get("change")) is not None:
        prev = last - _num(r.get("change"))
    if last is None or prev is None or not r.get("last_time"):
        raise RuntimeError("cnbc: incomplete quote for %s" % CNBC_CODE[sym])
    return {"price": last, "prev_close": prev,
            "time": int(_iso_time(r["last_time"]).timestamp()), "src": "cnbc"}


SOURCES = (("cnbc", quote_cnbc), ("yahoo", quote_yahoo), ("cboe", quote_cboe))


def quote(sym, fetcher=None):
    """Try each free source in turn; first success wins."""
    if fetcher:
        return fetcher(sym)
    errs = []
    for name, fn in SOURCES:
        try:
            return fn(sym)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            errs.append(msg if msg.startswith(name) else "%s: %s" % (name, msg))
    raise RuntimeError("quote failed for %s (%s)" % (sym, "; ".join(errs)))


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
    if q.get("tnx"):
        tnx = q["tnx"]["price"]
        if tnx > 20:      # ^TNX / _TNX are sometimes quoted as yield x 10
            tnx /= 10.0
    else:
        tnx = S["y10_tail"][-1]     # no live yield: hold last official close

    closes = S["spx_closes"]
    ma125 = (sum(closes[-124:]) + spx) / 125.0
    hi252 = max(closes[-251:] + [spx])
    vma50 = (sum(S["vix_closes"][-49:]) + vix) / 50.0

    if q.get("hyg") and q.get("ief"):
        ratio_now = q["hyg"]["price"] / q["ief"]["price"]
        ratio_prev = q["hyg"]["prev_close"] / q["ief"]["prev_close"]
        hy_est = S["hy_last"] - (ratio_now / ratio_prev - 1) * 100.0 / HYG_SPREAD_DURATION
    else:
        hy_est = S["hy_last"]       # no live credit quote: hold last official OAS

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
        is_live = {"fci": False, "junk": bool(q.get("hyg") and q.get("ief"))}.get(k, True)
        comps.append({"key": k, "raw": round(raw[k], 3),
                      "score": None if s is None else round(s, 1),
                      "live": is_live})
        if s is not None:
            vals.append(s)
    score = sum(vals) / len(vals) if len(vals) >= 5 else None
    return score, comps, {
        "spx": round(spx, 2), "vix": round(vix, 2), "vix3m": round(vxv, 2),
        "tnx": round(tnx, 3),
        "hyg": round(q["hyg"]["price"], 2) if q.get("hyg") else None,
        "ief": round(q["ief"]["price"], 2) if q.get("ief") else None,
        "hy_oas_est": round(hy_est, 3),
        "sources": {k: v["src"] for k, v in q.items() if v and "src" in v},
        "missing": [k for k in SYMBOLS if not q.get(k)],
    }


def run(fear_path, out_path, fetcher=None, now=None):
    with open(fear_path) as f:
        fear = json.load(f)
    if "state" not in fear:
        print("fear.json has no state block yet; run build.py first")
        return False

    q = {}
    for i, (k, sym) in enumerate(SYMBOLS.items()):
        if i and not fetcher:
            time.sleep(PACE_SECONDS)
        try:
            q[k] = quote(sym, fetcher)
            print("%-6s %-7s %10.3f  via %s" % (k, sym, q[k]["price"], q[k].get("src", "test")))
        except Exception as e:  # noqa: BLE001
            if k in REQUIRED:
                raise
            q[k] = None
            print("%-6s %-7s unavailable, holding last official value (%s)" % (k, sym, e))
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
