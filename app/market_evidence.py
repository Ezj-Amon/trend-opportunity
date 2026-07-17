from __future__ import annotations

from typing import Any, Protocol

from .amazon_validation import build_raw_amazon_validation
from .market_validation import (
    MarketValidationInput,
    MarketValidationResult,
    result_from_input,
)


class MarketplaceDataProvider(Protocol):
    async def validate(
        self, hypothesis: dict[str, Any]
    ) -> MarketValidationResult: ...


class SellerCentralCsvProvider:
    """Amazon first-party CSV adapter; raw files are parsed, not persisted."""

    def __init__(self, product_opportunity_csv: str, hot_search_terms_csv: str):
        self.product_opportunity_csv = product_opportunity_csv
        self.hot_search_terms_csv = hot_search_terms_csv

    async def validate(self, hypothesis: dict[str, Any]) -> MarketValidationResult:
        query_terms = hypothesis.get("query_terms") or []
        if not query_terms:
            raise ValueError("product hypothesis has no reviewed marketplace query term")
        value = build_raw_amazon_validation(
            opportunity_id=int(hypothesis["id"]),
            marketplace=str(hypothesis["target_marketplace"]),
            search_term=str(query_terms[0]),
            product_opportunity_csv=self.product_opportunity_csv,
            hot_search_terms_csv=self.hot_search_terms_csv,
        )
        return result_from_input(value)


class ManualMarketplaceDataProvider:
    def __init__(self, value: MarketValidationInput):
        self.value = value

    async def validate(self, hypothesis: dict[str, Any]) -> MarketValidationResult:
        return result_from_input(self.value)
