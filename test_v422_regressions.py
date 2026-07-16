"""Regressions for the four defects the NFLX 2026-07-16 run exposed.

Every test here names a real failure. None of them is hypothetical.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "microservice"))
import app                      # noqa: E402
import ivc_lib                  # noqa: E402
import finra_short_interest     # noqa: E402


class TestUnknownIsNotZero(unittest.TestCase):
    """A missing input is UNKNOWN. Scoring it 0 turns a data gap into a business judgment."""

    def test_unconfirmed_split_withholds_eps_score_instead_of_scoring_zero(self):
        gt = {"revenue": [{"end": "%d-12-31" % y, "val": v} for y, v in
                          [(2020, 24996e6), (2021, 29698e6), (2022, 31616e6),
                           (2023, 33723e6), (2024, 39001e6), (2025, 45180e6)]],
              "eps_cagr_5y": -0.16096, "eps_series_unreliable": True}
        a = ivc_lib.gps_quant(gt)["detail"]["A"]
        self.assertEqual(a["pts"]["eps"], "[UNVERIFIED]")
        self.assertEqual(a["max_quant"], 10, "the 6 eps points must leave the DENOMINATOR too")
        self.assertIn("split", a["eps_unverified_reason"])

    def test_unreliable_series_is_not_silently_recomputed_from_the_poisoned_series(self):
        """gps_quant used to fall back to _cagr(eps_series_obj) when eps_cagr_5y was null --
        which is the exact corrupted series we just refused to trust."""
        series = [{"end": "2020-12-31", "val": 6.08}, {"end": "2025-12-31", "val": 2.53}]
        gt = {"eps_cagr_5y": None, "eps_series_obj": series, "eps_series_unreliable": True}
        self.assertIsNone(ivc_lib.gps_quant(gt)["detail"]["A"]["eps_cagr5"])

    def test_null_dilution_does_not_score_zero(self):
        """NFLX was SHRINKING its share count (buybacks at 96.5% of FCF) and scored 0/4 for
        dilution because the clamp nulled the field. Sign inverted by a data gap."""
        d = ivc_lib.gps_quant({"debt_to_equity": 0.5, "dilution_cagr": None,
                               "sbc_to_revenue": 0.008})["detail"]["D"]
        self.assertEqual(d["pts"]["shares"], "[UNVERIFIED]")
        self.assertEqual(d["max"], 6)

    def test_null_valuation_inputs_do_not_read_as_expensive(self):
        """Stage 2b diagnosed this in prose: 'structurally starved of forward-PE data, not a
        judgment that valuation is cheap.'"""
        c = ivc_lib.gps_quant({"peg": 1.45, "fwd_pe_vs_sector": None,
                               "implied_cagr_base": None})["detail"]["C"]
        self.assertEqual(c["pts"]["fwd_pe"], "[UNVERIFIED]")
        self.assertEqual(c["max"], 5, "only PEG was measurable")

    def test_present_inputs_still_score_exactly_as_before(self):
        """The rule changed only for MISSING inputs. Real scores must not move."""
        c = ivc_lib.gps_quant({"peg": 1.45, "fwd_pe_vs_sector": 1.0,
                               "implied_cagr_base": 0.15})["detail"]["C"]
        self.assertEqual(c["pts"], {"peg": 4, "fwd_pe": 3, "icagr": 4})
        self.assertEqual(c["max"], 15)


class TestPeerBasisNeverAnchorsForwardCap(unittest.TestCase):
    def test_trailing_peer_median_is_excluded(self):
        d = {"peer_median_pe": 95.09, "peer_median_pe_basis": "edgar_tiingo_trailing_inhouse"}
        self.assertIsNone(app._pe_anchor_fwd(d))
        self.assertTrue(app._peer_pe_excluded(d))

    def test_forward_peer_median_is_still_used(self):
        d = {"peer_median_pe": 30.0, "peer_median_pe_basis": "yahoo_forward"}
        self.assertEqual(app._pe_anchor_fwd(d), 30.0)
        self.assertFalse(app._peer_pe_excluded(d))

    def test_unlabelled_peer_median_is_trusted_as_forward(self):
        """Legacy yahoo path sets no basis; it IS forward. Do not silently discard it."""
        self.assertEqual(app._pe_anchor_fwd({"peer_median_pe": 28.0}), 28.0)


