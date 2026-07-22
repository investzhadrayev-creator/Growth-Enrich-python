"""
edgar_form4.py — SEC EDGAR Form 4 (insider transactions) endpoint for the Growth Alpha /
Consilium Spine microservice. Phase 2 of the EDGAR integration (phase 1 = edgar_facts.py
financial series).

WHY THIS EXISTS: the pipeline's memo previously sourced insider activity from Perplexity's
FACT_PACK prose, which produced an impossible trade on a live run ("600 shares for $1,655" =
$2.76/share against a $1852 stock — the auditor caught it, but it shouldn't have been possible).
Form 4 filings report EVERY insider transaction with an exact SEC-reported price per share and
share count — pulling structured data from the filing itself, rather than an LLM's paraphrase
of secondary commentary, makes that class of error structurally impossible.

KEY DESIGN — discretionary vs non-discretionary transactions. SEC transaction codes:
  P = open-market/private PURCHASE      (discretionary, price is real market price)
  S = open-market/private SALE          (discretionary, price is real market price)
  A = grant/award                       (non-discretionary, price is often $0 or nominal)
  M = exercise/conversion of derivative (non-discretionary, price = strike, not market)
  F = tax withholding on vesting        (non-discretionary, not a "sale" in the ordinary sense)
  X = in/at-the-money option exercise   (non-discretionary)
  G = gift; C = conversion              (non-discretionary)
Conflating A/M/F/X with P/S is exactly how a $2.76/share "purchase" can look plausible in prose
(an award or tax-withholding transaction has a nominal/strike price, not a market price). This
client keeps them in SEPARATE buckets so the memo can never mix them.

DESIGN (matches edgar_facts.py discipline):
  - Every transaction is a first-source SEC fact with accession number + filing date.
  - Never fabricates: a filing that fails to parse is skipped with an error note, not guessed.
  - Never throws; returns a dict with '_errors' trail on partial failure.
  - Reuses edgar_facts.py's throttled HTTP client + ticker->CIK resolution (same SEC User-Agent,
    same ~8 req/s fair-access ceiling) — no separate rate-limit budget to manage.

SCOPE: last `lookback_days` (default 270 ~ 9 months) of Form 4 filings, capped at
`max_filings` (default 40) to bound latency or a very active filer.

ENDPOINT: POST /edgar_form4   body: {"ticker": "PLTR", "lookback_days": 270}
"""
import json
import time
import datetime as _dt
import xml.etree.ElementTree as ET

from edgar_facts import _get, _resolve_cik, SEC_USER_AGENT  # reuse throttled client + CIK map

DISCRETIONARY_CODES = {"P", "S"}
NON_DISCRETIONARY_CODES = {"A", "M", "F", "X", "G", "C", "D", "I", "J"}
CODE_LABELS = {
    "P": "open-market/private purchase", "S": "open-market/private sale",
    "A": "grant/award", "M": "derivative exercise/conversion",
    "F": "tax withholding on vesting", "X": "in/at-the-money option exercise",
    "G": "gift", "C": "conversion of derivative", "D": "disposition to issuer",
    "I": "discretionary transaction (Rule 16b-3)", "J": "other (see footnote)",
}


def _submissions(cik):
    return _get("https://data.sec.gov/submissions/CIK%s.json" % cik)


def _raw_doc_name(primary_doc):
    """SEC's primaryDocument for a Form 4 usually points at the XSL-RENDERED view
    (e.g. 'xslF345X05/wf-form4_1746.xml') which serves HTML, not parseable XML — that is why
    a naive fetch parses 0 of N filings. The machine-readable XML sits in the SAME folder
    without the xsl* path component."""
    if not primary_doc:
        return None
    parts = str(primary_doc).split("/")
    if len(parts) > 1 and parts[0].lower().startswith("xsl"):
        return parts[-1]
    return primary_doc


def _dir_url(cik_int, accession):
    return "https://www.sec.gov/Archives/edgar/data/%s/%s" % (cik_int, accession.replace("-", ""))


def _find_xml_via_index(cik_int, accession):
    """Fallback: list the filing folder and pick the ownership XML. Used when primaryDocument
    is missing or its stripped name still isn't valid XML."""
    idx = _get(_dir_url(cik_int, accession) + "/index.json")
    items = ((idx.get("directory") or {}).get("item") or [])
    names = [it.get("name", "") for it in items]
    cands = [n for n in names
             if n.lower().endswith(".xml") and not n.lower().startswith("xsl")
             and "form4" in n.lower().replace("_", "").replace("-", "")]
    if not cands:
        cands = [n for n in names if n.lower().endswith(".xml") and not n.lower().startswith("xsl")]
    return cands[0] if cands else None


