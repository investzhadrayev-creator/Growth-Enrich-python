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
from edgar_form4 import edgar_form4    # v2.8: SEC EDGAR Form 4 insider transactions (phase 2)
from market_facts import market_facts  # v3.9: second-source forward data + in-house peer P/E
from macro_prices import macro_prices  # v4.2: FRED risk-free + Tiingo series (keys stay server-side)

app = Flask(__name__)

# Hard cap so a runaway generated script can't hang the worker.


def _peer_pe_excluded(data):
    """True when peer_median_pe exists but its basis is trailing, so it cannot anchor the cap."""
    return (isinstance(data.get("peer_median_pe"), (int, float))
            and "trailing" in str(data.get("peer_median_pe_basis") or ""))


def _pe_anchor_fwd(data):
    """The peer/sector anchor for the FORWARD P/E cap -- forward-basis inputs only.

    A trailing peer median is not a forward anchor. It is not conservative either: a peer set
    with depressed earnings inflates the trailing median without saying anything about the
    multiple this name deserves. NFLX 2026-07-16: peers DIS/WBD/SPOT/PARA gave an in-house
    TRAILING median of 95.09 (WBD alone traded at 95x trailing on collapsed earnings). That
    became pe_sector_median -> cap = 1.5 x 95.09 ~ 143 -- a cap so loose it could never bind.
    The EVIDENCE PACK then printed it under a hardcoded "fwd P/E" label, so a trailing figure
    travelled through the whole report wearing a forward name.
    Dropping it here means the cap falls back to pe_hist_median, or to the conservative
    no-anchor default -- both defensible. A wrong anchor is worse than no anchor.
    """
    if _peer_pe_excluded(data):
        peer = None
    else:
        peer = data.get("peer_median_pe")
    for v in (peer, data.get("pe_sector_median")):
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


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

    # v2.9: SANITIZE the LLM spec. dict.get(k, default) does NOT substitute the default when the
    # key EXISTS with value None -- and Stage2a legitimately writes explicit nulls (e.g. PLTR pays
    # no dividend -> "dividend_growth": null). Those nulls reached ivc_lib and blew up on 1+None
    # ("unsupported operand type(s) for +: 'int' and 'NoneType'") -> empty numeric layer. The
    # deterministic layer must never trust the spec's shape: coerce here, once, at the boundary.
    def _f(v, dflt):
        return v if (isinstance(v, (int, float)) and not isinstance(v, bool)) else dflt

    def _clean_ov(ov):
        """Drop null overrides so the base assumption survives instead of poisoning ivc."""
        return {k: v for k, v in (ov or {}).items() if v is not None}

    pd = data.get("price_data", {}) if isinstance(data.get("price_data"), dict) else {}
    cp = pd.get("current_price")
    price = cp.get("adjClose") if isinstance(cp, dict) else (cp if cp else data.get("current_price"))

    _hurdle = _f(A.get("hurdle"), 0.12)
    base_inp = {
        "price": price,
        "eps_normalized": data.get("eps0_reported"),
        "levered_fcf_per_share": data.get("levered_fcf_per_share"),
        # growth_rate/future_pe stay None-able on purpose: ivc() returns an HONEST error for a
        # missing driver rather than silently substituting an invented default.
        "growth_rate": _f(A.get("growth_rate"), None),
        "future_pe": _f(A.get("future_pe"), None),
        "hurdle": _hurdle,
        "discount_rate": _f(A.get("discount_rate"), _hurdle),
        "share_dilution_cagr": _f(data.get("dilution_cagr"), 0.0),
        "pe_hist_median": _f(data.get("pe_hist_median"), None),
        "pe_sector_median": _pe_anchor_fwd(data),
        "dividend_yield": _f(A.get("dividend_yield"), _f(data.get("div_yield"), 0.0)),
        "dividend_growth": _f(A.get("dividend_growth"), 0.0),
        "fade": A.get("fade", True) is not False,
        "terminal_growth": _f(A.get("terminal_growth"), 0.04),
        "years": int(_f(A.get("years"), 10)),
        "mos_targets": A.get("mos_targets") or [0.10, 0.20, 0.30],
    }
    # v2.6: DETERMINISTIC PE-CAP (was a gate REWORK trigger 'pe_cap_unjustified'). The LLM's
    # future_pe is clamped to a defensible anchor here, so it can never overreach and the gate
    # never has to reject-to-REWORK. Anchor = best of peer/hist/sector median (allow up to 1.2x).
    # If NO anchor exists at all -> conservative constant + flag (produces a verdict, not a REWORK).
    NO_ANCHOR_PE = 20.0
    _anchors = [x for x in (_pe_anchor_fwd(data), data.get("pe_hist_median"))
                if isinstance(x, (int, float)) and x > 0]
    pe_flags = []
    if _peer_pe_excluded(data):
        pe_flags.append(
            "peer_median_pe_%.1f_EXCLUDED_from_cap_basis_is_trailing_not_forward"
            % data.get("peer_median_pe"))

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

    # ------------------------------------------------------------------------------------------
    # v3.4 DUAL BASIS (Variant B). GAAP EPS DOUBLE-COUNTS stock-based compensation for valuation:
    # SBC is subtracted from earnings AND the issued shares dilute the per-share base — the same
    # $1 charged twice. For SBC-heavy names this halves the apparent per-share economics
    # (NOW: GAAP $1.67/sh vs FCF $4.44/sh -> 63x vs 24x on the same price). The Graham-Dodd
    # answer is not to pick a side silently but to PRICE BOTH and show the gap:
    #   - GAAP leg: reported EPS + the spec's future_pe (as before).
    #   - FCF leg: levered FCF/share + GROSS dilution (before buybacks), so SBC is charged
    #     exactly ONCE — through the share count, not through the income statement. Using NET
    #     dilution here would double-CREDIT buybacks; gross is the honest pairing.
    #   - The FCF multiple is future_pe * FCF_PE_DISCOUNT (0.9): a conservative haircut, since
    #     an earnings multiple applied 1:1 to a larger FCF base would smuggle in optimism.
    # The verdict_cap is driven by the CONSERVATIVE leg (min implied CAGR): the bull case may
    # argue from the other leg in prose, but sizing discipline follows the stricter number.
    # ------------------------------------------------------------------------------------------
    FCF_PE_DISCOUNT = 0.90
    fcfps = data.get("levered_fcf_per_share")
    dil_gross = _f(data.get("dilution_cagr_gross"), None)  # optional upstream field
    if dil_gross is None:
        # GROSS dilution = what the share count would do WITHOUT buybacks = net dilution plus
        # the share-count effect of ONLY the SBC-offsetting portion of buybacks. Buybacks BEYOND
        # SBC are genuine capital return, not hidden dilution — adding them back would absurdly
        # charge shareholder-friendly names (ADBE: buyback/FCF=114%, count SHRINKING 2.5%/yr)
        # with double-digit phantom dilution. Cap the added-back portion at the SBC actually
        # granted (buyback_vs_sbc >= 1 -> everything issued was retired -> add back only SBC).
        dil_net = _f(data.get("dilution_cagr"), 0.0)
        bb_fcf = _f(data.get("buyback_to_fcf"), None)
        bb_vs_sbc = _f(data.get("buyback_vs_sbc"), None)
        fcf_total = _f(data.get("levered_fcf"), None)
        mcap = None
        if price and _f(data.get("shares_current"), None):
            mcap = price * data.get("shares_current")
        if bb_fcf is not None and fcf_total and mcap and mcap > 0:
            bb_dollars = bb_fcf * fcf_total
            if bb_vs_sbc is not None and bb_vs_sbc > 1:
                bb_dollars = bb_dollars / bb_vs_sbc      # only the SBC-offsetting share
            dil_gross = dil_net + bb_dollars / mcap
        else:
            dil_gross = dil_net + _f(data.get("sbc_to_revenue"), 0.0) * 0.5  # coarse proxy
    dil_gross = max(dil_gross, _f(data.get("dilution_cagr"), 0.0))  # gross can never be < net
    ivc_fcf = None
    if isinstance(fcfps, (int, float)) and fcfps > 0 and base_inp.get("future_pe"):
        fcf_inp = dict(base_inp)
        fcf_inp["eps_normalized"] = None                 # force the FCF engine in ivc()
        fcf_inp["levered_fcf_per_share"] = fcfps
        fcf_inp["future_pe"] = round(base_inp["future_pe"] * FCF_PE_DISCOUNT, 2)
        fcf_inp["share_dilution_cagr"] = round(dil_gross, 5)
        ivc_fcf = ivc(fcf_inp)
        if isinstance(ivc_fcf, dict) and "error" in ivc_fcf:
            ivc_fcf = None

    dual_basis = None
    if ivc_fcf:
        iv_g, iv_f = ivc_base.get("intrinsic_value"), ivc_fcf.get("intrinsic_value")
        ic_g, ic_f = ivc_base.get("implied_cagr_pct"), ivc_fcf.get("implied_cagr_pct")
        conservative = "gaap_eps" if (ic_g is not None and ic_f is not None and ic_g <= ic_f) else "fcf_per_share"
        dual_basis = {
            "gaap_eps": {"iv": iv_g, "implied_cagr_pct": ic_g,
                         "base_per_share": ivc_base.get("inputs", {}).get("base_per_share")},
            "fcf_per_share": {"iv": iv_f, "implied_cagr_pct": ic_f,
                              "base_per_share": fcfps,
                              "future_multiple": fcf_inp["future_pe"],
                              "gross_dilution_used": fcf_inp["share_dilution_cagr"]},
            "gap_iv_pct": (round((iv_f / iv_g - 1) * 100, 1) if (iv_g and iv_f) else None),
            "conservative_leg": conservative,
            "verdict_leg": conservative,
            "_note": ("GAAP charges SBC in earnings AND in the share count (double count); the FCF "
                      "leg charges it once, via GROSS dilution. A large gap means the verdict is "
                      "really a judgment about SBC, not about the business."),
        }

    # scenarios -> pwfv
    scen_spec = spec.get("scenarios") or {}
    defw = {"bear": 0.30, "base": 0.45, "bull": 0.25}
    scenarios, pwfv, wsum = {}, 0.0, 0.0
    for name in ("bear", "base", "bull"):
        s = scen_spec.get(name, {}) or {}
        w = float(_f(s.get("weight"), defw[name]))
        inp = dict(base_inp)
        inp.update(_clean_ov(s.get("overrides")))
        if "future_pe" in inp: inp["future_pe"] = _cap_pe(inp["future_pe"])
        r = ivc(inp)
        scenarios[name] = {"weight": w, "overrides": s.get("overrides") or {}, "result": r}
        iv = r.get("intrinsic_value") if isinstance(r, dict) else None
        if iv is not None:
            pwfv += w * iv
            wsum += w
    pwfv = round(pwfv, 2) if wsum > 0 else None

    _bb_args = [dict(a, overrides=_clean_ov(a.get("overrides")),
                     probability=_f(a.get("probability"), 0.5))
                for a in (spec.get("bull_bear_args") or []) if isinstance(a, dict)]
    bb = bull_bear_table(base_inp, _bb_args)
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
    for _b in blocks:
        _b["points"] = _f(_b.get("points"), 0)
    gps_total = round(sum(b["points"] for b in blocks), 1)
    gps = {"blocks": blocks, "total": gps_total, "quant_detail": q["detail"], "max": 100}

    # v3.3: THREE-BAND verdict_cap, matching the stage4 gate rule (check #3) exactly:
    #   <12%    -> AVOID   (fails the hurdle floor)
    #   12-16%  -> WATCH+  (clears the floor, below the 12-16% mandate target)
    #   >=16%   -> BUY     (in the mandate's target zone)
    # v2.5-v3.2 collapsed this to two bands ("AVOID" if <12 else "WATCH+"), which made BUY
    # structurally UNREACHABLE for every name regardless of how good the numbers were — the
    # gate prompt has always specified three bands, the harness only implemented two.
    # This is a CAP, not a verdict: it bounds how bullish the arbiter may be, it never forces
    # a BUY. The arbiter can still land lower on qualitative grounds.
    # v3.4: the cap is driven by the CONSERVATIVE leg when a dual basis exists — sizing follows
    # the stricter number; the memo may argue the other leg in prose.
    icb = ivc_base.get("implied_cagr_pct")
    if dual_basis:
        legs = [dual_basis["gaap_eps"]["implied_cagr_pct"],
                dual_basis["fcf_per_share"]["implied_cagr_pct"]]
        legs = [x for x in legs if x is not None]
        if legs:
            icb = min(legs)
    if icb is None or icb < 12.0:
        verdict_cap = "AVOID"
    elif icb < 16.0:
        verdict_cap = "WATCH+"
    else:
        verdict_cap = "BUY"

    # ------------------------------------------------------------------------------------------
    # v3.5 MARKET CONTEXT — deterministic "fear-discount" diagnostics. The recurring setup the
    # mandate wants to catch (GOOGL-2024, LLY-Aug-2025, the 2026 hyperscaler capex scare):
    # fundamentals keep compounding while the MULTIPLE is compressed by one named fear. Three
    # quantitative legs; the qualitative leg (naming the fear + its falsifier) lives in stage2b.
    # All inputs already exist in the payload — no new data dependencies.
    # ------------------------------------------------------------------------------------------
    def _series_vals(key):
        s = data.get(key) or []
        return [p.get("val") for p in s if isinstance(p, dict) and isinstance(p.get("val"), (int, float))]

    market_context = {}

    # (1) Multiple compression vs fundamentals deceleration.
    #     discount = how much cheaper than its own history the name trades;
    #     decel    = how much slower it actually grows. divergence = discount - decel.
    #     Large positive divergence -> the market prices far more deterioration than is showing.
    pe_now = _f(data.get("fwd_pe"), None)
    if (pe_now is None or pe_now <= 0) and price:
        e0 = _f(data.get("eps0_reported"), None)
        if e0 and e0 > 0:
            pe_now = price / e0
    pe_anchor = _f(data.get("pe_hist_median"), None)
    # v3.8: g_now MUST be a FORWARD estimate. The old fallback used eps_cagr_3y, but the 3y
    # window lies INSIDE the 5y window — comparing them is not "growth now vs history", it is
    # two overlapping trailing periods. On ADBE (Yahoo returned nothing, so no estimates) that
    # produced g_now=18.2% vs g_hist=9.0% -> decel=-101.9% -> divergence=169.5pp and a
    # FEAR-DISCOUNT flag fired on an artifact (the 5y window simply contains the COVID margin
    # trough). No forward estimate -> report the multiple discount ONLY, make no divergence
    # claim, raise no flag. Honest silence beats a confident artifact.
    g_now = None
    for est in (data.get("eps_estimates") or []):
        if isinstance(est, dict) and str(est.get("period", "")).lower() in ("+1y", "1y"):
            gv = est.get("growth")
            if isinstance(gv, (int, float)):
                g_now = gv if abs(gv) < 3 else gv / 100.0
            break
    g_hist = _f(data.get("eps_cagr_5y"), None)
    if pe_now and pe_anchor and pe_anchor > 0:
        mc = {"fwd_pe": round(pe_now, 2), "pe_hist_median": pe_anchor,
              "multiple_discount_pct": round((1 - pe_now / pe_anchor) * 100, 1)}
        if not isinstance(data.get("fwd_pe"), (int, float)) or data.get("fwd_pe") <= 0:
            mc["_pe_basis"] = "trailing (price/eps0) — forward P/E unavailable"
        if g_now is None:
            mc["divergence_available"] = False
            mc["fear_discount_setup"] = False
            mc["_why_no_divergence"] = ("no forward EPS estimate available; a trailing-window "
                                        "comparison would be an artifact, not a signal")
        elif g_hist and g_hist > 0.02:
            mc["divergence_available"] = True
            mc["growth_now_pct"] = round(g_now * 100, 1)
            mc["growth_hist_pct"] = round(g_hist * 100, 1)
            mc["growth_decel_pct"] = round((1 - g_now / g_hist) * 100, 1)
            mc["divergence_pp"] = round(mc["multiple_discount_pct"] - mc["growth_decel_pct"], 1)
            # flag only when the discount is real AND fundamentals are broadly intact
            mc["fear_discount_setup"] = bool(mc["multiple_discount_pct"] >= 25
                                             and mc["divergence_pp"] >= 20
                                             and g_now > 0)
        else:
            mc["divergence_available"] = False
            mc["fear_discount_setup"] = False
        market_context["multiple_compression"] = mc

    # (2) Earnings-revision vs price-momentum divergence (the LLY-Aug-25 pattern):
    #     analysts revising UP while the price grinds DOWN.
    erb = _f(data.get("erb_90d"), None)
    rs6 = _f(data.get("rel_strength_6m"), None)
    if erb is not None and rs6 is not None:
        market_context["revision_vs_price"] = {
            "erb_90d": erb, "rel_strength_6m": rs6,
            "divergence": bool(erb > 0.02 and rs6 < -0.15),
            "_note": "positive revisions into a falling price = market fear vs analyst evidence",
        }

    # (3) Reinvestment quality — the direct answer to the capex scare. Incremental ROIC:
    #     how much NEW operating income the last two years of capex actually produced.
    #     v3.8 GUARD: only meaningful when capex is MATERIAL to the business model. An
    #     asset-light name grows through R&D/S&M (opex), not capex, so dividing by a tiny
    #     capex base yields an absurd ratio — ADBE returned 568% off $0.36B of 2y capex,
    #     a number that looks like a finding and is pure arithmetic noise. Require capex to
    #     be >=5% of revenue before making the claim at all.
    oi = _series_vals("operating_income")
    cx = _series_vals("capex")
    rev = _series_vals("revenue")
    if len(oi) >= 3 and len(cx) >= 2:
        delta_oi = oi[-1] - oi[-3]
        deployed = abs(cx[-1]) + abs(cx[-2])          # capex reported as negative outflow sometimes
        capex_intensity = None
        if rev and rev[-1] and rev[-1] > 0:
            capex_intensity = abs(cx[-1]) / rev[-1]
        if deployed > 0 and capex_intensity is not None and capex_intensity >= 0.05:
            market_context["reinvestment_quality"] = {
                "delta_operating_income_2y": round(delta_oi, 0),
                "capex_deployed_2y": round(deployed, 0),
                "capex_intensity_pct": round(capex_intensity * 100, 1),
                "incremental_roic_pct": round(delta_oi / deployed * 100, 1),
                "_note": ("each capex $ producing operating income = Google-2004, not a bubble; "
                          "negative or near-zero = the fear may be right"),
            }
        elif deployed > 0:
            market_context["reinvestment_quality"] = {
                "not_meaningful": True,
                "capex_intensity_pct": (round(capex_intensity * 100, 1)
                                        if capex_intensity is not None else None),
                "_note": ("asset-light: capex is <5% of revenue, so incremental ROIC on capex is "
                          "not a meaningful measure of reinvestment — this business compounds "
                          "through R&D/S&M (opex), not capital deployment"),
            }

    market_context = market_context or None

    # ------------------------------------------------------------------------------------------
    # v3.6 STREET VIEW — how the sell side prices the same name. Deterministic (yahoo tier):
    # consensus target mean/high/low, analyst depth, recommendation split, and the two spreads
    # that matter: price -> target (what the street expects) and PWFV -> target (where OUR model
    # disagrees with the street). Named-bank targets ("BofA $835") are NOT reliably available
    # from free deterministic sources — those flow through the Stage1 fact pack with citations
    # and must be quoted with their source and date, never merged into this block.
    # ------------------------------------------------------------------------------------------
    street_view = None
    pt = data.get("price_target") if isinstance(data.get("price_target"), dict) else {}
    pt_mean = _f(pt.get("mean"), _f(data.get("price_target_mean"), None))
    if pt_mean and price:
        pwfv_vs_street = None
        if pwfv:
            pwfv_vs_street = round((pwfv / pt_mean - 1) * 100, 1)
        street_view = {
            "consensus_target_mean": pt_mean,
            "consensus_target_high": _f(pt.get("high"), _f(data.get("price_target_high"), None)),
            "consensus_target_low": _f(pt.get("low"), _f(data.get("price_target_low"), None)),
            "upside_to_target_pct": round((pt_mean / price - 1) * 100, 1),
            "analyst_count": data.get("analyst_count"),
            "recommendation_mean": _f(data.get("recommendation_mean"), None),
            "recommendation_key": data.get("recommendation_key"),
            "pwfv_vs_street_pct": pwfv_vs_street,
            "analyst_actions_recent": (data.get("analyst_actions_recent") or [])[:8],
            "_tier": "yahoo consensus; named-bank targets belong to FACT_PACK with source+date",
        }

    return {
        "_FALLBACK": False, "_harness": True,
        "ivc_base": ivc_base,
        "scenarios": scenarios, "pwfv": pwfv,
        "weights": {k: scenarios[k]["weight"] for k in scenarios},
        "bull_bear": bb, "sensitivity": sensitivity,
        "gps": gps, "mos_ladder": ivc_base.get("mos_ladder"),
        "gates": {"hurdle_gate": ivc_base.get("hurdle_gate")},
        "verdict_cap": verdict_cap,
        "dual_basis": dual_basis,
        "market_context": market_context,
        "street_view": street_view,
        "self_tests_all": bool(ivc_base.get("self_tests")),
        "flags": (ivc_base.get("flags", []) + pe_flags),
        "pe_cap": {"anchors_available": bool(_anchors), "anchor_used": (round(1.2*max(_anchors),1) if _anchors else NO_ANCHOR_PE), "flags": pe_flags},
    }


