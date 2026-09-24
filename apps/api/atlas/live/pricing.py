"""Token prices and cost ESTIMATES (USD per million tokens).

The numbers are estimates for the Command Center's cost meter, not billing data. Defaults by model
class (matched by substring of the model id):

    opus-class    $5 in / $25 out
    sonnet-class  $3 in / $15 out     (also the fallback for unknown models)
    haiku-class   $1 in / $5 out

Cache reads cost 10% of the input price and cache writes (5-minute ephemeral) 125%.

Override with `ATLAS_PRICES` (JSON). Keys are an exact model id or a class name (`opus`, `sonnet`,
`haiku`); values are `{"input": x, "output": y, "cache_read"?: z, "cache_write"?: w}` or `[x, y]`:

    ATLAS_PRICES='{"claude-sonnet-5": {"input": 3, "output": 15}, "haiku": [0.8, 4]}'
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("atlas.live")

CACHE_READ_FACTOR = 0.10
CACHE_WRITE_FACTOR = 1.25


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float

    @classmethod
    def of(cls, input: float, output: float, cache_read: float | None = None,
           cache_write: float | None = None) -> Price:
        return cls(
            float(input),
            float(output),
            float(cache_read if cache_read is not None else input * CACHE_READ_FACTOR),
            float(cache_write if cache_write is not None else input * CACHE_WRITE_FACTOR),
        )


DEFAULT_PRICES: dict[str, Price] = {
    "opus": Price.of(5, 25),
    "sonnet": Price.of(3, 15),
    "haiku": Price.of(1, 5),
}
FALLBACK_CLASS = "sonnet"


def _parse(raw: str | None) -> dict[str, Price]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out: dict[str, Price] = {}
        for key, val in data.items():
            if isinstance(val, (list, tuple)):
                out[key] = Price.of(*val)
            else:
                out[key] = Price.of(
                    val["input"], val["output"], val.get("cache_read"), val.get("cache_write")
                )
        return out
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("ignoring invalid ATLAS_PRICES: %s", exc)
        return {}


class PriceTable:
    def __init__(self, overrides: dict[str, Price] | None = None):
        self.prices = {**DEFAULT_PRICES, **(overrides or {})}

    @classmethod
    def from_env(cls) -> PriceTable:
        return cls(_parse(os.getenv("ATLAS_PRICES")))

    def price_for(self, model: str) -> Price:
        if model in self.prices:
            return self.prices[model]
        lower = model.lower()
        for cls_name in ("opus", "sonnet", "haiku"):
            if cls_name in lower and cls_name in self.prices:
                return self.prices[cls_name]
        return self.prices[FALLBACK_CLASS]

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Estimated USD cost. `input_tokens` excludes cache reads and cache writes."""
        p = self.price_for(model)
        total = (
            input_tokens * p.input
            + output_tokens * p.output
            + cache_read_tokens * p.cache_read
            + cache_write_tokens * p.cache_write
        )
        return total / 1_000_000
