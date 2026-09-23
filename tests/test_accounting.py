import unittest

from vampire_agent.accounting import Accounting


class AccountingTests(unittest.TestCase):
    def test_known_and_unknown_usage_remain_separate(self):
        accounting = Accounting()
        accounting.requests = 2
        accounting.applied_actions = 1
        accounting.add_usage({"input_tokens": 1_000_000, "output_tokens": 1000}, 120)
        accounting.add_usage(None)
        result = accounting.snapshot()
        self.assertEqual(result["estimated_cost_usd"], .042)
        self.assertEqual(result["input_tokens"], 1_000_000)
        self.assertEqual(result["output_tokens"], 1000)
        self.assertEqual(result["unknown_usage_requests"], 1)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["applied_actions"], 1)
        self.assertEqual(accounting.latencies, [120])

    def test_empty_accounting_does_not_claim_unknown_usage(self):
        result = Accounting().snapshot()
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["estimated_cost_usd"], 0)
        self.assertEqual(result["unknown_usage_requests"], 0)


if __name__ == "__main__":
    unittest.main()
