"""
app.py — deployable microservice for the Growth Alpha Pipeline.
Exposes two routes the n8n workflow calls:
  POST /run        -> executes the deterministic IVC wiring code (Stage 2a output)
  POST /enrich_yf  -> yfinance enrichment (fwd_pe, peers, revisions, short interest, ...)
  GET  /health     -> liveness probe

Design notes (matches pipeline discipline):
  - Both routes ALWAYS return JSON, never a bare 500 with an HTML body — the n8n
    HTTP nodes and Render Tables parse JSON; an HTML error page would break them.
  - /run executes untrusted-ish generated Python in a subprocess with a timeout,
    capturing stdout (the pipeline contract: last line of stdout is the JSON result).
  - /enrich_yf wraps enrich_yf() which itself never throws.
"""
import json
import os
import subprocess
import sys
import tempfile

from flask import Flask, request, jsonify

from enrich_yf import enrich_yf

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
        return {
            "_FALLBACK": True, "fallback_reason": reason,
            "scenarios": scen, "pwfv": round(pwfv, 2), "ivc_base": ivb,
            "mos_ladder": ivb.get("mos_ladder"),
            "gps": {"blocks": {"pinned_ACDF": gps}, "total": None,
                    "_note": "fallback: qualitative blocks not scored"},
            "flags": ["FALLBACK_IVC_USED_wiring_failed", "verdict_cap_forced_WATCH+_pending_rerun"],
            "verdict_cap": "WATCH+",
            "self_tests_all": bool(ivb.get("self_tests")),
        }
    except Exception as e:
        return {"error": "RUNNER_ERROR: fallback IVC failed: " + str(e)[:200],
                "fallback_reason": reason}


if __name__ == "__main__":
    # Railway/Render provide $PORT; default 8080 locally.
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
