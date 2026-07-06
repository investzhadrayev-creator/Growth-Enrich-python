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

    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(full)
            path = f.name
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "RUNNER_ERROR: execution timed out"}), 200
    except Exception as e:
        return jsonify({"error": "RUNNER_ERROR: " + str(e)[:200]}), 200
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()[:400]
        return jsonify({"error": "RUNNER_ERROR: no stdout", "stderr": err}), 200

    # Contract: last non-empty stdout line is the JSON result.
    last = [ln for ln in out.splitlines() if ln.strip()][-1]
    try:
        parsed = json.loads(last)
        # Return under both keys so Render Tables' tolerant parser finds it.
        return jsonify({"stdout": last, "result": parsed}), 200
    except Exception:
        # Return raw stdout so the caller can see what happened (still valid JSON envelope).
        return jsonify({"stdout": out[-4000:], "error": "RUNNER_ERROR: last line not JSON"}), 200


if __name__ == "__main__":
    # Railway/Render provide $PORT; default 8080 locally.
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
