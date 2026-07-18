from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class EvidenceType(StrEnum):
    FULL_ARTICLE = "full_article"
    OFFICIAL_NOTICE = "official_notice"
    ARTICLE_SUMMARY = "article_summary"
    CONSUMER_DISCUSSION = "consumer_discussion"
    CONSUMER_COMMENT = "consumer_comment"
    SEARCH_SNIPPET = "search_snippet"
    TITLE_ONLY = "title_only"
    MANUAL_EVIDENCE = "manual_evidence"


class FetchStatus(StrEnum):
    READY = "ready"
    CONTENT_TOO_SHORT = "content_too_short"
    CONTENT_IRRELEVANT = "content_irrelevant"
    LOGIN_REQUIRED = "login_required"
    REDIRECT_BLOCKED = "redirect_blocked"
    JAVASCRIPT_REQUIRED = "javascript_required"
    NOT_HTML = "not_html"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    ROBOTS_OR_ACCESS_DENIED = "robots_or_access_denied"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ManualEvidenceInput(BaseModel):
    evidence_type: EvidenceType
    source_name: str = Field(min_length=1, max_length=200)
    url: str = Field(default="", max_length=2000)
    title: str = Field(min_length=1, max_length=1000)
    excerpt: str = Field(min_length=1, max_length=50_000)
    is_consumer_voice: bool = False
    note: str = Field(default="", max_length=5000)


def normalize_fetch_status(row: dict) -> FetchStatus:
    explicit = str(row.get("fetch_status") or "").strip()
    if explicit in FetchStatus._value2member_map_ and explicit != "unknown":
        return FetchStatus(explicit)

    error = str(row.get("error") or "").strip().casefold()
    if error:
        if "content too short" in error or "too short" in error or "正文过短" in error:
            return FetchStatus.CONTENT_TOO_SHORT
        if "not relevant" in error or "content irrelevant" in error or "正文不相关" in error:
            return FetchStatus.CONTENT_IRRELEVANT
        if any(value in error for value in ("login", "sign in", "登录")):
            return FetchStatus.LOGIN_REQUIRED
        if "redirect" in error or "重定向" in error:
            return FetchStatus.REDIRECT_BLOCKED
        if any(value in error for value in ("javascript", "enable js", "动态渲染")):
            return FetchStatus.JAVASCRIPT_REQUIRED
        if any(value in error for value in ("not html", "non-html", "不是 html")):
            return FetchStatus.NOT_HTML
        if "timeout" in error or "timed out" in error or "超时" in error:
            return FetchStatus.TIMEOUT
        if any(value in error for value in ("robots", "access denied", "forbidden", "403")):
            return FetchStatus.ROBOTS_OR_ACCESS_DENIED
        if "unsupported" in error or "不支持" in error:
            return FetchStatus.UNSUPPORTED
        if "http" in error or row.get("http_status"):
            return FetchStatus.HTTP_ERROR
        return FetchStatus.FAILED

    if row.get("http_status") in (None, 200):
        return FetchStatus.READY
    return FetchStatus.HTTP_ERROR