class TestFinraStaleness(unittest.TestCase):
    def test_query_is_bounded_by_a_settlement_date_window(self):
        """A client-side sort cannot repair a server-side truncation: the old body asked for
        60 rows with no date bound, so we sorted the wrong 60 and returned 2022-11-30."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "microservice",
                                "finra_short_interest.py"), encoding="utf-8").read()
        self.assertIn("dateRangeFilters", src)
        self.assertIn("settlementDate", src.split("dateRangeFilters")[1][:200])

    def test_stale_settlement_is_refused_not_reported(self):
        calls = {}

        def fake_post(url, headers, body):
            calls["body"] = body
            return [{"settlementDate": "2022-11-30", "currentShortPositionQuantity": 10480000,
                     "previousShortPositionQuantity": 10890000, "daysToCoverQuantity": 1.39}]

        errs = {}
        orig_post, orig_tok = finra_short_interest._post_json, finra_short_interest._get_token
        finra_short_interest._post_json = fake_post
        finra_short_interest._get_token = lambda *a, **k: "tok"
        try:
            r = finra_short_interest.finra_short_interest("NFLX", "id", "secret", errs)
        finally:
            finra_short_interest._post_json, finra_short_interest._get_token = orig_post, orig_tok
        self.assertIsNone(r, "a 3.5-year-old settlement must never travel as a current fact")
        self.assertIn("2022-11-30", errs["finra_short"])

    def test_fresh_settlement_still_returns(self):
        import time
        fresh = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10 * 86400))
        orig_post, orig_tok = finra_short_interest._post_json, finra_short_interest._get_token
        finra_short_interest._post_json = lambda u, h, b: [
            {"settlementDate": fresh, "currentShortPositionQuantity": 50000000,
             "previousShortPositionQuantity": 48000000, "daysToCoverQuantity": 2.1}]
        finra_short_interest._get_token = lambda *a, **k: "tok"
        try:
            r = finra_short_interest.finra_short_interest("NFLX", "id", "secret", {})
        finally:
            finra_short_interest._post_json, finra_short_interest._get_token = orig_post, orig_tok
        self.assertIsNotNone(r)
        self.assertEqual(r["settlement_date"], fresh)


class TestSplitConfirmationReadsBothEndpoints(unittest.TestCase):
    def test_detector_merges_companyconcept(self):
        """companyfacts can keep one value per period; a split confirmation IS 'one period,
        two values'. A deduped source cannot confirm a split, by construction."""
        import edgar_facts
        facts = {"facts": {"us-gaap": {"WeightedAverageNumberOfDilutedSharesOutstanding": {
            "units": {"shares": [{"start": "2023-01-01", "end": "2023-12-31", "val": 449497000,
                                  "form": "10-K", "filed": "2024-01-25", "accn": "a1"}]}}}}}
        concept_only = {"shares": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 449497000,
             "form": "10-K", "filed": "2024-01-25", "accn": "a1"},
            {"start": "2023-01-01", "end": "2023-12-31", "val": 4494970000,
             "form": "10-K", "filed": "2026-01-27", "accn": "a2"}]}
        orig = edgar_facts._companyconcept
        edgar_facts._companyconcept = lambda cik, tax, tag: concept_only
        try:
            without = edgar_facts._detect_confirmed_splits(
                facts, ["WeightedAverageNumberOfDilutedSharesOutstanding"])
            with_cc = edgar_facts._detect_confirmed_splits(
                facts, ["WeightedAverageNumberOfDilutedSharesOutstanding"], cik="0001065280")
        finally:
            edgar_facts._companyconcept = orig
        self.assertEqual(without, [], "companyfacts alone cannot see the restatement")
        self.assertEqual(len(with_cc), 1)
        self.assertEqual(with_cc[0]["factor"], 10)
        self.assertEqual(with_cc[0]["end"], "2023-12-31")


if __name__ == "__main__":
    unittest.main(verbosity=2)
