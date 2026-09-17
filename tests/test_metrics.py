"""Prometheus subscription metrics stay aligned with provider usage semantics."""
import unittest

from app.metrics import Metrics


class PrometheusMetrics(unittest.TestCase):
    def test_tokens_are_cumulative_and_split_by_model(self):
        metrics = Metrics(10)
        metrics.start()
        metrics.end(
            "success",
            model='sonnet-5"quoted',
            reasoning_tokens=7,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {
                    "cached_tokens": 60,
                    "cache_write_tokens": 10,
                },
            },
        )

        text = metrics.prometheus({"groups": []})
        self.assertIn('model="sonnet-5\\"quoted",provider="claude",type="input_uncached"} 30', text)
        self.assertIn('model="sonnet-5\\"quoted",provider="claude",type="cache_read"} 60', text)
        self.assertIn('model="sonnet-5\\"quoted",provider="claude",type="cache_write"} 10', text)
        self.assertIn('model="sonnet-5\\"quoted",provider="claude",type="output"} 20', text)
        self.assertIn('model="sonnet-5\\"quoted",provider="claude",type="reasoning"} 7', text)

    def test_quota_uses_ratio_and_omits_unknown_windows(self):
        metrics = Metrics(10)
        text = metrics.prometheus({"groups": [{
            "id": "global",
            "observed_at": 123,
            "windows": [
                {"id": "five_hour", "used_percent": 12.0, "resets_at": 456,
                 "window_seconds": 18000},
                {"id": "seven_day", "used_percent": None, "resets_at": None,
                 "window_seconds": 604800},
            ],
        }]})

        labels = 'group="global",provider="claude",window="five_hour"'
        self.assertIn(f"subscription_wrapper_quota_used_ratio{{{labels}}} 0.12", text)
        self.assertIn(f"subscription_wrapper_quota_reset_timestamp_seconds{{{labels}}} 456", text)
        self.assertNotIn('subscription_wrapper_quota_used_ratio{group="global",provider="claude",window="seven_day"}', text)

    def test_nominal_cost_preserves_call_turn_and_unattributed_coverage(self):
        metrics = Metrics(10)
        for cost, scope, covered in ((0.25, "call", 1), (0.4, "turn", 3),
                                     (0.1, None, None), (float("nan"), "call", 1)):
            metrics.start()
            metrics.end("success", cost_usd=cost, cost_scope=scope,
                        cost_covered_requests=covered)

        snapshot = metrics.snapshot()["cost"]
        self.assertEqual(snapshot["kind"], "nominal_api_list_price")
        self.assertAlmostEqual(snapshot["total_usd"], 0.75)
        self.assertEqual(snapshot["observations"], {"call": 1, "turn": 1, "unattributed": 1})
        self.assertEqual(snapshot["covered_requests"], {"call": 1, "turn": 3})

        text = metrics.prometheus({"groups": []})
        self.assertIn('provider="claude",scope="call"} 0.25', text)
        self.assertIn('provider="claude",scope="turn"} 0.4', text)
        self.assertIn('provider="claude",scope="unattributed"} 0.1', text)
        self.assertIn('subscription_wrapper_cost_covered_requests_total{provider="claude",scope="turn"} 3', text)
        self.assertNotIn('subscription_wrapper_cost_covered_requests_total{provider="claude",scope="unattributed"}', text)


if __name__ == "__main__":
    unittest.main()
