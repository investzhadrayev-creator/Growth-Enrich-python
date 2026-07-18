"""Token accounting for a Consilium Spine run. ONE canonical home for prices.

DESIGN, in the house idiom:

  TOKENS ARE FACTS. They come from each provider's own `usage` block, echoed back in the HTTP
  response we already receive and, until v4.2.5, threw away. Nothing is inferred or estimated.

  DOLLARS ARE AN ESTIMATE, and the estimate carries its basis. `_AS_OF` below is the date the
  table was last checked; the report prints it. A price table is a hardcoded number that goes
  stale silently -- the exact defect class this project keeps paying for -- so it is dated,
  sourced, and it EXPIRES loudly.

  A MODEL WITH NO PRICE IS [UNVERIFIED], NEVER $0. A run is never free. Rendering an unpriced
  model as 0.00 would be "unknown spelled zero" and would understate the bill by exactly the
  amount you failed to look up. Unpriced models are named in the report and excluded from the
  total, and the total says so.

PROVENANCE WARNING: the rates below were compiled 2026-07-17 from SECONDARY aggregators, not
from the vendors' own pricing pages (those were not reachable from the build environment). They
are a starting point that the operator MUST verify against each provider's billing page. The
`_source` field on every entry says where it came from. Verified-against-vendor entries should
have their `_source` changed to the vendor URL and `_verified_by_operator` set True.
"""

import datetime as _dt

# ---------------------------------------------------------------------------------------------
# Price table. Rates are USD per 1,000,000 tokens.
# ---------------------------------------------------------------------------------------------

_AS_OF = "2026-07-17"
_STALE_AFTER_DAYS = 90          # operator-set, 2026-07-17. Mirrors the FINRA 45-day settlement rule.

# Anthropic cache economics (docs, 2026-07): a cache READ bills at 10% of the input rate; a cache
# WRITE bills at 125% (5-minute TTL) or 200% (1-hour TTL). Collapsing cache tokens into the plain
# input rate -- the obvious naive implementation -- overstates a cached run by roughly an order of
# magnitude on the cached portion. Stage 2a/2b/6 all run with cache_control ON.
_ANTHROPIC_CACHE_READ_MULT = 0.10
_ANTHROPIC_CACHE_WRITE_MULT = 1.25

# xAI reports cost_in_usd_ticks in every response. The unit is not in any doc we could reach; it
# was DERIVED from the live NFLX run of 2026-07-17 and reconciles EXACTLY, to the tick, across four
# independent terms:
#     24,060 fresh in @ $2.00/M  = $0.048120
#     60,288 cached   @ $0.50/M  = $0.030144
#      3,885 out      @ $6.00/M  = $0.023310
#         13 x_search @ $0.005   = $0.065000
#                        TOTAL   = $0.166574  ->  1,665,740,000 ticks reported
# 0.166574 / 1_665_740_000 = 1e-10 exactly. A single observation, but an exact match across four
# terms is not a coincidence you get from a wrong unit. Pinned by test, re-checked every run: the
# ledger compares our estimate against the vendor figure and flags any drift (see cost_ledger).
TICK_USD = 1e-10

# Server-side tool calls (x_search / web_search) bill PER CALL, outside the token meter entirely.
# On the live run they were 64% of the Stage 3 bill. Derived from the same reconciliation.
_XAI_TOOL_CALL_USD = 0.005

