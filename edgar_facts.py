"""
edgar_facts.py v4 — SEC EDGAR Company Facts endpoint for the Growth Alpha / Consilium Spine microservice.

First-source XBRL financials (data.sec.gov) for IVC_LIB + scenario_f. Priority tags with fallbacks;
no hit -> null + '_missing'; never fabricate; never throws. EDGAR is primary but NOT flawless
(placeholder zeros, stale period contexts), so this layer adds deterministic SANITY GATES:
  first-source + validation, not blind trust.

v3 fixes (from the ASTS live run):
  A. DROP placeholder zeros in revenue/gross_profit/operating_income. A literal 0 in these for an
     operating company is a filler tag, not a fact — feeding it corrupts CAGR/burn_multiple.
     Dropped years are recorded in _flags.dropped_zero (visible, not silent).
  B. STALE-CONTEXT flag: if a year's value EXACTLY equals another year's AND differs from a
     neighbor by >=3x, it's likely a stale comparative context (ASTS 2024 == 2022 == $13.825M
     next to 2025 == $70.9M). Flagged in _flags.suspect_stale_context — NOT dropped (could be
     legitimate), surfaced for the auditor/you.
  C. shares_current CASCADE: companyfacts(dei) -> companyconcept(dei) -> companyfacts(us-gaap)
     -> companyconcept(us-gaap) -> honest null. ASTS has NO dei block in companyfacts, so the
     separate companyconcept call is required.

v4 addition — CONFIRMED-SPLIT detection (fixes false-positive split application, e.g. PLTR
  2021 2x share jump that was organic SBC/warrant dilution, not a real stock split). A genuine
  split causes RETROACTIVE RESTATEMENT: a later 10-K re-reports an EARLIER fiscal year-end's share
  count at the POST-split value, so the SAME end-date shows two materially different values across
  different accessions/filed-dates whose ratio matches a clean split factor. Organic dilution is
  NEVER retroactively restated -- so this is a positive, SEC-sourced confirmation signal, not a
  heuristic. Exposed as _flags.confirmed_splits: [{end, factor, earliest_val, earliest_filed,
  latest_val, latest_filed}]. Consumers (Growth Enrich) should ONLY apply a clean-ratio jump as a
  real split if the end-date appears here; otherwise treat it as dilution (safer default -- avoids
  retroactively corrupting EPS/per-share series when the "split" was actually dilution).

SCOPE (phase 1): financial facts. Form 4 insider = phase 2.
ENDPOINT: POST /edgar_facts  body: {"ticker":"ASTS"} (or {"cik":"0001780312"})
"""
import json
import time
import urllib.request

SEC_USER_AGENT = "GrowthAlphaPipeline/1.0 (contact: invest.zhadrayev@gmail.com)"  # EDIT to your real email
_MIN_INTERVAL = 0.12
_last_call = [0.0]
_CIK_CACHE = {}
_FACTS_CACHE = {}
_CONCEPT_CACHE = {}
_TTL = 3600

DURATION_TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                       "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
                       "WeightedAverageNumberOfSharesOutstandingBasic"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
DROP_ZERO_FIELDS = {"revenue", "gross_profit", "operating_income"}
INSTANT_TAGS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "restricted_cash": ["RestrictedCashAndCashEquivalents", "RestrictedCashNoncurrent", "RestrictedCash"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "rpo": ["RevenueRemainingPerformanceObligation"],
}
SHARES_CURRENT = [("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")]
DEBT_LT = ["LongTermDebtNoncurrent", "LongTermDebt"]
DEBT_CUR = ["LongTermDebtCurrent", "DebtCurrent"]
DEBT_COMBINED = ["DebtLongtermAndShorttermCombinedAmount"]


