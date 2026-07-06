# Карта [UNVERIFIED] полей → бесплатные источники (для Growth Alpha Pipeline)

> Задача: закрыть поля, которые сейчас `[UNVERIFIED]` из-за платных Finnhub/Tiingo, бесплатными альтернативами. Ниже — что реально бесплатно, где, и как встроить в пайплайн без слома детерминированной дисциплины.

---

## Ключевое архитектурное решение (прочитать первым)

Три способа доставки данных, по убыванию надёжности для нашего пайплайна:

1. **HTTP-эндпоинт с JSON** → идёт прямо в n8n HTTP-ноду. Так работают SEC, FRED, Tiingo.
2. **Python-библиотека (yfinance)** → НЕ работает в n8n HTTP-ноде. Живёт в вашем **Python-микросервисе** (тот же, что исполняет Run Code). Growth Enrich вызывает микросервис, микросервис дёргает yfinance. Это чище, чем скрейпить в JS-ноде.
3. **Скрейпинг HTML** (StockAnalysis, OpenInsider) → хрупко, только как крайний fallback.

**Вывод:** большинство недостающих полей закрывает `yfinance` — но через микросервис, а не через n8n напрямую. Это добавляет один вызов микросервиса в Growth Enrich, но сохраняет детерминированность (числа считаются в Python, не в LLM).

---

## Таблица: поле → источник → доступность → как получить

| Поле (сейчас UNVERIFIED) | Бесплатный источник | Как | Надёжность |
|---|---|---|---|
| **fwd P/E** | yfinance `.info['forwardPE']` | микросервис | Высокая (это заменяет Finnhub 401) |
| **PEG** | yfinance `.info['pegRatio']` / `trailingPegRatio` | микросервис | Средняя (Yahoo считает по-своему — лучше пересчитать самим из fwd_pe и growth) |
| **peer multiples** (AMD, AVGO, INTC fwd P/E) | yfinance по каждому пиру `.info['forwardPE']` | микросервис, цикл по тикерам | Высокая — закрывает pe_cap named-comps |
| **sector median P/E** | вычислить: медиана fwd P/E по списку пиров (yfinance) | микросервис | Средняя (зависит от выбора пиров) |
| **ERB / analyst revisions** | yfinance `.recommendations` / `.recommendations_summary` / `.upgrades_downgrades` | микросервис | Средняя (заменяет Finnhub rec-trends) |
| **analyst price target** | yfinance `.analyst_price_targets` (или `.info['targetMeanPrice']`) | микросервис | Средняя |
| **EPS estimates FY+1/FY+2** | yfinance `.earnings_estimate` / `.growth_estimates` | микросервис | Средняя |
| **short interest (% float)** | yfinance `.info['shortPercentOfFloat']`, `sharesShort` | микросервис | Средняя |
| **dividend history (5y)** | yfinance `.dividends` (полный ряд) | микросервис | Высокая |
| **institutional holders** | yfinance `.institutional_holders`, `.major_holders` | микросервис | Средняя |
| **insider activity (Form 4)** | **SEC EDGAR** (у вас УЖЕ подключён) — full-text search Form 4, ИЛИ yfinance `.insider_transactions` | микросервис / SEC | SEC = высокая, первоисточник |
| **buyback authorized vs executed** | SEC 10-Q/10-K (у вас есть XBRL) — `PaymentsForRepurchaseOfCommonStock` из cash flow | уже в Gather, добавить concept | Высокая (первоисточник) |
| **segment revenue (data center vs gaming)** | SEC 10-K, но yfinance не даёт сегменты → парсить 10-K или FMP free `revenue-product-segmentation` (лимит) | микросервис/SEC | Средняя |
| **earnings call transcript / тон** | **платно почти везде.** Бесплатно: Motley Fool HTML (скрейп), или API Ninjas earnings transcript (free tier) | fallback | Низкая |
| **RPO (backlog)** | SEC 10-Q XBRL concept `RevenueRemainingPerformanceObligation` | добавить в Gather | Высокая (первоисточник) |

---

## Что закрывается ПОЛНОСТЬЮ бесплатно (первоисточник — SEC, у вас уже есть)

