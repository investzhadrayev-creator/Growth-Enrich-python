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
import os
import time
import urllib.request

# SEC fair-access rule: the User-Agent must identify a real person with a real contact email,
# or the SEC will eventually throttle or block the IP. Set SEC_USER_AGENT in the Railway
# service variables (growth-enrich -> Variables) so it survives every redeploy instead of
# being hand-edited into this file. The fallback is deliberately obvious garbage: if it shows
# up in a log, the variable is not set and SEC calls are going out unidentified.
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "ConsiliumSpine/1.0 (SEC_USER_AGENT NOT SET - contact unknown)")
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
# --- Debt concepts, kept semantically distinct (v4.2.23, BACKLOG #5) --------------------------
# US-GAAP identity: LongTermDebt = LongTermDebtNoncurrent + LongTermDebtCurrent.
#   DEBT_FULL_LT   : the whole long-term debt INCLUDING its current maturities (one authoritative
#                    figure — preferred when present, needs no summing).
#   DEBT_NONCURRENT: long-term debt EXCLUDING the current portion (a PART, never total on its own).
#   DEBT_CUR       : the current maturities of long-term debt (the other PART).
# The pre-v4.2.23 list ["LongTermDebtNoncurrent","LongTermDebt"] took the FIRST with data —
# LongTermDebtNoncurrent — and treated that partial as total. For NFLX that printed $11.83B
# (D/E 0.44) while the full LongTermDebt of $21.86B (D/E 0.82) sat unread in the same facts.
# total_debt is now DEFINED as the full long-term debt including the current portion:
#   priority 1: DEBT_FULL_LT directly (already complete);
#   priority 2: DEBT_NONCURRENT + DEBT_CUR (reassemble from the two parts).
# Short-term borrowings (ShortTermBorrowings: revolver / commercial paper) are DELIBERATELY
# EXCLUDED: the mandate's leverage view is the long-term capital structure, not an operating
# credit line, and mixing them would inflate D/E inconsistently with how the hurdle treats
# leverage. Netflix carries effectively none, so this is immaterial here and principled elsewhere.
DEBT_FULL_LT = ["LongTermDebt"]
DEBT_NONCURRENT = ["LongTermDebtNoncurrent"]
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


def _merge_units(a, b):
    """Union two units dicts, de-duped on the identity of a reported fact.

    WHY: `companyfacts` can carry ONE value per period where `companyconcept` carries the full
    filing history -- the same asymmetry already documented for dei/shares_current (note C).
    That is fatal here specifically: a split confirmation IS the fact "one period reported at two
    different values by two different filings". A source that keeps only the latest value per
    period can never confirm a split, by construction -- it has already discarded the evidence.
    NFLX 2026-07-16 proved it: the 10:1 split (SEC 8-K, effective 2025-11-17) went unconfirmed,
    so a real split was booked as 56%/yr dilution, EPS CAGR read -36.7% instead of +36%, and the
    growth block scored 0/6 for it.
    """
    out = {}
    for src in (a or {}), (b or {}):
        for unit, rows in src.items():
            bucket = out.setdefault(unit, {})
            for f in rows or []:
                bucket[(f.get("accn"), f.get("start"), f.get("end"), f.get("val"))] = f
    return {u: list(v.values()) for u, v in out.items()} or None


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
    # v4.2.45 (mandate BBB): collect EVERY tag's coverage FIRST, then prefer a SINGLE-TAG series.
    # The MA defect was not the priority ORDER — it was that ADJACENT YEARS came from DIFFERENT
    # tags: MA filed both `Revenues` (net) and `RevenueFromContract…` (gross) for 2018-2021, the
    # gross tag won by priority, and from 2022 only the net tag remained → a phantom -25.5% step at
    # the seam and a 5y CAGR of 6.78% instead of 16.47%. When one tag spans the whole range, using
    # it alone removes the question entirely; conflict resolution is only needed when none does.
    per_tag, tag_conflicts = {}, []
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
                # v4.2.47: carry FULL provenance — a jump means nothing without knowing whether
                # the tag, the unit, the form or the filing changed underneath it.
                this[end] = {"end": end, "val": f.get("val"), "accn": f.get("accn"), "filed": filed,
                             "unit": key, "form": f.get("form")}
        if drop_zero:
            for end in [e for e, r in this.items() if r["val"] == 0 or r["val"] is None]:
                dropped.append(end)
                del this[end]
        if this:
            per_tag[tag] = this
    # conflict detection: same fiscal year reported by 2+ tags with materially different values
    all_ends = sorted({e for t in per_tag.values() for e in t})
    for end in all_ends:
        vals = {tag: t[end]["val"] for tag, t in per_tag.items() if end in t and t[end]["val"]}
        if len(vals) > 1:
            lo, hi = min(vals.values()), max(vals.values())
            if hi > 0 and (hi - lo) / hi > 0.05:
                tag_conflicts.append({"end": end, "values": vals, "spread_pct": round((hi - lo) / hi * 100, 1)})
    # MAIN RULE: a single tag covering the full observed range wins outright (no stitching).
    full = [tag for tag in tags if tag in per_tag and len(per_tag[tag]) == len(all_ends)]
    if full:
        chosen = full[0]
        by_end = dict(per_tag[chosen])
        used.add(chosen)
    else:
        for tag in tags:
            for end, rec in (per_tag.get(tag) or {}).items():
                if end in by_end:
                    continue
                by_end[end] = rec
                used.add(tag)
    year_tag = {}
    for tag, t in per_tag.items():
        for end, rec in t.items():
            if by_end.get(end) is rec:
                year_tag[end] = tag
    return ([dict(by_end[k], tag=year_tag.get(k)) for k in sorted(by_end)], used,
            sorted(set(dropped) - set(by_end)), tag_conflicts)