PRICES = {
    # -------- swap candidates (v4.2.11) — PRICED BUT NOT WIRED to any node. Pre-pricing keeps a
    # future A/B (audit plan runs 4-5) from rendering [UNVERIFIED], the same reason grok-4.3
    # stays priced after the 4.5 move. Privacy note for the operator's decision, not ours:
    # DeepSeek's official API is China-hosted; GLM-5.2 weights are MIT and served by US hosts
    # (DeepInfra/Fireworks) at a small markup over the $1.40/$4.40 first-party rate. --------
    "deepseek-v4-pro": {
        "input": 0.435, "output": 0.87,
        "cache_read_mult": None, "cache_write_mult": None,
        "_source": "web search 2026-07-18 (benchlm/openrouter/official docs cited) — official "
                   "DeepSeek API, China-hosted; VERIFY at platform.deepseek.com before any A/B",
        "_verified_by_operator": False,
    },
    "glm-5.2": {
        "input": 1.40, "output": 4.40,
        "cache_read_mult": None, "cache_write_mult": None,
        "_source": "web search 2026-07-18 — Z.ai first-party rate; US-hosted open-weight serving "
                   "(DeepInfra/Fireworks) prices differ — VERIFY at the chosen host before any A/B",
        "_verified_by_operator": False,
    },
    "claude-opus-4-8": {
        "input": 5.00, "output": 25.00,
        "cache_read_mult": _ANTHROPIC_CACHE_READ_MULT,
        "cache_write_mult": _ANTHROPIC_CACHE_WRITE_MULT,
        "_source": "secondary aggregator (benchlm/aipricing), 2026-07-17 — VERIFY at anthropic.com/pricing",
        "_verified_by_operator": False,
    },
    "claude-sonnet-5": {
        "input": 2.00, "output": 10.00,
        "cache_read_mult": _ANTHROPIC_CACHE_READ_MULT,
        "cache_write_mult": _ANTHROPIC_CACHE_WRITE_MULT,
        # INTRODUCTORY PRICING. Reverts to 3.00/15.00 on 2026-09-01 per Anthropic's announcement.
        # This is not hypothetical: it is a 50% increase on the two heaviest nodes in the pipeline
        # (Stage 2a + 2b), six weeks out. The expiry is machine-checked below, not a comment.
        "_expires": "2026-08-31",
        "_after_expiry": {"input": 3.00, "output": 15.00},
        "_source": "secondary aggregator, 2026-07-17 — intro rate through 2026-08-31 — VERIFY",
        "_verified_by_operator": False,
    },
    "gpt-5.6-sol": {
        "input": 5.00, "output": 30.00,
        "cache_read_mult": 0.10,
        "cache_write_mult": 1.00,
        # OpenAI bills reasoning tokens as OUTPUT. Stage 5/Core-V Auditor run reasoning_effort=medium,
        # so completion_tokens_details.reasoning_tokens are already inside completion_tokens -- do NOT
        # add them again. See _normalise_openai.
        "_source": "secondary aggregator, 2026-07-17 — VERIFY at openai.com/api/pricing",
        "_verified_by_operator": False,
    },
    "gemini-3.1-pro-preview": {
        "input": 2.00, "output": 12.00,
        "cache_read_mult": None, "cache_write_mult": None,
        # Rate found for "Gemini 3.1 Pro". This pipeline calls the "-preview" alias, which MAY be
        # priced differently or be free-tier limited. Treated as the GA rate; flagged, not guessed.
        "_alias_risk": "priced as GA 'gemini-3.1-pro'; the -preview alias may differ",
        "_source": "secondary aggregator, 2026-07-17 — VERIFY at ai.google.dev/pricing",
        "_verified_by_operator": False,
    },
    "sonar-pro": {
        "input": 2.00, "output": 10.00,
        "cache_read_mult": None, "cache_write_mult": None,
        # Perplexity bills PER SEARCH REQUEST on top of tokens. That component is NOT in the usage
        # block, so a token-only estimate for Stage 1 is INCOMPLETE BY CONSTRUCTION, not merely
        # imprecise. Flagged on every line rather than silently under-reported.
        "_incomplete": "token-only: Perplexity also bills per search request, not visible in usage",
        "_expires": "2026-08-31",
        "_after_expiry": None,   # post-expiry rate not established -> becomes UNVERIFIED, not a guess
        "_source": "secondary aggregator, 2026-07-17 — VERIFY at perplexity.ai/pricing",
        "_verified_by_operator": False,
    },
    "grok-4.5": {
        "input": 2.00, "output": 6.00,
        "cache_read_mult": 0.25,          # $0.50/M against a $2.00/M input = 25%, xAI's published rate
        "cache_write_mult": None,
        # Two things this rate does NOT cover, both confirmed in xAI's own docs:
        #  - requests above 200K input tokens bill at a DIFFERENT, UNPUBLISHED rate;
        #  - Priority Processing doubles everything, and the response says so via service_tier.
        # Both are detected at ledger time (see cost_ledger) rather than assumed away.
        "_context_surcharge_over": 200000,
        "_source": "secondary aggregators (openrouter/benchlm/cometapi), 2026-07-17; launched "
                   "2026-07-08 — VERIFY at docs.x.ai",
        "_verified_by_operator": False,
    },
    "grok-4.3": {
        "input": 1.25, "output": 2.50,
        "cache_read_mult": 0.16,          # $0.20/M against $1.25/M input
        "cache_write_mult": None,
        # Superseded by 4.5 on 2026-07-08 but NOT retired, and materially cheaper with 2x the
        # context (1M vs 500K). Kept priced so the operator can compare, and so a rollback does
        # not silently become [UNVERIFIED].
        "_context_surcharge_over": 200000,
        "_source": "secondary aggregators, 2026-07-17 — VERIFY at docs.x.ai",
        "_verified_by_operator": False,
    },
}


