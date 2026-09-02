import unittest

from app.usage import UsagePricing


class UsagePricingTests(unittest.TestCase):
    def test_converts_model_tokens_to_twd(self):
        pricing = UsagePricing(
            input_usd_per_million=0.20,
            cached_input_usd_per_million=0.02,
            cache_write_usd_per_million=0.25,
            output_usd_per_million=1.20,
            usd_to_twd=32.5,
            monthly_budget_twd=1000,
        )

        cost = pricing.cost_twd(
            input_tokens=3_000_000,
            output_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            cache_write_input_tokens=1_000_000,
        )

        self.assertAlmostEqual(cost, 54.275)

    def test_summary_caps_progress_at_one_hundred_percent(self):
        pricing = UsagePricing(monthly_budget_twd=100)

        summary = pricing.summary(
            input_tokens=800,
            cached_input_tokens=100,
            cache_write_input_tokens=50,
            output_tokens=300,
            spend_twd=125.4,
            month="2026-08",
        )

        self.assertEqual(summary["month"], "2026-08")
        self.assertEqual(summary["spend_twd"], 125.4)
        self.assertEqual(summary["budget_twd"], 100.0)
        self.assertEqual(summary["progress_percent"], 100.0)
        self.assertEqual(summary["input_tokens"], 800)
        self.assertEqual(summary["cached_input_tokens"], 100)
        self.assertEqual(summary["cache_write_input_tokens"], 50)
        self.assertEqual(summary["output_tokens"], 300)


class BudgetLedgerTests(unittest.TestCase):
    """先預留、記完帳再釋放。不預留的話併發請求會一起穿過上限。"""

    def setUp(self):
        from app.server import BudgetLedger

        self.ledger = BudgetLedger()

    def test_a_reserved_amount_counts_against_the_budget_until_it_is_released(self):
        self.assertEqual(self.ledger.pending_for(1), 0.0)
        first = self.ledger.reserve(1, 0.65)
        second = self.ledger.reserve(1, 0.65)
        self.assertAlmostEqual(self.ledger.pending_for(1), 1.30)
        # 別人的帳不受影響。
        self.assertEqual(self.ledger.pending_for(2), 0.0)
        self.ledger.release(first)
        self.assertAlmostEqual(self.ledger.pending_for(1), 0.65)
        self.ledger.release(second)
        self.assertEqual(self.ledger.pending_for(1), 0.0)

    def test_releasing_twice_or_releasing_nothing_is_harmless(self):
        token = self.ledger.reserve(1, 0.5)
        self.ledger.release(token)
        self.ledger.release(token)
        self.ledger.release(None)
        self.assertEqual(self.ledger.pending_for(1), 0.0)

    def test_concurrent_reservations_are_all_counted(self):
        import threading

        start = threading.Barrier(8)
        tokens = []
        lock = threading.Lock()

        def reserve():
            start.wait()
            token = self.ledger.reserve(1, 0.25)
            with lock:
                tokens.append(token)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertEqual(len(tokens), 8)
        self.assertAlmostEqual(self.ledger.pending_for(1), 2.0)

    def test_the_typical_cost_tracks_the_configured_rates(self):
        from app.usage import UsagePricing

        cheap = UsagePricing(input_usd_per_million=0.1, output_usd_per_million=0.1)
        pricey = UsagePricing(input_usd_per_million=10.0, output_usd_per_million=10.0)

        self.assertGreater(pricey.typical_cost_twd(), cheap.typical_cost_twd())
        self.assertGreater(cheap.typical_cost_twd(), 0)


if __name__ == "__main__":
    unittest.main()
