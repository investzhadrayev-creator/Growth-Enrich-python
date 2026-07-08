# === IVC_LIB v1.0 (Growth Alpha) — PINNED, prepended by Extract Python. LLM must NOT redefine. ===
import json as _json, math as _math

def _med(xs):
    xs = sorted([x for x in xs if x is not None]);
    if not xs: return None
    n = len(xs); return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2.0

def ivc(inp):
    p = inp or {}; price = p.get("price"); eps0 = p.get("eps_normalized")
    fcfps = p.get("levered_fcf_per_share"); g = p.get("growth_rate"); pef = p.get("future_pe")
    hurdle = p.get("hurdle", 0.12); disc = p.get("discount_rate", hurdle)
    dy = p.get("dividend_yield", 0.0); dg = p.get("dividend_growth", 0.0)
    dil = p.get("share_dilution_cagr", 0.0); Y = int(p.get("years", 10))
    fade = bool(p.get("fade", True)); tg = p.get("terminal_growth", 0.04)
    pehm = p.get("pe_hist_median"); pesec = p.get("pe_sector_median")
    if price is None or price <= 0: return {"error": "RUNNER_ERROR: price missing"}
    if g is None or pef is None: return {"error": "RUNNER_ERROR: growth_rate/future_pe missing"}
    # DEFENSE IN DEPTH (v1.3): a |dilution_cagr|>20% is almost always a split artifact, not organic
    # dilution. Compounding it for 10y destroys the valuation (NVDA 10:1 -> /95x terminal EPS).
    # Refuse to silently poison the IVC: neutralize + flag loudly so the arbiter cannot miss it.
    _dil_flag = None
    if dil is not None and abs(dil) > 0.20:
        _dil_flag = "dilution_cagr_%.3f_REJECTED_likely_split_artifact_set_to_0_FIX_UPSTREAM" % dil
        dil = 0.0
    eng, base = "eps", eps0
    if base is None or base <= 0:
        if fcfps and fcfps > 0: eng, base = "levered_fcf_ps", fcfps
        else: return {"error": "RUNNER_ERROR: no positive EPS or FCF/share - Category-F, IVC N/A"}
    flags = []
    if _dil_flag: flags.append(_dil_flag)
    if g > 0.25 and not fade: flags.append("growth_gt_25pct_unfaded_FORCED_FADE"); fade = True
    if g > 0.40: flags.append("growth_gt_40pct_BLOCKING_justify_or_cut")
    caps = [v for v in [pehm, 1.5*pesec if pesec else None] if v]
    pecap = min(caps) if caps else None
    if pecap and pef > pecap: flags.append("future_pe_above_cap_%.1f_MAJOR" % pecap)
    e = base; path = [base]
    for y in range(1, Y+1):
        gy = g if (not fade or y <= 5) else g + (tg-g)*(y-5)/(Y-5)
        e *= (1+gy); path.append(e)
    epsT = path[-1]; epsTd = epsT/((1+dil)**Y) if dil else epsT
    fv10 = epsTd*pef
    d0 = price*dy
    pvd = sum(d0*((1+dg)**(y-1))/((1+disc)**y) for y in range(1, Y+1))
    fvdT = sum(d0*((1+dg)**(y-1))*((1+disc)**(Y-y)) for y in range(1, Y+1))
    iv = fv10/((1+disc)**Y) + pvd
    icagr = ((fv10+fvdT)/price)**(1.0/Y) - 1
    ivh = fv10/((1+hurdle)**Y) + (pvd if abs(disc-hurdle) < 1e-9 else
          sum(d0*((1+dg)**(y-1))/((1+hurdle)**y) for y in range(1, Y+1)))
    mos = (iv-price)/price*100
    ladder = []
    for t in p.get("mos_targets", [0.10, 0.20, 0.30]):
        thr = iv/(1+t); mthr = (iv-thr)/thr
        icthr = ((fv10 + (fvdT if dy else 0.0))/thr)**(1.0/Y) - 1
        ladder.append({"mos_target_pct": round(t*100,1), "buy_threshold_price": round(thr,2),
                       "discount_to_current_pct": round((price-thr)/price*100,2),
                       "implied_cagr_at_threshold_pct": round(icthr*100,2),
                       "reached": price <= thr, "selftest_mos_at_threshold_ok": abs(mthr-t) < 0.001})
    st = {}
    ivchk = base
    for y in range(1, Y+1):
        gy = g if (not fade or y <= 5) else g + (tg-g)*(y-5)/(Y-5)
        ivchk *= (1+gy)
    ivchk = (ivchk/((1+dil)**Y))*pef/((1+disc)**Y) + pvd
    st["iv_recompute_ok"] = abs(iv-ivchk) < 0.01
    if dy == 0:
        st["hurdle_identity_ok"] = abs(((fv10/ivh)**(1.0/Y)-1) - hurdle) < 0.001
        if abs(disc-hurdle) < 1e-9:
            st["mos_cagr_sign_identity_ok"] = ((mos > 0) == (icagr > hurdle)) or abs(icagr-hurdle) < 1e-6
    else:
        st["hurdle_identity_ok"] = "skipped_dividend_case"
    st["pe_cap_checked"] = pecap is not None
    gate = "PASS" if (icagr >= hurdle and not any("BLOCKING" in f for f in flags)) else "FAIL"
    return {"engine": eng, "years": Y,
            "inputs": {"price": price, "base_per_share": base, "g": g, "fade": fade, "terminal_g": tg,
                       "future_pe": pef, "hurdle": hurdle, "discount_rate": disc,
                       "dilution_cagr": dil, "div_yield": dy},
            "eps_terminal": round(epsT,4), "eps_terminal_dilution_adj": round(epsTd,4),
            "fv10_per_share": round(fv10,2), "intrinsic_value": round(iv,2),
            "implied_cagr_pct": round(icagr*100,2), "buy_threshold_hurdle": round(ivh,2),
            "mos_pct": round(mos,2), "pe_cap_effective": pecap, "flags": flags,
            "mos_ladder": ladder, "self_tests": st, "hurdle_gate": gate}

