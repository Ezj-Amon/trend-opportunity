from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


@dataclass(slots=True)
class EvidenceResult:
    url: str
    title: str
    excerpt: str
    fetched_at: str
    http_status: int | None
    content_hash: str | None
    error: str | None = None


def _host_is_public(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return True


async def fetch_evidence(url: str, fallback_title: str) -> EvidenceResult:
    fetched_at = datetime.now(timezone.utc).isoformat()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return EvidenceResult(url, fallback_title, "", fetched_at, None, None, "unsupported URL")
    is_public = await asyncio.to_thread(_host_is_public, parsed.hostname)
    if not is_public:
        return EvidenceResult(url, fallback_title, "", fetched_at, None, None, "non-public host blocked")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; trend-opportunity-demo/0.1; research)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            response = await client.get(url)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
        if "html" not in content_type.lower():
            return EvidenceResult(
                url, fallback_title, "", fetched_at, response.status_code, None, "not HTML"
            )
        raw = response.content[:1_000_000]
        soup = BeautifulSoup(raw, "html.parser")
        for element in soup(["script", "style", "noscript", "svg"]):
            element.decompose()
        title = (soup.title.string if soup.title and soup.title.string else fallback_title).strip()
        description = ""
        meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if meta and meta.get("content"):
            description = str(meta.get("content")).strip()
        paragraphs = [
            node.get_text(" ", strip=True)
            for node in soup.find_all("p")
            if len(node.get_text(" ", strip=True)) >= 30
        ]
        excerpt = re.sub(r"\s+", " ", " ".join([description, *paragraphs]))[:4000]
        if not excerpt:
            excerpt = fallback_title
        return EvidenceResult(
            url=url,
            title=title[:300],
            excerpt=excerpt,
            fetched_at=fetched_at,
            http_status=response.status_code,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )
    except Exception as exc:
        return EvidenceResult(
            url,
            fallback_title,
            fallback_title,
            fetched_at,
            getattr(getattr(exc, "response", None), "status_code", None),
            None,
            f"{type(exc).__name__}: {str(exc)[:240]}",
        )