def _provenance_of(rec):
    """v4.2.47: the provenance tuple of a series point. A jump is only a DEFECT when something in
    HERE changed underneath it — the tag boundary is just one member of this set."""
    return (rec.get("tag"), rec.get("unit"), rec.get("form"), rec.get("accn"))


def _ratio_continuity(num_series, den_series, tol=0.30):
    """v4.2.47 (mandate BBB/EEE, sharpened): a jump is a DEFECT only when it coincides with a
    change of PROVENANCE — not when it is merely large.

    History of this function is the argument for its shape. First version keyed on magnitude alone:
    disproved on live data, because MA's real tag-switch moved the operating margin 63% while
    NFLX moved it 88% (2012 expansion) and 277% (2013 recovery) on a SINGLE tag — a real business
    out-jumps a data defect, so no threshold separates them. Second version keyed on the tag
    boundary: correct but too narrow, since the same step is produced by a unit change (thousands
    vs millions), a form change, a restatement under a new accession, or a hole in the series.

    So: DEFECT = jump whose endpoints differ in ANY provenance field (tag, unit, form, accession)
    or that straddles a missing fiscal year. Everything else is a BUSINESS EVENT — not noise:
    a 277% margin step means the CAGR window spans a structural break and the growth measured
    across it is less representative. Both lists are returned; the caller prints the second as
    context to the growth anchor.

    Returns {"defects": [...], "business_events": [...]}.
    """
    num = {r["end"]: r["val"] for r in (num_series or []) if r.get("val")}
    den = {r["end"]: r for r in (den_series or []) if r.get("val")}
    ends = sorted(set(num) & set(den))
    defects, events, prev = [], [], None
    for end in ends:
        d = den[end]
        if not d["val"]:
            continue
        ratio = num[end] / d["val"]
        if prev is not None and prev["ratio"] > 0 and ratio > 0:
            jump = abs(ratio - prev["ratio"]) / prev["ratio"]
            if jump > tol:
                cur_p, prev_p = _provenance_of(d), prev["prov"]
                changed = [f for f, a, b in zip(("tag", "unit", "form", "accession"), prev_p, cur_p)
                           if a is not None and b is not None and a != b]
                # a skipped fiscal year is itself a provenance break: the step spans unseen data
                try:
                    gap = int(end[:4]) - int(prev["end"][:4]) > 1
                except Exception:
                    gap = False
                if gap:
                    changed.append("year_gap")
                rec = {"end": end, "prev_end": prev["end"],
                       "ratio": round(ratio, 4), "prev_ratio": round(prev["ratio"], 4),
                       "jump_pct": round(jump * 100, 1),
                       "tag": d.get("tag"), "prev_tag": prev["prov"][0],
                       "provenance_changed": changed}
                (defects if changed else events).append(rec)
        prev = {"end": end, "ratio": ratio, "prov": _provenance_of(d)}
    return {"defects": defects, "business_events": events}


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


