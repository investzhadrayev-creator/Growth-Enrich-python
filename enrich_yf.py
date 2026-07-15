"""
enrich_yf.py — yfinance enrichment microservice endpoint for Growth Alpha Pipeline (Wave 2).

Deploy alongside your existing Run Code microservice (same Railway service, add this route).
It closes the [UNVERIFIED] valuation-core fields that Finnhub (401) / Tiingo (403) left open:
  fwd_pe, peg (raw Yahoo — we recompute house PEG ourselves), peer forward multiples,
  peer-median PE (NOT true sector median — labeled honestly), analyst revisions/ERB proxy,
  price target, EPS estimates, short interest, dividend history, institutional holders.

DESIGN (matches pipeline discipline):
  - Every number is computed here (Python), never in the LLM.
  - Reliability tier = 'yahoo' (below SEC/Tiingo, above FACT_PACK) per v3.7 hierarchy.
  - Everything wrapped in try/except -> null on failure, never throws (like Growth Enrich).
  - yfinance is a scraper: unofficial, rate-limited, can break. Fallbacks + caching expected.
  - House PEG is recomputed as fwd_pe / fwd_eps_growth_pct (NOT Yahoo's pegRatio). v3.8:
    the growth denominator is the analyst +1y CONSENSUS (same figure surfaced to GROUND_TRUTH),
    not the forwardEps/trailingEps ratio, so peg_house cannot desync from the memo/auditor PEG.

Endpoint contract:
  POST /enrich_yf   body: {"ticker": "NVDA", "peers": ["AMD","AVGO","INTC","MSFT"]}
  returns: JSON dict of enrichment fields (all nullable).
"""
import json
import time
import concurrent.futures

# v2.4: in-process TTL cache so repeated peers and 2nd runs are instant (cold-start fix)
_CACHE = {}
_CACHE_TTL = 3600

# v2.9: explicit visibility into whether curl_cffi (Yahoo's TLS-impersonation requirement)
# loaded correctly. yfinance needs it for cookie/crumb negotiation; if it's missing or a
# bad version, .info silently returns {} WITHOUT raising -- this was the META incident:
# _errors was empty but every field was null. Surface it instead of guessing next time.
_CURL_CFFI_STATUS = None
def _check_curl_cffi():
    global _CURL_CFFI_STATUS
    if _CURL_CFFI_STATUS is not None:
        return _CURL_CFFI_STATUS
    try:
        import curl_cffi
        _CURL_CFFI_STATUS = {"available": True, "version": getattr(curl_cffi, "__version__", "unknown")}
    except Exception as e:
        _CURL_CFFI_STATUS = {"available": False, "error": str(e)[:150]}
    return _CURL_CFFI_STATUS


def _cached_info(ticker):
    now = time.time()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        # v3.1: do NOT cache a transient failure. yf.Ticker(x).info can return a near-empty
        # dict on a momentary Yahoo hiccup/rate-limit WITHOUT raising; caching that for
        # _CACHE_TTL (1h) makes the failure sticky -- every call in that window repeats it,
        # even after Yahoo recovers. This was the likely cause of META returning all-null
        # in the pipeline the same day a direct manual test succeeded. Only cache real data.
        if len(info) >= 5:
            _CACHE[ticker] = (now, info)
        return info
    except Exception:
        return None


def _peer_row(p):
    try:
        pinfo = _cached_info(p) or {}
        if len(pinfo) < 5:
            return {"ticker": p, "error": f"info_empty ({len(pinfo)} keys) -- Yahoo блок/crumb-сбой, не код"}
        fpe = pinfo.get("forwardPE")
        row = {"ticker": p, "fwd_pe": fpe, "eps_growth_pct": None,
               "sector": pinfo.get("sector")}
        e_fwd, e_ttm = pinfo.get("forwardEps"), pinfo.get("trailingEps")
        if e_fwd and e_ttm and e_ttm > 0:
            row["eps_growth_pct"] = round((e_fwd / e_ttm - 1) * 100, 2)
        return row
    except Exception as e:
        return {"ticker": p, "error": str(e)[:80]}


