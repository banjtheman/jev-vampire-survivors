"""Known-usage accounting shared with the original Jev gameplay harness.

Only validated API usage reaches this class. Missing usage remains explicitly
unknown; estimated cost does not imply a final provider invoice.
"""
PRICE_PER_MILLION = 0.042  # https://docs.typesafe.ai/models, checked 2026-09-22


class Accounting:
    def __init__(self):
        self.requests = 0
        self.applied_actions = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_usage_requests = 0
        self.latencies = []

    def add_usage(self, usage, latency=None):
        if usage is None:
            self.unknown_usage_requests += 1
        else:
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
        if latency is not None:
            self.latencies.append(latency)

    def snapshot(self):
        return {
            "requests": self.requests, "applied_actions": self.applied_actions,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.input_tokens / 1_000_000 * PRICE_PER_MILLION,
            "unknown_usage_requests": self.unknown_usage_requests,
        }
