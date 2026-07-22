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
        # v4.2.28 (BACKLOG P) BASE-GROWTH ANCHORING. The base scenario's growth_rate used to come
        # straight from the LLM (A.get("growth_rate")) — the ONE un-anchored driver of ivc_base,
        # so IV/PWFV/implied_cagr/MoS floated 6% across runs on identical facts while future_pe
        # (anchored by _cap_pe since v2.6) stayed put. Symmetric fix: the base leg is now anchored
        # to a DETERMINISTIC figure and the LLM's number is recorded but does not steer.
        #   base g = min(rev_cagr_3y, rev_cagr_5y), capped at 20% (mandate).
        # rev, not eps: extrapolating margin-expansion (eps_cagr 33%) into the BASE is against the
        # Graham-Dodd mandate — the margin bet's home is the BULL scenario, not base. min() takes
        # the more conservative of the two revenue horizons. Fade is untouched (ivc_lib applies g
        # years 1-5 then fades to terminal_g). Bull/bear remain fully LLM-driven downstream.
        "growth_rate": None,  # set just below to the anchored value
        "future_pe": _f(A.get("future_pe"), None),
        "hurdle": _hurdle,
        # v4.2.31 (BACKLOG P, base-determinism sweep — architect sanction on all 5 at once). Every
        # base input that fed IV from the LLM is pinned to a deterministic value; the LLM number is
        # recorded (llm_*) and flagged on divergence but does NOT steer the base. bull/bear keep the
        # LLM values (scenario analysis). This closes the class by audit, not by pair-induction.
        "discount_rate": _hurdle,   # = hurdle (mandate A); llm_disc recorded below
        "share_dilution_cagr": _f(data.get("dilution_cagr"), 0.0),
        "pe_hist_median": _f(data.get("pe_hist_median"), None),
        "pe_sector_median": _pe_anchor_fwd(data),
        # dividend from filings, not LLM: yield from data (its market-snapshot drift lives in the
        # scorecard market class); growth = min(DPS CAGR 3y, 5y) capped at base_g (a dividend cannot
        # be modelled growing faster than the business). Computed just below where base_g is known.
        "dividend_yield": _f(data.get("div_yield"), 0.0),
        "dividend_growth": 0.0,      # set below to the deterministic DPS-CAGR anchor
        "fade": True,                # mandate "fade untouched" = always on, never an LLM toggle
        # terminal_growth pinned to 0.04 with an asymmetry guard: the EFFECTIVE terminal is
        # min(0.04, base_g) — the fade may slow the tail, never accelerate it (a sub-4% grower must
        # not have its tail lifted to 4%). Set below where base_g is known.
        "terminal_growth": 0.04,
        "years": 10,                 # structural horizon (mandate); never an LLM value
        "mos_targets": [0.10, 0.20, 0.30],  # mandate ladder; not LLM
    }
    # record the LLM's base opinions (do not steer) + divergence flags
    base_llm_flags = []
    _llm_disc = _f(A.get("discount_rate"), None)
    if isinstance(_llm_disc, (int, float)):
        base_inp["llm_disc"] = _llm_disc
        if abs(_llm_disc - _hurdle) > 0.01:  # >1pp
            base_llm_flags.append("disc_divergence: LLM %.3f vs hurdle %.3f (>1pp)" % (_llm_disc, _hurdle))
    _llm_terminal = _f(A.get("terminal_growth"), None)
    if isinstance(_llm_terminal, (int, float)) and abs(_llm_terminal - 0.04) > 0.005:
        base_inp["llm_terminal_g"] = _llm_terminal
        base_llm_flags.append("terminal_g_divergence: LLM %.4f vs anchor 0.04" % _llm_terminal)
    _llm_years = _f(A.get("years"), None)
    if isinstance(_llm_years, (int, float)) and int(_llm_years) != 10:
        base_llm_flags.append("years_divergence: LLM %d vs structural 10" % int(_llm_years))
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

    # v4.2.30 (BACKLOG P, future_pe leg — FINAL architect mandate). base future_pe is anchored to
    #   min(pe_median_5y, pe_median_10y, 25), NO floor.
    # The two window medians come from Growth Enrich (each a median of FY year-points, where a
    # year-point is itself the median of that FY's 12 month-end prices / FY diluted EPS; outliers
    # PE in (0,100); >=3 year-points per window or the window is null). Ceiling 25 is the low end of
    # the band — doubt resolves conservatively; names worthy of more prove it through growth, not a
    # fatter exit multiple. No floor: a low median passes through (margin of safety lives in the
    # hurdle/MoS rungs, not an inflated exit multiple). If NEITHER window has >=3 points, there is
    # no history to anchor on -> fixed default 18 (long-run market median) with a LOUD flag. LLM
    # base future_pe is recorded (llm_base_pe) and flagged (pe_divergence >5) but does NOT steer the
    # base; bull/bear future_pe remain fully LLM-driven.
    PE_CAP = 25.0
    PE_DEFAULT = 18.0
    pe_anchor_flags = []
    _pe_m5 = data.get("pe_median_5y")
    _pe_m10 = data.get("pe_median_10y")
    _llm_base_pe = _f(base_inp.get("future_pe"), None)  # the LLM's base future_pe, pre-anchor
    _window_meds = [x for x in (_pe_m5, _pe_m10) if isinstance(x, (int, float)) and x > 0]
    if _window_meds:
        _anchored_pe = min(min(_window_meds), PE_CAP)     # min(5y, 10y, 25); NO floor
        if min(_window_meds) > PE_CAP:
            pe_anchor_flags.append("base_future_pe min(median_5y,10y) %.1f capped at %.0f"
                                   % (min(_window_meds), PE_CAP))
        base_inp["future_pe"] = _anchored_pe
        base_inp["future_pe_basis"] = "min(pe_median_5y, pe_median_10y, %.0f) — deterministic, no floor" % PE_CAP
    else:
        # No window has >=3 year-points: no history to anchor on -> fixed long-run default, loud flag.
        base_inp["future_pe"] = PE_DEFAULT
        base_inp["future_pe_basis"] = "DEFAULT %.0f (insufficient history)" % PE_DEFAULT
        pe_anchor_flags.append("[PE ANCHOR: DEFAULT — insufficient history]")
    if isinstance(_llm_base_pe, (int, float)):
        base_inp["llm_base_pe"] = _llm_base_pe
        _pe_div = abs(_llm_base_pe - base_inp["future_pe"])
        if _pe_div > 5.0:
            pe_anchor_flags.append(
                "pe_divergence: LLM base future_pe %.1f vs anchor %.1f (%.1f > 5 points)"
                % (_llm_base_pe, base_inp["future_pe"], _pe_div))

    # v4.2.28 (BACKLOG P): compute the anchored base growth_rate deterministically.
    # _cagr is the SAME function the GPS block-A uses for rev_cagr5/rev_cagr3, so the anchor is
    # byte-identical to what the scorecard reports — no second, divergent computation.
    from ivc_lib import _cagr as _rev_cagr
    _rev_series = data.get("revenue")
    _rc5 = _rev_cagr(_rev_series, 5)
    _rc3 = _rev_cagr(_rev_series, 3)
    _anchor_candidates = [x for x in (_rc3, _rc5) if isinstance(x, (int, float))]
    GROWTH_CAP = 0.20  # absolute ceiling, symmetric to _cap_pe's multiple ceiling
    growth_flags = []
    if _anchor_candidates:
        _anchored_g = min(_anchor_candidates)              # conservative of the two horizons
        if _anchored_g > GROWTH_CAP:
            growth_flags.append("base_growth %.4f capped at %.2f" % (_anchored_g, GROWTH_CAP))
            _anchored_g = GROWTH_CAP
        base_inp["growth_rate"] = _anchored_g
        base_inp["growth_rate_basis"] = "min(rev_cagr_3y, rev_cagr_5y) capped %.0f%% (deterministic anchor)" % (GROWTH_CAP * 100)
    else:
        # No revenue series to anchor on. Fall back to the LLM number rather than fabricate one,
        # but flag loudly that the base leg is unanchored this run (honest, not silent).
        base_inp["growth_rate"] = _f(A.get("growth_rate"), None)
        base_inp["growth_rate_basis"] = "UNANCHORED: no revenue series; fell back to LLM growth_rate"
        growth_flags.append("base_growth_unanchored_no_revenue_series")
    # Record the LLM's base growth opinion WITHOUT letting it steer the base leg; flag material
    # divergence so the LLM's judgment stays visible to the auditor and to us.
    _llm_base_g = _f(A.get("growth_rate"), None)
    if isinstance(_llm_base_g, (int, float)):
        base_inp["llm_base_g"] = _llm_base_g
        if isinstance(base_inp.get("growth_rate"), (int, float)):
            _div_pp = abs(_llm_base_g - base_inp["growth_rate"]) * 100
            if _div_pp > 3.0:
                growth_flags.append(
                    "growth_divergence: LLM base g %.1f%% vs anchor %.1f%% (%.1fpp > 3pp)"
                    % (_llm_base_g * 100, base_inp["growth_rate"] * 100, _div_pp))

    # v4.2.31: terminal_growth asymmetry guard (mandate). Effective terminal = min(0.04, base_g):
    # the fade may only SLOW the tail toward terminal, never ACCELERATE a sub-4% grower up to 4%.
    _bg = base_inp.get("growth_rate")
    if isinstance(_bg, (int, float)):
        _eff_tg = min(0.04, _bg)
        if _eff_tg < 0.04:
            base_llm_flags.append("terminal_g asymmetry: capped to base_g %.4f (< 0.04, tail not lifted)" % _bg)
        base_inp["terminal_growth"] = _eff_tg

    # v4.2.31: dividend_growth from filings, not LLM. DPS series is split-normalized 10-K data;
    # growth = min(DPS CAGR 3y, DPS CAGR 5y), and never above base_g (a dividend cannot be modelled
    # growing faster than the business that funds it). If no DPS series -> 0 (honest, not invented).
    # NOTE (v4.2.31): the deterministic DPS series (dps_series) is NOT yet emitted by Growth Enrich —
    # the available sources are EDGAR `dividends_paid` (totals, need /shares to become per-share) and
    # yfinance `dividend_history` (per-share but market-side). Until Growth Enrich emits a
    # split-normalized dps_series, dividend_growth stays 0 with an explicit flag. The formula below
    # is the target and activates the moment the series is wired — no code change needed then.
    _dps = data.get("dps_series") or data.get("dividends_series")
    _dps_g = None
    if _dps:
        _d3 = _rev_cagr(_dps, 3)
        _d5 = _rev_cagr(_dps, 5)
        _dps_cands = [x for x in (_d3, _d5) if isinstance(x, (int, float))]
        if _dps_cands:
            _dps_g = min(_dps_cands)
            if isinstance(_bg, (int, float)):
                _dps_g = min(_dps_g, _bg)   # never above the business growth
    base_inp["dividend_growth"] = _dps_g if isinstance(_dps_g, (int, float)) else 0.0
    if _dps_g is None:
        base_llm_flags.append("dividend_growth=0: deterministic dps_series not yet wired in Growth Enrich")

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
        _gap = (round((iv_f / iv_g - 1) * 100, 1) if (iv_g and iv_f) else None)
        # v4.2.32 mandate (a): a gap this large is a DATA defect, not a business story. MA
        # 2026-07-22 produced gap 595.8% purely from mismatched share denominators, and the memo
        # rationalised it in prose as an "asset-light structural difference". Absurdity checks are
        # NEVER trusted to prose — only to deterministic Python. Above the threshold the FCF leg is
        # marked unreliable and the flag is raised as a DATA-class hard flag.
        GAP_IV_HARD_PCT = 100.0
        _gap_unreliable = _gap is not None and abs(_gap) > GAP_IV_HARD_PCT
        if _gap_unreliable:
            base_llm_flags.append(
                "[DATA] gap_iv_pct %.1f%% > %.0f%% — FCF leg UNRELIABLE (check share denominators)"
                % (_gap, GAP_IV_HARD_PCT))
        dual_basis = {
            "gaap_eps": {"iv": iv_g, "implied_cagr_pct": ic_g,
                         "base_per_share": ivc_base.get("inputs", {}).get("base_per_share")},
            "fcf_per_share": {"iv": iv_f, "implied_cagr_pct": ic_f,
                              "base_per_share": fcfps,
                              "future_multiple": fcf_inp["future_pe"],
                              "gross_dilution_used": fcf_inp["share_dilution_cagr"]},
            "gap_iv_pct": _gap,
            "gap_hard_threshold_pct": GAP_IV_HARD_PCT,
            "fcf_leg_unreliable": _gap_unreliable,
            "shares_used": data.get("shares_used"),
            "conservative_leg": conservative,
            "verdict_leg": conservative,
            "_note": ("GAAP charges SBC in earnings AND in the share count (double count); the FCF "
                      "leg charges it once, via GROSS dilution. A large gap means the verdict is "
                      "really a judgment about SBC, not about the business."),
        }

    # scenarios -> pwfv
    scen_spec = spec.get("scenarios") or {}
    # v4.2.31: scenario weights were the SIXTH LLM driver — pwfv is a weighted mean of the three
    # scenarios, so LLM-chosen weights made pwfv/implied_cagr drift even with a deterministic base
    # (NFLX pair: 25/50/25 vs 30/45/25; MA: 25/50/25 both). Fixed by CONVENTION to the mode/median
    # of observed runs: bear 0.25 / base 0.50 / bull 0.25 (base weighted highest — it is the
    # anchored, most-reliable leg). The LLM's proposed weights are recorded (llm_weights) but do
    # NOT steer pwfv. (Convention values proposed to the architect; mandate ratifies.)
    CONV_W = {"bear": 0.25, "base": 0.50, "bull": 0.25}
    _llm_weights = {}
    scenarios, pwfv, wsum = {}, 0.0, 0.0
    for name in ("bear", "base", "bull"):
        s = scen_spec.get(name, {}) or {}
        _lw = _f(s.get("weight"), None)
        if isinstance(_lw, (int, float)):
            _llm_weights[name] = _lw
        w = CONV_W[name]   # deterministic convention, NOT the LLM weight
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
    #
    # v4.2.4 -- feed the verdict's own implied CAGR into the C block. It was NEVER wired: gps_quant
    # reads gt["implied_cagr_base"], NOTHING in this harness or in any workflow node ever set it, so
    # C's icagr leg read [UNVERIFIED] for every ticker on every run since v4 -- while the number sat
    # in ivc_base ~100 lines above and in the report's own headline verdict table. ivc_lib._sub calls
    # this "the case of record"; v4.2.2 fixed the SYMPTOM (stopped scoring the gap as 0) and left the
    # cause standing, so the block reported an honest "unknown" about a number it already had.
    #
    # WHICH LEG: the conservative one -- the same leg verdict_cap follows (see dual_basis above).
    # Scoring valuation on the optimistic leg while the verdict is set by the pessimistic one would
    # let the scorecard credit exactly what the verdict denies. Falls back to ivc_base when there is
    # no FCF leg to compare against.
    #
    # UNITS: implied_cagr_pct is PERCENT (ivc_lib rounds icagr*100); the gps_quant grid compares
    # against FRACTIONS (0.16/0.14/0.12). Feeding 13.55 where 0.1355 is expected would silently
    # score every ticker a perfect 5. Hence the explicit /100 and the test that pins it.
    _verdict_ic_pct = None
    if dual_basis:
        _verdict_ic_pct = (dual_basis.get(dual_basis["verdict_leg"]) or {}).get("implied_cagr_pct")
    if _verdict_ic_pct is None:
        _verdict_ic_pct = ivc_base.get("implied_cagr_pct")
    _gps_in = dict(data)  # never mutate the caller's payload
    if _verdict_ic_pct is not None:
        _gps_in["implied_cagr_base"] = _verdict_ic_pct / 100.0
    q = gps_quant(_gps_in)
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

    # v4.2.4 -- the QUANT maxima are whatever gps_quant measured, not the nominal 16/15/15/10/10.
    # These five were hardcoded, which threw away the entire v4.2.2 reduced-denominator mechanism at
    # the boundary: gps_quant computed the honest max, stored it in detail[X], and this list
    # overwrote it with the nominal. ivc_lib.gps_quant's own docstring says "Consumers must read
    # max/max_quant from the output, never assume the nominal 16/15/15/10/10" -- this list did
    # exactly the forbidden thing, so a degraded run still printed /100 and Render Tables' own
    # "max reduced from 100" branch was unreachable dead code. The gap was computed, then discarded.
    # Qualitative maxima (runway/moat/forecast/capalloc/sentiment) stay nominal: they are LLM domain,
    # always scoreable, and never reduced.
    def _qmax(key, nominal):
        d = q["detail"].get(key) or {}
        m = d.get("max_quant", d.get("max"))
        return m if isinstance(m, (int, float)) else nominal

    blocks = [
        {"name": "A (growth)", "points": q["A_quant"], "max": _qmax("A", 16), "evidence": q["detail"]["A"]},
        {"name": "A_runway", "points": _qp("A_runway"), "max": 4, "evidence": _qe("A_runway")},
        {"name": "B (profitability)", "points": q["B"], "max": _qmax("B", 15), "evidence": q["detail"]["B"]},
        {"name": "C (valuation)", "points": q["C"], "max": _qmax("C", 15), "evidence": q["detail"]["C"]},
        {"name": "D (balance sheet)", "points": q["D"], "max": _qmax("D", 10), "evidence": q["detail"]["D"]},
        {"name": "E_moat", "points": _qp("E_moat"), "max": 15, "evidence": _qe("E_moat")},
        {"name": "F (momentum)", "points": q["F_quant"], "max": _qmax("F", 10), "evidence": q["detail"]["F"]},
        {"name": "F_forecast_trend", "points": _qp("F_forecast_trend"), "max": 5, "evidence": _qe("F_forecast_trend")},
        {"name": "G_capalloc", "points": _qp("G_capalloc"), "max": 5, "evidence": _qe("G_capalloc")},
        {"name": "H_sentiment", "points": _qp("H_sentiment"), "max": 5, "evidence": _qe("H_sentiment")},
    ]
    for _b in blocks:
        _b["points"] = _f(_b.get("points"), 0)
    gps_total = round(sum(b["points"] for b in blocks), 1)
    # The headline denominator is the sum of what was actually measurable. A GPS that always says
    # /100 cannot distinguish "scored badly" from "could not be scored" -- the whole point of v4.2.2.
    gps_max = round(sum(b["max"] for b in blocks if isinstance(b.get("max"), (int, float))), 1)
    gps = {"blocks": blocks, "total": gps_total, "quant_detail": q["detail"], "max": gps_max,
           "max_nominal": 100,
           "_max_note": (None if gps_max >= 100 else
                         "denominator reduced from 100: sub-blocks with unavailable inputs are "
                         "[UNVERIFIED] and drop out of BOTH numerator and denominator")}

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
    # v4.2.10: analyst coverage. The yahoo `analyst_count` field nulls on cloud IPs, but the
    # Finnhub recommendation split (rec_trends, already in the payload) carries the SAME fact:
    # the number of covering analysts, by rating bucket. NFLX 2026-07-17 shipped a report with
    # analyst_count=null while rec_trends held 29 buy + 16 strongBuy + 13 hold in the same
    # payload — the count existed, only the dead field was read. Fallback + carry the breakdown,
    # with the basis labelled (a house rule: the basis travels with the number).
    _an_count = data.get("analyst_count")
    _an_basis = "yahoo" if _an_count is not None else None
    _rec_breakdown = None
    _rec = data.get("rec_trends") if isinstance(data.get("rec_trends"), dict) else None
    if _rec and isinstance(_rec.get("months"), list) and _rec["months"]:
        _m0 = _rec["months"][0]
        _tot = 0
        _rec_breakdown = {"period": _m0.get("period")}
        for _k in ("strongBuy", "buy", "hold", "sell", "strongSell"):
            _v = _m0.get(_k)
            _rec_breakdown[_k] = _v
            _tot += int(_v or 0)
        _rec_breakdown["total"] = _tot or None
        if _an_count is None and _tot:
            _an_count = _tot
            _an_basis = "finnhub rec_trends (sum of latest-month rating buckets)"
    if pt_mean and price:
        pwfv_vs_street = None
        if pwfv:
            pwfv_vs_street = round((pwfv / pt_mean - 1) * 100, 1)
        street_view = {
            "consensus_target_mean": pt_mean,
            "consensus_target_high": _f(pt.get("high"), _f(data.get("price_target_high"), None)),
            "consensus_target_low": _f(pt.get("low"), _f(data.get("price_target_low"), None)),
            "upside_to_target_pct": round((pt_mean / price - 1) * 100, 1),
            "analyst_count": _an_count,
            "analyst_count_basis": _an_basis,
            "recommendation_breakdown": _rec_breakdown,
            "recommendation_mean": _f(data.get("recommendation_mean"), None),
            "recommendation_key": data.get("recommendation_key"),
            "pwfv_vs_street_pct": pwfv_vs_street,
            "analyst_actions_recent": (data.get("analyst_actions_recent") or [])[:8],
            "_tier": "yahoo consensus; named-bank targets belong to FACT_PACK with source+date",
        }

    # v4.2.34 (mandate HH): THE PUBLICATION LAYER MUST FOLLOW THE VERDICT LEG. Third recurrence of
    # the class "consumer numbers taken from the base leg while the verdict is set by the
    # conservative one" (v4.2.19 Render Tables -> app.py:717 mos_ladder -> trigger bands). Sweep,
    # not a one-line patch: every consumer number below is resolved against the verdict leg once.
    # The assumption "verdict leg == conservative leg" is VERIFIED, not inherited: if the verdict
    # leg's IV reads HIGHER than the other leg, that is flagged and BOTH are printed.
    _vleg_name = (dual_basis or {}).get("verdict_leg")
    _vleg_ivc = None
    if _vleg_name == "fcf_per_share":
        _vleg_ivc = ivc_fcf if isinstance(ivc_fcf, dict) and "error" not in ivc_fcf else None
    elif _vleg_name == "gaap_eps":
        _vleg_ivc = ivc_base
    _pub = _vleg_ivc if isinstance(_vleg_ivc, dict) else ivc_base
    publication_flags = []
    if dual_basis:
        _iv_v = (dual_basis.get(_vleg_name) or {}).get("iv")
        _other = "gaap_eps" if _vleg_name == "fcf_per_share" else "fcf_per_share"
        _iv_o = (dual_basis.get(_other) or {}).get("iv")
        if isinstance(_iv_v, (int, float)) and isinstance(_iv_o, (int, float)) and _iv_v > _iv_o:
            publication_flags.append(
                "[LEG] verdict leg %s IV %.2f is HIGHER than %s IV %.2f — 'verdict leg is the "
                "conservative one' does NOT hold this run; both legs printed" % (_vleg_name, _iv_v, _other, _iv_o))
    # MoS of BOTH legs, explicitly, with the verdict one marked: publishing only the base leg's
    # mos_pct produced a false sustained claim against a memo that had quoted the verdict leg
    # correctly (MA 2026-07-22: memo -48.94% FCF leg vs RESULT -45.74% base leg).
    _mos_by_leg = {}
    if dual_basis:
        for _ln in ("gaap_eps", "fcf_per_share"):
            _ivx = (dual_basis.get(_ln) or {}).get("iv")
            if isinstance(_ivx, (int, float)) and price:
                _mos_by_leg[_ln] = round((_ivx - price) / price * 100, 2)

    return {
        "_FALLBACK": False, "_harness": True,
        "ivc_base": ivc_base,
        "scenarios": scenarios, "pwfv": pwfv,
        "weights": {k: scenarios[k]["weight"] for k in scenarios},
        "bull_bear": bb, "sensitivity": sensitivity,
        "gps": gps, "mos_ladder": _pub.get("mos_ladder"),
        "mos_ladder_leg": _vleg_name or "gaap_eps",
        "mos_pct_by_leg": _mos_by_leg,
        "mos_pct_verdict_leg": _mos_by_leg.get(_vleg_name),
        "fv10_verdict_leg": _pub.get("fv10_per_share"),
        "gates": {"hurdle_gate": _pub.get("hurdle_gate")},
        "verdict_cap": verdict_cap,
        "dual_basis": dual_basis,
        "market_context": market_context,
        "street_view": street_view,
        "self_tests_all": bool(ivc_base.get("self_tests")),
        "flags": (ivc_base.get("flags", []) + pe_flags + growth_flags + pe_anchor_flags + base_llm_flags + publication_flags),
        "pe_cap": {"anchors_available": bool(_anchors), "anchor_used": (round(1.2*max(_anchors),1) if _anchors else NO_ANCHOR_PE), "flags": pe_flags},
        "growth_anchor": {
            "base_growth_used": base_inp.get("growth_rate"),
            "basis": base_inp.get("growth_rate_basis"),
            "rev_cagr_3y": _rc3, "rev_cagr_5y": _rc5,
            "llm_base_g": base_inp.get("llm_base_g"),
            "flags": growth_flags,
        },
        "pe_anchor": {
            "base_future_pe_used": base_inp.get("future_pe"),
            "basis": base_inp.get("future_pe_basis"),
            "pe_median_5y": data.get("pe_median_5y"),
            "pe_median_10y": data.get("pe_median_10y"),
            "llm_base_pe": base_inp.get("llm_base_pe"),
            "flags": pe_anchor_flags,
        },
        "base_determinism": {
            "discount_rate_used": base_inp.get("discount_rate"),
            "terminal_growth_used": base_inp.get("terminal_growth"),
            "dividend_yield_used": base_inp.get("dividend_yield"),
            "dividend_growth_used": base_inp.get("dividend_growth"),
            "fade_used": base_inp.get("fade"),
            "years_used": base_inp.get("years"),
            "scenario_weights_used": {k: scenarios[k]["weight"] for k in scenarios},
            "llm_disc": base_inp.get("llm_disc"),
            "llm_terminal_g": base_inp.get("llm_terminal_g"),
            "llm_weights": _llm_weights,
            "flags": base_llm_flags,
        },
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


@app.route("/cost", methods=["POST"])
def _cost():
    """v4.2.5 token/cost ledger. Body: {"stages":[{stage,provider,model,response,ran}], "today"?}.

    Lives here rather than in a Code node so the price table has ONE home, is covered by the python
    suite, and can be corrected without re-importing the workflow into n8n — prices move, and a
    price edit must not cost a workflow migration.

    Never throws, same contract as /analyze: a billing ledger that 500s would take the whole report
    down over a cosmetic section. On failure it degrades to a named error, not to silence — and
    never to zero.
    """
    b = request.get_json(force=True, silent=True) or {}
    try:
        from pricing import cost_ledger
        import datetime as _d
        t = b.get("today")
        today = _d.date.fromisoformat(t) if isinstance(t, str) and t else None
        return jsonify(cost_ledger(b.get("stages") or [], today)), 200
    except Exception as e:
        return jsonify({"error": "COST_LEDGER_ERROR: " + str(e)[:200], "_FALLBACK": True,
                        "_note": "cost accounting failed; this is NOT a $0 run"}), 200


@app.route("/macro_prices", methods=["POST"])
def _macro_prices():
    """v4.2: risk-free (FRED) + adjusted price series (Tiingo). Moved off the n8n side because
    n8n 2.x Code nodes cannot read env vars (task-runner sandbox), and the alternative was
    inlining the keys into the workflow JSON. Keys are read from THIS service's environment."""
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(macro_prices(b.get("ticker"), b.get("benchmark", "SPY"),
                                b.get("start", "2023-01-01"))), 200


def trigger_prices(result, ticker=None, spec_date=None, spec_version=None):
    """BACKLOG #4 / ARCHITECTURE §3 trigger_prices: 5 transition prices, pure math from RESULT.

    Contract (do not change without the architect): band AVOID->WATCH+ = FV10/1.12^10,
    WATCH+->BUY = FV10/1.16^10, ladder = IV/(1+t) for t=10/20/30%. Ladder prices are NOT
    recomputed here — they are read verbatim from RESULT.mos_ladder, which ivc_lib (pinned math)
    already produced. One home per number; recomputing a pinned figure is how two "identical"
    numbers drift apart.

    Honesty rules, same as everywhere: a missing driver -> that row is absent AND named in
    _errors; never a zero, never a guess. Dividend payers: the §3 band formula ignores the
    dividend FV leg, while ivc_lib's buy_threshold_hurdle includes it — when they diverge >0.5%
    the divergence is SURFACED (band12_vs_hurdle_threshold), not averaged (house rule).
    """
    out = {"ticker": ticker, "derived_from_spec_date": spec_date, "spec_version": spec_version,
           "triggers": [], "_errors": {}}
    if not isinstance(result, dict) or not result:
        out["_errors"]["result"] = "no RESULT payload — nothing to derive triggers from"
        return out
    ivb = result.get("ivc_base") or {}
    # v4.2.34 (mandate HH): the bands must be built from the VERDICT leg's FV10, not the base
    # leg's. RESULT now publishes fv10_verdict_leg; fall back to ivc_base only for older payloads.
    fv10 = result.get("fv10_verdict_leg")
    if not isinstance(fv10, (int, float)) or fv10 <= 0:
        fv10 = ivb.get("fv10_per_share")

    def _row(ttype, price):
        out["triggers"].append({"ticker": ticker, "trigger_type": ttype,
                                "price": round(float(price), 2),
                                "derived_from_spec_date": spec_date,
                                "spec_version": spec_version})

    if isinstance(fv10, (int, float)) and fv10 > 0:
        band12 = fv10 / (1.12 ** 10)
        band16 = fv10 / (1.16 ** 10)
        _row("band_avoid_to_watch", band12)
        _row("band_watch_to_buy", band16)
        bth = ivb.get("buy_threshold_hurdle")
        if isinstance(bth, (int, float)) and bth > 0 and abs(bth - band12) / bth > 0.005:
            # dividend FV leg present in ivc_lib's threshold but absent from the §3 formula
            out["band12_vs_hurdle_threshold"] = {
                "band_formula": round(band12, 2), "ivc_lib_threshold": round(bth, 2),
                "note": "divergence >0.5% — dividend-paying name; the §3 band formula omits the "
                        "dividend FV leg. Both shown; reconcile before alerting on the band."}
    else:
        out["_errors"]["fv10_per_share"] = "missing/non-positive in RESULT.ivc_base — band rows withheld"

    ladder = result.get("mos_ladder") or ivb.get("mos_ladder") or []
    got = set()
    for rung in ladder:
        try:
            t = rung.get("mos_target_pct")
            p = rung.get("buy_threshold_price")
            if t in (10, 10.0, 20, 20.0, 30, 30.0) and isinstance(p, (int, float)) and p > 0:
                _row("ladder_%d" % int(t), p)
                got.add(int(t))
        except Exception:
            continue
    missing = sorted({10, 20, 30} - got)
    if missing:
        out["_errors"]["mos_ladder"] = ("rungs absent from RESULT.mos_ladder: %s — withheld, "
                                        "not recomputed" % missing)
    out["complete"] = (len(out["triggers"]) == 5)
    return out


@app.route("/triggers", methods=["POST"])
def _triggers():
    """BACKLOG #4. Body: {"result": RESULT, "ticker"?, "spec_date"?, "spec_version"?}.
    Same never-throw contract as /analyze: a trigger derivation that 500s would take down a
    caller over pure arithmetic; failure degrades to named errors."""
    b = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(trigger_prices(b.get("result") or {}, b.get("ticker"),
                                      b.get("spec_date"), b.get("spec_version"))), 200
    except Exception as e:
        return jsonify({"error": "TRIGGERS_ERROR: " + str(e)[:200], "_FALLBACK": True,
                        "triggers": [], "complete": False}), 200


# ==============================================================================================
# BACKLOG #5 — REPRICE. A dossier verdict at a NEW price without re-running the LLM chain.
#
# The whole thing is a RESCALING, not a revaluation. In ivc(): IV, FV10, eps_terminal, the
# ladder thresholds (thr = IV/(1+t)) and buy_threshold_hurdle do NOT depend on the current
# price (the dividend legs anchor to the DOLLAR dividend d0 fixed at spec time, which is the
# honest economics: the company pays dollars, not a yield). The only price-dependent outputs
# are implied CAGR, MoS, ladder reached/discount, hurdle_gate and verdict_cap. And implied
# CAGR obeys an exact identity: (fv10+fvdT) = price_old*(1+icagr_old)^Y, therefore
#     icagr_new = (1+icagr_old) * (price_old/price_new)^(1/Y) - 1
# — leg-universal (GAAP, FCF, every scenario), no ivc() call, no drift between two "homes"
# of the same number. Self-test: price_new == price_old must reproduce the stored figures.
#
# FRESHNESS GATES (do not weaken): a rescaled verdict is only honest while the SPEC is honest.
#   1. spec older than 30 days -> refuse (assumptions have a shelf life);
#   2. any 10-K/10-Q/8-K filed AFTER spec_date -> refuse, naming form/date/accession
#      (the fundamentals may have changed; a reprice would launder a stale spec);
#   3. no fresh price obtainable -> refuse (never reprice against a guessed price).
# A refusal is a first-class answer, not an error.
# ==============================================================================================

REPRICE_MAX_SPEC_AGE_DAYS = 30
_REPRICE_FILING_FORMS = ("10-K", "10-Q", "8-K")


def _rescale_icagr_pct(icagr_old_pct, price_old, price_new, years):
    """Exact identity rescale; returns pct or None on missing/garbage inputs."""
    try:
        if not all(isinstance(v, (int, float)) for v in (icagr_old_pct, price_old, price_new)):
            return None
        if price_old <= 0 or price_new <= 0:
            return None
        y = int(years or 10)
        return round(((1 + icagr_old_pct / 100.0)
                      * (price_old / price_new) ** (1.0 / y) - 1) * 100.0, 2)
    except Exception:
        return None


def _rescale_leg(leg, price_old, price_new, years):
    """Rescale a full ivc() output dict IN A COPY. Only price-dependent fields move."""
    if not isinstance(leg, dict) or "implied_cagr_pct" not in leg:
        return leg
    r = json.loads(json.dumps(leg))  # deep copy; never mutate the stored dossier
    ic_new = _rescale_icagr_pct(r.get("implied_cagr_pct"), price_old, price_new, years)
    if ic_new is not None:
        r["implied_cagr_pct"] = ic_new
    iv = r.get("intrinsic_value")
    if isinstance(iv, (int, float)) and price_new > 0:
        r["mos_pct"] = round((iv - price_new) / price_new * 100, 2)
    for rung in (r.get("mos_ladder") or []):
        thr = rung.get("buy_threshold_price")
        if isinstance(thr, (int, float)) and thr > 0:
            rung["reached"] = price_new <= thr
            rung["discount_to_current_pct"] = round((price_new - thr) / price_new * 100, 2)
    if isinstance(r.get("inputs"), dict):
        r["inputs"]["price"] = price_new
        r["inputs"]["price_at_spec"] = price_old
    hurdle = (r.get("inputs") or {}).get("hurdle", 0.12)
    flags = r.get("flags") or []
    if ic_new is not None:
        r["hurdle_gate"] = ("PASS" if (ic_new / 100.0 >= hurdle
                                       and not any("BLOCKING" in str(f) for f in flags))
                            else "FAIL")
    return r


def reprice_result(result, price_new, ticker=None, spec_date=None):
    """Pure rescaling of a stored RESULT to price_new. Never throws; names every gap."""
    out = {"ticker": ticker, "derived_from_spec_date": spec_date, "repriced": False,
           "_errors": {}, "self_tests": {}}
    if not isinstance(result, dict) or not result:
        out["_errors"]["result"] = "no stored RESULT — nothing to reprice"
        return out
    ivb = result.get("ivc_base") or {}
    price_old = (ivb.get("inputs") or {}).get("price")
    years = ivb.get("years", 10)
    if not (isinstance(price_old, (int, float)) and price_old > 0):
        out["_errors"]["price_old"] = "stored RESULT.ivc_base.inputs.price missing/non-positive"
        return out
    if not (isinstance(price_new, (int, float)) and price_new > 0):
        out["_errors"]["price_new"] = "fresh price missing/non-positive — refuse to guess"
        return out
    out.update({"price_at_spec": price_old, "price_new": round(float(price_new), 2),
                "price_change_pct": round((price_new - price_old) / price_old * 100, 2)})

    out["ivc_base"] = _rescale_leg(ivb, price_old, price_new, years)

    db = result.get("dual_basis")
    if isinstance(db, dict):
        db2 = json.loads(json.dumps(db))
        for lk in ("gaap_eps", "fcf_per_share"):
            leg = db2.get(lk)
            if isinstance(leg, dict):
                leg["implied_cagr_pct"] = _rescale_icagr_pct(
                    leg.get("implied_cagr_pct"), price_old, price_new, years)
        legs = [x for x in ((db2.get("gaap_eps") or {}).get("implied_cagr_pct"),
                            (db2.get("fcf_per_share") or {}).get("implied_cagr_pct"))
                if x is not None]
        if legs:
            db2["conservative_leg"] = db2["verdict_leg"] = (
                "gaap_eps" if (db2.get("gaap_eps") or {}).get("implied_cagr_pct") == min(legs)
                else "fcf_per_share")
        out["dual_basis"] = db2

    scen = result.get("scenarios")
    if isinstance(scen, dict):
        out["scenarios"] = {
            name: dict(s, result=_rescale_leg((s or {}).get("result"), price_old,
                                              price_new, years))
            for name, s in scen.items() if isinstance(s, dict)}
    out["pwfv"] = result.get("pwfv")  # probability-weighted FAIR VALUE: price-independent

    # verdict_cap: same three-band rule as analyze(), driven by the conservative leg.
    icb = (out["ivc_base"] or {}).get("implied_cagr_pct")
    if out.get("dual_basis"):
        _legs = [x for x in ((out["dual_basis"].get("gaap_eps") or {}).get("implied_cagr_pct"),
                             (out["dual_basis"].get("fcf_per_share") or {}).get("implied_cagr_pct"))
                 if x is not None]
        if _legs:
            icb = min(_legs)
    out["verdict_cap"] = ("AVOID" if (icb is None or icb < 12.0)
                          else ("WATCH+" if icb < 16.0 else "BUY"))
    out["stored_verdict_cap"] = result.get("verdict_cap")
    out["verdict_cap_changed"] = (out["verdict_cap"] != result.get("verdict_cap")
                                  if result.get("verdict_cap") else None)

    # Self-test: repricing at the OLD price must reproduce the stored figure exactly.
    st = _rescale_icagr_pct(ivb.get("implied_cagr_pct"), price_old, price_old, years)
    out["self_tests"]["identity_at_old_price_ok"] = (
        st is not None and ivb.get("implied_cagr_pct") is not None
        and abs(st - ivb["implied_cagr_pct"]) < 0.01)

    out["triggers"] = trigger_prices(result, ticker=ticker, spec_date=spec_date)
    out["repriced"] = True
    return out


def reprice_freshness(ticker, spec_date, max_age_days=REPRICE_MAX_SPEC_AGE_DAYS):
    """Gates 1+2. Returns {'fresh': bool, 'refusal': {...}|None, '_errors': {...}}."""
    import time as _t
    out = {"fresh": True, "refusal": None, "_errors": {}}

    def _refuse(reason, **kw):
        out["fresh"] = False
        out["refusal"] = dict({"reason": reason}, **kw)
        return out

    try:
        spec_ts = _t.mktime(_t.strptime(str(spec_date)[:10], "%Y-%m-%d"))
    except Exception:
        return _refuse("spec_date_unparseable", spec_date=spec_date,
                       note="cannot verify freshness -> refuse, never assume fresh")
    age_days = (_t.time() - spec_ts) / 86400.0
    if age_days > max_age_days:
        return _refuse("spec_stale_age", age_days=round(age_days, 1),
                       max_age_days=max_age_days,
                       note="assumptions have a shelf life; run a full analysis instead")

    # Gate 2: any 10-K/10-Q/8-K filed AFTER the spec date invalidates the spec.
    try:
        import edgar_facts as _ef
        cik = _ef._resolve_cik(ticker)
        if not cik:
            return _refuse("cik_unresolved", ticker=ticker,
                           note="cannot check EDGAR for newer filings -> refuse")
        subs = _ef._get("https://data.sec.gov/submissions/CIK%s.json" % cik)
        recent = ((subs.get("filings") or {}).get("recent") or {})
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accns = recent.get("accessionNumber") or []
        spec_day = str(spec_date)[:10]
        newer = [{"form": f, "filingDate": d, "accession": a}
                 for f, d, a in zip(forms, dates, accns)
                 if f in _REPRICE_FILING_FORMS and d > spec_day]
        if newer:
            return _refuse("newer_filing_since_spec", filings=newer[:5],
                           note="fundamentals may have changed; a reprice would launder "
                                "a stale spec — run a full analysis")
    except Exception as e:
        return _refuse("edgar_unreachable", error=str(e)[:160],
                       note="cannot PROVE freshness -> refuse (unknown is not fresh)")
    out["spec_age_days"] = round(age_days, 1)
    return out


@app.route("/reprice", methods=["POST"])
def _reprice():
    """BACKLOG #5. Body: {"ticker", "result": stored RESULT, "spec_date"}.
    Never-throw contract. Refusals are 200s with {"repriced": false, "refusal": {...}}."""
    b = request.get_json(force=True, silent=True) or {}
    try:
        ticker = b.get("ticker")
        spec_date = b.get("spec_date")
        fr = reprice_freshness(ticker, spec_date)
        if not fr["fresh"]:
            return jsonify({"ticker": ticker, "repriced": False, "refusal": fr["refusal"],
                            "derived_from_spec_date": spec_date}), 200
        # Gate 3: a fresh price, or nothing. Tiingo adjusted close, latest observation.
        from macro_prices import tiingo_series
        _err = {}
        series = tiingo_series(ticker, _err)
        price_new = series[-1] if series else None
        if not (isinstance(price_new, (int, float)) and price_new > 0):
            return jsonify({"ticker": ticker, "repriced": False,
                            "refusal": {"reason": "no_fresh_price", "errors": _err,
                                        "note": "never reprice against a guessed price"},
                            "derived_from_spec_date": spec_date}), 200
        out = reprice_result(b.get("result") or {}, float(price_new),
                             ticker=ticker, spec_date=spec_date)
        out["spec_age_days"] = fr.get("spec_age_days")
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": "REPRICE_ERROR: " + str(e)[:200], "_FALLBACK": True,
                        "repriced": False}), 200


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