@app.route("/edgar_form4", methods=["POST"])
def _edgar_form4():
    """
    SEC EDGAR Form 4 insider transactions (deterministic, replaces Perplexity-sourced prose).
    Body: {"ticker": "PLTR", "lookback_days": 270}.
    edgar_form4() never throws; parse failures per-filing are recorded in '_errors', not guessed.
    """
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(edgar_form4(body.get("ticker"), body.get("cik"),
                               body.get("lookback_days", 270))), 200


@app.route("/market_facts", methods=["POST"])
def _market_facts():
    """v3.9: second-source market layer. Body carries the keys (n8n holds them, nothing is
    stored here): {"ticker","peers","av_key","finnhub_key","tiingo_token","price","yahoo":{...}}.
    Never throws; failures land in _errors per source."""
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(market_facts(b.get("ticker"), b.get("peers"), b.get("av_key"),
                                b.get("finnhub_key"), b.get("tiingo_token"),
                                b.get("price"), b.get("yahoo"),
                                b.get("finra_client_id"), b.get("finra_client_secret"),
                                b.get("shares_outstanding"))), 200


@app.route("/macro_prices", methods=["POST"])
def _macro_prices():
    """v4.2: risk-free (FRED) + adjusted price series (Tiingo). Moved off the n8n side because
    n8n 2.x Code nodes cannot read env vars (task-runner sandbox), and the alternative was
    inlining the keys into the workflow JSON. Keys are read from THIS service's environment."""
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(macro_prices(b.get("ticker"), b.get("benchmark", "SPY"),
                                b.get("start", "2023-01-01"))), 200


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
