from __future__ import annotations

import hashlib
import html
import json
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx


@dataclass(slots=True)
class SourceItem:
    source: str
    external_id: str
    title: str
    url: str
    rank: int
    item_count: int
    fetched_at: str
    raw: dict[str, Any]
    market: str = "CN"
    language: str = "zh"
    signal_type: str = "news"


@dataclass(slots=True)
class SourceResult:
    source: str
    success: bool
    status_code: int | None
    latency_ms: int
    fetched_at: str
    payload_hash: str | None
    raw_payload: dict[str, Any] | None
    items: list[SourceItem]
    error: str | None = None
    market: str = "CN"
    language: str = "zh"
    signal_type: str = "news"


@dataclass(frozen=True, slots=True)
class SourceProfile:
    market: str
    language: str
    signal_type: str


SOURCE_PROFILES: dict[str, SourceProfile] = {
    "weibo": SourceProfile("CN", "zh", "social"),
    "zhihu": SourceProfile("CN", "zh", "community"),
    "baidu": SourceProfile("CN", "zh", "search"),
    "douyin": SourceProfile("CN", "zh", "social"),
    "toutiao": SourceProfile("CN", "zh", "news"),
    "bilibili-hot-search": SourceProfile("CN", "zh", "social"),
    "coolapk": SourceProfile("CN", "zh", "community"),
    "tieba": SourceProfile("CN", "zh", "community"),
    "hackernews": SourceProfile("GLOBAL", "en", "community"),
    "producthunt": SourceProfile("US", "en", "product_launch"),
    "github-trending-today": SourceProfile("GLOBAL", "en", "developer"),
}


def source_profile(source: str) -> SourceProfile:
    return SOURCE_PROFILES.get(source, SourceProfile("CN", "zh", "news"))


class NewsNowSource:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def fetch(self, source: str) -> SourceResult:
        profile = source_profile(source)
        fetched_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        url = f"{self.base_url}/api/s"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                    "Referer": f"{self.base_url}/",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                },
            ) as client:
                response = None
                for attempt in range(3):
                    response = await client.get(url, params={"id": source})
                    if response.status_code < 500 and response.status_code not in {403, 429}:
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                response.raise_for_status()
                payload = response.json()
            raw_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("NewsNow response has no items list")
            item_count = len(raw_items)
            items: list[SourceItem] = []
            for rank, raw in enumerate(raw_items, 1):
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or "").strip()
                item_url = html.unescape(
                    str(raw.get("url") or raw.get("mobileUrl") or "").strip()
                )
                if not title or not item_url:
                    continue
                external_id = str(raw.get("id") or item_url or title)
                items.append(
                    SourceItem(
                        source=source,
                        external_id=external_id,
                        title=title,
                        url=item_url,
                        rank=rank,
                        item_count=item_count,
                        fetched_at=fetched_at,
                        raw=raw,
                        market=profile.market,
                        language=profile.language,
                        signal_type=profile.signal_type,
                    )
                )
            if not items:
                raise ValueError("NewsNow returned zero valid items")
            return SourceResult(
                source=source,
                success=True,
                status_code=response.status_code,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=hashlib.sha256(raw_bytes).hexdigest(),
                raw_payload=payload,
                items=items,
                market=profile.market,
                language=profile.language,
                signal_type=profile.signal_type,
            )
        except Exception as exc:
            status_code = None
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
            return SourceResult(
                source=source,
                success=False,
                status_code=status_code,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=None,
                raw_payload=None,
                items=[],
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                market=profile.market,
                language=profile.language,
                signal_type=profile.signal_type,
            )