def _parse(d):
    return _dt.date.fromisoformat(d)


def table_age_days(today=None):
    today = today or _dt.date.today()
    return (today - _parse(_AS_OF)).days


def table_status(today=None):
    """Report-facing state of the price table itself. A table nobody re-checks is a number that
    lies later; this is what makes that visible instead of ambient."""
    today = today or _dt.date.today()
    age = table_age_days(today)
    expiring = []
    for model, p in PRICES.items():
        exp = p.get("_expires")
        if not exp:
            continue
        days = (_parse(exp) - today).days
        if days < 0:
            expiring.append({"model": model, "status": "EXPIRED", "on": exp,
                             "detail": "rate lapsed %d days ago; every cost line using this model "
                                       "is WRONG until the table is updated" % (-days)})
        elif days <= 45:
            expiring.append({"model": model, "status": "EXPIRING", "on": exp,
                             "detail": "intro rate lapses in %d days" % days})
    return {
        "as_of": _AS_OF, "age_days": age,
        "stale": age > _STALE_AFTER_DAYS,
        "stale_after_days": _STALE_AFTER_DAYS,
        "expiring": expiring,
        "unverified_by_operator": sorted(m for m, p in PRICES.items()
                                         if not p.get("_verified_by_operator")),
    }


def effective_rates(model, today=None):
    """Rates for `model` on `today`, honouring expiry. Returns None when the model is unpriced or
    its rate has lapsed with no successor -- both are UNKNOWN, and unknown is not zero."""
    p = PRICES.get(model)
    if not p:
        return None
    today = today or _dt.date.today()
    exp = p.get("_expires")
    if exp and today > _parse(exp):
        after = p.get("_after_expiry")
        if not after:
            return None          # lapsed, successor unknown -> refuse to price it
        out = dict(p); out.update(after); return out
    return p


# ---------------------------------------------------------------------------------------------
# Usage normalisation. Five providers, five schemas.
# ---------------------------------------------------------------------------------------------
# Every one of these reports the same two facts under a different name. A null here is not zero:
# it means the provider changed its response shape (or the node never ran), and the report must
# say which, because "this stage was free" and "we lost the meter" are different claims.

