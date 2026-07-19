"""
market_facts.py — second-source market/forward data + in-house peer P/E.

WHY THIS EXISTS: the pipeline's forward-looking fields (fwd P/E, consensus EPS growth, price
targets, peer multiples) all hung off ONE unofficial scraper (yfinance), which intermittently
returns nothing on cloud IPs (ADBE went full-null twice on 2026-07-15). Historical financials
never break — they come from SEC EDGAR. This module applies the same first-source discipline
to the market layer:

  1. ALPHA VANTAGE (official API, free key) — second source for consensus EPS, forward P/E,
     PEG, analyst target. Where possible we do NOT ingest ready-made ratios: fwd P/E is
     COMPUTED here as price / consensus-EPS, so the arithmetic is ours and auditable.
  2. FINNHUB (official API, key already in the pipeline) — recommendation trends by month:
     a real revision-breadth signal to replace the Yahoo ERB proxy when it is missing.
  3. IN-HOUSE PEER P/E — peer EPS from SEC EDGAR (net_income / shares_diluted, latest FY) and
     peer price from Tiingo. Both are primary/official sources; the ratio is computed by THIS
     code. Yahoo drops out of the PE-anchor critical path entirely. Labelled TRAILING —
     comparing it to a forward multiple is not like-for-like and the label travels with it.
  4. FINRA (official API, Public credential) — short interest from the authority that
     collects it, with an exact biweekly settlement date and days-to-cover.
  5. QUORUM — where two sources report the same field, divergence > 5% is flagged, exactly
     like the EDGAR-vs-inline divergence check in phase 1.

Design rules (same as edgar_facts): never throws; every failure lands in '_errors'; nothing
is invented — a missing field is None with a reason, not a guess.

API keys arrive IN THE REQUEST BODY from n8n (which already holds FINN/TIINGO); nothing is
stored here. Alpha Vantage free tier = 25 req/day — fine for interactive runs, and the caller
should not loop tickers through it.

ENDPOINT: POST /market_facts
BODY: {"ticker": "ADBE", "peers": ["CRM","NOW"], "av_key": "...", "finnhub_key": "...",
       "tiingo_token": "...", "price": 220.78, "shares_outstanding": 397000000,
       "finra_client_id": "...", "finra_client_secret": "...",
       "yahoo": {"fwd_pe": ..., "peg": ..., "price_target_mean": ..., "eps_growth_1y": ...}}
"""
import json
import os
import re
import time
import urllib.request

from edgar_facts import edgar_facts
from finra_short_interest import finra_short_interest

_UA = "ConsiliumSpine/1.0 market_facts"
_LAST_CALL = {"t": 0.0}


def _get_json(url, timeout=15):
    """Small throttled GET->JSON. Module-level so tests can monkeypatch it."""
    wait = 0.25 - (time.time() - _LAST_CALL["t"])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL["t"] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))


