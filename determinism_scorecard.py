#!/usr/bin/env python3
"""Determinism scorecard — mandatory acceptance gate for every control pair (architect rule,
2026-07-20). Byte-compares the metrics that MUST be deterministic across runs on identical inputs,
and separately lists the metrics that are expected to float until the base-growth anchoring
changeset (P-РЕШЕНО) lands.

Usage:
    python tools/determinism_scorecard.py report_run1.md report_run2.md [report_run3.md ...]

Exit code 0 if the deterministic layer is byte-identical across all given reports; 1 if any
deterministic metric drifts (that is a real bug and blocks package acceptance).

The point is narrow and strict: EDGAR-derived facts, balance-sheet ratios, growth anchors and PEG
come from one deterministic source and cannot legitimately differ between two runs of the same
ticker. If they do, the math is not deterministic and the package must not be accepted. Metrics
downstream of the (currently un-anchored) base growth_rate are listed separately as expected-float
so a reader is never misled into thinking their movement is the same class of problem.
"""
import re
import sys

# Metrics that MUST be byte-identical across runs (deterministic from EDGAR / GROUND_TRUTH).
DETERMINISTIC = [
    "rev_cagr5", "rev_cagr3", "eps_cagr5", "de", "dilution_cagr",
    "sbc_rev",
]

# v4.2.31 (mandate AA.3): market-snapshot class — price/market-dependent metrics that legitimately
# move a little between runs (different snapshot instants). Tolerance <=1% relative; drift within
# tolerance is NOT a defect. These were false-red under the old 2-class scorecard.
MARKET_SNAPSHOT = ["peg", "fwd_pe_vs_sector"]
MARKET_TOLERANCE = 0.01  # 1% relative

# Metrics deterministic once the base-determinism sweep (v4.2.31) has landed: base IV / implied
# cagr / pwfv are now byte-identical (all six base drivers anchored).
DETERMINISTIC_AFTER_SWEEP = ["implied_cagr"]


def extract(text):
    out = {}
    for key in DETERMINISTIC + MARKET_SNAPSHOT + DETERMINISTIC_AFTER_SWEEP:
        m = re.search(r'"%s":\s*(-?[0-9.]+)' % re.escape(key), text)
        out[key] = m.group(1) if m else None
    m = re.search(r"\| NFLX \| ([0-9]+)/100 \| ([0-9.]+)% \| \$([0-9.]+) \| (-?[0-9.]+)%", text)
    if m:
        out["GPS"], out["PWFV"], out["MoS"] = m.group(1), m.group(3), m.group(4)
    return out


def main(paths):
    if len(paths) < 2:
        print("need at least two report paths to compare", file=sys.stderr)
        return 2
    data = {}
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data[p] = extract(f.read())

    print("=== DETERMINISM SCORECARD (3-class) ===")
    print("reports: %s\n" % ", ".join(paths))

    drift = False

    print("CLASS 1 — DETERMINISTIC-FROM-FILINGS (must be byte-identical):")
    for key in DETERMINISTIC + DETERMINISTIC_AFTER_SWEEP:
        vals = [data[p].get(key) for p in paths]
        ok = len(set(vals)) == 1 and vals[0] is not None
        if not ok:
            drift = True
        print("  %-20s %s  %s" % (key, "IDENTICAL ok" if ok else "DRIFT  FAIL",
                                  vals[0] if ok else vals))

    print("\nCLASS 2 — MARKET-SNAPSHOT (tolerance <=1%% relative):")
    for key in MARKET_SNAPSHOT:
        vals = [data[p].get(key) for p in paths]
        nums = [float(v) for v in vals if v is not None]
        if len(nums) == len(paths) and nums and max(nums) > 0:
            rel = (max(nums) - min(nums)) / max(nums)
            within = rel <= MARKET_TOLERANCE
            if not within:
                drift = True
            print("  %-20s %s  rel=%.3f%%  %s" % (
                key, "WITHIN ok" if within else "EXCEEDS FAIL", rel * 100, vals))
        else:
            print("  %-20s (missing)  %s" % (key, vals))

    print("\nCLASS 3 — LLM-BY-DESIGN (informational, bounded by check 27):")
    for key in ("PWFV", "MoS", "GPS"):
        vals = [data[p].get(key) for p in paths]
        if len(set(str(v) for v in vals)) > 1:
            print("  %-20s varies: %s" % (key, vals))

    print()
    if drift:
        print("RESULT: a deterministic or market-snapshot metric is out of bounds — "
              "package must NOT be accepted.")
        return 1
    print("RESULT: deterministic layer byte-identical; market-snapshot within 1%. Clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
