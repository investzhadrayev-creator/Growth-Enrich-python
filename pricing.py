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

PRICES = {
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


def _normalise_anthropic(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    return {"input": _n(u.get("input_tokens")), "output": _n(u.get("output_tokens")),
            "cache_read": _n(u.get("cache_read_input_tokens")) or 0,
            "cache_write": _n(u.get("cache_creation_input_tokens")) or 0}


def _normalise_openai(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    # reasoning_tokens are ALREADY counted inside completion_tokens. Adding them would double-bill
    # the most expensive leg of a reasoning model.
    return {"input": _n(u.get("prompt_tokens")), "output": _n(u.get("completion_tokens")),
            "cache_read": _n(((u.get("prompt_tokens_details") or {}).get("cached_tokens"))) or 0,
            "cache_write": 0,
            "_reasoning_tokens": _n(((u.get("completion_tokens_details") or {})
                                     .get("reasoning_tokens")))}


def _normalise_gemini(j):
    u = (j or {}).get("usageMetadata")
    if not isinstance(u, dict):
        return None
    # thoughtsTokenCount is billed as output on thinking models and is NOT included in
    # candidatesTokenCount. Unlike OpenAI's, this one MUST be added.
    out = _n(u.get("candidatesTokenCount")) or 0
    thoughts = _n(u.get("thoughtsTokenCount")) or 0
    return {"input": _n(u.get("promptTokenCount")), "output": out + thoughts,
            "cache_read": _n(u.get("cachedContentTokenCount")) or 0, "cache_write": 0,
            "_thoughts_tokens": thoughts or None}


def _normalise_xai(j):
    u = (j or {}).get("usage")
    if not isinstance(u, dict):
        return None
    # /v1/responses shape; falls back to the chat/completions names if x.ai returns those.
    out = {"input": _n(u.get("input_tokens")) or _n(u.get("prompt_tokens")),
           "output": _n(u.get("output_tokens")) or _n(u.get("completion_tokens")),
           "cache_read": _n(u.get("cached_tokens")) or 0, "cache_write": 0}
    # xAI is the ONLY provider here that returns its own cost figure. That beats any estimate of
    # ours -- but the unit ("ticks") is not documented anywhere we could reach, so converting it to
    # dollars would be a guess wearing a vendor's authority, which is worse than our honest
    # estimate. Carry it RAW, unconverted, and let the operator reconcile it against the xAI
    # console once. After that the conversion can be pinned by a test instead of assumed.
    ticks = _n(u.get("cost_in_usd_ticks"))
    if ticks is not None:
        out["_vendor_cost_ticks"] = ticks
    # Priority Processing bills at 2x. The response declares it; we must not silently price a
    # priority request at the standard rate.
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
    unpriced, lost, incomplete = [], [], []

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
            c += (norm["cache_read"] / 1e6) * rates["input"] * crm
        if cwm and norm.get("cache_write"):
            c += (norm["cache_write"] / 1e6) * rates["input"] * cwm
        row["est_cost_usd"] = round(c, 6)
        row["cost_status"] = "estimate"

        # Carry the vendor's OWN cost number through untouched when it exists (xAI). Not converted:
        # the unit is undocumented. It is here so the operator can reconcile one run against the
        # provider console and then pin the conversion, instead of us inventing it now.
        if norm.get("_vendor_cost_ticks") is not None:
            row["vendor_cost_ticks"] = norm["_vendor_cost_ticks"]
            row["_vendor_note"] = ("provider reported its own cost as %s ticks; the tick->USD unit "
                                   "is undocumented, so it is NOT converted. Reconcile against the "
                                   "vendor console." % norm["_vendor_cost_ticks"])

        # Two documented ways this estimate silently understates the bill.
        over = rates.get("_context_surcharge_over")
        if over and (norm["input"] or 0) > over:
            row["cost_status"] = "estimate_understated"
            row["note"] = ("input %d tokens exceeds the %d-token threshold above which this vendor "
                           "bills an UNPUBLISHED higher rate — the real cost is higher by an amount "
                           "we cannot compute" % (norm["input"], over))
            incomplete.append(name)
        if norm.get("_service_tier") == "priority":
            row["cost_status"] = "estimate_understated"
            row["note"] = "served at priority tier: the vendor bills 2x the standard rate"
            incomplete.append(name)

        if rates.get("_incomplete"):
            row["note"] = rates["_incomplete"]; incomplete.append(name)
        tot_cost += c
        rows.append(row)

    return {
        "rows": rows,
        "totals": {"input_tokens": tot_in, "output_tokens": tot_out,
                   "est_cost_usd": round(tot_cost, 4),
                   "est_cost_is_partial": bool(unpriced or lost or incomplete),
                   "excluded_unpriced": unpriced, "excluded_meter_lost": lost,
                   "understated_incomplete": incomplete},
        "price_table": table_status(today),
        "_basis": ("tokens: exact, from each provider's own usage block. dollars: ESTIMATE at the "
                   "rates in pricing.py as of %s — not an invoice. Providers bill on their own "
                   "meter; caching, minimums, rounding and per-search fees can move the real "
                   "number." % _AS_OF),
    }
