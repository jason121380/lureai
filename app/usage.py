import os
from dataclasses import dataclass


@dataclass(frozen=True)
class UsagePricing:
    input_usd_per_million: float = 0.20
    cached_input_usd_per_million: float = 0.02
    cache_write_usd_per_million: float = 0.25
    output_usd_per_million: float = 1.20
    usd_to_twd: float = 32.5
    monthly_budget_twd: float = 1000.0

    @classmethod
    def from_env(cls) -> "UsagePricing":
        return cls(
            input_usd_per_million=float(os.getenv("LLM_INPUT_USD_PER_MILLION", "0.20")),
            cached_input_usd_per_million=float(os.getenv("LLM_CACHED_INPUT_USD_PER_MILLION", "0.02")),
            cache_write_usd_per_million=float(os.getenv("LLM_CACHE_WRITE_USD_PER_MILLION", "0.25")),
            output_usd_per_million=float(os.getenv("LLM_OUTPUT_USD_PER_MILLION", "1.20")),
            usd_to_twd=float(os.getenv("USD_TO_TWD", "32.5")),
            monthly_budget_twd=float(os.getenv("MONTHLY_BUDGET_TWD", "1000")),
        )

    def cost_twd(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
    ) -> float:
        total_input = max(0, int(input_tokens))
        cached = min(total_input, max(0, int(cached_input_tokens)))
        cache_write = min(total_input - cached, max(0, int(cache_write_input_tokens)))
        uncached = total_input - cached - cache_write
        usd = (
            uncached * self.input_usd_per_million
            + cached * self.cached_input_usd_per_million
            + cache_write * self.cache_write_usd_per_million
            + max(0, int(output_tokens)) * self.output_usd_per_million
        ) / 1_000_000
        return usd * self.usd_to_twd

    def summary(
        self,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_input_tokens: int,
        output_tokens: int,
        spend_twd: float,
        month: str,
    ) -> dict:
        budget = max(0.0, float(self.monthly_budget_twd))
        spend = max(0.0, float(spend_twd))
        percent = 0.0 if budget <= 0 else min(100.0, spend / budget * 100)
        return {
            "month": month,
            "input_tokens": max(0, int(input_tokens)),
            "cached_input_tokens": max(0, int(cached_input_tokens)),
            "cache_write_input_tokens": max(0, int(cache_write_input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "total_tokens": max(0, int(input_tokens)) + max(0, int(output_tokens)),
            "spend_twd": round(spend, 4),
            "budget_twd": round(budget, 2),
            "progress_percent": round(percent, 2),
            "pricing": {
                "input_usd_per_million": self.input_usd_per_million,
                "cached_input_usd_per_million": self.cached_input_usd_per_million,
                "cache_write_usd_per_million": self.cache_write_usd_per_million,
                "output_usd_per_million": self.output_usd_per_million,
                "usd_to_twd": self.usd_to_twd,
            },
        }