def ivc_delta(inp, overrides, label=""):
    b = ivc(inp)
    if "error" in b: return b
    m = dict(inp or {}); m.update(overrides or {}); a = ivc(m)
    if "error" in a: return a
    return {"label": label, "overrides": overrides, "iv_base": b["intrinsic_value"],
            "iv_alt": a["intrinsic_value"], "delta_iv": round(a["intrinsic_value"]-b["intrinsic_value"],2),
            "delta_iv_pct": round((a["intrinsic_value"]/b["intrinsic_value"]-1)*100,2),
            "delta_implied_cagr_pp": round(a["implied_cagr_pct"]-b["implied_cagr_pct"],2)}

def bull_bear_table(inp, arguments):
    rows, s = [], 0.0
    for a in arguments or []:
        d = ivc_delta(inp, a.get("overrides", {}), a.get("label", ""))
        if "error" in d: rows.append({"label": a.get("label"), "error": d["error"]}); continue
        pr = float(a.get("probability", 0.5)); ex = round(pr*d["delta_iv"], 2); s += ex
        rows.append({"side": a.get("side"), "label": a.get("label"), "probability": pr,
                     "delta_iv": d["delta_iv"], "delta_iv_pct": d["delta_iv_pct"],
                     "delta_implied_cagr_pp": d["delta_implied_cagr_pp"], "expected_impact": ex})
    rows.sort(key=lambda r: -abs(r.get("expected_impact", 0)))
    bull = round(sum(r.get("expected_impact",0) for r in rows if r.get("side") == "BULL"), 2)
    bear = round(sum(r.get("expected_impact",0) for r in rows if r.get("side") == "BEAR"), 2)
    return {"rows": rows, "sum_expected_impact": round(s,2), "bull_total": bull,
            "bear_total": bear, "net_skew": round(bull+bear,2)}

def _cagr(series, yrs):
    v = [x.get("val") for x in (series or []) if x and x.get("val") is not None]
    if len(v) < yrs+1: yrs = len(v)-1
    if yrs < 2 or v[-yrs-1] is None or v[-yrs-1] <= 0 or v[-1] is None or v[-1] <= 0: return None
    return (v[-1]/v[-yrs-1])**(1.0/yrs) - 1