class GoogleTrendsSource:
    """Public Google Trends RSS adapter; no private Trends endpoint is used."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    @staticmethod
    def _text(node: ElementTree.Element, local_name: str) -> str:
        for child in node.iter():
            if child.tag.rsplit("}", 1)[-1] == local_name:
                return (child.text or "").strip()
        return ""

    @staticmethod
    def _texts(node: ElementTree.Element, local_name: str) -> list[str]:
        return [
            (child.text or "").strip()
            for child in node.iter()
            if child.tag.rsplit("}", 1)[-1] == local_name and (child.text or "").strip()
        ]

    async def fetch(self, geo: str) -> SourceResult:
        geo = geo.upper()
        source = f"google-trends-{geo.lower()}"
        fetched_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        url = "https://trends.google.com/trending/rss"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "TrendOpportunityLab/0.2",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                    "Accept-Language": "en-US,en;q=0.8",
                },
            ) as client:
                response = await client.get(url, params={"geo": geo})
                response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            nodes = root.findall("./channel/item")
            item_count = len(nodes)
            items: list[SourceItem] = []
            raw_items: list[dict[str, Any]] = []
            for rank, node in enumerate(nodes, 1):
                title = self._text(node, "title")
                if not title:
                    continue
                news_titles = self._texts(node, "news_item_title")
                news_urls = self._texts(node, "news_item_url")
                traffic = self._text(node, "approx_traffic")
                published = self._text(node, "pubDate")
                item_url = (
                    "https://trends.google.com/trends/explore?"
                    f"q={quote_plus(title)}&geo={geo}"
                )
                raw = {
                    "title": title,
                    "geo": geo,
                    "approx_traffic": traffic,
                    "published": published,
                    "news_titles": news_titles,
                    "news_urls": news_urls,
                    "extra": {
                        "info": traffic,
                        "hover": " | ".join(news_titles[:4]),
                    },
                }
                raw_items.append(raw)
                items.append(
                    SourceItem(
                        source=source,
                        external_id=hashlib.sha256(
                            f"{geo}:{title}:{published}".encode("utf-8")
                        ).hexdigest(),
                        title=title,
                        url=item_url,
                        rank=rank,
                        item_count=item_count,
                        fetched_at=fetched_at,
                        raw=raw,
                        market=geo,
                        language="ja" if geo == "JP" else "en",
                        signal_type="search",
                    )
                )
            if not items:
                raise ValueError("Google Trends RSS returned zero valid items")
            payload = {"geo": geo, "items": raw_items}
            raw_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return SourceResult(
                source=source,
                success=True,
                status_code=response.status_code,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=hashlib.sha256(raw_bytes).hexdigest(),
                raw_payload=payload,
                items=items,
                market=geo,
                language="ja" if geo == "JP" else "en",
                signal_type="search",
            )
        except Exception as exc:
            status_code = None
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
            return SourceResult(
                source=source,
                success=False,
                status_code=status_code,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=None,
                raw_payload=None,
                items=[],
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                market=geo,
                language="ja" if geo == "JP" else "en",
                signal_type="search",
            )


class RedditOAuthSource:
    """Reddit consumer-discussion source backed by Async PRAW and OAuth."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
        subreddits: tuple[str, ...],
        limit: int = 40,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.subreddits = subreddits
        self.limit = limit

    async def fetch(self) -> SourceResult:
        source = "reddit-consumer-us"
        fetched_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        try:
            import asyncpraw

            reddit = asyncpraw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
            )
            raw_items: list[dict[str, Any]] = []
            try:
                subreddit = await reddit.subreddit("+".join(self.subreddits))
                async for submission in subreddit.hot(limit=self.limit):
                    if submission.stickied or submission.over_18:
                        continue
                    raw_items.append(
                        {
                            "id": submission.id,
                            "title": submission.title,
                            "permalink": submission.permalink,
                            "subreddit": str(submission.subreddit),
                            "score": submission.score,
                            "num_comments": submission.num_comments,
                            "upvote_ratio": submission.upvote_ratio,
                            "created_utc": submission.created_utc,
                            "selftext": (submission.selftext or "")[:1800],
                            "extra": {
                                "info": (
                                    f"{submission.score} points · "
                                    f"{submission.num_comments} comments"
                                ),
                                "hover": (submission.selftext or "")[:500],
                            },
                        }
                    )
            finally:
                await reddit.close()
            item_count = len(raw_items)
            items = [
                SourceItem(
                    source=source,
                    external_id=item["id"],
                    title=item["title"],
                    url=f"https://www.reddit.com{item['permalink']}",
                    rank=rank,
                    item_count=item_count,
                    fetched_at=fetched_at,
                    raw=item,
                    market="US",
                    language="en",
                    signal_type="social",
                )
                for rank, item in enumerate(raw_items, 1)
            ]
            if not items:
                raise ValueError("Reddit returned zero safe consumer posts")
            payload = {"subreddits": self.subreddits, "items": raw_items}
            raw_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return SourceResult(
                source=source,
                success=True,
                status_code=200,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=hashlib.sha256(raw_bytes).hexdigest(),
                raw_payload=payload,
                items=items,
                market="US",
                language="en",
                signal_type="social",
            )
        except Exception as exc:
            return SourceResult(
                source=source,
                success=False,
                status_code=None,
                latency_ms=round((time.perf_counter() - started) * 1000),
                fetched_at=fetched_at,
                payload_hash=None,
                raw_payload=None,
                items=[],
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                market="US",
                language="en",
                signal_type="social",
            )