def _fetch_xml(cik_int, accession, primary_doc):
    """Return the Form 4 XML text. Tries the de-XSL'd primaryDocument, then the folder index."""
    doc = _raw_doc_name(primary_doc)
    last_err = None
    if doc:
        try:
            txt = _get_raw(_dir_url(cik_int, accession) + "/" + doc)
            if txt.lstrip()[:1] == "<" and "ownershipDocument" in txt:
                return txt
            last_err = "not an ownershipDocument XML (likely an XSL/HTML rendering)"
        except Exception as e:
            last_err = str(e)[:120]
    alt = _find_xml_via_index(cik_int, accession)
    if alt and alt != doc:
        txt = _get_raw(_dir_url(cik_int, accession) + "/" + alt)
        if "ownershipDocument" in txt:
            return txt
        last_err = "index fallback doc is not an ownershipDocument"
    raise ValueError("no parseable Form 4 XML (%s)" % (last_err or "no candidate document"))


def _get_raw(url):
    """Like edgar_facts._get but returns raw bytes/text (XML, not JSON)."""
    import urllib.request
    import time as _t
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def _txt(el, path, default=None):
    node = el.find(path)
    if node is None:
        return default
    v = node.find("value")
    if v is not None and v.text is not None:
        return v.text.strip()
    return node.text.strip() if node.text else default


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _parse_form4(xml_text, accession, filed_date):
    root = ET.fromstring(xml_text)
    issuer_symbol = _txt(root, "./issuer/issuerTradingSymbol")
    owner_name = _txt(root, "./reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("./reportingOwner/reportingOwnerRelationship")
    role = []
    if rel is not None:
        if (_txt(rel, "isDirector") or "0") == "1":
            role.append("director")
        if (_txt(rel, "isOfficer") or "0") == "1":
            title = _txt(rel, "officerTitle") or "officer"
            role.append(title)
        if (_txt(rel, "isTenPercentOwner") or "0") == "1":
            role.append("10%_owner")
    footnote_text = " ".join((fn.text or "") for fn in root.findall(".//footnote"))
    is_10b5_1 = "10b5-1" in footnote_text or "10b5‑1" in footnote_text

    txns = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = _txt(tx, "./transactionCoding/transactionCode")
        shares = _num(_txt(tx, "./transactionAmounts/transactionShares"))
        price = _num(_txt(tx, "./transactionAmounts/transactionPricePerShare"))
        ad_code = _txt(tx, "./transactionAmounts/transactionAcquiredDisposedCode")  # A=acquired, D=disposed
        post_shares = _num(_txt(tx, "./postTransactionAmounts/sharesOwnedFollowingTransaction"))
        date = _txt(tx, "./transactionDate")
        txns.append({
            "date": date, "code": code, "code_label": CODE_LABELS.get(code, code),
            "discretionary": code in DISCRETIONARY_CODES,
            "shares": shares, "price_per_share": price,
            "value": round(shares * price, 2) if (shares is not None and price is not None) else None,
            "acquired_or_disposed": ad_code, "shares_owned_after": post_shares,
            "is_10b5_1_plan": is_10b5_1 or None,
        })
    return {
        "accession": accession, "filed": filed_date, "issuer_symbol": issuer_symbol,
        "owner_name": owner_name, "owner_role": role, "transactions": txns,
    }


def edgar_form4(ticker=None, cik=None, lookback_days=270, max_filings=40):
    out = {"_source": "sec_edgar_form4", "_ticker": ticker, "_errors": {}, "_filings_parsed": 0,
          "_filings_failed": 0}
    if not cik:
        cik = _resolve_cik(ticker)
    if not cik:
        out["_errors"]["cik"] = "ticker not found in SEC company_tickers map"
        return out
    out["_cik"] = cik
    cik_int = str(int(cik))  # drop leading zeros for the Archives path

    try:
        subs = _submissions(cik)
    except Exception as e:
        out["_errors"]["submissions"] = str(e)[:160]
        return out

    recent = (subs.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    accns = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    docs = recent.get("primaryDocument", [])
    cutoff = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()

    candidates = []
    for i in range(len(forms)):
        if forms[i] != "4":
            continue
        if i >= len(dates) or dates[i] < cutoff:
            continue
        candidates.append((accns[i], dates[i], docs[i] if i < len(docs) else None))
    candidates = candidates[:max_filings]

    filings = []
    for accn, filed, doc in candidates:
        try:
            xml_text = _fetch_xml(cik_int, accn, doc)
            parsed = _parse_form4(xml_text, accn, filed)
            filings.append(parsed)
            out["_filings_parsed"] += 1
        except Exception as e:
            out["_filings_failed"] += 1
            if len(out["_errors"]) < 5:          # cap: a sample is enough to diagnose
                out["_errors"][accn] = str(e)[:140]

    # aggregate
    all_txns = [dict(t, accession=f["accession"], owner_name=f["owner_name"], owner_role=f["owner_role"])
                for f in filings for t in f["transactions"]]
    disc = [t for t in all_txns if t["discretionary"]]
    nondisc = [t for t in all_txns if not t["discretionary"]]

    def _sum_signed(txns, code):
        s = sum((t["shares"] or 0) for t in txns if t["code"] == code)
        return s

    buy_shares = _sum_signed(disc, "P")
    sell_shares = _sum_signed(disc, "S")
    buy_value = sum((t["value"] or 0) for t in disc if t["code"] == "P")
    sell_value = sum((t["value"] or 0) for t in disc if t["code"] == "S")

    out["lookback_days"] = lookback_days
    out["discretionary_summary"] = {
        "buy_shares": buy_shares, "sell_shares": sell_shares,
        "net_shares": round(buy_shares - sell_shares, 2),
        "buy_value_usd": round(buy_value, 2), "sell_value_usd": round(sell_value, 2),
        "net_value_usd": round(buy_value - sell_value, 2),
        "unique_insiders": len(set(t["owner_name"] for t in disc if t["owner_name"])),
        "any_10b5_1_plan": any(t.get("is_10b5_1_plan") for t in disc),
    }
    out["non_discretionary_summary"] = {
        "count": len(nondisc),
        "codes_seen": sorted(set(t["code"] for t in nondisc if t["code"])),
        "_note": "grants/exercises/tax-withholding — NOT open-market conviction signals; "
                 "do not cite as insider buying/selling",
    }
    out["discretionary_transactions"] = sorted(disc, key=lambda t: t.get("date") or "", reverse=True)[:20]
    out["_note"] = ("Prices/shares are SEC-reported facts, not inferred. discretionary_transactions "
                    "= P/S only (real open-market conviction). Anything in non_discretionary_summary "
                    "(grants/vesting/exercises) must NOT be described as a purchase or sale.")
    return out


# --- Flask route glue (add to app.py) ---
#   from edgar_form4 import edgar_form4
#   @app.route("/edgar_form4", methods=["POST"])
#   def _edgar_form4():
#       b = request.get_json(force=True, silent=True) or {}
#       return jsonify(edgar_form4(b.get("ticker"), b.get("cik"), b.get("lookback_days", 270))), 200

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # OFFLINE self-test of the XML parser + discretionary/non-discretionary split.
        SAMPLE_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
      <officerTitle>CEO</officerTitle><isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>600</value></transactionShares>
        <transactionPricePerShare><value>1852.30</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-04-15</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1200</value></transactionShares>
        <transactionPricePerShare><value>0.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50600</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes><footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted March 2026.</footnote></footnotes>
</ownershipDocument>"""
        parsed = _parse_form4(SAMPLE_XML, "0000000000-26-000001", "2026-05-03")
        assert parsed["owner_name"] == "Jane Doe"
        assert parsed["owner_role"] == ["CEO"]
        s_tx = next(t for t in parsed["transactions"] if t["code"] == "S")
        f_tx = next(t for t in parsed["transactions"] if t["code"] == "F")
        assert s_tx["discretionary"] is True and s_tx["value"] == 600 * 1852.30
        assert f_tx["discretionary"] is False, "tax withholding must NOT be flagged discretionary"
        assert s_tx["is_10b5_1_plan"] is True
        print("SELFTEST OK — S(sale, discretionary, $%.2f/share) vs F(tax withholding, non-discretionary) "
              "correctly separated; 10b5-1 plan detected from footnote." % s_tx["price_per_share"])
        print(json.dumps({"S": s_tx, "F": f_tx}, indent=2))
    else:
        tk = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
        print(json.dumps(edgar_form4(tk), indent=2, default=str))
