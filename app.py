import os
import sys
import io
import json
import traceback
from flask import Flask, request, jsonify

# Импортируем вашу логику из лежащего рядом enrich_yf.py
from enrich_yf import enrich_yf

app = Flask(__name__)

@app.route('/enrich_yf', methods=['POST'])
def api_enrich_yf():
    body = request.get_json(force=True) or {}
    ticker = body.get("ticker")
    peers = body.get("peers")
    
    if not ticker:
        return jsonify({"_source_tier": "yahoo", "_ticker": None, "_errors": {"request": "missing ticker"}}), 400
        
    # Вызов детерминированной функции обогащения
    result = enrich_yf(ticker, peers)
    return jsonify(result)

@app.route('/run', methods=['POST'])
def api_run_code():
    body = request.get_json(force=True) or {}
    code = body.get("code", "")
    payload = body.get("data", {})

    # Перехват стандартного вывода (LLM генерирует print(json.dumps(...)))
    old_stdout = sys.stdout
    redirected_output = sys.stdout = io.StringIO()

    try:
        # Пробрасываем payload в глобальную область видимости скрипта
        exec_globals = {
            "payload": payload,
            "json": json,
            "math": __import__('math')
        }
        # Исполняем код, сгенерированный LLM + пришитый IVC_LIB
        exec(code, exec_globals)
        
        stdout_str = redirected_output.getvalue()
        return jsonify({"stdout": stdout_str})
        
    except Exception as e:
        error_str = traceback.format_exc()
        return jsonify({
            "error": "RUNNER_ERROR: " + str(e),
            "traceback": error_str
        }), 500
        
    finally:
        sys.stdout = old_stdout

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)