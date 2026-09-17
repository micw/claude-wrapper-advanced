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


if __name__ == "__main__":
    unittest.main()