def _f(v):
    """AV returns numbers as strings ('None', '-', '1.234'). Coerce honestly."""
    if v in (None, "None", "-", "", "NaN"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------------------------
def alpha_vantage_overview(ticker, key, errors):
    """AV OVERVIEW: consensus-derived fields from an official API (25 req/day free)."""
    try:
        d = _get_json("https://www.alphavantage.co/query?function=OVERVIEW&symbol=%s&apikey=%s"
                      % (ticker, key))
    except Exception as e:
        errors["alpha_vantage"] = str(e)[:140]
        return None
    if not d or "Symbol" not in d:
        errors["alpha_vantage"] = ("rate-limited or empty: %s" % str(d)[:100]) if d else "empty"
        return None
    return {
        "eps_ttm": _f(d.get("EPS")),
        "forward_pe_reported": _f(d.get("ForwardPE")),
        "peg_reported": _f(d.get("PEGRatio")),
        "analyst_target": _f(d.get("AnalystTargetPrice")),
        "trailing_pe_reported": _f(d.get("TrailingPE")),
        "_source": "alpha_vantage OVERVIEW",
    }


def finnhub_rec_trends(ticker, key, errors):
    """Finnhub /stock/recommendation: monthly analyst recommendation counts. The DELTA in
    buy-share over ~3 months is a genuine revision-breadth signal (ERB replacement)."""
    try:
        rows = _get_json("https://finnhub.io/api/v1/stock/recommendation?symbol=%s&token=%s"
                         % (ticker, key))
    except Exception as e:
        errors["finnhub_rec"] = str(e)[:140]
        return None
    if not isinstance(rows, list) or not rows:
        errors["finnhub_rec"] = "empty"
        return None
    rows = sorted(rows, key=lambda r: r.get("period", ""), reverse=True)[:4]

    def buy_share(r):
        b = (r.get("strongBuy") or 0) + (r.get("buy") or 0)
        tot = b + (r.get("hold") or 0) + (r.get("sell") or 0) + (r.get("strongSell") or 0)
        return (b / tot) if tot else None

    latest, oldest = buy_share(rows[0]), buy_share(rows[-1])
    return {
        "months": [{"period": r.get("period"), "strongBuy": r.get("strongBuy"),
                    "buy": r.get("buy"), "hold": r.get("hold"), "sell": r.get("sell"),
                    "strongSell": r.get("strongSell")} for r in rows],
        "buy_share_latest": round(latest, 3) if latest is not None else None,
        "buy_share_delta_3m": (round(latest - oldest, 3)
                               if (latest is not None and oldest is not None) else None),
        "_source": "finnhub /stock/recommendation",
    }


def tiingo_last_price(ticker, token, errors):
    try:
        rows = _get_json("https://api.tiingo.com/tiingo/daily/%s/prices?token=%s"
                         % (ticker, token))
        if isinstance(rows, list) and rows:
            return rows[0].get("adjClose") or rows[0].get("close")
    except Exception as e:
        errors["tiingo_%s" % ticker] = str(e)[:120]
    return None


def peer_pe_inhouse(peers, tiingo_token, errors):
    """Peer TRAILING P/E from primary sources only: EDGAR EPS (net_income / shares_diluted,
    latest FY) x Tiingo price. Computed by this code — no third-party ratio ingested.
    TRAILING, and the label must travel with the number: it is an anchor of last resort when
    forward peer multiples are unavailable, not a like-for-like replacement."""
    rows = []
    for p in (peers or [])[:6]:
        row = {"ticker": p}
        try:
            ef = edgar_facts(ticker=p)
            ni = (ef.get("net_income") or [])
            sh = (ef.get("shares_diluted") or [])
            if ni and sh and sh[-1].get("val"):
                eps = ni[-1]["val"] / sh[-1]["val"]
                row["eps_fy"] = round(eps, 3)
                row["fy_end"] = ni[-1].get("end")
            price = tiingo_last_price(p, tiingo_token, errors) if tiingo_token else None
            row["price"] = price
            if row.get("eps_fy") and row["eps_fy"] > 0 and price:
                row["pe_trailing"] = round(price / row["eps_fy"], 2)
        except Exception as e:
            row["error"] = str(e)[:100]
        rows.append(row)
    pes = sorted(r["pe_trailing"] for r in rows if r.get("pe_trailing"))
    median = pes[len(pes) // 2] if pes else None
    return {"rows": rows, "peer_median_pe_trailing": median,
            "basis": "EDGAR latest-FY EPS x Tiingo price (TRAILING, in-house computed)",
            "n_priced": len(pes)}


def _quorum(name, a, b, out, tol=0.05):
    """Two sources report the same field: agreement is quiet, divergence is loud."""
    if a is None or b is None or not b:
        return
    rel = abs(a - b) / abs(b)
    if rel > tol:
        out.setdefault("_divergence", {})[name] = {
            "primary": a, "secondary": b, "rel_diff_pct": round(rel * 100, 1)}


# ----------------------------------------------------------------------------------------------
def market_facts(ticker, peers=None, av_key=None, finnhub_key=None, tiingo_token=None,
                 price=None, yahoo=None, finra_client_id=None, finra_client_secret=None,
                 shares_outstanding=None):
    out = {"_ticker": ticker, "_errors": {}, "_sources_used": []}
    yahoo = yahoo or {}
    # v4.2: keys are read from THIS service's environment. They used to be passed in the request
    # body from n8n, which meant they lived inside the n8n workflow — but n8n 2.x runs Code nodes
    # in a task-runner sandbox with no access to the container env ("access to env vars denied"),
    # so the only way to keep them there was to inline them into the exported JSON. A key belongs
    # to the service that uses it. The body params stay supported for local testing only.
    av_key = av_key or os.environ.get("ALPHAVANTAGE_KEY")
    finnhub_key = finnhub_key or os.environ.get("FINNHUB_KEY")
    tiingo_token = tiingo_token or os.environ.get("TIINGO_TOKEN")
    finra_client_id = finra_client_id or os.environ.get("FINRA_CLIENT_ID")
    finra_client_secret = finra_client_secret or os.environ.get("FINRA_CLIENT_SECRET")

    av = alpha_vantage_overview(ticker, av_key, out["_errors"]) if av_key else None
    if av:
        out["alpha_vantage"] = av
        out["_sources_used"].append("alpha_vantage")

    rec = finnhub_rec_trends(ticker, finnhub_key, out["_errors"]) if finnhub_key else None
    if rec:
        out["rec_trends"] = rec
        out["_sources_used"].append("finnhub")

    # COMPUTED forward P/E: our arithmetic from price + consensus EPS-growth applied to AV
    # TTM EPS. We deliberately PREFER computing over ingesting AV's ForwardPE, so a bad
    # ready-made ratio cannot pass unexamined; the reported one is the cross-checked fallback.
    #
    # v4.2.22 (B1) INTEGRATION FIX. Diagnosed from the raw /market_facts response path, not the
    # [UNVERIFIED] symptom in the report (per protocol; three AV-key swaps chased the wrong cause).
    # The real cause: fwd_pe reaches the report from Yahoo (enrich_yf), which is silently rate-
    # limited on cloud IPs — so the report fell back to `fwd_pe_computed`. But the reported-AV
    # branch was NESTED inside `if av.get("eps_ttm")` AND the computation needed `yahoo.eps_growth_1y`
    # — i.e. the ONE source that is official and IP-independent (AV's own ForwardPE) was locked
    # behind two LESS reliable inputs (AV EPS presence + a Yahoo growth number). When Yahoo is
    # blocked, growth is null; when AV omits EPS, the whole block is skipped and forward_pe_reported
    # — which was sitting right there in the response — never gets read. Hence [UNVERIFIED].
    # Fix: forward_pe_reported is now a FIRST-CLASS fallback, gated on nothing but its own presence.
    # Priority preserved: (1) our computed ratio (explainable, best) -> (2) AV reported (official,
    # IP-independent) -> (3) null. The computed path still requires its inputs; only the reported
    # fallback is freed.
    fwd_pe_computed = None
    if av and av.get("eps_ttm") and price:
        g1 = yahoo.get("eps_growth_1y")
        if isinstance(g1, (int, float)):
            fwd_eps = av["eps_ttm"] * (1 + g1)
            if fwd_eps > 0:
                fwd_pe_computed = round(price / fwd_eps, 2)
                out["fwd_pe_computed"] = fwd_pe_computed
                out["fwd_pe_computed_basis"] = "price / (AV eps_ttm x (1 + yahoo +1y growth))"
    # Reported-AV fallback, no longer nested behind eps_ttm/growth: if we could not compute our
    # own ratio for ANY reason, take AV's official ForwardPE directly when present.
    if out.get("fwd_pe_computed") is None and av and av.get("forward_pe_reported"):
        out["fwd_pe_computed"] = av["forward_pe_reported"]
        out["fwd_pe_computed_basis"] = "AV ForwardPE reported (computation unavailable; official second source)"

    # quorum cross-checks where both sources speak
    if av:
        _quorum("fwd_pe", yahoo.get("fwd_pe"), av.get("forward_pe_reported"), out)
        _quorum("peg", yahoo.get("peg"), av.get("peg_reported"), out)
        _quorum("price_target_mean", yahoo.get("price_target_mean"), av.get("analyst_target"), out)

    # v4.0: short interest from the PRIMARY source. Broker-dealers report to FINRA; Yahoo
    # merely republishes. Percent-of-float is COMPUTED here from FINRA's share count and the
    # EDGAR share count already in the payload — neither side is an ingested ratio.
    if finra_client_id and finra_client_secret:
        si = finra_short_interest(ticker, finra_client_id, finra_client_secret, out["_errors"])
        if si:
            # A percentage is only meaningful if numerator and denominator share a basis.
            # NFLX 2026-07-16: a 2022 short-share count (pre-split scale, ~430M shares out)
            # was divided by the CURRENT post-split count (4.2B) -> "0.25% of shares
            # outstanding" when the real 2022 figure was ~2.4%. Understated ~10x, because a
            # stale numerator met a live denominator. The settlement window is now bounded
            # upstream, but assert the invariant here too: cheap, and this is the exact spot
            # where the two bases meet.
            _shares = si.get("short_shares")
            if shares_outstanding and _shares:
                _pct = _shares / shares_outstanding * 100
                if _pct > 60:
                    si["short_pct_shares_outstanding"] = None
                    si["_basis_error"] = (
                        "short_shares %.0f vs shares_outstanding %.0f implies %.1f%% -- "
                        "implausible; the two are almost certainly on different split bases. "
                        "Reporting null rather than a wrong percentage."
                        % (_shares, shares_outstanding, _pct))
                    out["_errors"]["short_interest_basis"] = si["_basis_error"]
                else:
                    si["short_pct_shares_outstanding"] = round(_pct, 2)
                si["_pct_basis"] = ("FINRA short shares / EDGAR shares outstanding — "
                                    "SHARES OUTSTANDING, not float; a float-based figure "
                                    "(e.g. Yahoo's) will read higher")
            out["short_interest"] = si
            out["_sources_used"].append("finra")

    if peers:
        out["peer_pe_inhouse"] = peer_pe_inhouse(peers, tiingo_token, out["_errors"])
        out["_sources_used"].append("edgar+tiingo peers")

    _sanitize_errors(out["_errors"], [av_key, finnhub_key, tiingo_token,
                                      finra_client_id, finra_client_secret])
    return out


def _sanitize_errors(errors, secrets):
    """v4.2.11 SECURITY. Provider error bodies quote the request back — Alpha Vantage's
    rate-limit message literally reads 'We have detected your API key as <KEY>', and on
    2026-07-17 that string travelled errors -> growth_diag -> Render Tables reason row ->
    the shipped NFLX report. An error message is untrusted input like any other: redact
    every known secret verbatim, plus anything key-shaped, before it leaves this module.
    The render layer redacts again (belt and suspenders), but the SOURCE must never emit
    a secret in the first place."""
    known = [s for s in (secrets or []) if s]
    for k in list(errors.keys()):
        v = errors[k]
        if not isinstance(v, str):
            continue
        for s in known:
            v = v.replace(s, "[REDACTED]")
        # key-shaped residue: apikey/token URL params, then long unbroken alnum tokens
        v = re.sub(r"(?i)(apikey|token|client_secret|key)=[A-Za-z0-9_\-]{8,}",
                   r"\1=[REDACTED]", v)
        v = re.sub(r"\b[A-Z0-9]{14,}\b", "[REDACTED]", v)
        errors[k] = v
