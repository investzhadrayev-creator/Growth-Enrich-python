"""
app.py — deployable microservice for the Growth Alpha Pipeline.
Exposes routes the n8n workflow calls:
  POST /run            -> executes the deterministic IVC wiring code (Stage 2a output)
  POST /enrich_yf      -> yfinance enrichment (fwd_pe, peers, revisions, short interest, ...)
  POST /scenario_tree  -> Category-F deterministic anchors for pre-profit names (Core-V)
  GET  /health         -> liveness probe

Design notes (matches pipeline discipline):
  - Every route ALWAYS returns JSON, never a bare 500 with an HTML body — the n8n
    HTTP nodes and Render Tables parse JSON; an HTML error page would break them.
  - /run executes untrusted-ish generated Python in a subprocess with a timeout,
    capturing stdout (the pipeline contract: last line of stdout is the JSON result).
  - /enrich_yf wraps enrich_yf() which itself never throws.
  - /scenario_tree wraps scenario_tree() which itself never throws.
"""
import json
import os

from flask import Flask, request, jsonify

from enrich_yf import enrich_yf
from scenario_f import scenario_tree   # v1.5: Core-V Category-F anchors
from edgar_facts import edgar_facts    # v1.8: SEC EDGAR primary-source financials

app = Flask(__name__)

# Hard cap so a runaway generated script can't hang the worker.


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "growth-alpha-microservice"})


@app.route("/enrich_yf", methods=["POST"])
def _enrich_yf():
    body = request.get_json(force=True, silent=True) or {}
    ticker = body.get("ticker")
    peers = body.get("peers") or []
    if not ticker:
        return jsonify({"_errors": {"request": "ticker missing"}}), 200
    # enrich_yf never throws; returns a dict with _errors trail on partial failure.
    return jsonify(enrich_yf(ticker, peers)), 200


@app.route("/scenario_tree", methods=["POST"])
def _scenario_tree():
    """
    Core-V (pre-profit / Category-F) deterministic ANCHORS.
    Body: {"data": {...eligibility payload...}}  (also accepts a bare payload).
    scenario_tree() never throws; returns a dict with a '_warnings' trail.
    """
    body = request.get_json(force=True, silent=True) or {}
    data = body.get("data", body)
    return jsonify(scenario_tree(data)), 200


@app.route("/edgar_facts", methods=["POST"])
def _edgar_facts():
    """
    SEC EDGAR primary-source financials (deterministic XBRL facts).
    Body: {"ticker": "ASTS"}  (or {"cik": "0001780312"}).
    edgar_facts() never throws; missing fields come back null with a '_missing' trail.
    """
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(edgar_facts(body.get("ticker"), body.get("cik"))), 200


