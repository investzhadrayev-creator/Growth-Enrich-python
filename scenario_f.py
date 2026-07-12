"""
scenario_f.py — Category-F (pre-profit / early-commercial) deterministic ANCHOR computer.

Powers the Core-V `/scenario_tree` route of the Growth Alpha / Consilium Spine microservice.
Faithful port of the Master pipeline's "Prompts F" methodology to deterministic Python.

DESIGN (matches pipeline discipline, per Prompts F line 33):
  - This layer computes ONLY the deterministic ANCHORS: survival (burn/runway),
    dilution, unit economics, market context, and the reverse-anchor + sector sanity.
  - Scenario PROBABILITIES and TAM are the LLM memo's job (Core-V Narrative), NOT here.
    We provide a scenario SKELETON scaffold (labels + anchors) for the memo to fill,
    exactly like the deterministic radar skeleton in the Growth contour.
  - A standard DCF is INVALID for Category F — value is a venture option / path-to-FCF.
  - Never throws. Every section is isolated; on failure a section returns null + a
    '_warnings' entry, so STDOUT is never empty and one crash never discards the rest.

INPUT (payload dict) — annual series are lists of {"end","val"}, oldest-first:
  revenue, gross_profit, operating_income, net_income, ocf, capex, sbc, shares_diluted
  instants: cash, short_term_investments, total_debt, rpo, shares_current
  price:    current_price (or price_data.current_price.adjClose)
  sector hint (optional): sector / business_model  ('hardware' | 'software' | ...)
  overrides (optional): mature_ev_sales, per_name_cap_pct, book_cap_pct

ENDPOINT:
  POST /scenario_tree   body: {"data": {...eligibility payload...}}
  returns: JSON dict of Category-F anchors (all nullable).
"""
import math

VENTURE_DISCOUNT_RATE = 0.30          # Prompts F: hurdle for scenario valuation, NOT WACC
PRE_REVENUE_THRESHOLD = 50e6          # latest revenue < $50M -> pre_revenue sub-mode
DEFAULT_PER_NAME_CAP_PCT = 2.0        # hard position cap per name
DEFAULT_BOOK_CAP_PCT = 10.0           # hard cap on total Core-V book


# ---------- helpers (null-safe) ----------
def _series_vals(s):
    """Return list of numeric vals from a [{'end','val'}] series, oldest-first, nulls dropped-in-place."""
    if not isinstance(s, list):
        return []
    out = []
    for x in s:
        if isinstance(x, dict):
            out.append(x.get("val"))
        else:
            out.append(x)
    return out


def _latest(s):
    vals = [v for v in _series_vals(s) if v is not None]
    return vals[-1] if vals else None


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _get(payload, *keys):
    """First present key among aliases."""
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return None


def _cagr(vals, years=None):
    v = [x for x in vals if x is not None]
    if len(v) < 2:
        return None
    n = (len(v) - 1) if years is None else min(years, len(v) - 1)
    first, last = v[-n - 1], v[-1]
    if first is None or last is None or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / n) - 1


def _sector_band(sector_hint):
    """Mature EV/Sales band by business model (Prompts F sector sanity)."""
    h = (sector_hint or "").lower()
    hardware_kw = ("hardware", "aerospace", "defense", "defence", "industrial", "auto",
                   "semi", "launch", "space", "satellite", "capital")
    software_kw = ("software", "saas", "marketplace", "internet", "platform", "fintech")
    if any(k in h for k in hardware_kw):
        return {"sector_band": "1.5x-3.0x", "assumed": 2.25, "kind": "capital_intensive_hardware",
                "comparables_named": ["LMT ~1.9x", "NOC ~2.3x"]}
    if any(k in h for k in software_kw):
        return {"sector_band": "5x-10x", "assumed": 6.0, "kind": "software_marketplace_light",
                "comparables_named": []}
    # unknown -> CONSERVATIVE hardware band (Core-V is mostly space/hardware; bias conservative)
    return {"sector_band": "1.5x-3.0x (default: sector unknown, conservative)", "assumed": 2.25,
            "kind": "unknown_defaulted_conservative", "comparables_named": []}