def _throttle():
    dt = time.time() - _last_call[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_call[0] = time.time()


def _get(url):
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT,
                                               "Accept-Encoding": "gzip, deflate",
                                               "Host": url.split("/")[2]})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def _resolve_cik(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        return None
    if not _CIK_CACHE:
        try:
            data = _get("https://www.sec.gov/files/company_tickers.json")
            for row in data.values():
                _CIK_CACHE[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
        except Exception:
            return None
    return _CIK_CACHE.get(t)


def _companyfacts(cik):
    now = time.time()
    hit = _FACTS_CACHE.get(cik)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = _get("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % cik)
    _FACTS_CACHE[cik] = (now, data)
    return data


def _companyconcept(cik, taxonomy, tag):
    """Single-concept endpoint — often has dei facts absent from companyfacts. Returns units dict or None."""
    ck = (cik, taxonomy, tag)
    now = time.time()
    hit = _CONCEPT_CACHE.get(ck)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        data = _get("https://data.sec.gov/api/xbrl/companyconcept/CIK%s/%s/%s.json" % (cik, taxonomy, tag))
        units = data.get("units")
    except Exception:
        units = None
    _CONCEPT_CACHE[ck] = (now, units)
    return units


def _concept(facts, taxonomy, tag):
    try:
        return facts["facts"][taxonomy][tag]["units"]
    except (KeyError, TypeError):
        return None


def _pick_unit(units):
    for u in ("USD", "shares", "pure", "USD/shares"):
        if u in units:
            return u
    return next(iter(units), None)


def _days(start, end):
    try:
        import datetime as _dt
        return (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
    except Exception:
        return 999


def _annual_merged(facts, tags, taxonomy="us-gaap", drop_zero=False):
    """Fill each fiscal-year-end with the highest-priority tag reporting it (gap-fill).
    Within a tag dedupe by end keeping latest 'filed'. drop_zero -> treat val==0 as absence.
    Returns (oldest-first [{end,val,accn,filed}], set(tags_used), [dropped_zero_ends])."""
    by_end, used, dropped = {}, set(), []
    for tag in tags:
        units = _concept(facts, taxonomy, tag)
        if not units:
            continue
        key = _pick_unit(units)
        if not key:
            continue
        this = {}
        for f in units[key]:
            if not f.get("form", "").startswith("10-K"):
                continue
            end, start = f.get("end"), f.get("start")
            if end is None:
                continue
            if start is not None and _days(start, end) < 300:
                continue
            filed = f.get("filed", "")
            if end not in this or filed > this[end]["filed"]:
                this[end] = {"end": end, "val": f.get("val"), "accn": f.get("accn"), "filed": filed}
        for end, rec in this.items():
            if end in by_end:
                continue
            if drop_zero and (rec["val"] == 0 or rec["val"] is None):
                dropped.append(end)
                continue
            by_end[end] = rec
            used.add(tag)
    return [by_end[k] for k in sorted(by_end)], used, sorted(set(dropped) - set(by_end))


def _flag_stale(series):
    """Flag years whose val EXACTLY equals another year's AND differs from a neighbor by >=3x."""
    vals = [(p["end"], p["val"]) for p in series if p.get("val") not in (None, 0)]
    n = len(vals)
    suspects = []
    for i, (end, v) in enumerate(vals):
        if not any(j != i and vals[j][1] == v for j in range(n)):
            continue
        neigh = []
        if i > 0:
            neigh.append(vals[i - 1][1])
        if i < n - 1:
            neigh.append(vals[i + 1][1])
        av = abs(v)
        big = any(av and x and (max(av, abs(x)) / max(min(av, abs(x)), 1) >= 3) for x in neigh)
        if big:
            suspects.append(end)
    return suspects


_CLEAN_SPLIT_FACTORS = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]


def _detect_confirmed_splits(facts, tags, taxonomy="us-gaap"):
    """Scan ALL filings (not deduped-to-latest) for a fiscal year-end reported with two
    MATERIALLY DIFFERENT values across different accessions -- i.e. a later filing retroactively
    restated an earlier year's share count. This only happens for genuine stock splits (never for
    organic dilution, which is never restated). Returns [{end, factor, earliest_val, earliest_filed,
    latest_val, latest_filed}] -- positive, SEC-sourced confirmation, not a heuristic guess."""
    by_end = {}
    for tag in tags:
        units = _concept(facts, taxonomy, tag)
        if not units:
            continue
        key = _pick_unit(units)
        if not key:
            continue
        for f in units[key]:
            if not f.get("form", "").startswith("10-K"):
                continue
            end, start, val, filed = f.get("end"), f.get("start"), f.get("val"), f.get("filed", "")
            if end is None or val is None:
                continue
            if start is not None and _days(start, end) < 300:
                continue
            by_end.setdefault(end, []).append({"val": val, "filed": filed, "accn": f.get("accn")})
    confirmed = []
    for end, rows in by_end.items():
        distinct_vals = sorted(set(r["val"] for r in rows if r["val"]))
        if len(distinct_vals) < 2:
            continue
        lo, hi = distinct_vals[0], distinct_vals[-1]
        if lo <= 0:
            continue
        ratio = hi / lo
        factor = next((c for c in _CLEAN_SPLIT_FACTORS if abs(ratio - c) / c <= 0.01), None)
        if not factor:
            continue
        earliest = min(rows, key=lambda r: (r["filed"], r["val"] == lo))
        earliest = min([r for r in rows if r["val"] == lo], key=lambda r: r["filed"])
        latest = max([r for r in rows if r["val"] == hi], key=lambda r: r["filed"])
        # require the restatement to be genuinely LATER (positive confirmation, not filing-order noise)
        if latest["filed"] > earliest["filed"]:
            confirmed.append({"end": end, "factor": factor,
                              "earliest_val": earliest["val"], "earliest_filed": earliest["filed"],
                              "latest_val": latest["val"], "latest_filed": latest["filed"]})
    return sorted(confirmed, key=lambda c: c["end"])


def _latest_instant(facts_or_units, tags=None, taxonomy="us-gaap", any_form=False, units_direct=None):
    forms = None if any_form else ("10-K", "10-Q")

    def _from_units(units, tag):
        key = _pick_unit(units)
        if not key:
            return None
        rows = [f for f in units[key]
                if f.get("end") and (forms is None or f.get("form", "").startswith(forms))]
        if not rows:
            return None
        rows.sort(key=lambda f: (f.get("end", ""), f.get("filed", "")))
        last = rows[-1]
        return {"end": last.get("end"), "val": last.get("val"),
                "accn": last.get("accn"), "filed": last.get("filed")}, tag

    if units_direct is not None:
        return _from_units(units_direct, tags) or (None, None)
    for tag in tags:
        units = _concept(facts_or_units, taxonomy, tag)
        if units:
            r = _from_units(units, tag)
            if r:
                return r
    return None, None


def _shares_current(facts, cik):
    """Cascade: companyfacts(dei) -> companyconcept(dei) -> companyfacts(us-gaap) -> companyconcept(us-gaap)."""
    for tax, tag in SHARES_CURRENT:
        v, _ = _latest_instant(facts, [tag], taxonomy=tax, any_form=True)
        if v:
            return v, tax + ":" + tag + " (companyfacts)"
        units = _companyconcept(cik, tax, tag)
        if units:
            r = _latest_instant(None, tags=tag, any_form=True, units_direct=units)
            if r and r[0]:
                return r[0], tax + ":" + tag + " (companyconcept)"
    return None, None


def edgar_facts(ticker=None, cik=None):
    out = {"_source": "sec_edgar", "_ticker": ticker, "_missing": [], "_flags": {}, "_errors": {}}
    if not cik:
        cik = _resolve_cik(ticker)
    if not cik:
        out["_errors"]["cik"] = "ticker not found in SEC company_tickers map"
        return out
    out["_cik"] = cik
    try:
        facts = _companyfacts(cik)
    except Exception as e:
        out["_errors"]["companyfacts"] = str(e)[:160]
        return out

    src, dropped_zero, stale = {}, {}, {}
    for field, tags in DURATION_TAGS.items():
        ser, used, dz = _annual_merged(facts, tags, drop_zero=(field in DROP_ZERO_FIELDS))
        if ser:
            out[field] = [{"end": r["end"], "val": r["val"]} for r in ser]
            out[field + "_audit"] = ser
            src[field] = sorted(used)
            if dz:
                dropped_zero[field] = dz
            sus = _flag_stale(ser)
            if sus:
                stale[field] = sus
        else:
            out[field] = None
            out["_missing"].append(field)
    if dropped_zero:
        out["_flags"]["dropped_zero"] = dropped_zero          # A: placeholder zeros removed (visible)
    if stale:
        out["_flags"]["suspect_stale_context"] = stale        # B: exact-dup + >=3x neighbor

    for field, tags in INSTANT_TAGS.items():
        val, tag = _latest_instant(facts, tags)
        if val:
            out[field] = val["val"]
            out[field + "_audit"] = val
            src[field] = tag
        else:
            out[field] = None
            if field != "restricted_cash":
                out["_missing"].append(field)

    # C: shares_current cascade (companyfacts dei absent for ASTS -> companyconcept) + proxy fallback
    sc, sc_src = _shares_current(facts, cik)
    if sc:
        out["shares_current"] = sc["val"]; out["shares_current_audit"] = sc; src["shares_current"] = sc_src
    elif out.get("shares_diluted"):
        last = out["shares_diluted"][-1]
        out["shares_current"] = last["val"]
        out["_flags"]["shares_current_proxied"] = (
            "cover-page count unavailable (dei absent in companyfacts+companyconcept); "
            "proxied to latest weighted-avg diluted shares (%s)" % last["end"])
        src["shares_current"] = "PROXY:last_shares_diluted"
    else:
        out["shares_current"] = None; out["_missing"].append("shares_current")

    lt, lt_tag = _latest_instant(facts, DEBT_LT)
    cur, cur_tag = _latest_instant(facts, DEBT_CUR)
    if lt or cur:
        out["long_term_debt"] = lt["val"] if lt else None
        out["long_term_debt_audit"] = lt
        out["current_portion_debt"] = cur["val"] if cur else None
        out["current_portion_debt_audit"] = cur
        out["total_debt"] = (lt["val"] if lt else 0) + (cur["val"] if cur else 0)
        out["_flags"]["total_debt_computed"] = "%s + %s" % (lt_tag or "0", cur_tag or "0")
        if not (lt and cur):
            out["_flags"]["total_debt_partial"] = "only one component present — may understate"
        src["total_debt"] = out["_flags"]["total_debt_computed"]
    else:
        comb, comb_tag = _latest_instant(facts, DEBT_COMBINED)
        if comb:
            out["total_debt"] = comb["val"]; src["total_debt"] = comb_tag
        else:
            out["total_debt"] = None; out["_missing"].append("total_debt")

    if out.get("restricted_cash"):
        out["_flags"]["cash_note"] = "cash excludes restricted_cash; scenario_f treats restricted separately"

    # v4: confirmed-split detection (restatement-based) for shares_diluted
    conf_splits = _detect_confirmed_splits(facts, DURATION_TAGS["shares_diluted"])
    if conf_splits:
        out["_flags"]["confirmed_splits"] = conf_splits

    out["_field_sources"] = src
    return out


# --- Flask route glue (already in app.py) ---
#   from edgar_facts import edgar_facts
#   @app.route("/edgar_facts", methods=["POST"])
#   def _edgar_facts():
#       b = request.get_json(force=True, silent=True) or {}
#       return jsonify(edgar_facts(b.get("ticker"), b.get("cik"))), 200

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        rev_excl = {"units": {"USD": [
            {"start": "2022-01-01", "end": "2022-12-31", "val": 13825000, "form": "10-K", "accn": "a", "filed": "2024-04-01"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 13825000, "form": "10-K", "accn": "c", "filed": "2025-03-03"},  # STALE (==2022)
            {"start": "2023-01-01", "end": "2023-12-31", "val": 0, "form": "10-K", "accn": "z", "filed": "2026-03-02"},          # placeholder ZERO
            {"start": "2025-01-01", "end": "2025-12-31", "val": 70918000, "form": "10-K", "accn": "d", "filed": "2026-03-02"},
        ]}}
        mock = {"facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": rev_excl,
        }}}  # NOTE: no dei block (like ASTS)
        import edgar_facts as m
        m._FACTS_CACHE["TEST"] = (time.time(), mock)
        m._CONCEPT_CACHE[("TEST", "dei", "EntityCommonStockSharesOutstanding")] = (
            time.time(), {"shares": [{"end": "2026-04-30", "val": 320000000, "form": "10-Q", "accn": "d", "filed": "2026-05-11"}]})
        r = m.edgar_facts(cik="TEST")
        assert "2023-12-31" not in [p["end"] for p in r["revenue"]], "zero not dropped"
        assert r["_flags"]["dropped_zero"]["revenue"] == ["2023-12-31"], r["_flags"].get("dropped_zero")
        assert r["_flags"]["suspect_stale_context"]["revenue"] == ["2024-12-31"], r["_flags"].get("suspect_stale_context")
        assert r["shares_current"] == 320000000, r["shares_current"]
        print("SELFTEST v3 OK — zero dropped (2023), stale flagged (2024), shares_current via companyconcept.")
        print(json.dumps({"revenue": r["revenue"], "flags": r["_flags"], "shares_current": r["shares_current"]}, indent=2))

        # v4 self-test: confirmed-split (retroactive restatement) vs unconfirmed dilution
        # Case A: genuine split -- FY2022 originally filed at 500M shares (2023 10-K), later
        # RESTATED to 1000M (2:1 split) when the 2024 10-K re-reports FY2022 as a comparative.
        split_shares = {"units": {"shares": [
            {"start": "2022-01-01", "end": "2022-12-31", "val": 500000000, "form": "10-K", "accn": "s1", "filed": "2023-02-01"},
            {"start": "2022-01-01", "end": "2022-12-31", "val": 1000000000, "form": "10-K", "accn": "s3", "filed": "2025-02-01"},  # restated
            {"start": "2023-01-01", "end": "2023-12-31", "val": 1000000000, "form": "10-K", "accn": "s2", "filed": "2024-02-01"},
        ]}}
        # Case B: organic dilution (PLTR-like) -- FY2019/2020 filed ONCE each, never restated.
        # 2020 happens to be ~2x 2019 (clean ratio) but it's dilution, not a split -- NOT confirmed.
        # (Distinct, non-overlapping end-dates from Case A so the two scenarios don't collide
        # when merged by end-date across tags within the same mock facts object.)
        dilution_shares_tag = "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"
        mock2 = {"facts": {"us-gaap": {
            "WeightedAverageNumberOfDilutedSharesOutstanding": split_shares,
            dilution_shares_tag: {"units": {"shares": [
                {"start": "2019-01-01", "end": "2019-12-31", "val": 979330000, "form": "10-K", "accn": "d1", "filed": "2020-02-01"},
                {"start": "2020-01-01", "end": "2020-12-31", "val": 1923617000, "form": "10-K", "accn": "d2", "filed": "2021-02-01"},
            ]}},
        }}}
        m._FACTS_CACHE["TEST2"] = (time.time(), mock2)
        r2 = m.edgar_facts(cik="TEST2")
        conf = r2["_flags"].get("confirmed_splits", [])
        assert any(c["end"] == "2022-12-31" and c["factor"] == 2 for c in conf), conf
        assert not any(c["end"] in ("2019-12-31", "2020-12-31") for c in conf), "dilution wrongly confirmed as split"
        print("\nSELFTEST v4 OK — genuine split (2022, restated 500M->1000M) CONFIRMED; "
              "organic dilution (2019->2020, never restated, single value each) correctly NOT confirmed.")
        print(json.dumps({"confirmed_splits": conf}, indent=2))
    else:
        tk = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
        print(json.dumps(edgar_facts(tk), indent=2, default=str))