def gps_quant(gt):
    """Deterministic GPS sub-scores (pinned scales, spec 2.1-2.6). gt = enriched payload.
    Qualitative blocks (runway 0-4, moat 0-15, capalloc 0-5, sentiment 0-5) are LLM domain."""
    out = {"detail": {}}
    def grid_growth(c):
        if c is None: return 0
        c *= 100
        return 0 if c < 8 else 2 if c < 12 else 4 if c < 20 else 6 if c <= 30 else 5
    rc5, rc3 = _cagr(gt.get("revenue"), 5), _cagr(gt.get("revenue"), 3)
    ec5 = gt.get("eps_cagr_5y"); ec5 = ec5 if ec5 is not None else _cagr(gt.get("eps_series_obj"), 5)
    a1, a2 = grid_growth(rc5), grid_growth(ec5)
    dur = 4 if (rc3 is not None and rc5 is not None and rc3 >= rc5) else \
          2 if (rc3 is not None and rc5 is not None and (rc5-rc3) <= 0.05) else 0
    out["A_quant"] = a1+a2+dur
    out["detail"]["A"] = {"rev_cagr5": rc5, "eps_cagr5": ec5, "rev_cagr3": rc3,
                          "pts": {"rev": a1, "eps": a2, "durability": dur}, "max_quant": 16}
    peg = gt.get("peg"); fpe_rel = gt.get("fwd_pe_vs_sector") or gt.get("fwd_pe_vs_peer"); ic = gt.get("implied_cagr_base")
    c1 = 5 if (peg is not None and peg < 1) else 4 if (peg is not None and peg < 1.5) else \
         2 if (peg is not None and peg < 2) else 0
    c2 = 0 if fpe_rel is None else (5 if fpe_rel < 0.8 else 3 if fpe_rel < 1.2 else 1 if fpe_rel < 2 else 0)
    c3 = 0 if ic is None else (5 if ic >= 0.16 else 4 if ic >= 0.14 else 2 if ic >= 0.12 else 0)
    out["C"] = c1+c2+c3
    out["detail"]["C"] = {"peg": peg, "fwd_pe_vs_sector": fpe_rel, "implied_cagr": ic,
                          "pts": {"peg": c1, "fwd_pe": c2, "icagr": c3}, "max": 15}
    de = gt.get("debt_to_equity"); dilc = gt.get("dilution_cagr"); sbcr = gt.get("sbc_to_revenue")
    d1 = 0 if de is None else (4 if de < 0.5 else 3 if de < 1.0 else 1 if de < 1.5 else 0)
    d2 = 0 if dilc is None else (4 if dilc <= -0.01 else 3 if abs(dilc) < 0.01 else 1 if dilc <= 0.05 else 0)
    d3 = 0 if sbcr is None else (2 if sbcr < 0.03 else 1 if sbcr <= 0.08 else 0)
    out["D"] = d1+d2+d3
    out["detail"]["D"] = {"de": de, "dilution_cagr": dilc, "sbc_rev": sbcr,
                          "pts": {"de": d1, "shares": d2, "sbc": d3}, "max": 10}
    erb = gt.get("erb_90d"); rs = gt.get("rel_strength_6m")
    f1 = 0 if erb is None else (6 if erb > 0.3 else 4 if erb > 0.1 else 2 if erb > -0.1 else 0)
    f2 = 0 if rs is None else (4 if rs > 0.10 else 3 if rs > 0 else 1 if rs > -0.10 else 0)
    out["F_quant"] = f1+f2
    out["detail"]["F"] = {"erb_90d": erb, "rel_strength_6m": rs,
                          "pts": {"erb": f1, "rel_strength": f2}, "max_quant": 10}
    # --- B block (Profitability & Returns) — DETERMINISTIC, pinned, sourced (v1.5).
    # Moves ROE / margin-trend / FCF-conversion out of LLM wiring (v3.22 provenance discipline).
    roe = gt.get("roe"); de = gt.get("debt_to_equity")
    # leverage-inflated ROE not rewarded: discount score if D/E>1.5
    b1 = 0
    if roe is not None:
        b1 = 5 if roe > 0.30 else 4 if roe > 0.20 else 3 if roe > 0.12 else 1 if roe > 0 else 0
        if de is not None and de > 1.5: b1 = max(0, b1 - 2)
    # margin trend from op-margin series (expansion/flat/contraction), same trend logic as v3.20 Fix C
    om = gt.get("op_margin_series") or []
    om = [x for x in om if isinstance(x, (int, float))]
    b2 = 0
    if len(om) >= 3:
        if om[-1] > om[0] and om[-1] >= om[len(om)//2]: b2 = 5          # expanding
        elif abs(om[-1] - om[0]) <= 0.02: b2 = 3                        # plateau
        else: b2 = 0                                                     # contracting
    # FCF conversion = levered_fcf / net_income (latest)
    fcfc = gt.get("fcf_conversion")
    b3 = 0 if fcfc is None else (5 if fcfc >= 0.9 else 3 if fcfc >= 0.6 else 1 if fcfc > 0 else 0)
    out["B"] = b1 + b2 + b3
    out["detail"]["B"] = {"roe": roe, "op_margin_series": om, "fcf_conversion": fcfc,
                          "de_haircut_applied": (de is not None and de > 1.5),
                          "pts": {"roe": b1, "margin_trend": b2, "fcf_conv": b3}, "max": 15,
                          "source": "deterministic from payload.roe / op_margin_series / fcf_conversion (GROUND_TRUTH)"}
    return out
# === END IVC_LIB ===
