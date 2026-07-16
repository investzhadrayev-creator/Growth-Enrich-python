"""
finra_short_interest.py — short interest from the PRIMARY source (FINRA Query API).

FINRA is where short interest data originates (broker-dealers report to FINRA biweekly);
Yahoo and every aggregator republish it. Pulling it here removes one more field from the
single-scraper dependency and gives an exact settlement date + days-to-cover from the

authority itself.

Verified against developer.finra.org docs (2026-07-16):
  - OAuth2 client-credentials: POST
      https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials
    with header  Authorization: Basic base64("client_id:client_secret").
    Response: {"access_token": ..., "expires_in": "43170", ...}. Docs recommend caching the
    token ~30 minutes; we cache min(expires_in - 60s, 25 min).
  - Data: POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest
    with a compareFilter on **symbolCode** (NOT issueSymbolIdentifier — that field name
    belongs to the legacy EquityShortInterest dataset). Accept: application/json.
    Fields (from the dataset's mock sample): symbolCode, settlementDate,
    currentShortPositionQuantity, previousShortPositionQuantity, changePercent,
    averageDailyVolumeQuantity, daysToCoverQuantity, marketClassCode, revisionFlag.
  - Publication is BIWEEKLY (mid-month and end-of-month settlement dates) — a 2-week-old
    settlement date is normal, not stale.
  - Public Credential covers this dataset; usage cap 10GB/month.

Design rules (same as edgar_facts / market_facts): never throws; failures land in the
errors dict passed in; nothing invented — missing data is None with a reason. No server-side
sort (sortFields requires partition-field EQUAL filters); we sort client-side by
settlementDate instead.
"""
import base64
import json
import time
import urllib.request

TOKEN_URL = ("https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
             "?grant_type=client_credentials")
DATA_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"

_TOKEN_CACHE = {"token": None, "exp": 0.0, "cid": None}


def _post_json(url, headers, body=None, timeout=20):
    """POST returning parsed JSON. Module-level so tests can monkeypatch it."""
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))


def _days_since(datestr):
    try:
        import datetime as _dt
        return (_dt.date.today() - _dt.date.fromisoformat(str(datestr)[:10])).days
    except Exception:
        return None


def _get_token(client_id, client_secret, errors):
    """OAuth2 client-credentials -> bearer token, cached per docs guidance (~25 min)."""
    now = time.time()
    if (_TOKEN_CACHE["token"] and _TOKEN_CACHE["cid"] == client_id
            and now < _TOKEN_CACHE["exp"]):
        return _TOKEN_CACHE["token"]
    basic = base64.b64encode(("%s:%s" % (client_id, client_secret)).encode()).decode()
    try:
        resp = _post_json(TOKEN_URL, {"Authorization": "Basic " + basic})
    except Exception as e:
        errors["finra_token"] = str(e)[:140]
        return None
    tok = (resp or {}).get("access_token")
    if not tok:
        errors["finra_token"] = "no access_token in response: %s" % str(resp)[:100]
        return None
    try:
        ttl = float(resp.get("expires_in", 1800))
    except (TypeError, ValueError):
        ttl = 1800.0
    _TOKEN_CACHE.update({"token": tok, "cid": client_id,
                         "exp": now + min(ttl - 60, 1500)})
    return tok


def finra_short_interest(ticker, client_id, client_secret, errors=None):
    """Latest + previous biweekly short-interest rows for a ticker.

    Returns None on any failure (reason in errors); otherwise a dict with the latest
    settlement row, the biweekly delta, and days-to-cover — all verbatim FINRA figures.
    """
    errors = errors if errors is not None else {}
    if not (ticker and client_id and client_secret):
        errors["finra_short"] = "missing ticker or credentials"
        return None
    tok = _get_token(client_id, client_secret, errors)
    if not tok:
        return None
    # A client-side sort CANNOT repair a server-side truncation. The old body asked for
    # `limit: 60` with no date bound; the API returns the first 60 rows in ITS order (there is
    # no server sort here -- see module note), so we sorted the wrong 60 and took the newest of
    # those. Biweekly publication = 26 rows/yr, so 60 rows is ~2.3y of history measured from
    # whatever the server calls "first". NFLX 2026-07-16 reported settlement 2022-11-30 --
    # 3.5 years stale, presented as the current figure.
    # Bound the WINDOW instead, so the 60 rows are guaranteed to contain the latest settlement.
    _end = time.strftime("%Y-%m-%d", time.gmtime())
    _start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 120 * 86400))
    body = {
        "limit": 60,
        "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode",
                            "fieldValue": ticker.upper()}],
        "dateRangeFilters": [{"fieldName": "settlementDate",
                              "startDate": _start, "endDate": _end}],
    }
    try:
        rows = _post_json(DATA_URL, {
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, body)
    except Exception as e:
        errors["finra_short"] = str(e)[:140]
        return None
    if not isinstance(rows, list) or not rows:
        errors["finra_short"] = "no rows for %s" % ticker
        return None
    rows = sorted(rows, key=lambda r: r.get("settlementDate") or "", reverse=True)
    latest = rows[0]
    # Staleness is a HARD failure, not a footnote. Short interest publishes biweekly, so >45d
    # means the query is broken (or the symbol stopped reporting) -- either way the number is
    # not a current fact and must not travel as one. Returning None costs the fear-discount
    # diagnostic one input; returning a 3.5-year-old figure silently corrupts the whole read.
    _sd = latest.get("settlementDate") or ""
    _age = _days_since(_sd)
    if _age is None or _age > 45:
        errors["finra_short"] = (
            "latest settlement %s is %s -- expected <=45d (biweekly publication). "
            "Refusing to present it as current." % (_sd or "(missing)",
                                                    "unparseable" if _age is None else "%dd old" % _age))
        return None

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    cur = _num(latest.get("currentShortPositionQuantity"))
    prev = _num(latest.get("previousShortPositionQuantity"))
    out = {
        "settlement_date": latest.get("settlementDate"),
        "short_shares": cur,
        "short_shares_previous": prev,
        "change_pct_biweekly": _num(latest.get("changePercent")),
        "avg_daily_volume": _num(latest.get("averageDailyVolumeQuantity")),
        "days_to_cover": _num(latest.get("daysToCoverQuantity")),
        "market_class": latest.get("marketClassCode"),
        "revision_flag": latest.get("revisionFlag"),
        "history": [{"settlement_date": r.get("settlementDate"),
                     "short_shares": _num(r.get("currentShortPositionQuantity"))}
                    for r in rows[:6]],
        "_source": "FINRA consolidatedShortInterest (primary source, biweekly)",
    }
    if out["change_pct_biweekly"] is None and cur is not None and prev:
        out["change_pct_biweekly"] = round((cur / prev - 1) * 100, 2)
    return out


if __name__ == "__main__":
    # Live smoke test (needs real credentials):
    #   python3 finra_short_interest.py AAPL <client_id> <client_secret>
    import sys
    if len(sys.argv) == 4:
        errs = {}
        r = finra_short_interest(sys.argv[1], sys.argv[2], sys.argv[3], errs)
        print(json.dumps({"result": r, "errors": errs}, indent=2))
    else:
        print("usage: python3 finra_short_interest.py TICKER CLIENT_ID CLIENT_SECRET")