def scenario_tree(payload):
    if not isinstance(payload, dict):
        return {"error": "RUNNER_ERROR: payload not a dict", "engine": "category_f"}
    R = {"engine": "category_f", "_warnings": []}
    W = R["_warnings"]

    def section(name, fn):
        try:
            fn()
        except Exception as e:
            W.append("section_failed:%s:%s" % (name, repr(e)[:120]))

    # ---- price / shares ----
    price = _num(_get(payload, "current_price"))
    if price is None:
        pd = payload.get("price_data") or {}
        cp = pd.get("current_price") if isinstance(pd, dict) else None
        price = _num(cp.get("adjClose")) if isinstance(cp, dict) else _num(cp)
    shares_cur = _num(_get(payload, "shares_current", "shares_diluted_current"))

    # ---- 1) SUB-MODE ----
    rev_series = _get(payload, "revenue", "revenue_series")
    latest_rev = _num(_latest(rev_series))
    R["latest_revenue"] = latest_rev
    R["sub_mode"] = "pre_revenue" if (latest_rev is None or latest_rev < PRE_REVENUE_THRESHOLD) else "early_commercial"

    # ---- 2) SURVIVAL ----
    def _survival():
        cash = _num(_get(payload, "cash"))
        sti = _num(_get(payload, "short_term_investments", "st_investments"))
        debt = _num(_get(payload, "total_debt")) or 0.0
        liquidity = (cash or 0.0) + (sti or 0.0)
        ocf = _num(_latest(_get(payload, "ocf", "operating_cash_flow")))
        capex = _num(_latest(_get(payload, "capex", "capital_expenditure")))
        sbc = _num(_latest(_get(payload, "sbc", "stock_comp"))) or 0.0
        annual_burn = None
        fcf_burn = None
        if ocf is not None and capex is not None:
            annual_burn = ocf - capex
            fcf_burn = ocf - capex - sbc
        runway_y = runway_q = None
        self_funding = False
        if annual_burn is not None:
            if annual_burn < 0:
                runway_y = round(liquidity / abs(annual_burn), 2) if liquidity else 0.0
                runway_q = round(runway_y * 4, 1) if runway_y is not None else None
            else:
                self_funding = True
        R["survival"] = {
            "liquidity": round(liquidity, 2), "annual_burn": _r(annual_burn), "fcf_burn": _r(fcf_burn),
            "cash_runway_years": runway_y, "cash_runway_quarters": runway_q,
            "self_funding": self_funding, "net_cash": round(liquidity - debt, 2),
        }
        if cash is None and sti is None:
            W.append("survival:liquidity_inputs_missing")
        if ocf is None or capex is None:
            W.append("survival:burn_inputs_missing(ocf/capex)")
    section("survival", _survival)

    # ---- 3) DILUTION ----
    def _dilution():
        sh = _series_vals(_get(payload, "shares_diluted", "shares_diluted_series"))
        sh = [x for x in sh if x is not None]
        dc = _cagr(sh)
        R["dilution"] = {
            "shares_earliest": sh[0] if sh else None, "shares_latest": sh[-1] if sh else None,
            "dilution_cagr": _r(dc, 4),
            "flag_high_dilution": (dc is not None and dc > 0.10),
            "flag_serial_dilution": (dc is not None and dc > 0.25),
        }
        if not sh:
            W.append("dilution:shares_diluted_series_missing")
    section("dilution", _dilution)

    # ---- 4) UNIT ECONOMICS (early_commercial) ----
    def _unit_econ():
        gp = _series_vals(_get(payload, "gross_profit", "gross_profit_series"))
        rv = _series_vals(rev_series)
        gm = []
        for g, r in zip(gp, rv):
            gm.append(round(g / r, 4) if (g is not None and r not in (None, 0)) else None)
        gm_clean = [x for x in gm if x is not None]
        trend = None
        if len(gm_clean) >= 2:
            trend = "improving" if gm_clean[-1] > gm_clean[0] else \
                    "flat" if abs(gm_clean[-1] - gm_clean[0]) <= 0.02 else "declining"
        rev_cagr = _cagr([x for x in rv if x is not None])
        d_rev = None
        rvv = [x for x in rv if x is not None]
        if len(rvv) >= 2:
            d_rev = rvv[-1] - rvv[-2]
        fcf_burn = (R.get("survival") or {}).get("fcf_burn")
        burn_mult = None
        if fcf_burn is not None and d_rev is not None:
            burn_mult = round(abs(fcf_burn) / max(d_rev, 1.0), 2)
        R["unit_economics"] = {
            "gross_margin_latest": gm_clean[-1] if gm_clean else None,
            "gross_margin_trend": trend, "revenue_cagr": _r(rev_cagr, 4),
            "delta_revenue_latest_year": _r(d_rev), "burn_multiple": burn_mult,
            "flag_burn_multiple_gt_1_5": (burn_mult is not None and burn_mult > 1.5),
        }
    section("unit_economics", _unit_econ)

    # ---- 5) MARKET CONTEXT ----
    def _market():
        debt = _num(_get(payload, "total_debt")) or 0.0
        liquidity = (R.get("survival") or {}).get("liquidity") or 0.0
        mcap = round(price * shares_cur, 2) if (price is not None and shares_cur) else None
        if mcap is None:
            W.append("market:market_cap_null_shares_or_price_missing_do_not_fabricate")
        ev = round(mcap + debt - liquidity, 2) if mcap is not None else None
        R["market_context"] = {
            "market_cap": mcap, "ev": ev,
            "ev_to_sales": round(ev / latest_rev, 2) if (ev is not None and latest_rev) else None,
        }
    section("market_context", _market)

    # ---- 6) BACKLOG ----
    def _backlog():
        rpo = _num(_get(payload, "rpo", "remaining_performance_obligations"))
        R["backlog"] = {"rpo": rpo,
                        "rpo_coverage_years": round(rpo / latest_rev, 2) if (rpo is not None and latest_rev) else None}
    section("backlog", _backlog)

    # ---- 7) REVERSE ANCHOR + sector sanity + gate ----
    def _reverse():
        band = _sector_band(_get(payload, "sector", "business_model"))
        assumed = _num(_get(payload, "mature_ev_sales")) or band["assumed"]
        ev = (R.get("market_context") or {}).get("ev")
        rev_cagr = (R.get("unit_economics") or {}).get("revenue_cagr")
        implied_fwd_rev = round(ev / assumed, 2) if (ev is not None and assumed) else None
        years_req = None
        if (implied_fwd_rev is not None and latest_rev not in (None, 0) and
                rev_cagr not in (None, 0) and (1 + rev_cagr) > 0 and implied_fwd_rev > 0):
            try:
                years_req = round(math.log(implied_fwd_rev / latest_rev) / math.log(1 + rev_cagr), 1)
            except (ValueError, ZeroDivisionError):
                years_req = None
        implied_mult = round(implied_fwd_rev / latest_rev, 2) if (implied_fwd_rev and latest_rev) else None

        # sector plausibility self-check (Prompts F): >3x for capital-intensive hardware = self-error
        plausible = True
        if band["kind"].startswith("capital_intensive") and assumed > 3.0:
            plausible = False
            W.append("reverse:mature_ev_sales_%.1f_TOO_HIGH_for_hardware_recompute_2.0-2.5x" % assumed)
        R["mature_multiple_check"] = {"assumed": assumed, "sector_band": band["sector_band"],
                                      "kind": band["kind"], "comparables_named": band["comparables_named"],
                                      "plausible": plausible}

        # REVERSE-ANCHOR GATE decision rule (Prompts F)
        dil = (R.get("dilution") or {}).get("dilution_cagr")
        burn_mult = (R.get("unit_economics") or {}).get("burn_multiple")
        cond_years = (years_req is not None and years_req > 5 and (rev_cagr or 0) > 0.40)
        cond_mult = (implied_mult is not None and implied_mult > 10)
        net_cash = (R.get("survival") or {}).get("net_cash")
        cond_triple = (burn_mult is not None and burn_mult > 1.5 and
                       dil is not None and dil > 0.25 and
                       net_cash is not None and net_cash < 0)
        no_mos = bool(cond_years or cond_mult or cond_triple)
        R["reverse_anchor_gate"] = {
            "years_required": years_req, "implied_fwd_rev_multiple": implied_mult,
            "implied_forward_revenue": implied_fwd_rev, "burn_multiple": burn_mult, "dilution_cagr": dil,
            "no_margin_of_safety": no_mos,
            "verdict_basis": ("AVOID/SELL — priced above any defensible value (reverse-anchor unachievable)"
                              if no_mos else
                              "speculative slot defensible ONLY if survival strong AND reverse-anchor "
                              "achievable AND asymmetric upside — memo must state all three explicitly"),
        }
    section("reverse_anchor", _reverse)

    # ---- SCENARIO SKELETON (scaffold only; probabilities + TAM are the memo's job) ----
    def _skeleton():
        R["venture_discount_rate"] = VENTURE_DISCOUNT_RATE
        R["scenario_skeleton"] = [
            {"id": "SB", "path": "bull", "anchor": "TAM captured / milestones hit / self-funding reached",
             "probability": None, "tam_input": None, "_note": "memo assigns probability + TAM"},
            {"id": "SN", "path": "base", "anchor": "partial execution / one more raise / slower ramp",
             "probability": None, "tam_input": None, "_note": "memo assigns probability + TAM"},
            {"id": "SR", "path": "bear", "anchor": "milestone slip / dilution or down-round / runway wall",
             "probability": None, "tam_input": None, "_note": "memo assigns probability + TAM"},
        ]
    section("scenario_skeleton", _skeleton)

    # ---- POSITION CAP guardrail (hard, deterministic) ----
    per_name = _num(_get(payload, "per_name_cap_pct")) or DEFAULT_PER_NAME_CAP_PCT
    book = _num(_get(payload, "book_cap_pct")) or DEFAULT_BOOK_CAP_PCT
    R["position_cap"] = {"per_name_cap_pct": per_name, "book_cap_pct": book,
                         "_note": "hard cap: pre-profit names sized <=%.1f%% each, <=%.1f%% of book total"
                                  % (per_name, book)}
    return R