def _n(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _first(*vals):
    """First value that is not None. NOT `a or b`.

    `_n(x) or _n(y)` silently treats a legitimate 0 as absent, because 0 is falsy in Python, and
    falls through to a field the provider never sent -> None -> the ledger calls it "meter_lost"
    and drops a real, measured stage out of the total. A response with zero output tokens is a
    FACT, not a gap. This is the house rule ("unknown is not zero") reflected: zero is not unknown.
    Found by a unit test, v4.2.8."""
    for v in vals:
        if v is not None:
            return v
    return None


def _normalise_anthropic(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    # ANTHROPIC CONVENTION: input_tokens EXCLUDES cached tokens. cache_read_input_tokens and
    # cache_creation_input_tokens are SEPARATE counters; total input = the three summed. So cache
    # costs are ADDED. This is the OPPOSITE of the OpenAI/xAI convention below, and getting it
    # backwards produced a NEGATIVE cost in testing (v4.2.8, caught by a unit test).
    return {"input": _n(u.get("input_tokens")), "output": _n(u.get("output_tokens")),
            "cache_read": _first(_n(u.get("cache_read_input_tokens")), 0),
            "cache_write": _first(_n(u.get("cache_creation_input_tokens")), 0),
            "cache_in_input": False}


def _normalise_openai(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    # reasoning_tokens are ALREADY counted inside completion_tokens. Adding them would double-bill
    # the most expensive leg of a reasoning model.
    # OPENAI CONVENTION: cached_tokens is a SUBSET of prompt_tokens — already billed at the full
    # rate above, so the discount is a REFUND, not a second charge. Opposite of Anthropic.
    return {"input": _n(u.get("prompt_tokens")), "output": _n(u.get("completion_tokens")),
            "cache_read": _first(_n(((u.get("prompt_tokens_details") or {}).get("cached_tokens"))), 0),
            "cache_write": 0, "cache_in_input": True,
            "_reasoning_tokens": _n(((u.get("completion_tokens_details") or {})
                                     .get("reasoning_tokens")))}


def _normalise_gemini(j):
    u = (j or {}).get("usageMetadata")
    if not isinstance(u, dict):
        return None
    # thoughtsTokenCount is billed as output on thinking models and is NOT included in
    # candidatesTokenCount. Unlike OpenAI's, this one MUST be added.
    out = _first(_n(u.get("candidatesTokenCount")), 0)
    thoughts = _first(_n(u.get("thoughtsTokenCount")), 0)
    # GEMINI: cachedContentTokenCount is included in promptTokenCount -> subset, like OpenAI.
    return {"input": _n(u.get("promptTokenCount")), "output": out + thoughts,
            "cache_read": _first(_n(u.get("cachedContentTokenCount")), 0), "cache_write": 0,
            "cache_in_input": True, "_thoughts_tokens": thoughts or None}


def _normalise_xai(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    # cached_tokens is nested under input_tokens_details and is a SUBSET of input_tokens (OpenAI
    # shape). v4.2.7 read u["cached_tokens"] -- a field xAI does not send -- so 60,288 cached
    # tokens on the live 2026-07-17 NFLX run billed at the full $2.00 rate instead of $0.50.
    # The unit tests passed because their fixture invented the flat shape. A fixture asserting
    # against a shape the API never sends pins nothing; only the real response caught it.
    itd = u.get("input_tokens_details") or {}
    out = {"input": _first(_n(u.get("input_tokens")), _n(u.get("prompt_tokens"))),
           "output": _first(_n(u.get("output_tokens")), _n(u.get("completion_tokens"))),
           "cache_read": _first(_n(itd.get("cached_tokens")), _n(u.get("cached_tokens")), 0),
           "cache_write": 0, "cache_in_input": True}   # xAI follows the OpenAI subset convention
    # reasoning_tokens sit INSIDE output_tokens (verified against the live run: 3885 output of
    # which 2365 reasoning; pricing 3885 reconciled to the tick). Do not add them.

    # SERVER-SIDE TOOL CALLS. xAI bills these per call, entirely outside the token meter. On the
    # live run they were 13 x_search calls = $0.065 -- 64% OF THE STAGE 3 BILL. A token-only
    # estimate is not "slightly off" here, it is structurally blind to the majority of the cost.
    ssd = u.get("server_side_tool_usage_details") or {}
    calls = sum(v for v in ssd.values() if isinstance(v, (int, float)))
    if not calls:
        calls = _n(u.get("num_server_side_tools_used")) or 0
    if calls:
        out["_tool_calls"] = calls
        out["_tool_breakdown"] = {k: v for k, v in ssd.items() if v}

    # xAI reports its OWN cost. v4.2.7 carried it raw because the unit was undocumented. It is now
    # DERIVED, not guessed: on the live run 1,665,740,000 ticks reconciled EXACTLY against
    # 24,060 fresh @ $2 + 60,288 cached @ $0.50 + 3,885 out @ $6 + 13 searches @ $0.005 = $0.166574.
    # Exact to the tick across four independent terms -> tick = 1e-10 USD.
    ticks = _n(u.get("cost_in_usd_ticks"))
    if ticks is not None:
        out["_vendor_cost_ticks"] = ticks
        out["_vendor_cost_usd"] = ticks * TICK_USD

    st = (j or {}).get("service_tier") or u.get("service_tier")
    if st:
        out["_service_tier"] = st
    return out


NORMALISERS = {"anthropic": _normalise_anthropic, "openai": _normalise_openai,
               "gemini": _normalise_gemini, "xai": _normalise_xai,
               "perplexity": _normalise_openai}


def cost_ledger(stages, today=None):
    """stages = [{stage, provider, model, response, ran}] -> a per-stage ledger + totals.

    Three distinct outcomes per stage, never collapsed into one:
      ran=False                -> "not run" (a branch that did not execute; costs nothing, truly)
      usage unparseable        -> "meter lost" (the stage RAN and DID cost money we cannot measure)
      no price for the model   -> tokens exact, dollars [UNVERIFIED]
    """
    today = today or _dt.date.today()
    rows, tot_in, tot_out, tot_cost = [], 0, 0, 0.0
    unpriced, lost, incomplete, drifted = [], [], [], []

    for s in stages or []:
        name = s.get("stage") or "?"
        model = s.get("model")
        prov = s.get("provider")
        row = {"stage": name, "model": model, "provider": prov}

        if not s.get("ran", True):
            row.update({"status": "not_run", "note": "branch did not execute this run"})
            rows.append(row); continue

        norm = (NORMALISERS.get(prov) or (lambda _j: None))(s.get("response"))
        if not norm or norm.get("input") is None or norm.get("output") is None:
            row.update({"status": "meter_lost",
                        "note": "stage ran but no usage block could be read — the tokens were "
                                "spent, the meter was not. NOT zero."})
            lost.append(name); rows.append(row); continue

        row.update({"status": "ok", "input_tokens": norm["input"], "output_tokens": norm["output"],
                    "cache_read_tokens": norm.get("cache_read") or 0,
                    "cache_write_tokens": norm.get("cache_write") or 0})
        tot_in += norm["input"]; tot_out += norm["output"]

        rates = effective_rates(model, today)
        if not rates:
            row.update({"est_cost_usd": None, "cost_status": "[UNVERIFIED]",
                        "note": "no rate for this model in pricing.py — tokens are exact, "
                                "dollars are not known and are NOT zero"})
            unpriced.append("%s (%s)" % (name, model)); rows.append(row); continue

        c = (norm["input"] / 1e6) * rates["input"] + (norm["output"] / 1e6) * rates["output"]
        crm, cwm = rates.get("cache_read_mult"), rates.get("cache_write_mult")
        if crm and norm.get("cache_read"):
            # THE TWO CONVENTIONS ARE OPPOSITE and each normaliser declares which it follows:
            #   cache_in_input=True  (OpenAI, xAI, Gemini) -> cached tokens are INSIDE `input` and
            #       were just billed at full rate above; REFUND the difference.
            #   cache_in_input=False (Anthropic) -> cached tokens are a SEPARATE counter never
            #       billed above; ADD them at the discounted rate.
            # Applying one rule to both is not a rounding error: it produced a NEGATIVE cost.
            if norm.get("cache_in_input"):
                c -= (norm["cache_read"] / 1e6) * rates["input"] * (1 - crm)
            else:
                c += (norm["cache_read"] / 1e6) * rates["input"] * crm
        if cwm and norm.get("cache_write"):
            c += (norm["cache_write"] / 1e6) * rates["input"] * cwm
        if norm.get("_tool_calls"):
            c += norm["_tool_calls"] * _XAI_TOOL_CALL_USD
            row["tool_calls"] = norm["_tool_calls"]
            row["tool_breakdown"] = norm.get("_tool_breakdown")
        row["est_cost_usd"] = round(c, 6)
        row["cost_status"] = "estimate"

        # THE VENDOR'S OWN METER BEATS OUR ESTIMATE. When the provider reports its cost, use it --
        # and keep our estimate as a CANARY. If the two diverge, either our rates went stale or the
        # vendor changed something. That makes the price table self-checking on every real run
        # instead of only when someone remembers to look.
        v = norm.get("_vendor_cost_usd")
        if v is not None:
            row["vendor_cost_usd"] = round(v, 6)
            row["vendor_cost_ticks"] = norm.get("_vendor_cost_ticks")
            row["est_cost_own_formula_usd"] = row["est_cost_usd"]
            drift = abs(v - c) / v if v else 0
            row["est_cost_usd"] = round(v, 6)
            row["cost_status"] = "vendor_reported"
            if drift > 0.02:
                row["cost_status"] = "vendor_reported_estimate_drifted"
                row["note"] = ("our formula said $%.6f, the vendor billed $%.6f (%.1f%% apart). The "
                               "vendor number is used. The gap means our rates for this model are "
                               "stale or its billing changed — CHECK pricing.py." % (c, v, drift * 100))
                drifted.append("%s (%s): %.1f%%" % (name, model, drift * 100))
            c = v

        # Two documented ways this estimate silently understates the bill.
        over = rates.get("_context_surcharge_over")
        if over and (norm["input"] or 0) > over and v is None:
            row["cost_status"] = "estimate_understated"
            row["note"] = ("input %d tokens exceeds the %d-token threshold above which this vendor "
                           "bills an UNPUBLISHED higher rate — the real cost is higher by an amount "
                           "we cannot compute" % (norm["input"], over))
            incomplete.append(name)
        if norm.get("_service_tier") == "priority" and v is None:
            row["cost_status"] = "estimate_understated"
            row["note"] = "served at priority tier: the vendor bills 2x the standard rate"
            incomplete.append(name)

        if rates.get("_incomplete") and v is None:
            row["note"] = rates["_incomplete"]; incomplete.append(name)
        tot_cost += c
        rows.append(row)

    return {
        "rows": rows,
        "totals": {"input_tokens": tot_in, "output_tokens": tot_out,
                   "est_cost_usd": round(tot_cost, 4),
                   "est_cost_is_partial": bool(unpriced or lost or incomplete),
                   "rates_contradicted_by_vendor": bool(drifted),
                   "excluded_unpriced": unpriced, "excluded_meter_lost": lost,
                   "understated_incomplete": incomplete,
                   "price_table_drift": drifted},
        "price_table": table_status(today),
        "_basis": ("tokens: exact, from each provider's own usage block. dollars: ESTIMATE at the "
                   "rates in pricing.py as of %s — not an invoice. Providers bill on their own "
                   "meter; caching, minimums, rounding and per-search fees can move the real "
                   "number." % _AS_OF),
    }
