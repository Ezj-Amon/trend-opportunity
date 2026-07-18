from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .evidence_quality import (
    MIN_FULL_ARTICLE_CHARACTERS,
    MIN_SUMMARY_CHARACTERS,
    content_fingerprint,
    validate_extracted_content,
)
from .evidence_types import EvidenceType, FetchStatus, normalize_fetch_status


HostValidator = Callable[[str], bool | Awaitable[bool]]


@dataclass(slots=True)
class EvidenceResult:
    url: str
    title: str
    excerpt: str
    fetched_at: str
    http_status: int | None
    content_hash: str | None
    error: str | None = None
    evidence_type: str = ""
    fetch_status: str = ""
    fetch_method: str = "direct_public_page"
    raw_metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.fetch_status:
            self.fetch_status = normalize_fetch_status(
                {"error": self.error, "http_status": self.http_status}
            ).value
        if not self.evidence_type:
            length = len(self.excerpt.strip())
            if self.fetch_status != FetchStatus.READY.value:
                self.evidence_type = EvidenceType.TITLE_ONLY.value
            elif length >= MIN_FULL_ARTICLE_CHARACTERS:
                self.evidence_type = EvidenceType.FULL_ARTICLE.value
            elif length >= MIN_SUMMARY_CHARACTERS:
                self.evidence_type = EvidenceType.ARTICLE_SUMMARY.value
            else:
                self.evidence_type = EvidenceType.TITLE_ONLY.value


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


async def _validate_host(hostname: str, validator: HostValidator | None) -> bool:
    if validator is None:
        return await asyncio.to_thread(_host_is_public, hostname)
    result = validator(hostname)
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


async def is_public_url(url: str, host_validator: HostValidator | None = None) -> bool:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    sensitive_query_keys = {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "password",
        "secret",
        "token",
    }
    if any(key.casefold() in sensitive_query_keys for key, _ in parse_qsl(parsed.query)):
        return False
    return await _validate_host(parsed.hostname, host_validator)