def _detect_confirmed_splits(facts, tags, taxonomy="us-gaap", cik=None):
    """Scan ALL filings (not deduped-to-latest) for a fiscal year-end reported with two
    MATERIALLY DIFFERENT values across different accessions -- i.e. a later filing retroactively
    restated an earlier year's share count. This only happens for genuine stock splits (never for
    organic dilution, which is never restated). Returns [{end, factor, earliest_val, earliest_filed,
    latest_val, latest_filed, tag}] -- positive, SEC-sourced confirmation, not a heuristic guess.

    PER-TAG, deliberately (v4.2.3). A restatement is within-tag evidence: the same tag, the same
    period, two values. Until v4.2.3 all tags pooled into one bucket per end-date, so for a company
    reporting BOTH basic and diluted weighted averages (live NFLX does; every fixture used one tag)
    lo = basic pre-split and hi = diluted post-split, and the ratio absorbed the basic/diluted
    dilution wedge: a clean 10.00x read as ~10.17x, missing the 1% tolerance. The split stayed
    unconfirmed on live data while the suite was green -- deterministically, on every run."""
    confirmed_by_end = {}
    for tag in tags:
        units = _concept(facts, taxonomy, tag)
        # companyfacts alone is not sufficient -- see _merge_units. Ask companyconcept for the
        # same tag and union the two; the restatement lives in whichever one kept it.
        if cik:
            try:
                units = _merge_units(units, _companyconcept(cik, taxonomy, tag))
            except Exception:
                pass
        if not units:
            continue
        key = _pick_unit(units)
        if not key:
            continue
        by_end = {}  # THIS tag only -- never pool values across tags (see docstring)
        for f in units[key]:
            if not f.get("form", "").startswith("10-K"):
                continue
            end, start, val, filed = f.get("end"), f.get("start"), f.get("val"), f.get("filed", "")
            if end is None or val is None:
                continue
            if start is not None and _days(start, end) < 300:
                continue
            by_end.setdefault(end, []).append({"val": val, "filed": filed, "accn": f.get("accn")})
        for end, rows in by_end.items():
            if end in confirmed_by_end:
                continue  # tags are priority-ordered; the first tag to confirm an end wins
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
            earliest = min([r for r in rows if r["val"] == lo], key=lambda r: r["filed"])
            latest = max([r for r in rows if r["val"] == hi], key=lambda r: r["filed"])
            # require the restatement to be genuinely LATER (positive confirmation, not filing-order noise)
            if latest["filed"] > earliest["filed"]:
                confirmed_by_end[end] = {"end": end, "factor": factor,
                                         "earliest_val": earliest["val"], "earliest_filed": earliest["filed"],
                                         "latest_val": latest["val"], "latest_filed": latest["filed"],
                                         "tag": tag}
    return sorted(confirmed_by_end.values(), key=lambda c: c["end"])


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


