from __future__ import annotations

import re


AMAZON_MARKETPLACES = {
    "US": "Amazon.com",
    "GB": "Amazon.co.uk",
    "DE": "Amazon.de",
    "JP": "Amazon.co.jp",
    "CA": "Amazon.ca",
    "FR": "Amazon.fr",
    "IT": "Amazon.it",
    "ES": "Amazon.es",
}

MARKETPLACE_ALIASES = {
    **{code: code for code in AMAZON_MARKETPLACES},
    **{name.casefold(): code for code, name in AMAZON_MARKETPLACES.items()},
    "uk": "GB",
    "amazon uk": "GB",
    "amazon us": "US",
    "amazon japan": "JP",
}


def marketplace_code(value: str | None, default: str = "US") -> str:
    """Resolve an Amazon marketplace code without confusing it with signal origin."""
    candidate = (value or "").strip()
    if not candidate:
        return default.upper()
    return MARKETPLACE_ALIASES.get(candidate.casefold(), candidate.upper())


def marketplace_name(code: str) -> str:
    normalized = marketplace_code(code)
    return AMAZON_MARKETPLACES.get(normalized, f"Amazon {normalized}")


def is_supported_marketplace(code: str) -> bool:
    return marketplace_code(code) in AMAZON_MARKETPLACES


GENERIC_SEARCH_TERMS = {
    "ai",
    "accessory",
    "accessories",
    "bundle",
    "device",
    "kit",
    "product",
    "products",
    "solution",
    "technology",
}


def normalize_search_term(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def is_search_term_ready(value: str | None, marketplace: str = "US") -> bool:
    """Require a usable buyer-facing query before asking a human to search Seller Central."""
    term = normalize_search_term(value)
    if len(term) < 3 or len(term) > 120:
        return False
    if marketplace_code(marketplace) == "US" and not term.isascii():
        return False
    tokens = re.findall(r"[a-z0-9]+", term.casefold())
    if not tokens or len(tokens) > 10:
        return False
    informative = [token for token in tokens if token not in GENERIC_SEARCH_TERMS]
    return bool(informative) and not (len(tokens) == 1 and len(tokens[0]) < 4)


def pick_search_term(
    explicit: str | None,
    keywords: list[str] | tuple[str, ...],
    marketplace: str = "US",
) -> str:
    candidates = [explicit or "", *keywords]
    for candidate in candidates:
        normalized = normalize_search_term(candidate)
        if is_search_term_ready(normalized, marketplace):
            return normalized
    return ""
