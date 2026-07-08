# Growth Alpha Microservice — деплой на Railway

Сервис, который вызывает n8n-пайплайн. Два роута: `/run` (детерминированный IVC) и `/enrich_yf` (обогащение через yfinance).

## Почему падал прежний деплой
Railpack не смог определить, как собрать приложение: в папке лежал голый `enrich_yf.py` — **библиотека без точки входа**. Не было `requirements.txt`, веб-сервера и команды запуска. PaaS не умеет запускать функцию — ему нужен процесс, слушающий порт. Этот пакет добавляет всё недостающее.

## Файлы в этом пакете
| Файл | Зачем |
|---|---|
| `app.py` | Точка входа — Flask-сервер с роутами `/run`, `/enrich_yf`, `/health` |
| `enrich_yf.py` | Логика yfinance-обогащения (вызывается из app.py) |
| `requirements.txt` | Зависимости — Railpack ставит их автоматически |
| `Procfile` | Команда запуска (gunicorn, production) |
| `railway.json` | Явная конфигурация build/start для Railway |
| `.python-version` | Пин Python 3.11 |

## Шаги деплоя

### Вариант А — отдельный сервис (как вы пробовали)
1. Положите ВСЕ файлы из этой папки (`deploy/`) в корень репозитория/сервиса — не только `enrich_yf.py`. Критично: `app.py`, `requirements.txt`, `Procfile` должны быть в корне.
2. Railway → New → Deploy from repo (или залейте папку). Railpack теперь увидит `requirements.txt` → определит Python → поставит зависимости → запустит `gunicorn app:app`.
3. После деплоя Railway даст публичный URL, например `https://growth-yf-production.up.railway.app`.
4. **Egress к Yahoo:** yfinance ходит на `query1.finance.yahoo.com` / `query2.finance.yahoo.com`. На Railway исходящий трафик по умолчанию открыт — специально ничего добавлять не нужно (в отличие от sandbox с allowlist). Проверьте роутом `/health`, затем реальным тикером.
5. Проверка после деплоя:
   ```
   curl https://<ваш-url>/health
   curl -X POST https://<ваш-url>/enrich_yf -H 'Content-Type: application/json' -d '{"ticker":"NVDA","peers":["AMD","AVGO","INTC"]}'
   ```
   Второй должен вернуть непустые `fwd_pe`, `peer_median_pe`, `peer_multiples`.

### Вариант Б — добавить роут в СУЩЕСТВУЮЩИЙ Run Code сервис (рекомендую)
У вас уже есть работающий микросервис для `/run`. Плодить второй сервис — лишняя точка отказа. Если у существующего сервиса тоже Flask:
1. Скопируйте `enrich_yf.py` рядом с его кодом.
2. Добавьте в его requirements: `yfinance`, `pandas`.
3. Добавьте роут:
   ```python
   from enrich_yf import enrich_yf
   @app.route("/enrich_yf", methods=["POST"])
   def _enrich_yf():
       b = request.get_json(force=True, silent=True) or {}
       return jsonify(enrich_yf(b.get("ticker"), b.get("peers") or [])), 200
   ```
4. Redeploy. Тогда `YOUR_PYTHON_SERVICE_URL` в n8n остаётся один и тот же для `/run` и `/enrich_yf`.

## После деплоя — прописать URL в n8n
В workflow (`growth_alpha_pipeline_v2_3.json`) замените плейсхолдер `YOUR_PYTHON_SERVICE_URL` в двух местах:
- нода **Run Code**: `YOUR_PYTHON_SERVICE_URL/run`
- нода **Growth Enrich** (внутри JS): `YOUR_PYTHON_SERVICE_URL/enrich_yf`

## Проверка, что всё связалось
Прогоните `growth NVDA` в Telegram. В EVIDENCE PACK отчёта поля `fwd P/E`, `PEER multiples`, `PEER-median fwd P/E`, `Short interest` должны стать непустыми, а гейт `pe_cap` — закрыться (Base P/E якорится к peer-median).

## Честные ограничения
- yfinance неофициален (скрейпит Yahoo) — может ломаться при изменениях на их стороне. Сервис деградирует в null, пайплайн не падает (`_errors` покажет причину).
- Yahoo rate-limit не документирован — не гоняйте десятки тикеров в минуту; при массовых прогонах добавьте кэш.
- Дословный тон earnings calls остаётся `[UNVERIFIED]` — надёжного бесплатного источника нет.