def raw_tags(ticker=None, cik=None, tags=None, taxonomy="us-gaap"):
    """DIAGNOSTIC, read-only: raw EDGAR facts per tag — (tag, fiscal year, value, accession, filed).

    Exists because the assembled series hides WHICH tag filled WHICH year: `_annual_merged` fills
    each fiscal year with the FIRST tag in priority order that reports it, and only the SET of tags
    used survives into `sources`. That is how MA's revenue series came to be stitched from three
    tags (RevenueFromContractWithCustomerExcludingAssessedTax / Revenues / SalesRevenueNet) with a
    ~1.6x step at the 2018 and 2022 boundaries, detectable only indirectly through the margin
    series. This endpoint returns the un-merged truth so the year->tag->value map can be read
    directly. No LLM, no cost, no mutation — it only reads what edgar_facts already fetches.
    """
    import datetime as _dt
    tags = [t for t in (tags or []) if isinstance(t, str)] or list(DURATION_TAGS.get("revenue", []))
    out = {"ticker": ticker, "cik": cik, "taxonomy": taxonomy, "tags": {}, "_errors": []}
    try:
        if not cik:
            cik = _resolve_cik(ticker)
        out["cik"] = cik
        if not cik:
            out["_errors"].append("cik_not_resolved")
            return out
        facts = _companyfacts(cik) or {}
        blocks = ((facts.get("facts") or {}).get(taxonomy) or {})
        for tag in tags:
            rows = []
            units = ((blocks.get(tag) or {}).get("units") or {})
            for unit, arr in units.items():
                for f in (arr or []):
                    # annual duration facts only: 10-K, period >= 300 days
                    if f.get("form") != "10-K" or not f.get("start") or not f.get("end"):
                        continue
                    try:
                        if (_dt.date.fromisoformat(f["end"]) - _dt.date.fromisoformat(f["start"])).days < 300:
                            continue
                    except Exception:
                        continue
                    rows.append({"fy_end": f.get("end"), "value": f.get("val"), "unit": unit,
                                 "accession": f.get("accn"), "filed": f.get("filed"),
                                 "fy": f.get("fy"), "fp": f.get("fp")})
            # newest filing wins per fiscal year end, but keep every year
            by_end = {}
            for r in sorted(rows, key=lambda x: (x["fy_end"], x["filed"] or "")):
                by_end[r["fy_end"]] = r
            out["tags"][tag] = [by_end[k] for k in sorted(by_end)]
        # convenience: which tags cover which year, and where coverage switches
        cover = {}
        for tag, rows in out["tags"].items():
            for r in rows:
                cover.setdefault(r["fy_end"][:4], []).append(tag)
        out["year_to_tags"] = {y: sorted(set(v)) for y, v in sorted(cover.items())}
    except Exception as e:
        out["_errors"].append("raw_tags: %s" % str(e)[:200])
    return out


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
    tag_conflict_map, mixed_tag_map = {}, {}
    for field, tags in DURATION_TAGS.items():
        ser, used, dz, conflicts = _annual_merged(facts, tags, drop_zero=(field in DROP_ZERO_FIELDS))
        if ser:
            out[field] = [{"end": r["end"], "val": r["val"]} for r in ser]
            out[field + "_audit"] = ser          # v4.2.45: each point now carries its source `tag`
            src[field] = sorted(used)
            # v4.2.45 (mandate BBB): a series stitched from >1 tag is the defect class itself —
            # surface it by name, with the year->tag map, instead of leaving it to be inferred.
            if len(used) > 1:
                mixed_tag_map[field] = {r["end"][:4]: r.get("tag") for r in ser}
            if conflicts:
                tag_conflict_map[field] = conflicts
            if dz:
                dropped_zero[field] = dz
            sus = _flag_stale(ser)
            if sus:
                stale[field] = sus
        else:
            out[field] = None
            out["_missing"].append(field)
    # v4.2.45: continuity check on the paired ratio — the mechanism that actually caught MA.
    cont = _ratio_continuity(out.get("operating_income"), out.get("revenue_audit"))
    if cont["defects"]:
        out["_flags"]["series_discontinuity_provenance"] = cont["defects"]
    if cont["business_events"]:
        # v4.2.47 (mandate): business events are NOT noise — a structural margin step inside the
        # CAGR window means the growth measured across it is less representative. Mark which
        # windows each event falls into so the report can print it as context to the growth anchor.
        _rev = out.get("revenue") or []
        _last = int(_rev[-1]["end"][:4]) if _rev else None
        for _e in cont["business_events"]:
            try:
                _y = int(_e["end"][:4])
                _e["in_cagr_window_3y"] = bool(_last and _y > _last - 3)
                _e["in_cagr_window_5y"] = bool(_last and _y > _last - 5)
            except Exception:
                _e["in_cagr_window_3y"] = _e["in_cagr_window_5y"] = None
        out["_flags"]["margin_step_business_event"] = cont["business_events"]
    if mixed_tag_map:
        out["_flags"]["series_tag_mixed"] = mixed_tag_map
    if tag_conflict_map:
        out["_flags"]["tag_conflict"] = tag_conflict_map
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

    full, full_tag = _latest_instant(facts, DEBT_FULL_LT)
    nonc, nonc_tag = _latest_instant(facts, DEBT_NONCURRENT)
    cur, cur_tag = _latest_instant(facts, DEBT_CUR)
    # expose the parts for audit regardless of which path sets total_debt
    out["long_term_debt"] = full["val"] if full else (nonc["val"] if nonc else None)
    out["long_term_debt_audit"] = full or nonc
    out["current_portion_debt"] = cur["val"] if cur else None
    out["current_portion_debt_audit"] = cur
    if full:
        # priority 1: the authoritative full figure already includes current maturities.
        out["total_debt"] = full["val"]
        out["_flags"]["total_debt_computed"] = full_tag
        src["total_debt"] = full_tag
        # If a standalone noncurrent tag also exists and is MATERIALLY smaller, record the divergence
        # so a downstream reader is never silently handed one when the other was meant. This is the
        # exact NFLX case: LongTermDebt 21.86B vs LongTermDebtNoncurrent 11.83B.
        if nonc and full["val"] and abs(full["val"] - nonc["val"]) / full["val"] > 0.05:
            out["_flags"]["long_term_debt_full_vs_noncurrent"] = (
                "using full LongTermDebt %s; noncurrent-only %s would understate" % (full_tag, nonc_tag))
    elif nonc or cur:        # priority 2: no single full figure — reassemble from the two parts.
        out["total_debt"] = (nonc["val"] if nonc else 0) + (cur["val"] if cur else 0)
        out["_flags"]["total_debt_computed"] = "%s + %s" % (nonc_tag or "0", cur_tag or "0")
        if not (nonc and cur):
            out["_flags"]["total_debt_partial"] = (
                "no full LongTermDebt tag; assembled from one component only — may understate")
        src["total_debt"] = out["_flags"]["total_debt_computed"]
    else:
        comb, comb_tag = _latest_instant(facts, DEBT_COMBINED)
        if comb:
            out["total_debt"] = comb["val"]; src["total_debt"] = comb_tag
        else:
            out["total_debt"] = None; out["_missing"].append("total_debt")

    # ------------------------------------------------------------------------------------------
    # v4.2.32 mandate (b): DEBT RECONCILIATION GATE. Not a third patch on tag priority — a gate.
    # MA 2026-07-22: the LongTermDebt tag returned $21M while the assembled components said $19.0B
    # (a 99.9% divergence). The chosen figure went straight into D/E = 0.0027 and the GPS D-block
    # awarded a full 10/10 "leverage negligible" against a real D/E of ~2.46. Rules:
    #   (1) if the CHOSEN total_debt diverges from the components sum by >20%, the COMPONENTS WIN
    #       — always. A single tag never outvotes the assembled parts.
    #   (2) if there are no components and the available sources diverge by >20%, take the
    #       CONSERVATIVE (larger) figure and raise debt_uncertain.
    #   (3) debt_uncertain forbids a full-mark D-block downstream (gps_quant honours the flag) —
    #       "zero instead of unknown" must not buy a perfect score.
    RECON_TOL = 0.20
    DOUBLE_COUNT_X = 2.0
    _chosen = out.get("total_debt")
    _comp_sum = None
    # v4.2.33 mandate (1): expose the ACTUAL tags behind every component, so a reader can audit the
    # composition instead of trusting a label. §3 v1.5 semantics: total_debt is the FULL long-term
    # debt INCLUDING current maturities; a components sum is only equivalent when BOTH parts exist.
    out["debt_components_tags"] = {
        "full_long_term_debt": full_tag if full else None,
        "noncurrent": nonc_tag if nonc else None,
        "current_maturities": cur_tag if cur else None,
        "components_complete": bool(nonc and cur),
    }
    if nonc or cur:
        _comp_sum = (nonc["val"] if nonc else 0) + (cur["val"] if cur else 0)
        if not (nonc and cur):
            out["_flags"]["debt_components_incomplete"] = (
                "components sum built from %s only — INCOMPLETE by construction, not comparable "
                "to a full LongTermDebt figure" % ("noncurrent" if nonc else "current"))
    if isinstance(_chosen, (int, float)) and _chosen > 0 and isinstance(_comp_sum, (int, float)) and _comp_sum > 0:
        _rel = abs(_chosen - _comp_sum) / max(_chosen, _comp_sum)
        # v4.2.33 mandate (2): a components sum more than DOUBLE the chosen figure is more likely a
        # DOUBLE COUNT (the same debt appearing in two tags) than a real under-report. Flag the
        # composition as suspect rather than silently adopting an inflated number.
        if _comp_sum > _chosen * DOUBLE_COUNT_X:
            out["_flags"]["debt_components_suspect"] = (
                "components sum %.0f exceeds chosen %.0f by >%.0fx — composition suspect "
                "(possible double count across tags)" % (_comp_sum, _chosen, DOUBLE_COUNT_X))
        if _rel > RECON_TOL and _comp_sum > _chosen:
            # (1) components win when they diverge >20% AND read HIGHER. The "higher wins" rule is
            # the same conservatism the mandate sets in (2), and it is required for correctness:
            # DEBT_FULL_LT already INCLUDES current maturities, so a components sum that is SMALLER
            # is simply the noncurrent part (NFLX: full 21.86B vs noncurrent-only 11.83B) and must
            # NOT displace the fuller figure — that was the v4.2.23 defect. A components sum that is
            # LARGER means the tag under-reports (MA: tag $21M vs components $19.0B) — components win.
            out["total_debt"] = _comp_sum
            out["_flags"]["total_debt_reconciled"] = (
                "chosen %s=%.0f diverged %.1f%% BELOW components sum %.0f -> COMPONENTS WIN (gate v4.2.32)"
                % (src.get("total_debt", "?"), _chosen, _rel * 100, _comp_sum))
            out["_flags"]["total_debt_divergence"] = True
            out["_flags"]["debt_uncertain"] = (
                "tag and components disagree by %.1f%% — leverage reading disputed" % (_rel * 100))
            src["total_debt"] = "components_sum(reconciled)"
        elif _rel > RECON_TOL:
            # divergence exists but the chosen (fuller) figure reads higher — keep it, still flag.
            out["_flags"]["total_debt_divergence"] = True
    elif isinstance(_chosen, (int, float)) and _chosen > 0:
        # (2) no components to reconcile against: compare the other available full-debt readings
        _alts = []
        for _tags in (DEBT_FULL_LT, DEBT_COMBINED):
            _v, _t = _latest_instant(facts, _tags)
            if _v and _v.get("val"):
                _alts.append((_v["val"], _t))
        if _alts:
            _mx, _mx_tag = max(_alts, key=lambda x: x[0])
            if _mx > 0 and abs(_mx - _chosen) / max(_mx, _chosen) > RECON_TOL:
                out["total_debt"] = _mx           # conservative = larger
                out["_flags"]["debt_uncertain"] = (
                    "sources diverge >%.0f%%: chose conservative %.0f (%s) over %.0f"
                    % (RECON_TOL * 100, _mx, _mx_tag, _chosen))
                out["_flags"]["total_debt_divergence"] = True
                src["total_debt"] = _mx_tag

    if out.get("restricted_cash"):
        out["_flags"]["cash_note"] = "cash excludes restricted_cash; scenario_f treats restricted separately"

    # v4: confirmed-split detection (restatement-based) for shares_diluted
    conf_splits = _detect_confirmed_splits(facts, DURATION_TAGS["shares_diluted"], cik=cik)
    if conf_splits:
        out["_flags"]["confirmed_splits"] = conf_splits
    else:
        # Say so explicitly. Silence here is indistinguishable from "no split ever happened",
        # and the consumer then books any clean ratio jump as dilution.
        out["_flags"]["confirmed_splits_none"] = (
            "no retroactive share-count restatement found in companyfacts+companyconcept "
            "10-K history; a clean ratio jump downstream is UNCONFIRMED, not proven dilution")

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
