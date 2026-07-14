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
import subprocess
import sys
import tempfile

from flask import Flask, request, jsonify

from enrich_yf import enrich_yf
from scenario_f import scenario_tree   # v1.5: Core-V Category-F anchors
from edgar_facts import edgar_facts    # v1.8: SEC EDGAR primary-source financials

app = Flask(__name__)

# Hard cap so a runaway generated script can't hang the worker.
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "120"))


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


@app.route("/run", methods=["POST"])
def _run():
    """
    Body: {"code": "<python source>", "data": {...payload...}}
    The generated wiring code expects a variable `payload` in scope and prints
    a single JSON line as its last stdout line. We inject payload as a JSON literal
    prefixed to the code, run it in a subprocess, and return the parsed final line.
    """
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("code", "")
    data = body.get("data", {})
    if not code:
        return jsonify({"error": "RUNNER_ERROR: no code provided"}), 200

    # Prefix: make `payload` available to the wiring code deterministically.
    preamble = "import json as _json\npayload = _json.loads(r'''" + \
        json.dumps(data).replace("'''", "'\\''") + "''')\n"
    full = preamble + code

    # SYNTAX PRE-CHECK: catch SyntaxError BEFORE running, so we can fall back cleanly
    # instead of dumping an empty RESULT (the META failure mode). If the LLM's wiring
    # is malformed, we still produce a baseline IVC from the pinned library.
    syntax_ok = True
    syntax_err = None
    try:
        compile(full, "<wiring>", "exec")
    except SyntaxError as e:
        syntax_ok = False
        syntax_err = f"{e.msg} (line {e.lineno})"

    if syntax_ok:
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(full)
                path = f.name
            proc = subprocess.run(
                [sys.executable, path],
                capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return jsonify(_fallback_ivc(data, "execution timed out")), 200
        except Exception as e:
            return jsonify(_fallback_ivc(data, str(e)[:200])), 200
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

        out = (proc.stdout or "").strip()
        if out:
            last = [ln for ln in out.splitlines() if ln.strip()][-1]
            try:
                parsed = json.loads(last)
                if isinstance(parsed, dict) and not parsed.get("error"):
                    return jsonify({"stdout": last, "result": parsed}), 200
                # wiring returned an error dict -> fall back
                return jsonify(_fallback_ivc(data, parsed.get("error", "wiring error"))), 200
            except Exception:
                return jsonify(_fallback_ivc(data, "last stdout line not JSON")), 200
        # empty stdout -> fall back
        return jsonify(_fallback_ivc(data, "no stdout; stderr=" + (proc.stderr or "")[:300])), 200

    # SyntaxError -> fallback baseline IVC so RESULT is never empty
    return jsonify(_fallback_ivc(data, "Stage2a SyntaxError: " + str(syntax_err))), 200


def _fallback_ivc(data, reason):
    """
    When the LLM wiring fails (syntax error, timeout, empty output), compute a
    BASELINE IVC directly from the pinned library so RESULT is never empty.
    Uses conservative default assumptions clearly flagged as fallback.
    The memo/gate see this is a fallback (flags carry 'FALLBACK_...') and can
    treat the run as degraded rather than dead.
    """
    try:
        from ivc_lib import ivc, ivc_delta, bull_bear_table, gps_quant
    except Exception as e:
        return {"error": "RUNNER_ERROR: fallback lib import failed: " + str(e)[:150],
                "fallback_reason": reason}
    try:
        pd = data.get("price_data", {}) if isinstance(data, dict) else {}
        cp = pd.get("current_price")
        price = cp.get("adjClose") if isinstance(cp, dict) else cp
        eps0 = data.get("eps0_reported")
        fcfps = data.get("levered_fcf_per_share")
        dil = data.get("dilution_cagr") or 0.0
        pehm = data.get("pe_hist_median")
        peer_med = data.get("peer_median_pe")
        # conservative defaults; fade guards extreme growth
        g_base = 0.15
        pe_base = peer_med if peer_med else (min(pehm, 30) if pehm else 25)
        base = {"price": price, "eps_normalized": eps0, "levered_fcf_per_share": fcfps,
                "growth_rate": g_base, "future_pe": pe_base, "hurdle": 0.12,
                "share_dilution_cagr": dil, "pe_hist_median": pehm}
        scen = {
            "bear": {"weight": 0.30, "growth_rate": 0.08, "future_pe": (pe_base * 0.7),
                     "result": ivc(dict(base, growth_rate=0.08, future_pe=pe_base * 0.7))},
            "base": {"weight": 0.45, "growth_rate": g_base, "future_pe": pe_base,
                     "result": ivc(base)},
            "bull": {"weight": 0.25, "growth_rate": 0.22, "future_pe": (pe_base * 1.25),
                     "result": ivc(dict(base, growth_rate=0.22, future_pe=pe_base * 1.25))},
        }
        ivb = scen["base"]["result"]
        pwfv = sum(scen[k]["weight"] * (scen[k]["result"].get("intrinsic_value") or 0) for k in scen)
        gps = gps_quant(data) if isinstance(data, dict) else {}
        # v3.0: COMPUTE the verdict from the real hurdle floor/band, never hardcode it.
        # Bug fixed: previously hardcoded "WATCH+" regardless of implied_cagr, which let a
        # NVDA run with base implied_cagr=8.68% (< 12% floor -> should be AVOID) report
        # WATCH+ -- Stage4's gate_override check correctly caught this as a real defect.
        icagr_base_pct = ivb.get("implied_cagr_pct")
        if icagr_base_pct is None:
            verdict_cap = "AVOID"  # can't compute -> most conservative, not a guess
        elif icagr_base_pct < 12.0:
            verdict_cap = "AVOID"  # hard floor, same rule as the real pipeline (v1.7)
        else:
            # Even if icagr clears the floor/band, fallback mode has NO qualitative GPS,
            # NO Bull/Bear, NO pe_cap/growth-ceiling gate checks -- structurally cannot
            # justify anything above WATCH+. This is a ceiling, not an endorsement.
            verdict_cap = "WATCH+"
        return {
            "_FALLBACK": True, "fallback_reason": reason,
            "bull_bear_unavailable": True, "radar_unavailable": True,
            "scenarios": scen, "pwfv": round(pwfv, 2), "ivc_base": ivb,
            "mos_ladder": ivb.get("mos_ladder"),
            "gps": {"blocks": {"pinned_ACDF": gps}, "total": None,
                    "_note": "fallback: qualitative blocks (E/A_runway/G/H) not scored -- Stage2a wiring failed"},
            "flags": ["FALLBACK_IVC_USED_wiring_failed",
                      "bull_bear_and_radar_UNAVAILABLE_this_run_rerun_required",
                      "verdict_cap_computed_from_hurdle_floor_not_hardcoded"],
            "verdict_cap": verdict_cap,
            "self_tests_all": bool(ivb.get("self_tests")),
        }
    except Exception as e:
        return {"error": "RUNNER_ERROR: fallback IVC failed: " + str(e)[:200],
                "fallback_reason": reason}



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
        "flags": ivc_base.get("flags", []),
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