# v3.3: sector-derived peer fallback. The n8n PEER_MAP is a curated list (best quality when it
# hits), but any ticker missing from it used to fall through with peers=[] -> peer_median_pe=null
# -> the PE cap lost its strongest anchor and GPS block C silently lost points. Yahoo already
# tells us the ticker's own sector, so derive a default comp set from that rather than returning
# nothing. Labelled honestly downstream: a sector default is weaker evidence than named comps.
SECTOR_PEERS = {
    "Technology": ["MSFT", "AAPL", "NVDA", "ORCL"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS"],
    "Consumer Cyclical": ["AMZN", "HD", "MCD", "NKE"],
    "Consumer Defensive": ["PG", "KO", "PEP", "COST"],
    "Financial Services": ["JPM", "V", "MA", "BAC"],
    "Healthcare": ["LLY", "JNJ", "UNH", "MRK"],
    "Industrials": ["CAT", "HON", "GE", "UNP"],
    "Energy": ["XOM", "CVX", "COP", "SLB"],
    "Basic Materials": ["LIN", "SHW", "APD", "FCX"],
    "Real Estate": ["PLD", "AMT", "EQIX", "SPG"],
    "Utilities": ["NEE", "DUK", "SO", "D"],
}


def _sector_peers(ticker, info):
    """Derive a comp set from the ticker's own Yahoo sector when no explicit peers were given."""
    sector = (info or {}).get("sector")
    if not sector:
        return [], None
    peers = [p for p in SECTOR_PEERS.get(sector, []) if p.upper() != (ticker or "").upper()]
    return peers[:4], sector


def enrich_yf(ticker, peers=None):
    out = {"_source_tier": "yahoo", "_ticker": ticker, "_errors": {}}
    out["_curl_cffi"] = _check_curl_cffi()
    try:
        import yfinance as yf
    except Exception as e:
        out["_errors"]["import"] = f"yfinance not installed: {e}"
        return out

    def safe(fn, key):
        try:
            return fn()
        except Exception as e:
            out["_errors"][key] = str(e)[:120]
            return None

    t = yf.Ticker(ticker)
    info = _cached_info(ticker) or {}
    # v2.9: yfinance can return {} (or a near-empty dict) WITHOUT raising when Yahoo's
    # cookie/crumb handshake fails (common on cloud/datacenter IPs or bad curl_cffi).
    # Silence here was exactly the META incident (_errors empty, everything null).
    if len(info) < 5:
        out["_errors"]["info_empty"] = (
            f"yf.Ticker({ticker}).info returned only {len(info)} keys -- Yahoo likely "
            f"blocked/rejected the request silently (cloud IP or curl_cffi handshake failure), "
            f"not a code exception. curl_cffi status: {out['_curl_cffi']}"
        )

    # --- valuation multiples (raw Yahoo) ---
    out["fwd_pe"] = info.get("forwardPE")
    out["trailing_pe"] = info.get("trailingPE")
    out["price_to_book"] = info.get("priceToBook")
    out["yahoo_peg_raw"] = info.get("pegRatio") or info.get("trailingPegRatio")  # NOT used for scoring
    out["market_cap"] = info.get("marketCap")
    out["beta"] = info.get("beta")

    # --- EPS estimates -> house PEG (fwd_pe / fwd_eps_growth_pct) ---
    # v3.8 (peg desync fix): the PEG denominator MUST be the same fwd EPS growth the
    # pipeline surfaces to GROUND_TRUTH (analyst +1y consensus), NOT the cruder
    # forwardEps/trailingEps ratio. Using two different growth figures made peg_house
    # (0.591 via 53.7% ratio) disagree with the memo/auditor's PEG (0.724 via 43.83%
    # consensus). Priority: analyst-consensus +1y growth -> fallback forwardEps/trailingEps.
    g_pct = None
    g_basis = None
    est = safe(lambda: t.earnings_estimate, "earnings_estimate")
    if est is not None:
        try:
            er = est.reset_index()
            out["eps_estimates"] = er.to_dict(orient="records")
            for _, r in er.iterrows():
                per = str(r.get("period") or r.get("index") or "").lower()
                if per in ("+1y", "1y"):
                    gv = r.get("growth")
                    if gv is not None:
                        gv = float(gv)
                        # yfinance growth is a fraction (0.4383); guard if already pct
                        g_pct = gv * 100 if abs(gv) < 3 else gv
                        g_basis = "analyst_consensus_+1y"
                    break
        except Exception as e:
            out["_errors"]["earnings_estimate"] = str(e)[:120]
    eps_fwd = info.get("forwardEps")
    eps_ttm = info.get("trailingEps")
    if g_pct is None and eps_fwd and eps_ttm and eps_ttm > 0:
        g_pct = (eps_fwd / eps_ttm - 1) * 100
        g_basis = "forwardEps_over_trailingEps_fallback"
    if g_pct is not None:
        out["fwd_eps_growth_pct"] = round(g_pct, 2)
        out["fwd_eps_growth_basis"] = g_basis
        if out["fwd_pe"] and g_pct > 0:
            out["peg_house"] = round(out["fwd_pe"] / g_pct, 3)   # pipeline-consistent PEG

    # --- analyst revisions / ERB proxy ---
    # yfinance recommendations_summary: strongBuy/buy/hold/sell/strongSell by period
    rs = safe(lambda: t.recommendations_summary, "recommendations_summary")
    if rs is None:
        rs = safe(lambda: t.recommendations, "recommendations")
    if rs is not None:
        try:
            rows = rs.to_dict(orient="records") if hasattr(rs, "to_dict") else None
            out["recommendations_summary"] = rows
            if rows:
                # ERB proxy = (pos share now) - (pos share 3 periods ago), like the Finnhub proxy
                def pos(r): return (r.get("strongBuy", 0) or 0) + (r.get("buy", 0) or 0)
                def tot(r):
                    return sum((r.get(k, 0) or 0) for k in
                               ["strongBuy", "buy", "hold", "sell", "strongSell"])
                cur = rows[0]
                old = rows[min(3, len(rows) - 1)]
                if tot(cur) > 0 and tot(old) > 0:
                    out["erb_90d"] = round(pos(cur) / tot(cur) - pos(old) / tot(old), 4)
                out["rec_strongbuy"] = cur.get("strongBuy")
                out["rec_buy"] = cur.get("buy")
                out["rec_hold"] = cur.get("hold")
                out["rec_sell"] = cur.get("sell")
        except Exception as e:
            out["_errors"]["rec_parse"] = str(e)[:120]

    # --- price target ---
    pt = safe(lambda: t.analyst_price_targets, "price_targets")
    if isinstance(pt, dict):
        out["price_target_mean"] = pt.get("mean")
        out["price_target_high"] = pt.get("high")
        out["price_target_low"] = pt.get("low")
    else:
        out["price_target_mean"] = info.get("targetMeanPrice")
        out["price_target_high"] = info.get("targetHighPrice")
        out["price_target_low"] = info.get("targetLowPrice")
    # v3.6: consensus depth + named-firm actions (the deterministic part of "street view").
    # Per-bank PRICE TARGETS are not reliably available from free sources — those belong to the
    # Stage1 fact pack with named citations. What IS deterministic here: how many analysts,
    # the mean recommendation, and recent named upgrades/downgrades (firm + action + grade).
    out["analyst_count"] = info.get("numberOfAnalystOpinions")
    out["recommendation_mean"] = info.get("recommendationMean")   # 1=StrongBuy .. 5=Sell
    out["recommendation_key"] = info.get("recommendationKey")
    ud = safe(lambda: t.upgrades_downgrades, "upgrades_downgrades")
    if ud is not None:
        try:
            recent = ud.reset_index().head(8)
            actions = []
            for _, r in recent.iterrows():
                row = {k: r.get(k) for k in r.index}
                actions.append({
                    "date": str(row.get("GradeDate") or row.get("index") or "")[:10],
                    "firm": row.get("Firm"),
                    "action": row.get("Action"),
                    "to_grade": row.get("ToGrade"),
                    "from_grade": row.get("FromGrade"),
                })
            out["analyst_actions_recent"] = actions
        except Exception as e:
            out["_errors"]["upgrades_parse"] = str(e)[:120]

    # --- short interest ---
    out["short_percent_of_float"] = info.get("shortPercentOfFloat")
    out["shares_short"] = info.get("sharesShort")
    out["short_ratio"] = info.get("shortRatio")

    # --- dividend history (full series) ---
    div = safe(lambda: t.dividends, "dividends")
    if div is not None:
        try:
            d = div.tail(40)
            out["dividend_history"] = [
                {"date": str(idx.date()), "amount": float(v)} for idx, v in d.items()
            ]
            out["dividend_last"] = float(div.iloc[-1]) if len(div) else None
        except Exception:
            pass

    # --- institutional holders (top) ---
    ih = safe(lambda: t.institutional_holders, "institutional_holders")
    if ih is not None:
        try:
            out["institutional_holders_top"] = ih.head(5).to_dict(orient="records")
            out["institutional_pct"] = info.get("heldPercentInstitutions")
        except Exception:
            pass

    # --- insider transactions (Form 4 summary) ---
    it = safe(lambda: t.insider_transactions, "insider_transactions")
    if it is not None:
        try:
            out["insider_transactions_recent"] = it.head(10).to_dict(orient="records")
        except Exception:
            pass

    # --- PEER MULTIPLES -> named-comps anchor for pe_cap (THE key field) ---
    # v2.4: fetch peers IN PARALLEL with a hard wall-clock budget so cold starts
    # with 4-5 tickers don't blow the caller's timeout (the META failure mode).
    peers = (peers or [])[:6]
    peer_basis = "named_comps" if peers else None
    if not peers:
        peers, sec = _sector_peers(ticker, info)
        if peers:
            peer_basis = "sector_default(%s)" % sec
    out["_peer_basis"] = peer_basis or "none_available"
    peer_rows = []
    peer_pes = []
    if peers:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(_peer_row, p): p for p in peers}
                done, not_done = concurrent.futures.wait(futs, timeout=18)
                for f in done:
                    peer_rows.append(f.result())
                for f in not_done:
                    peer_rows.append({"ticker": futs[f], "error": "peer fetch timeout"})
                    f.cancel()
        except Exception as e:
            out["_errors"]["peers"] = str(e)[:120]
        for row in peer_rows:
            fpe = row.get("fwd_pe")
            if fpe and 0 < fpe < 300:
                peer_pes.append(fpe)
    out["peer_multiples"] = peer_rows
    if peer_pes:
        peer_pes.sort()
        n = len(peer_pes)
        out["peer_median_pe"] = round(
            peer_pes[n // 2] if n % 2 else (peer_pes[n // 2 - 1] + peer_pes[n // 2]) / 2, 2)
        out["peer_pe_count"] = n
        # honest label: this is a PEER median, not a true sector median
        out["_peer_median_note"] = "median of provided peers' forward P/E, NOT true sector median"

    return out


# --- Flask/FastAPI route glue (adapt to your existing microservice framework) ---
# Example with Flask (if your Run Code service uses Flask):
#
#   from flask import Flask, request, jsonify
#   app = Flask(__name__)
#
#   @app.route("/enrich_yf", methods=["POST"])
#   def _enrich_yf():
#       body = request.get_json(force=True) or {}
#       return jsonify(enrich_yf(body.get("ticker"), body.get("peers")))
#
# requirements: yfinance (pip install yfinance). Add to the microservice's requirements.txt.

if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    prs = sys.argv[2].split(",") if len(sys.argv) > 2 else ["AMD", "AVGO", "INTC", "MSFT"]
    print(json.dumps(enrich_yf(tk, prs), indent=2, default=str))