def _r(x, nd=2):
    return round(x, nd) if isinstance(x, (int, float)) else x


# --- Flask route glue (add to app.py, same pattern as enrich_yf) ---
#   from scenario_f import scenario_tree
#   @app.route("/scenario_tree", methods=["POST"])
#   def _scenario_tree():
#       body = request.get_json(force=True, silent=True) or {}
#       return jsonify(scenario_tree(body.get("data", body))), 200

if __name__ == "__main__":
    import json as _j
    demo = {
        "current_price": 42.0, "shares_current": 300e6,
        "revenue": [{"end": "2023", "val": 55e6}, {"end": "2024", "val": 120e6}, {"end": "2025", "val": 260e6}],
        "gross_profit": [{"end": "2023", "val": 20e6}, {"end": "2024", "val": 50e6}, {"end": "2025", "val": 120e6}],
        "ocf": [{"end": "2025", "val": -180e6}], "capex": [{"end": "2025", "val": 120e6}],
        "sbc": [{"end": "2025", "val": 60e6}],
        "shares_diluted": [{"end": "2023", "val": 200e6}, {"end": "2025", "val": 300e6}],
        "cash": 400e6, "short_term_investments": 150e6, "total_debt": 200e6,
        "sector": "space launch hardware",
    }
    print(_j.dumps(scenario_tree(demo), indent=2, default=str))