# ============================================================================
# v2.5 DETERMINISTIC HARNESS (Variant 1) — replaces LLM-generated wiring.
# Stage2a now supplies a JSON SPEC (judgment inputs), NOT Python code. This fixed
# harness assembles the full RESULT deterministically: quant blocks from gps_quant
# (ONCE), qualitative blocks from the LLM's scored inputs, IVC + scenarios + bull/bear
# from ivc_lib. Eliminates: wiring code errors, GPS double-count/omission, degraded
# runs, GPS_TOTAL_MISMATCH. Same RESULT shape Render Tables/gate/auditor already read.
# ============================================================================
def analyze(data, spec):
    from ivc_lib import ivc, bull_bear_table, gps_quant
    data = data or {}
    spec = spec or {}
    A = spec.get("assumptions", {}) or {}

    pd = data.get("price_data", {}) if isinstance(data.get("price_data"), dict) else {}
    cp = pd.get("current_price")
    price = cp.get("adjClose") if isinstance(cp, dict) else (cp if cp else data.get("current_price"))

    base_inp = {
        "price": price,
        "eps_normalized": data.get("eps0_reported"),
        "levered_fcf_per_share": data.get("levered_fcf_per_share"),
        "growth_rate": A.get("growth_rate"),
        "future_pe": A.get("future_pe"),
        "hurdle": A.get("hurdle", 0.12),
        "discount_rate": A.get("discount_rate", A.get("hurdle", 0.12)),
        "share_dilution_cagr": data.get("dilution_cagr", 0.0),
        "pe_hist_median": data.get("pe_hist_median"),
        "pe_sector_median": data.get("peer_median_pe") or data.get("pe_sector_median"),
        "dividend_yield": A.get("dividend_yield", data.get("div_yield", 0.0)),
        "dividend_growth": A.get("dividend_growth", 0.0),
        "fade": A.get("fade", True),
        "terminal_growth": A.get("terminal_growth", 0.04),
        "years": A.get("years", 10),
        "mos_targets": A.get("mos_targets", [0.10, 0.20, 0.30]),
    }
    # v2.6: DETERMINISTIC PE-CAP (was a gate REWORK trigger 'pe_cap_unjustified'). The LLM's
    # future_pe is clamped to a defensible anchor here, so it can never overreach and the gate
    # never has to reject-to-REWORK. Anchor = best of peer/hist/sector median (allow up to 1.2x).
    # If NO anchor exists at all -> conservative constant + flag (produces a verdict, not a REWORK).
    NO_ANCHOR_PE = 20.0
    _anchors = [x for x in (data.get("peer_median_pe"), data.get("pe_hist_median"),
                            data.get("pe_sector_median")) if isinstance(x, (int, float)) and x > 0]
    pe_flags = []

    def _cap_pe(v):
        if not isinstance(v, (int, float)) or v <= 0:
            return v
        if _anchors:
            cap = 1.2 * max(_anchors)
            if v > cap:
                pe_flags.append("future_pe %.1f capped at 1.2x anchor = %.1f" % (v, cap))
                return round(cap, 1)
            return v
        if v > NO_ANCHOR_PE:
            pe_flags.append("no PE anchor (peer/hist/sector all null) -> future_pe %.1f capped at conservative %.0f" % (v, NO_ANCHOR_PE))
            return NO_ANCHOR_PE
        return v

    base_inp["future_pe"] = _cap_pe(base_inp.get("future_pe"))
    ivc_base = ivc(base_inp)
    if isinstance(ivc_base, dict) and "error" in ivc_base:
        # honest error (e.g., Category-F / missing inputs) — NOT a crash, NOT fabricated
        return {"error": ivc_base["error"], "_harness": True, "_FALLBACK": True,
                "ivc_base": ivc_base, "verdict_cap": "AVOID",
                "flags": ["harness_ivc_error_inputs_insufficient"]}

    # scenarios -> pwfv
    scen_spec = spec.get("scenarios") or {}
    defw = {"bear": 0.30, "base": 0.45, "bull": 0.25}
    scenarios, pwfv, wsum = {}, 0.0, 0.0
    for name in ("bear", "base", "bull"):
        s = scen_spec.get(name, {}) or {}
        w = float(s.get("weight", defw[name]))
        inp = dict(base_inp)
        inp.update(s.get("overrides") or {})
        if "future_pe" in inp: inp["future_pe"] = _cap_pe(inp["future_pe"])
        r = ivc(inp)
        scenarios[name] = {"weight": w, "overrides": s.get("overrides") or {}, "result": r}
        iv = r.get("intrinsic_value") if isinstance(r, dict) else None
        if iv is not None:
            pwfv += w * iv
            wsum += w
    pwfv = round(pwfv, 2) if wsum > 0 else None

    bb = bull_bear_table(base_inp, spec.get("bull_bear_args") or [])
    ivbv = ivc_base.get("intrinsic_value")
    sensitivity = {"sum_expected_impact": bb.get("sum_expected_impact"),
                   "pwfv_minus_ivbase": (round(pwfv - ivbv, 2) if (pwfv is not None and ivbv is not None) else None),
                   "_note": "Sum EI is a one-factor sensitivity sum; NOT additive to scenario PWFV-IV_base"}

    # GPS: quant (deterministic, ONCE) + qualitative (LLM-scored inputs)
    q = gps_quant(data)
    ql = spec.get("qualitative_scores") or {}

    def _qp(k):
        v = ql.get(k)
        if isinstance(v, dict):
            try: return float(v.get("points", 0))
            except (TypeError, ValueError): return 0
        return float(v) if isinstance(v, (int, float)) else 0

    def _qe(k):
        v = ql.get(k)
        return v.get("evidence", "") if isinstance(v, dict) else ""

    blocks = [
        {"name": "A (growth)", "points": q["A_quant"], "max": 16, "evidence": q["detail"]["A"]},
        {"name": "A_runway", "points": _qp("A_runway"), "max": 4, "evidence": _qe("A_runway")},
        {"name": "B (profitability)", "points": q["B"], "max": 15, "evidence": q["detail"]["B"]},
        {"name": "C (valuation)", "points": q["C"], "max": 15, "evidence": q["detail"]["C"]},
        {"name": "D (balance sheet)", "points": q["D"], "max": 10, "evidence": q["detail"]["D"]},
        {"name": "E_moat", "points": _qp("E_moat"), "max": 15, "evidence": _qe("E_moat")},
        {"name": "F (momentum)", "points": q["F_quant"], "max": 10, "evidence": q["detail"]["F"]},
        {"name": "F_forecast_trend", "points": _qp("F_forecast_trend"), "max": 5, "evidence": _qe("F_forecast_trend")},
        {"name": "G_capalloc", "points": _qp("G_capalloc"), "max": 5, "evidence": _qe("G_capalloc")},
        {"name": "H_sentiment", "points": _qp("H_sentiment"), "max": 5, "evidence": _qe("H_sentiment")},
    ]
    gps_total = round(sum(b["points"] for b in blocks), 1)
    gps = {"blocks": blocks, "total": gps_total, "quant_detail": q["detail"], "max": 100}

    icb = ivc_base.get("implied_cagr_pct")
    verdict_cap = "AVOID" if (icb is None or icb < 12.0) else "WATCH+"

    return {
        "_FALLBACK": False, "_harness": True,
        "ivc_base": ivc_base,
        "scenarios": scenarios, "pwfv": pwfv,
        "weights": {k: scenarios[k]["weight"] for k in scenarios},
        "bull_bear": bb, "sensitivity": sensitivity,
        "gps": gps, "mos_ladder": ivc_base.get("mos_ladder"),
        "gates": {"hurdle_gate": ivc_base.get("hurdle_gate")},
        "verdict_cap": verdict_cap,
        "self_tests_all": bool(ivc_base.get("self_tests")),
        "flags": (ivc_base.get("flags", []) + pe_flags),
        "pe_cap": {"anchors_available": bool(_anchors), "anchor_used": (round(1.2*max(_anchors),1) if _anchors else NO_ANCHOR_PE), "flags": pe_flags},
    }


@app.route("/analyze", methods=["POST"])
def _analyze():
    """v2.5 deterministic harness. Body: {"data": {...payload...}, "spec": {...judgment inputs...}}.
    Never executes LLM code; assembles RESULT from ivc_lib + the LLM's JSON spec."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(analyze(body.get("data", {}), body.get("spec", {}))), 200
    except Exception as e:
        return jsonify({"error": "RUNNER_ERROR: harness exception: " + str(e)[:200], "_FALLBACK": True}), 200


if __name__ == "__main__":
    # Railway/Render provide $PORT; default 8080 locally.
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