def _json_ld_text(soup: BeautifulSoup) -> list[str]:
    values: list[str] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in {"articleBody", "description", "text"} and isinstance(item, str):
                cleaned = re.sub(r"\s+", " ", item).strip()
                if len(cleaned) >= 30:
                    values.append(cleaned)
            elif isinstance(item, (dict, list)):
                visit(item)

    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            visit(json.loads(node.string or node.get_text() or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return values


def _extract_html(
    raw: bytes, fallback_title: str, url: str
) -> tuple[str, str, str, dict]:
    soup = BeautifulSoup(raw, "html.parser")
    title = fallback_title
    if soup.title and soup.title.string:
        title = soup.title.string.strip() or fallback_title

    descriptions: list[str] = []
    for attrs in (
        {"name": re.compile(r"^description$", re.I)},
        {"property": re.compile(r"^og:description$", re.I)},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            value = re.sub(r"\s+", " ", str(meta.get("content"))).strip()
            if value:
                descriptions.append(value)

    json_ld_values = _json_ld_text(soup)
    trafilatura_text = ""
    trafilatura_metadata: dict[str, str] = {}
    try:
        document = trafilatura.bare_extraction(
            raw,
            url=url,
            favor_recall=True,
            include_comments=False,
            include_tables=False,
        )
        if document is not None:
            extracted = (
                document.as_dict()
                if hasattr(document, "as_dict")
                else dict(document)
                if isinstance(document, dict)
                else {}
            )
            trafilatura_text = re.sub(
                r"\s+", " ", str(extracted.get("text") or "")
            ).strip()
            extracted_title = str(extracted.get("title") or "").strip()
            if extracted_title:
                title = extracted_title
            trafilatura_metadata = {
                key: str(extracted[key])[:1000]
                for key in ("author", "date", "sitename", "hostname")
                if extracted.get(key)
            }
    except (TypeError, ValueError, AttributeError):
        trafilatura_text = ""
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    container = soup.find("article") or soup.find("main") or soup
    paragraphs = [
        re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        for node in container.find_all("p")
    ]
    paragraphs = [value for value in paragraphs if len(value) >= 30]

    body_values = (
        [trafilatura_text]
        if trafilatura_text
        else [*json_ld_values, *paragraphs]
    )
    evidence_type = (
        EvidenceType.FULL_ARTICLE.value
        if body_values and len(" ".join(body_values)) >= 120
        else EvidenceType.ARTICLE_SUMMARY.value
        if descriptions or body_values
        else EvidenceType.TITLE_ONLY.value
    )
    unique: list[str] = []
    seen: set[str] = set()
    for value in [*descriptions, *body_values]:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    excerpt = re.sub(r"\s+", " ", " ".join(unique)).strip()[:4000]
    return title[:300], excerpt, evidence_type, {
        "extractor": "trafilatura-2" if trafilatura_text else "html-fallback-v2",
        **trafilatura_metadata,
    }


def _looks_like_login_wall(title: str, raw_text: str) -> bool:
    corpus = f"{title} {raw_text[:2000]}".casefold()
    markers = ("login required", "sign in to continue", "请登录", "登录后查看", "扫码登录")
    return any(marker in corpus for marker in markers)


def _looks_javascript_required(raw_text: str) -> bool:
    folded = raw_text[:3000].casefold()
    markers = ("enable javascript", "requires javascript", "请启用javascript", "请开启 javascript")
    return any(marker in folded for marker in markers)


def _failure(
    url: str,
    title: str,
    fetched_at: str,
    status: FetchStatus,
    error: str,
    http_status: int | None = None,
    metadata: dict | None = None,
) -> EvidenceResult:
    return EvidenceResult(
        url=url,
        title=title,
        excerpt=title if status == FetchStatus.CONTENT_TOO_SHORT else "",
        fetched_at=fetched_at,
        http_status=http_status,
        content_hash=None,
        error=error,
        evidence_type=EvidenceType.TITLE_ONLY.value,
        fetch_status=status.value,
        raw_metadata=metadata or {},
    )


async def fetch_evidence(
    url: str,
    fallback_title: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    host_validator: HostValidator | None = None,
    max_redirects: int = 3,
) -> EvidenceResult:
    fetched_at = datetime.now(timezone.utc).isoformat()
    if not await is_public_url(url, host_validator):
        parsed = urlparse(url)
        status = (
            FetchStatus.UNSUPPORTED
            if parsed.scheme not in {"http", "https"} or not parsed.hostname
            else FetchStatus.ROBOTS_OR_ACCESS_DENIED
        )
        return _failure(url, fallback_title, fetched_at, status, "non-public or unsupported URL")

    current_url = url
    redirect_chain: list[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TrendOpportunityLab/0.3; research)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            response: httpx.Response | None = None
            for _ in range(max_redirects + 1):
                response = await client.get(current_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    return _failure(
                        current_url,
                        fallback_title,
                        fetched_at,
                        FetchStatus.REDIRECT_BLOCKED,
                        "redirect response omitted Location",
                        response.status_code,
                        {"redirect_chain": redirect_chain},
                    )
                next_url = urljoin(current_url, location)
                if not await is_public_url(next_url, host_validator):
                    return _failure(
                        current_url,
                        fallback_title,
                        fetched_at,
                        FetchStatus.REDIRECT_BLOCKED,
                        "redirect target is not a validated public URL",
                        response.status_code,
                        {"redirect_chain": [*redirect_chain, next_url]},
                    )
                redirect_chain.append(next_url)
                current_url = next_url
            else:
                response = None

        if response is None or response.status_code in {301, 302, 303, 307, 308}:
            return _failure(
                current_url,
                fallback_title,
                fetched_at,
                FetchStatus.REDIRECT_BLOCKED,
                "redirect limit exceeded",
                response.status_code if response else None,
                {"redirect_chain": redirect_chain},
            )
        if response.status_code in {401, 407}:
            return _failure(
                current_url,
                fallback_title,
                fetched_at,
                FetchStatus.LOGIN_REQUIRED,
                f"HTTP {response.status_code} login required",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )
        if response.status_code in {403, 429}:
            return _failure(
                current_url,
                fallback_title,
                fetched_at,
                FetchStatus.ROBOTS_OR_ACCESS_DENIED,
                f"HTTP {response.status_code} access denied",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )
        if response.status_code >= 400:
            return _failure(
                current_url,
                fallback_title,
                fetched_at,
                FetchStatus.HTTP_ERROR,
                f"HTTP {response.status_code}",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.casefold():
            return _failure(
                current_url,
                fallback_title,
                fetched_at,
                FetchStatus.NOT_HTML,
                f"not HTML: {content_type}",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )

        raw = response.content[:1_000_000]
        raw_text = response.text
        title, excerpt, evidence_type, extraction_metadata = _extract_html(
            raw, fallback_title, current_url
        )
        if _looks_like_login_wall(title, raw_text):
            return _failure(
                current_url,
                title,
                fetched_at,
                FetchStatus.LOGIN_REQUIRED,
                "login required",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )
        if not excerpt and _looks_javascript_required(raw_text):
            return _failure(
                current_url,
                title,
                fetched_at,
                FetchStatus.JAVASCRIPT_REQUIRED,
                "JavaScript rendering required",
                response.status_code,
                {"redirect_chain": redirect_chain},
            )
        validation = validate_extracted_content(
            url=current_url,
            expected_title=fallback_title,
            extracted_title=title,
            text=excerpt,
        )
        metadata = {
            "redirect_chain": redirect_chain,
            "raw_content_hash": hashlib.sha256(raw).hexdigest(),
            "extraction": extraction_metadata,
            "content_validation": {
                "accepted": validation.accepted,
                "content_level": validation.content_level,
                "relevance_score": validation.relevance_score,
                "reasons": list(validation.reasons),
            },
        }
        if not excerpt or validation.content_level == EvidenceType.TITLE_ONLY.value:
            return _failure(
                current_url,
                title,
                fetched_at,
                FetchStatus.CONTENT_TOO_SHORT,
                "content too short",
                response.status_code,
                metadata,
            )
        if not validation.accepted:
            status = (
                FetchStatus.CONTENT_IRRELEVANT
                if any("not relevant" in reason for reason in validation.reasons)
                else FetchStatus.CONTENT_TOO_SHORT
            )
            return _failure(
                current_url,
                title,
                fetched_at,
                status,
                "; ".join(validation.reasons),
                response.status_code,
                metadata,
            )
        evidence_type = validation.content_level
        return EvidenceResult(
            url=current_url,
            title=title,
            excerpt=excerpt,
            fetched_at=fetched_at,
            http_status=response.status_code,
            content_hash=content_fingerprint(excerpt),
            evidence_type=evidence_type,
            fetch_status=FetchStatus.READY.value,
            raw_metadata=metadata,
        )
    except httpx.TimeoutException as exc:
        return _failure(
            current_url,
            fallback_title,
            fetched_at,
            FetchStatus.TIMEOUT,
            f"{type(exc).__name__}: {str(exc)[:240]}",
            metadata={"redirect_chain": redirect_chain},
        )
    except Exception as exc:
        return _failure(
            current_url,
            fallback_title,
            fetched_at,
            FetchStatus.FAILED,
            f"{type(exc).__name__}: {str(exc)[:240]}",
            getattr(getattr(exc, "response", None), "status_code", None),
            {"redirect_chain": redirect_chain},
        )
