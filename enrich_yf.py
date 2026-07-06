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
  - House PEG is recomputed as fwd_pe / fwd_eps_growth_pct (NOT Yahoo's pegRatio) to avoid
    desync with the pipeline's pinned PEG convention.

Endpoint contract:
  POST /enrich_yf   body: {"ticker": "NVDA", "peers": ["AMD","AVGO","INTC","MSFT"]}
  returns: JSON dict of enrichment fields (all nullable).
"""
import json

def enrich_yf(ticker, peers=None):
    out = {"_source_tier": "yahoo", "_ticker": ticker, "_errors": {}}
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
    info = safe(lambda: t.info, "info") or {}

    # --- valuation multiples (raw Yahoo) ---
    out["fwd_pe"] = info.get("forwardPE")
    out["trailing_pe"] = info.get("trailingPE")
    out["price_to_book"] = info.get("priceToBook")
    out["yahoo_peg_raw"] = info.get("pegRatio") or info.get("trailingPegRatio")  # NOT used for scoring
    out["market_cap"] = info.get("marketCap")
    out["beta"] = info.get("beta")

    # --- EPS estimates -> house PEG (fwd_pe / fwd_eps_growth_pct) ---
    eps_fwd = info.get("forwardEps")
    eps_ttm = info.get("trailingEps")
    if eps_fwd and eps_ttm and eps_ttm > 0:
        g_pct = (eps_fwd / eps_ttm - 1) * 100
        out["fwd_eps_growth_pct"] = round(g_pct, 2)
        if out["fwd_pe"] and g_pct > 0:
            out["peg_house"] = round(out["fwd_pe"] / g_pct, 3)   # pipeline-consistent PEG
    # richer estimates if available
    est = safe(lambda: t.earnings_estimate, "earnings_estimate")
    if est is not None:
        try:
            out["eps_estimates"] = est.reset_index().to_dict(orient="records")
        except Exception:
            pass

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
    peers = peers or []
    peer_pes = []
    peer_rows = []
    for p in peers[:6]:
        try:
            pinfo = yf.Ticker(p).info
            fpe = pinfo.get("forwardPE")
            row = {"ticker": p, "fwd_pe": fpe,
                   "eps_growth_pct": None, "sector": pinfo.get("sector")}
            e_fwd, e_ttm = pinfo.get("forwardEps"), pinfo.get("trailingEps")
            if e_fwd and e_ttm and e_ttm > 0:
                row["eps_growth_pct"] = round((e_fwd / e_ttm - 1) * 100, 2)
            peer_rows.append(row)
            if fpe and 0 < fpe < 300:
                peer_pes.append(fpe)
        except Exception as e:
            peer_rows.append({"ticker": p, "error": str(e)[:80]})
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
