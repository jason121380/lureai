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


if __name__ == "__main__":
    unittest.main()
