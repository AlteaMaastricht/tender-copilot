"""Price table and cost calculation. Prices are USD per million tokens.

Every row records where the price was read and on which date. A model without a row cannot be
called through the gateway, so no call can escape the daily cost cap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from llm.providers.base import Usage

_MILLION = Decimal(1_000_000)
_CENT_PARTS = Decimal("0.000001")  # llm_calls.cost_usd is numeric(10,6)

ANTHROPIC_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True, slots=True)
class Rates:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # 5-minute cache write


@dataclass(frozen=True, slots=True)
class ModelPrice:
    provider: str
    rates: Rates
    source: str
    as_of: date
    # Some models charge more for a long prompt. The whole request then uses `long_rates`.
    long_prompt_over: int | None = None
    long_rates: Rates | None = None

    def cost(self, usage: Usage) -> Decimal:
        """Cost of one request. Priced per request, because the long-prompt rule is."""
        rates = self.rates
        if self.long_prompt_over is not None and usage.prompt_tokens > self.long_prompt_over:
            assert self.long_rates is not None
            rates = self.long_rates
        total = (
            usage.input_tokens * rates.input
            + usage.cache_read_tokens * rates.cache_read
            + usage.cache_write_tokens * rates.cache_write
            + usage.output_tokens * rates.output
        ) / _MILLION
        return total.quantize(_CENT_PARTS, rounding=ROUND_HALF_UP)


def _rates(input_: str, output: str, cache_read: str, cache_write: str) -> Rates:
    return Rates(Decimal(input_), Decimal(output), Decimal(cache_read), Decimal(cache_write))


PRICES: Mapping[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        provider="anthropic",
        rates=_rates("0.10", "0.50", "0.01", "0.125"),
        long_prompt_over=100_000,  # counts cache reads and writes
        long_rates=_rates("0.50", "2.50", "0.05", "0.625"),
        source=ANTHROPIC_PRICING,
        as_of=date(2026, 11, 14),
    ),
    "claude-sonnet-5-5": ModelPrice(
        provider="anthropic",
        rates=_rates("2.00", "10.00", "0.10", "2.50"),
        source=ANTHROPIC_PRICING,
        as_of=date(2026, 11, 14),
    ),
}

# Mistral: read on 14 Nov 2026 but not confirmed, so not callable yet (DECISIONS.md Q4).
#   Mistral Large 4: 0.50 in / 1.50 out, https://mistral.ai/pricing. API model ID not found.
#   Mistral Small 4: 0.15 / 0.60, https://docs.mistral.ai/models/mistral-small-4-0-26-03.
#     The page does not say which figure is input; the ID is shown as "mistral-small-2603+1".
# Add a row to PRICES only after checking the ID and prices against the live API.
