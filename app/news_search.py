from __future__ import annotations

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

import httpx
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder

from .evidence_quality import (
    canonicalize_public_url,
    content_relevance_score,
    registrable_domain,
)

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True, slots=True)
class NewsSearchHit:
    url: str
    title: str
    snippet: str
    source_name: str
    published_at: str | None
    provider: str
    rank: int
    query_hash: str
    provider_url: str = ""


class PublicNewsSearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        market: str,
        language: str,
        limit: int,
    ) -> list[NewsSearchHit]: ...


UrlDecoder = Callable[[str], Awaitable[str | None]]


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ")).strip()


def _parse_published(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.isoformat()


async def decode_google_news_url(url: str) -> str | None:
    def decode() -> str | None:
        result = gnewsdecoder(url, interval=None)
        if not isinstance(result, dict) or not result.get("status"):
            return None
        decoded = str(result.get("decoded_url") or "").strip()
        return decoded or None

    try:
        return await asyncio.wait_for(asyncio.to_thread(decode), timeout=15)
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError):
        return None


_GOOGLE_LOCALES = {
    "CN": ("zh-CN", "CN", "CN:zh-Hans"),
    "US": ("en-US", "US", "US:en"),
    "GB": ("en-GB", "GB", "GB:en"),
    "DE": ("de", "DE", "DE:de"),
    "JP": ("ja", "JP", "JP:ja"),
    "CA": ("en-CA", "CA", "CA:en"),
    "FR": ("fr", "FR", "FR:fr"),
    "IT": ("it", "IT", "IT:it"),
    "ES": ("es", "ES", "ES:es"),
}


class GoogleNewsRssSearchProvider:
    name = "google-news-rss"

    def __init__(
        self,
        *,
        decoder: UrlDecoder = decode_google_news_url,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.decoder = decoder
        self.transport = transport
        self._decoded_cache: dict[str, str | None] = {}

    async def search(
        self,
        query: str,
        *,
        market: str,
        language: str,
        limit: int,
    ) -> list[NewsSearchHit]:
        del language
        hl, gl, ceid = _GOOGLE_LOCALES.get(
            market.upper(), _GOOGLE_LOCALES["US"]
        )
        effective_query = query.strip()
        if not re.search(r"\b(?:when|after|before):", effective_query, re.I):
            effective_query = f"{effective_query} when:7d"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5),
            transport=self.transport,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TrendOpportunityLab/0.4; research)",
                "Accept": "application/rss+xml,application/xml,text/xml",
            },
        ) as client:
            response = await client.get(
                "https://news.google.com/rss/search",
                params={"q": effective_query, "hl": hl, "gl": gl, "ceid": ceid},
            )
            response.raise_for_status()
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ValueError("Google News RSS returned invalid XML") from exc

        unresolved: list[tuple[str, str, str, str, str | None, int]] = []
        used_publishers: set[str] = set()
        for rank, item in enumerate(root.findall("./channel/item"), 1):
            provider_url = str(item.findtext("link") or "").strip()
            title = str(item.findtext("title") or "").strip()
            source = str(item.findtext("source") or "").strip()
            snippet = _clean_html(str(item.findtext("description") or ""))
            if not provider_url or not title:
                continue
            display_title = title
            if source and display_title.endswith(f" - {source}"):
                display_title = display_title[: -(len(source) + 3)].strip()
            if content_relevance_score(query, f"{display_title} {snippet}") < 0.2:
                continue
            publisher_key = source.casefold() or display_title.casefold()
            if publisher_key in used_publishers:
                continue
            used_publishers.add(publisher_key)
            unresolved.append(
                (
                    provider_url,
                    display_title,
                    snippet,
                    source,
                    _parse_published(item.findtext("pubDate")),
                    rank,
                )
            )
            if len(unresolved) >= max(limit * 2, limit):
                break

        hits: list[NewsSearchHit] = []
        for provider_url, title, snippet, source, published_at, rank in unresolved:
            if provider_url not in self._decoded_cache:
                self._decoded_cache[provider_url] = await self.decoder(provider_url)
            decoded = self._decoded_cache[provider_url]
            if not decoded:
                continue
            url = canonicalize_public_url(decoded)
            hits.append(
                NewsSearchHit(
                    url=url,
                    title=title,
                    snippet=snippet,
                    source_name=source or registrable_domain(url) or self.name,
                    published_at=published_at,
                    provider=self.name,
                    rank=rank,
                    query_hash=_query_hash(query),
                    provider_url=provider_url,
                )
            )
            if len(hits) >= limit:
                break
        return hits


class SearxngNewsSearchProvider:
    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    async def search(
        self,
        query: str,
        *,
        market: str,
        language: str,
        limit: int,
    ) -> list[NewsSearchHit]:
        del market
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5),
            transport=self.transport,
            headers={"User-Agent": "TrendOpportunityLab/0.4 research"},
        ) as client:
            response = await client.get(
                f"{self.base_url}/search",
                params={
                    "q": query,
                    "categories": "news",
                    "language": language or "all",
                    "time_range": "week",
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        hits: list[NewsSearchHit] = []
        for rank, result in enumerate(payload.get("results") or [], 1):
            url = canonicalize_public_url(str(result.get("url") or ""))
            title = str(result.get("title") or "").strip()
            snippet = _clean_html(str(result.get("content") or ""))
            if not url or not title:
                continue
            if content_relevance_score(query, f"{title} {snippet}") < 0.2:
                continue
            hits.append(
                NewsSearchHit(
                    url=url,
                    title=title,
                    snippet=snippet,
                    source_name=registrable_domain(url) or self.name,
                    published_at=_parse_published(
                        str(result.get("publishedDate") or "")
                    ),
                    provider=self.name,
                    rank=rank,
                    query_hash=_query_hash(query),
                )
            )
            if len(hits) >= limit:
                break
        return hits


class CompositeNewsSearchProvider:
    name = "composite-public-news"

    def __init__(self, providers: list[PublicNewsSearchProvider]):
        self.providers = providers

    async def search(
        self,
        query: str,
        *,
        market: str,
        language: str,
        limit: int,
    ) -> list[NewsSearchHit]:
        results = await asyncio.gather(
            *[
                provider.search(
                    query,
                    market=market,
                    language=language,
                    limit=limit,
                )
                for provider in self.providers
            ],
            return_exceptions=True,
        )
        hits: list[NewsSearchHit] = []
        seen_urls: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                continue
            for hit in result:
                if hit.url in seen_urls:
                    continue
                seen_urls.add(hit.url)
                hits.append(hit)
        hits.sort(key=lambda item: (item.rank, item.provider))
        return hits[:limit]


def build_public_news_search_provider(
    settings: Settings,
) -> PublicNewsSearchProvider | None:
    if not settings.enable_public_news_search:
        return None
    providers: list[PublicNewsSearchProvider] = []
    if settings.searxng_base_url:
        providers.append(SearxngNewsSearchProvider(settings.searxng_base_url))
    providers.append(GoogleNewsRssSearchProvider())
    return CompositeNewsSearchProvider(providers)


def build_fact_search_queries(
    event: dict,
    source_items: list[dict],
    max_queries: int,
) -> list[str]:
    if max_queries <= 0:
        return []
    values = [str(event.get("canonical_title") or "").strip()]
    values.extend(str(item.get("title") or "").strip() for item in source_items)
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip(" -_|，。！？!?：:")
        key = normalized.casefold()
        if len(normalized) < 3 or key in seen:
            continue
        seen.add(key)
        queries.append(normalized[:180])
        if len(queries) >= min(max_queries, 3):
            break
    return queries
