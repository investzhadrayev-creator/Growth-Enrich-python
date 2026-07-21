"""
macro_prices.py — risk-free rate (FRED) + adjusted price series (Tiingo).

WHY THIS EXISTS: these two calls used to be made from the n8n `Gather Data` Code node, which
meant the FRED and Tiingo keys had to live inside n8n. As of n8n 2.x, Code nodes run inside a
**task runner** — a separate sandboxed process that deliberately does NOT inherit the main
container's environment ("access to env vars denied"). Passing the keys any other way would put
them back into the exported workflow JSON.

The fix is architectural, not a workaround: a key belongs to the service that USES it. These
calls now happen here, on the Railway service, which reads its own environment. n8n orchestrates
and never sees a key.

ENV (set on the growth-enrich Railway service -> Variables):
  FRED_KEY       — https://fred.stlouisfed.org/docs/api/api_key.html (free)
  TIINGO_TOKEN   — https://www.tiingo.com (free tier is enough)

ENDPOINT: POST /macro_prices   BODY: {"ticker": "ADBE", "benchmark": "SPY", "start": "2023-01-01"}
RETURNS: {"risk_free": 0.0421, "prices": [...], "benchmark_prices": [...],
          "monthly_prices": [{"date","adjClose"}...], "_errors": {...}}

`monthly_prices` (10y, month-end) feeds pe_hist_median in Growth Enrich. It lives here for the
same reason as everything else on this route: it is a Tiingo call, and Tiingo's token lives
here. v4.2 briefly left that fetch in the Code node after deleting its token -> ReferenceError,
swallowed by a try/catch, pe_hist_median silently null, PE cap left without its best anchor.

Same discipline as the rest of the service: never throws, every failure lands in `_errors`,
a missing value is null with a reason — never a plausible default. `risk_free` in particular
must NOT fall back to a hardcoded 4% guess: the whole valuation hangs off it.
"""
import json
import os
import time
import urllib.request

_UA = "ConsiliumSpine/1.0 macro_prices"
_LAST = {"t": 0.0}


def _get_json(url, timeout=25):
    """Small throttled GET->JSON. Module-level so tests can monkeypatch it."""
    wait = 0.2 - (time.time() - _LAST["t"])
    if wait > 0:
        time.sleep(wait)
    _LAST["t"] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))


def fred_risk_free(errors, series="DGS10"):
    """10y Treasury constant maturity, latest observation, as a decimal (4.21% -> 0.0421)."""
    key = os.environ.get("FRED_KEY")
    if not key:
        errors["fred"] = "FRED_KEY not set on this service"
        return None
    try:
        d = _get_json("https://api.stlouisfed.org/fred/series/observations"
                      "?series_id=%s&api_key=%s&file_type=json&sort_order=desc&limit=1"
                      % (series, key))
        obs = (d or {}).get("observations") or []
        if not obs:
            errors["fred"] = "no observations returned"
            return None
        # FRED marks missing values with "." on holidays — that is not a zero rate.
        raw = obs[0].get("value")
        if raw in (None, ".", ""):
            errors["fred"] = "latest observation is missing (value='%s')" % raw
            return None
        return float(raw) / 100.0
    except Exception as e:
        errors["fred"] = str(e)[:140]
        return None


def tiingo_series(symbol, errors, start="2023-01-01"):
    """Adjusted close series. Adjusted matters: splits would otherwise read as crashes."""
    token = os.environ.get("TIINGO_TOKEN")
    if not token:
        errors["tiingo"] = "TIINGO_TOKEN not set on this service"
        return []
    try:
        rows = _get_json("https://api.tiingo.com/tiingo/daily/%s/prices?startDate=%s&token=%s"
                         % (symbol, start, token))
        if not isinstance(rows, list):
            errors["tiingo_%s" % symbol] = "unexpected shape: %s" % str(rows)[:80]
            return []
        return [r.get("adjClose") for r in rows if r.get("adjClose") is not None]
    except Exception as e:
        errors["tiingo_%s" % symbol] = str(e)[:140]
        return []


def tiingo_monthly(symbol, errors, years=10):
    """Month-end adjClose for the last `years`. adjClose IS split-adjusted; the EPS series it is
    joined against (in Growth Enrich) is NOT, so the caller must split-normalize EPS first or the
    resulting P/E is nonsense for every pre-split year."""
    token = os.environ.get("TIINGO_TOKEN")
    if not token:
        errors["tiingo_monthly"] = "TIINGO_TOKEN not set on this service"
        return []
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - years * 365.25 * 86400))
    try:
        rows = _get_json("https://api.tiingo.com/tiingo/daily/%s/prices"
                         "?startDate=%s&resampleFreq=monthly&token=%s" % (symbol, start, token))
        if not isinstance(rows, list):
            errors["tiingo_monthly"] = "unexpected response for %s" % symbol
            return []
        return [{"date": r.get("date"), "adjClose": r.get("adjClose")}
                for r in rows if r.get("adjClose") is not None and r.get("date")]
    except Exception as exc:
        errors["tiingo_monthly"] = "%s: %s" % (symbol, str(exc)[:120])
        return []


def macro_prices(ticker, benchmark="SPY", start="2023-01-01"):
    out = {"_errors": {}}
    out["risk_free"] = fred_risk_free(out["_errors"])
    out["prices"] = tiingo_series(ticker, out["_errors"], start) if ticker else []
    out["benchmark_prices"] = tiingo_series(benchmark, out["_errors"], start)
    out["monthly_prices"] = tiingo_monthly(ticker, out["_errors"]) if ticker else []
    out["_meta"] = {"ticker": ticker, "benchmark": benchmark, "start": start,
                    "n_prices": len(out["prices"]),
                    "n_monthly": len(out["monthly_prices"]),
                    "n_benchmark": len(out["benchmark_prices"]),
                    "risk_free_series": "FRED DGS10 (10y CMT), latest observation"}
    return out


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    r = macro_prices(t)
    r["prices"] = r["prices"][:3] + (["...(%d total)" % len(r["prices"])] if r["prices"] else [])
    r["benchmark_prices"] = r["benchmark_prices"][:3]
    r["monthly_prices"] = r["monthly_prices"][:2] + (["...(%d total)" % len(r["monthly_prices"])]
                                                     if r["monthly_prices"] else [])
    print(json.dumps(r, indent=2))