Эти поля вообще не должны быть UNVERIFIED — они в отчётности, которую Gather уже качает:
- **buyback executed** → XBRL `PaymentsForRepurchaseOfCommonStock` (cash flow statement)
- **RPO / backlog** → XBRL `RevenueRemainingPerformanceObligation`
- **insider Form 4** → EDGAR full-text search (тот же CIK, что для XBRL)
- **D/E** (сейчас `—`) → уже есть `total_debt` и `total_equity` в Gather, просто не дошло до payload — это баг проводки, не отсутствие данных
- **ROE** (сейчас `—`) → аналогично: `net_income` и `total_equity` есть, ROE считается в Growth Enrich, но в EVIDENCE PACK показал `—`

> **Замечание аналитика:** то, что D/E и ROE показали `—`, хотя данные для них ЕСТЬ в Gather — это внутренний баг проводки (как RESULT=null в v2.1), а не отсутствие источника. Чинится бесплатно, в коде.

---

## Приоритет внедрения (по соотношению польза/усилие)

**Волна 1 — бесплатно, первоисточник, максимальная отдача:**
1. Починить D/E и ROE в EVIDENCE PACK (данные уже есть, баг проводки) — 0 новых источников.
2. Добавить в Gather XBRL-концепты: `PaymentsForRepurchaseOfCommonStock` (buyback), `RevenueRemainingPerformanceObligation` (RPO). Закрывает 2 поля из первоисточника.

**Волна 2 — yfinance через микросервис, закрывает ядро оценки:**
3. fwd P/E, peer multiples (AMD/AVGO/INTC), sector median → закрывает `pe_cap_unjustified` (главный незакрытый гейт NVDA).
4. ERB/revisions, price target, EPS estimates → заменяет весь Finnhub (401).
5. short interest, dividend history, institutional holders → закрывает блоки G/H.

**Волна 3 — fallback, низкая надёжность:**
6. Earnings transcript тон — оставить `[UNVERIFIED]` или скрейп Motley Fool. Честнее пометить, чем тащить ненадёжное.

---

## Как это ложится в архитектуру (без слома дисциплины)

```
Growth Enrich (n8n)
  ├─ HTTP: SEC XBRL (buyback, RPO — новые концепты)   ← первоисточник
  ├─ HTTP: вызов Python-микросервиса /enrich_yf        ← НОВОЕ
  │        (микросервис внутри дёргает yfinance:
  │         fwd_pe, peers, revisions, short_interest,
  │         dividends, holders — всё числами)
  └─ детерминированный расчёт как сейчас
```

Микросервис возвращает JSON с числами → Growth Enrich кладёт в payload → всё дальше по пайплайну как обычно. **LLM по-прежнему не считает — числа приходят из Python.** yfinance-числа помечаются tier «yahoo» (ниже SEC, выше FACT_PACK) в иерархии надёжности v3.7.

---

## Честные ограничения (важно понимать)

1. **yfinance неофициален** — скрейпит Yahoo, может ломаться при изменениях на их стороне. Для личного research это норма, для продакшена — держать fallback и try/catch (как везде в Growth Enrich).
2. **Rate limits Yahoo** — не документированы, но при десятках тикеров в минуту начинает отдавать пустое. Кэшировать, не дёргать пиров на каждый прогон.
3. **Yahoo forward P/E и PEG считает по своей методике** — для pe_cap используйте peer multiples (сырые fwd_pe пиров), а PEG пересчитывайте сами из fwd_pe и вашего growth, не берите готовый Yahoo PEG (иначе рассинхрон с house-формулой PEG=fwd_PE/growth).
4. **Sector median из пиров ≠ настоящая секторная медиана** — это медиана вашего списка из 4-5 comps, не всего сектора. Честно называть это «peer median», не «sector median».
5. **Transcript тон** — единственное поле, где бесплатной надёжной замены практически нет. Лучше оставить UNVERIFIED, чем тащить мусор.

---

## Итог одной строкой
**~80% ваших [UNVERIFIED] закрывается бесплатно: часть — из SEC, которую вы уже качаете (buyback, RPO, insider, D/E, ROE — это баги проводки, не отсутствие данных), ядро оценки (fwd P/E, peers, revisions, short interest) — через yfinance в вашем Python-микросервисе. Единственное, что честнее оставить непроверяемым — дословный тон earnings calls.**
